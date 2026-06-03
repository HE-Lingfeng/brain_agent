from __future__ import annotations

import csv
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from ..credentials import load_credentials
from ..diagnostics import diagnose_sim_result
from ..memory import AlphaMemory, extract_operators, memory_path_for_run_dir
from ..models import AdapterResult, CandidateStatus, RunConfig
from ..prompting import prompt_env
from ..progress import build_simulation_progress, render_simulation_progress
from ..quota_allocator import allocate_simulation_quota
from ..repository import Repository
from ..scoring import score_candidate, score_candidates
from ..selection import classify_candidate
from ..simulation_leases import SimulationLeasePool
from ..task_runner import TaskRunner
from ..thesis import build_factor_thesis, load_idea_context, thesis_lineage_from_row
from ..utils import expression_fingerprint, file_sha256, read_json, write_json
from ..variant_search import build_variant_search, variant_lineage_from_row


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / ".agents" / "skills"


_LITERAL_TOKENS = {
    "and",
    "false",
    "filter",
    "gaussian",
    "nan",
    "nan_mask",
    "or",
    "true",
    "uniform",
}

_BUILTIN_EXPRESSION_SYMBOLS = {
    "adv20",
    "adv60",
    "cap",
    "close",
    "country",
    "exchange",
    "high",
    "industry",
    "low",
    "market",
    "open",
    "returns",
    "sector",
    "subindustry",
    "volume",
    "vwap",
}

_ENHANCE_DEFAULT_MAX_OPERATORS = 5
_ENHANCE_DEFAULT_MAX_DATAFIELDS = 4
_GROUPING_SYMBOLS = {"country", "exchange", "industry", "market", "sector", "subindustry"}
_BATCH_DIVERSITY_MIN_STRUCTURAL_THEMES = 4
_BATCH_DIVERSITY_MAX_COMMON_OPERATOR_REPEATS = 2
_COMMON_BATCH_OPERATORS = {"rank", "zscore", "ts_mean", "ts_sum", "ts_std_dev", "winsorize", "scale", "trade_when"}
_STRUCTURAL_OPERATOR_THEMES = {
    "conditional_holding": {"trade_when", "keep", "if_else", "nan_mask"},
    "denoise_fill": {
        "days_from_last_change",
        "filter",
        "group_backfill",
        "hump",
        "hump_decay",
        "jump_decay",
        "kth_element",
        "last_diff_value",
        "ts_backfill",
    },
    "tail_robustness": {"clamp", "left_tail", "nan_out", "pasteurize", "purify", "replace", "right_tail", "tail", "truncate", "winsorize"},
    "neutralize_project": {
        "group_multi_regression",
        "group_vector_neut",
        "group_vector_proj",
        "multi_regression",
        "regression_neut",
        "regression_proj",
        "ts_poly_regression",
        "ts_regression",
        "ts_theilsen",
        "ts_vector_neut",
        "ts_vector_proj",
        "vector_neut",
        "vector_proj",
    },
    "co_movement": {"ts_co_kurtosis", "ts_co_skewness", "ts_corr", "ts_covariance", "ts_partial_corr", "ts_triple_corr"},
    "turnover_position": {
        "inst_pnl",
        "inst_tvr",
        "one_side",
        "rank_by_side",
        "scale",
        "scale_down",
        "ts_delta_limit",
        "ts_target_tvr_decay",
        "ts_target_tvr_delta_limit",
        "ts_target_tvr_hump",
    },
}


def _has_unresolved_bare_variable(expression: str) -> bool:
    """Detect short raw field names that survived template implementation.

    Inspect can occasionally emit a mixed expression such as
    ``divide(apg, add(fnd31_..._ohlsonscore, 0.001))``. BRAIN rejects the bare
    ``apg`` token, so skip those candidates before batch simulation. Function
    names are identified by a following ``(``; real dataset ids normally contain
    an underscore or a digit.
    """

    text = str(expression or "")
    for match in re.finditer(r"\b([A-Za-z][A-Za-z0-9_]*)\b", text):
        token = match.group(1)
        end = match.end(1)
        rest = text[end:].lstrip()
        if rest.startswith("("):
            continue
        if token.lower() in _LITERAL_TOKENS:
            continue
        if "_" in token or any(ch.isdigit() for ch in token):
            continue
        return True
    return False


class SkillAdapter:
    name = "base"

    def __init__(self, repo: Repository, run_id: str, run_dir: Path):
        self.repo = repo
        self.run_id = run_id
        self.run_dir = run_dir
        self.artifacts_dir = run_dir / "artifacts"

    def record_artifact(self, kind: str, path: Path, stage: str) -> dict[str, Any]:
        digest = file_sha256(path) if path.exists() else ""
        self.repo.add_artifact(self.run_id, kind, path, digest, stage)
        return {"kind": kind, "path": str(path), "sha256": digest, "source_stage": stage}

    def run_command(self, adapter: str, cmd: list[str], cwd: Path, env: dict[str, str] | None = None) -> tuple[str, int]:
        runner = TaskRunner(self.run_dir / "tasks")
        def _record_running(handle):
            self.repo.create_task(
                self.run_id,
                handle.task_id,
                adapter,
                pid=handle.pid,
                status="running",
                stdout_path=handle.stdout_path,
                stderr_path=handle.stderr_path,
            )

        last_progress_line = {"text": ""}

        def _print_progress(handle):
            if adapter != "batchSim":
                return
            progress = build_simulation_progress(self.repo, self.run_id, self.run_dir)
            line = render_simulation_progress(progress)
            if line != last_progress_line["text"]:
                print(line, flush=True)
                last_progress_line["text"] = line

        handle, returncode = runner.run_tracked(
            adapter,
            cmd,
            cwd,
            env=env,
            on_start=_record_running,
            on_progress=_print_progress if adapter == "batchSim" else None,
            progress_interval_seconds=30,
        )
        status = "completed" if returncode == 0 else "failed"
        self.repo.update_task_status(handle.task_id, status)
        _print_progress(handle)
        return handle.task_id, returncode

    def config_stub(self) -> Path:
        path = self.artifacts_dir / "config" / "empty_config.json"
        if not path.exists():
            write_json(path, {})
        return path


class MakeSomeGemAdapter(SkillAdapter):
    name = "makeSomeGem"

    def parse_final_expressions(
        self,
        path: Path,
        source: str = "makeSomeGem",
        *,
        artifact_kind: str = "final_expressions",
        source_stage: str = "GENERATE",
    ) -> AdapterResult:
        try:
            data = read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            return AdapterResult(status="failed", error_summary=f"Could not parse final expressions JSON: {path}: {exc}")
        if isinstance(data, dict):
            expressions = data.get("expressions") or data.get("expression_list") or []
        else:
            expressions = data
        if not isinstance(expressions, list):
            return AdapterResult(status="failed", error_summary=f"Unsupported final_expressions format: {path}")

        candidates = []
        theses = []
        for item in expressions:
            if isinstance(item, str):
                expression = item
                idea_file = ""
                item_data: dict[str, Any] = {}
            elif isinstance(item, dict):
                item_data = item
                expression = item.get("expression") or item.get("regular") or item.get("regular_expression") or ""
                idea_file = item.get("idea_file") or item.get("source") or ""
            else:
                continue
            if not expression:
                continue
            idea_context = _load_idea_context_near(path, idea_file) if idea_file else {}
            thesis = build_factor_thesis(expression, item=item_data, idea_context=idea_context, source=source)
            fp = expression_fingerprint(expression)
            cid = self.repo.upsert_candidate(
                self.run_id,
                expression,
                fp,
                status=CandidateStatus.GENERATED.value,
                idea_file=idea_file,
                source=source,
                thesis=thesis,
            )
            candidates.append({"candidate_id": cid, "expression": expression, "fingerprint": fp, "idea_file": idea_file, "thesis": thesis})
            theses.append({"candidate_id": cid, "fingerprint": fp, "expression": expression, "factor_thesis": thesis})
        artifact = self.record_artifact(artifact_kind, path, source_stage)
        artifacts = [artifact]
        if theses:
            thesis_path = path.with_name(path.stem + "_factor_theses.json")
            write_json(thesis_path, theses)
            artifacts.append(self.record_artifact("factor_theses", thesis_path, source_stage))
        return AdapterResult(status="ok", artifacts=artifacts, candidates_delta=candidates)

    def dry_run(self, config: RunConfig) -> AdapterResult:
        path = self.artifacts_dir / "01_generate" / "final_expressions.json"
        expressions = [
            f"rank(ts_mean({config.dataset}_field_a, 20))",
            f"-rank(ts_delta({config.dataset}_field_b, 5))",
            f"rank(group_neutralize(ts_zscore({config.dataset}_field_c, 60), industry))",
        ]
        write_json(path, expressions)
        return self.parse_final_expressions(path, source="dry_run_generate")

    def run_real(self, config: RunConfig) -> AdapterResult:
        skill_root = SKILLS_ROOT / "brain-makeSomeGem"
        runner_dir = skill_root / "scripts" / "headless_runner"
        script = runner_dir / "run.py"
        if not script.exists():
            return AdapterResult(status="failed", error_summary=f"makeSomeGem runner not found: {script}")

        env = _credential_env(require_brain=True, require_llm=True)
        env.update(prompt_env(config))
        env["APPROVED_FORUM_LESSONS_QUERY"] = " ".join(
            [config.dataset, config.region, config.data_type, config.neutralization, _dataset_category(config.dataset)]
        )
        category = _dataset_category(config.dataset)
        cmd = [
            sys.executable,
            str(script),
            "--config",
            str(self.config_stub()),
            "--data-category",
            category,
            "--region",
            config.region,
            "--delay",
            str(config.delay),
            "--dataset-id",
            config.dataset,
            "--universe",
            config.universe,
            "--instrument-type",
            "EQUITY",
            "--data-type",
            config.data_type,
        ]
        if config.max_fields is not None:
            cmd.extend(["--max-fields", str(int(config.max_fields))])
        if config.max_operators is not None:
            cmd.extend(["--max-operators", str(int(config.max_operators))])
        _, returncode = self.run_command(self.name, cmd, runner_dir, env=env)
        output_dir = (
            skill_root
            / "scripts"
            / "trailSomeAlphas"
            / "skills"
            / "brain-feature-implementation"
            / "data"
            / f"{config.dataset}_{config.region}_delay{config.delay}"
        )
        final_src = output_dir / "final_expressions.json"
        if returncode != 0 and not final_src.exists():
            return AdapterResult(status="failed", error_summary=f"makeSomeGem failed; final expressions missing: {final_src}")
        if not final_src.exists():
            return AdapterResult(status="failed", error_summary=f"makeSomeGem final expressions missing: {final_src}")

        dest_dir = self.artifacts_dir / "01_generate"
        final_path = copy_artifact_to_run(final_src, dest_dir)
        result = self.parse_final_expressions(final_path)
        for idea_src in sorted(output_dir.glob("*_idea_*.json")):
            idea_path = copy_artifact_to_run(idea_src, dest_dir)
            result.artifacts.append(self.record_artifact("idea_file", idea_path, "GENERATE"))
        return result


class InspectRawTemplateAdapter(SkillAdapter):
    name = "inspectRawTemplate"

    def parse_alpha_list(self, path: Path, source: str = "inspectRawTemplate") -> AdapterResult:
        try:
            data = read_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            return AdapterResult(status="failed", error_summary=f"Could not parse alpha_list JSON: {path}: {exc}")
        if not isinstance(data, list):
            return AdapterResult(status="failed", error_summary=f"alpha_list must be a JSON list: {path}")
        candidates = []
        for item in data:
            if not isinstance(item, dict):
                continue
            expression = item.get("regular") or item.get("regular_expression") or item.get("expression") or ""
            settings = item.get("settings") if isinstance(item.get("settings"), dict) else {}
            if not expression:
                continue
            if _has_unresolved_bare_variable(expression):
                continue
            fp = _candidate_fingerprint(expression, settings)
            lineage = variant_lineage_from_row(item)
            parent_candidate_id = lineage.get("parent_candidate_id")
            thesis = thesis_lineage_from_row(item)
            if not thesis:
                thesis = build_factor_thesis(
                    expression,
                    item=item,
                    idea_context=_load_idea_context_near(path, item.get("idea_file") or ""),
                    source=source,
                )
            cid = self.repo.upsert_candidate(
                self.run_id,
                expression,
                fp,
                status=CandidateStatus.SIM_PENDING.value,
                idea_file=item.get("idea_file") or "",
                source=source,
                parent_candidate_id=int(parent_candidate_id) if parent_candidate_id else None,
                variant_strategy=str(lineage.get("variant_strategy") or ""),
                variant_params=lineage.get("variant_params") if isinstance(lineage.get("variant_params"), dict) else {},
                lineage=lineage,
                thesis=thesis,
            )
            score = score_candidate(
                {
                    "candidate_id": cid,
                    "expression": expression,
                    "status": CandidateStatus.SIM_PENDING.value,
                    "source": source,
                    "parent_candidate_id": parent_candidate_id,
                    "variant_strategy": lineage.get("variant_strategy") or "",
                    "thesis_json": thesis,
                },
                memory_context=_memory_context_for_run_dir(self.run_dir, _run_config_filters(self.repo, self.run_id, settings)),
            )
            self.repo.update_candidate_score(self.run_id, cid, score.score, score.breakdown)
            candidates.append({"candidate_id": cid, "expression": expression, "fingerprint": fp, "settings": settings, "thesis": thesis})
        artifact = self.record_artifact("alpha_list", path, "INSPECT")
        return AdapterResult(status="ok", artifacts=[artifact], candidates_delta=candidates)

    def dry_run(self, config: RunConfig) -> AdapterResult:
        generated = self.repo.list_rows("candidates", self.run_id)
        path = self.artifacts_dir / "02_inspect" / "alpha_list.json"
        rows = []
        for idx, row in enumerate(generated):
            settings = _settings_for_candidate_index(config, str(row["expression"]), idx)
            alpha_row = {"type": "REGULAR", "settings": settings, "regular": row["expression"]}
            thesis = _json_obj(row.get("thesis_json"))
            if thesis:
                alpha_row["factor_thesis"] = thesis
            rows.append(alpha_row)
        write_json(path, rows)
        return self.parse_alpha_list(path, source="dry_run_inspect")

    def run_real(self, idea_file: Path, config: RunConfig) -> AdapterResult:
        skill_root = SKILLS_ROOT / "brain-inspectRawTemplate-create-Setting"
        process_script = skill_root / "scripts" / "process_template.py"
        build_script = skill_root / "scripts" / "build_alpha_list.py"
        if not process_script.exists() or not build_script.exists():
            return AdapterResult(status="failed", error_summary=f"inspect scripts missing under {skill_root}")

        env = _credential_env(require_brain=True, require_llm=False)
        idea_file_abs = Path(idea_file).resolve()
        _, returncode = self.run_command(
            self.name,
            [sys.executable, str(process_script), "--file", str(idea_file_abs)],
            skill_root,
            env=env,
        )
        output_dir = skill_root / "processed_templates" / idea_file.stem
        candidates_path = output_dir / "settings_candidates.json"
        idea_context_path = output_dir / "idea_context.json"
        if returncode != 0 or not candidates_path.exists() or not idea_context_path.exists():
            return AdapterResult(status="failed", error_summary=f"inspect process failed for {idea_file}")

        settings = _choose_settings(candidates_path, config)
        alpha_list_path = output_dir / "alpha_list.json"
        alpha_list_path.unlink(missing_ok=True)
        _, build_returncode = self.run_command(
            f"{self.name}_build",
            [
                sys.executable,
                str(build_script),
                "--idea",
                str(idea_context_path),
                "--settings_json",
                json.dumps(settings, ensure_ascii=False),
                "--out",
                str(alpha_list_path),
            ],
            skill_root,
            env=env,
        )
        if build_returncode != 0 or not alpha_list_path.exists():
            return AdapterResult(status="failed", error_summary=f"alpha_list build failed for {idea_file}")

        dest_dir = self.artifacts_dir / "02_inspect" / idea_file.stem
        artifacts = []
        for src, kind in (
            (idea_context_path, "idea_context"),
            (candidates_path, "settings_candidates"),
            (alpha_list_path, "alpha_list"),
        ):
            dest = copy_artifact_to_run(src, dest_dir)
            artifacts.append(self.record_artifact(kind, dest, "INSPECT"))
        parsed = self.parse_alpha_list(dest_dir / "alpha_list.json")
        parsed.artifacts.extend(artifacts)
        return parsed

    def write_combined_alpha_list(self, alpha_list_paths: list[Path]) -> Path:
        combined: list[Any] = []
        for path in alpha_list_paths:
            data = read_json(path)
            if isinstance(data, list):
                combined.extend(data)
        out = self.artifacts_dir / "02_inspect" / "alpha_list_combined.json"
        write_json(out, combined)
        self.record_artifact("alpha_list_combined", out, "INSPECT")
        return out

    def write_field_factory_alpha_list(self, config: RunConfig, *, max_rows: int | None = None) -> AdapterResult:
        """Build a small deterministic alpha list directly from datafield metadata."""
        env = _credential_env(require_brain=True, require_llm=False)
        fields = _load_datafield_metadata(config, self.artifacts_dir / "02_inspect" / "datafields", env)
        rows = _field_factory_alpha_rows(config, fields, max_rows=max_rows)
        if not rows:
            return AdapterResult(status="ok")
        out = self.artifacts_dir / "02_inspect" / "alpha_list_field_factory.json"
        write_json(out, rows)
        parsed = self.parse_alpha_list(out, source="field_factory")
        parsed.artifacts.append(self.record_artifact("alpha_list_field_factory", out, "INSPECT"))
        return parsed

    def write_alpha_list_for_candidates(
        self,
        candidates: list[dict[str, Any]],
        config: RunConfig,
        *,
        name: str = "alpha_list_candidates.json",
    ) -> Path:
        rows = []
        for idx, candidate in enumerate(candidates):
            expression = str(candidate.get("expression") or "")
            if not expression:
                continue
            settings = _settings_for_candidate_index(config, expression, idx)
            row = {"type": "REGULAR", "settings": settings, "regular": expression}
            thesis = thesis_lineage_from_row(candidate)
            if not thesis and candidate.get("thesis_json"):
                thesis = thesis_lineage_from_row({"thesis_json": _json_obj(candidate.get("thesis_json"))})
            if thesis:
                row["factor_thesis"] = thesis
            rows.append(row)
        out = self.artifacts_dir / "02_inspect" / name
        write_json(out, rows)
        self.record_artifact("alpha_list_generated", out, "INSPECT")
        self.parse_alpha_list(out, source="generated_alpha_list")
        return out


class VariantSearchAdapter(SkillAdapter):
    name = "variantSearch"

    def run(self, alpha_list_path: Path, config: RunConfig, *, iteration: int) -> AdapterResult:
        try:
            rows = read_json(alpha_list_path)
        except (OSError, json.JSONDecodeError) as exc:
            return AdapterResult(status="failed", error_summary=f"Could not parse alpha_list for variant search: {alpha_list_path}: {exc}")
        if not isinstance(rows, list):
            return AdapterResult(status="failed", error_summary=f"alpha_list must be a JSON list: {alpha_list_path}")

        pnl_series = _load_pnl_cache(self.run_dir)
        variant_rows, report = build_variant_search(
            rows,
            self.repo.list_rows("candidates", self.run_id),
            self.repo.latest_sim_results_by_candidate(self.run_id),
            max_variant_alphas=int(config.max_variant_alphas),
            max_variants_per_alpha=int(config.max_variants_per_alpha),
            latest_gate_by_candidate=_latest_gate_by_candidate(self.repo, self.run_id),
            pnl_series_by_alpha_id=pnl_series if pnl_series else None,
        )
        out = self.artifacts_dir / "04_variants" / f"alpha_list_variants_iter{iteration}.json"
        report_path = self.artifacts_dir / "04_variants" / f"variant_search_report_iter{iteration}.json"
        write_json(out, variant_rows)
        write_json(report_path, report)
        artifacts = [
            self.record_artifact("alpha_list_variants", out, "VARIANT_SEARCH"),
            self.record_artifact("variant_search_report", report_path, "VARIANT_SEARCH"),
        ]
        if not variant_rows:
            return AdapterResult(status="ok", artifacts=artifacts)
        parsed = InspectRawTemplateAdapter(self.repo, self.run_id, self.run_dir).parse_alpha_list(
            out,
            source="variant_search",
        )
        parsed.artifacts = artifacts + parsed.artifacts
        return parsed


class BatchSimAdapter(SkillAdapter):
    name = "batchSim"

    def parse_simulation_status(self, path: Path) -> AdapterResult:
        metrics = []
        try:
            f = path.open("r", encoding="utf-8", newline="")
        except OSError as exc:
            return AdapterResult(status="failed", error_summary=f"Could not open simulation status CSV: {path}: {exc}")
        with f:
            reader = csv.DictReader(f)
            if not reader.fieldnames:
                return AdapterResult(status="failed", error_summary=f"simulation status CSV has no header: {path}")
            for row in reader:
                expression = row.get("regular_expression") or row.get("regular") or row.get("expression") or ""
                settings = _json_obj(row.get("settings_json"))
                legacy_fp = row.get("fingerprint") or ""
                candidate_fp = _candidate_fingerprint(expression, settings)
                fp = legacy_fp or candidate_fp
                candidate = self.repo.conn.execute(
                    "SELECT candidate_id, fingerprint FROM candidates WHERE run_id = ? AND fingerprint = ?", (self.run_id, candidate_fp)
                ).fetchone()
                if candidate is None:
                    legacy_candidate_fp = expression_fingerprint(expression)
                    candidate = self.repo.conn.execute(
                        "SELECT candidate_id, fingerprint FROM candidates WHERE run_id = ? AND fingerprint = ?",
                        (self.run_id, legacy_candidate_fp),
                    ).fetchone()
                if candidate is None and expression:
                    candidate = self.repo.conn.execute(
                        "SELECT candidate_id, fingerprint FROM candidates WHERE run_id = ? AND expression = ? ORDER BY candidate_id LIMIT 1",
                        (self.run_id, expression),
                    ).fetchone()
                candidate_id = int(candidate["candidate_id"]) if candidate else None
                stored_candidate_fp = str(candidate["fingerprint"]) if candidate else candidate_fp
                result = {
                    "candidate_id": candidate_id,
                    "fingerprint": stored_candidate_fp,
                    "simulation_fingerprint": fp,
                    "alpha_id": row.get("alpha_id") or "",
                    "sim_id": row.get("sim_id") or "",
                    "status": row.get("status") or "",
                    "pnl": _float_or_none(row.get("pnl")),
                    "sharpe": _float_or_none(row.get("sharpe")),
                    "turnover": _float_or_none(row.get("turnover")),
                    "fitness": _float_or_none(row.get("fitness")),
                    "returns": _float_or_none(row.get("returns")),
                    "drawdown": _float_or_none(row.get("drawdown")),
                    "error": row.get("error") or row.get("error_details") or "",
                }
                diagnosis = diagnose_sim_result(result)
                if "cand_neg" in diagnosis["failure_tags"] and expression:
                    suggestions = _short_flip_suggestions(expression)
                    diagnosis["short_flip_suggestions"] = suggestions
                    result["short_flip_suggestions"] = suggestions
                result["failure_tags"] = diagnosis["failure_tags"]
                result["repair_objectives"] = diagnosis["repair_objectives"]
                result["diagnosis"] = diagnosis
                self.repo.add_sim_result(self.run_id, result)
                new_status = classify_candidate(
                    sim_status=result["status"],
                    sharpe=result["sharpe"],
                    fitness=result["fitness"],
                    turnover=result["turnover"],
                    hard_error=result["error"],
                ).value
                expression_for_update = expression or fp
                if candidate_id is None and expression:
                    candidate_id = self.repo.upsert_candidate(
                        self.run_id,
                        expression_for_update,
                        stored_candidate_fp,
                        status=new_status,
                        alpha_id=result["alpha_id"],
                        source="batchSim",
                    )
                    result["candidate_id"] = candidate_id
                else:
                    self.repo.update_candidate_status(self.run_id, stored_candidate_fp, new_status, alpha_id=result["alpha_id"])
                if candidate_id is not None:
                    score = score_candidate(
                        {
                            "candidate_id": candidate_id,
                            "expression": expression_for_update,
                            "status": new_status,
                            "alpha_id": result.get("alpha_id") or "",
                            "failure_tags": result.get("failure_tags") or [],
                            "repair_objectives": result.get("repair_objectives") or [],
                        },
                        result,
                        _memory_context_for_run_dir(self.run_dir, _run_config_filters(self.repo, self.run_id, settings)),
                    )
                    self.repo.update_candidate_score(self.run_id, candidate_id, score.score, score.breakdown)
                metrics.append(result)
        artifact = self.record_artifact("simulation_status", path, "SIMULATE")
        return AdapterResult(status="ok", artifacts=[artifact], metrics_delta=metrics)

    def dry_run(self) -> AdapterResult:
        rows = self.repo.list_rows("candidates", self.run_id)
        path = self.artifacts_dir / "03_simulate" / "simulation_status.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "fingerprint",
            "regular_expression",
            "settings_json",
            "sim_id",
            "status",
            "alpha_id",
            "pnl",
            "sharpe",
            "turnover",
            "fitness",
            "error",
            "error_details",
        ]
        scores = [(1.34, 1.08, 0.22), (0.92, 0.62, 0.35), (0.22, 0.12, 0.18)]
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for idx, row in enumerate(rows):
                sharpe, fitness, turnover = scores[idx % len(scores)]
                writer.writerow(
                    {
                        "fingerprint": row["fingerprint"],
                        "regular_expression": row["expression"],
                        "settings_json": "{}",
                        "sim_id": f"dry_sim_{idx}",
                        "status": "COMPLETE",
                        "alpha_id": f"DRY{idx:04d}",
                        "pnl": 100000 * (idx + 1),
                        "sharpe": sharpe,
                        "turnover": turnover,
                        "fitness": fitness,
                        "error": "",
                        "error_details": "",
                    }
                )
        return self.parse_simulation_status(path)

    def run_real(self, alpha_list_path: Path, config: RunConfig) -> AdapterResult:
        skill_root = SKILLS_ROOT / "brain-simAlphasinBatch-and-track"
        script = skill_root / "scripts" / "batch_simulator.py"
        if not script.exists():
            return AdapterResult(status="failed", error_summary=f"batch simulator not found: {script}")
        env = _credential_env(require_brain=True, require_llm=False)
        alpha_dest = copy_artifact_to_run(alpha_list_path, self.artifacts_dir / "03_simulate" / "input")
        alpha_dest = self._score_sorted_alpha_list(alpha_dest)
        if config.max_sim_alphas is not None:
            alpha_dest = self._allocate_alpha_list_quota(alpha_dest, int(config.max_sim_alphas))
        preflight = self._preflight_alpha_list_fields(alpha_dest, config, env)
        if preflight.status != "ok":
            return preflight
        preflight_metrics = []
        preflight_artifacts = []
        for artifact in preflight.artifacts:
            if artifact.get("kind") == "simulation_precheck_status":
                parsed = self.parse_simulation_status(Path(str(artifact.get("path"))))
                preflight_metrics.extend(parsed.metrics_delta)
                preflight_artifacts.extend(parsed.artifacts)
            else:
                preflight_artifacts.append(artifact)
        if preflight.candidates_delta:
            filtered_path = Path(str(preflight.candidates_delta[0].get("filtered_alpha_list") or ""))
            if filtered_path.exists():
                alpha_dest = filtered_path
        alpha_dest, diversity_artifacts = self._filter_alpha_list_by_batch_diversity(alpha_dest)
        preflight_artifacts.extend(diversity_artifacts)
        alpha_count = _alpha_list_count(alpha_dest)
        if alpha_count == 0:
            return AdapterResult(
                status="ok",
                artifacts=preflight_artifacts,
                candidates_delta=[{"submitted_alpha_count": 0}],
                metrics_delta=preflight_metrics,
            )
        alpha_dest = alpha_dest.resolve()
        output_csv = (self.artifacts_dir / "03_simulate" / "simulation_status.csv").resolve()
        runtime_root = self.run_dir.parent.parent if self.run_dir.parent.name == "runs" else self.run_dir.parent
        lease_pool = SimulationLeasePool(runtime_root)
        requested_slots = min(alpha_count, max(1, int(config.batch_size) * int(config.concurrency)))
        lease = lease_pool.acquire(run_id=self.run_id, requested_slots=requested_slots, log_prefix="[batchSim]")
        if lease.slots < alpha_count:
            deferred = alpha_count - lease.slots
            alpha_dest = _limited_alpha_list(alpha_dest, lease.slots).resolve()
            limited_artifact = self.record_artifact("alpha_list_simulation_lease_limited", alpha_dest, "SIMULATE")
            preflight_artifacts.append(limited_artifact)
            print(
                "[batchSim] global simulation lease limited this batch "
                f"to {lease.slots} alpha(s); {deferred} deferred "
                f"(active_slots={lease.active_slots}/{lease.max_slots})"
            )
        else:
            print(f"[batchSim] acquired simulation lease slots={lease.slots} active={lease.active_slots}/{lease.max_slots}")
        try:
            cmd = [
                sys.executable,
                str(script),
                "--config",
                str(self.config_stub()),
                "--alpha-json",
                str(alpha_dest),
                "--output-csv",
                str(output_csv),
                "--batch-size",
                str(config.batch_size),
                "--concurrency",
                str(config.concurrency),
                "--stale-healthcheck-minutes",
                "15",
            ]
            _, returncode = self.run_command(self.name, cmd, skill_root, env=env)
        finally:
            lease_pool.release(lease)
        if returncode != 0 and not output_csv.exists():
            return AdapterResult(status="failed", error_summary=f"batch simulation failed; CSV missing: {output_csv}")
        if not output_csv.exists():
            return AdapterResult(status="failed", error_summary=f"batch simulation CSV missing: {output_csv}")
        parsed = self.parse_simulation_status(output_csv)
        _update_pnl_cache_from_metrics(self.run_dir, parsed.metrics_delta, env)
        parsed.artifacts = preflight_artifacts + parsed.artifacts
        parsed.metrics_delta = preflight_metrics + parsed.metrics_delta
        parsed.candidates_delta.append({"submitted_alpha_count": _alpha_list_count(alpha_dest)})
        return parsed

    def _filter_alpha_list_by_batch_diversity(self, path: Path) -> tuple[Path, list[dict[str, Any]]]:
        filtered_path, rejected_path, report_path = _filter_json_list_by_batch_diversity(path, _expression_from_alpha_list_item)
        artifacts = [self.record_artifact("alpha_list_diversity_report", report_path, "SIMULATE")]
        if rejected_path:
            artifacts.append(self.record_artifact("alpha_list_diversity_rejected", rejected_path, "SIMULATE"))
        return filtered_path, artifacts

    def _preflight_alpha_list_fields(self, alpha_list_path: Path, config: RunConfig, env: dict[str, str]) -> AdapterResult:
        rows = read_json(alpha_list_path)
        if not isinstance(rows, list) or not rows:
            return AdapterResult(status="ok")
        field_result = self._available_datafields(config, env)
        if field_result.status != "ok":
            incomplete_path = self.artifacts_dir / "03_simulate" / "datafields_preflight_incomplete.json"
            write_json(
                incomplete_path,
                {
                    "status": "incomplete",
                    "error_type": _platform_error_type(field_result.error_summary),
                    "error": field_result.error_summary,
                    "policy": "Datafield availability preflight is non-fatal; continue to batch simulation and let simulator/platform errors be recorded per alpha.",
                },
            )
            return AdapterResult(
                status="ok",
                artifacts=[self.record_artifact("datafields_preflight_incomplete", incomplete_path, "SIMULATE")],
            )
        available_fields = set(field_result.candidates_delta[0].get("field_ids") or [])
        field_types = {
            str(key): str(value or "").upper()
            for key, value in (field_result.candidates_delta[0].get("field_types") or {}).items()
        }
        if not field_types:
            field_types = {
                str(row.get("id")): str(row.get("type") or "").upper()
                for row in (field_result.candidates_delta[0].get("fields") or [])
                if isinstance(row, dict) and row.get("id")
            }
        allowed = available_fields | _BUILTIN_EXPRESSION_SYMBOLS
        keep_rows = []
        skipped_rows = []
        for row in rows:
            if not isinstance(row, dict):
                keep_rows.append(row)
                continue
            expression = str(row.get("regular") or row.get("regular_expression") or row.get("expression") or "")
            tokens = _expression_datafield_tokens(expression)
            missing = sorted(
                token
                for token in tokens
                if token not in allowed and token.lower() not in allowed
            )
            if missing:
                skipped_rows.append(
                    {
                        "row": row,
                        "expression": expression,
                        "error": f"Datafield not available in target universe: {', '.join(missing)}",
                        "details": {"missing_datafields": missing},
                        "sim_id_prefix": "precheck_missing_fields",
                    }
                )
                continue
            incompatible = _incompatible_datafield_tokens(tokens, field_types, config.data_type)
            if incompatible:
                rendered = ", ".join(f"{field}({field_type})" for field, field_type in incompatible)
                skipped_rows.append(
                    {
                        "row": row,
                        "expression": expression,
                        "error": f"Datafield type incompatible with {str(config.data_type).upper()}: {rendered}",
                        "details": {
                            "incompatible_datafields": [
                                {"field": field, "type": field_type, "target_data_type": str(config.data_type).upper()}
                                for field, field_type in incompatible
                            ]
                        },
                        "sim_id_prefix": "precheck_incompatible_fields",
                    }
                )
            else:
                keep_rows.append(row)

        artifacts = list(field_result.artifacts)
        candidates_delta = []
        if len(keep_rows) != len(rows):
            filtered = alpha_list_path.with_name(alpha_list_path.stem + "_field_prechecked.json")
            write_json(filtered, keep_rows)
            artifacts.append(self.record_artifact("alpha_list_field_prechecked", filtered, "SIMULATE"))
            candidates_delta.append({"filtered_alpha_list": str(filtered)})
        if skipped_rows:
            status_csv = self.artifacts_dir / "03_simulate" / "simulation_precheck_status.csv"
            self._write_precheck_status(status_csv, skipped_rows)
            artifacts.append(self.record_artifact("simulation_precheck_status", status_csv, "SIMULATE"))
        return AdapterResult(status="ok", artifacts=artifacts, candidates_delta=candidates_delta)

    def _available_datafields(self, config: RunConfig, env: dict[str, str]) -> AdapterResult:
        cache = (
            self.artifacts_dir
            / "03_simulate"
            / "datafields"
            / f"{config.dataset}_{config.region}_delay{config.delay}_{config.universe}_{config.data_type}.json"
        )
        if cache.exists():
            data = read_json(cache)
            field_ids = data.get("field_ids") if isinstance(data, dict) else []
            field_types = data.get("field_types") if isinstance(data, dict) and isinstance(data.get("field_types"), dict) else {}
            fields = data.get("fields") if isinstance(data, dict) and isinstance(data.get("fields"), list) else []
            if field_types or fields:
                return AdapterResult(
                    status="ok",
                    artifacts=[self.record_artifact("datafields_preflight", cache, "SIMULATE")],
                    candidates_delta=[{"field_ids": list(field_ids or []), "field_types": field_types, "fields": fields}],
                )
        old_env = os.environ.copy()
        os.environ.update(env)
        try:
            shared_scripts = SKILLS_ROOT / "brain-shared" / "scripts"
            if str(shared_scripts) not in sys.path:
                sys.path.insert(0, str(shared_scripts))
            import ace_lib  # type: ignore

            session = ace_lib.start_session()
            df = ace_lib.get_datafields(
                session,
                instrument_type="EQUITY",
                region=config.region,
                delay=config.delay,
                universe=config.universe,
                dataset_id=config.dataset,
                data_type=config.data_type,
            )
            field_ids = _field_ids_from_df(df)
            fields = _datafield_records_from_df(df)
            field_types = {str(row.get("id")): str(row.get("type") or "").upper() for row in fields if row.get("id")}
        except Exception as exc:
            return AdapterResult(status="failed", error_summary=f"datafield preflight failed: {exc}")
        finally:
            os.environ.clear()
            os.environ.update(old_env)
        write_json(
            cache,
            {
                "dataset": config.dataset,
                "region": config.region,
                "delay": config.delay,
                "universe": config.universe,
                "data_type": config.data_type,
                "field_ids": sorted(field_ids),
                "field_types": field_types,
                "fields": fields,
            },
        )
        artifact = self.record_artifact("datafields_preflight", cache, "SIMULATE")
        return AdapterResult(
            status="ok",
            artifacts=[artifact],
            candidates_delta=[{"field_ids": sorted(field_ids), "field_types": field_types, "fields": fields}],
        )

    def _write_precheck_status(self, path: Path, skipped_rows: list[dict[str, Any]]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fieldnames = [
            "fingerprint",
            "regular_expression",
            "settings_json",
            "sim_id",
            "status",
            "alpha_id",
            "pnl",
            "sharpe",
            "turnover",
            "fitness",
            "error",
            "error_details",
        ]
        with path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for idx, skipped in enumerate(skipped_rows):
                row = skipped.get("row") if isinstance(skipped.get("row"), dict) else {}
                expression = str(skipped.get("expression") or "")
                error = str(skipped.get("error") or "Simulation precheck failed")
                details = skipped.get("details") if isinstance(skipped.get("details"), dict) else {}
                sim_id_prefix = str(skipped.get("sim_id_prefix") or "precheck_failed")
                settings = row.get("settings") if isinstance(row.get("settings"), dict) else {}
                writer.writerow(
                    {
                        "fingerprint": _candidate_fingerprint(expression, settings),
                        "regular_expression": expression,
                        "settings_json": json.dumps(settings, ensure_ascii=False),
                        "sim_id": f"{sim_id_prefix}_{idx}",
                        "status": "PRECHECK_FAILED",
                        "alpha_id": "",
                        "pnl": "",
                        "sharpe": "",
                        "turnover": "",
                        "fitness": "",
                        "error": error,
                        "error_details": json.dumps(details, ensure_ascii=False),
                    }
                )

    def _score_sorted_alpha_list(self, alpha_list_path: Path) -> Path:
        rows = read_json(alpha_list_path)
        if not isinstance(rows, list):
            return alpha_list_path
        candidates = self.repo.list_rows("candidates", self.run_id)
        if not candidates:
            return alpha_list_path
        scored_candidates = score_candidates(
            candidates,
            self.repo.latest_sim_results_by_candidate(self.run_id),
            _memory_context_for_run_dir(self.run_dir, _run_config_filters(self.repo, self.run_id, _settings_from_alpha_rows(rows))),
            _latest_gate_by_candidate(self.repo, self.run_id),
        )
        by_candidate_id = {
            int(candidate["candidate_id"]): candidate
            for candidate in scored_candidates
            if candidate.get("candidate_id") is not None
        }
        by_expression = {str(candidate.get("expression") or ""): candidate for candidate in scored_candidates}
        by_fingerprint = {str(candidate.get("fingerprint") or ""): candidate for candidate in scored_candidates}

        def row_key(row: Any) -> tuple[float, int]:
            if not isinstance(row, dict):
                return (0.0, 0)
            expression = str(row.get("regular") or row.get("regular_expression") or row.get("expression") or "")
            settings = row.get("settings") if isinstance(row.get("settings"), dict) else {}
            fingerprint = _candidate_fingerprint(expression, settings)
            candidate = by_fingerprint.get(fingerprint) or by_expression.get(expression)
            if not candidate:
                return (0.0, 0)
            candidate_id = int(candidate.get("candidate_id") or 0)
            score = float(candidate.get("selection_score") or 0)
            breakdown = candidate.get("score_breakdown") if isinstance(candidate.get("score_breakdown"), dict) else {}
            if candidate_id:
                self.repo.update_candidate_score(self.run_id, candidate_id, score, breakdown)
                by_candidate_id[candidate_id] = candidate
            return (score, -candidate_id)

        sorted_rows = sorted(rows, key=row_key, reverse=True)
        out = alpha_list_path.with_name(alpha_list_path.stem + "_score_sorted.json")
        write_json(out, sorted_rows)
        self.record_artifact("alpha_list_score_sorted", out, "SIMULATE")
        return out

    def _allocate_alpha_list_quota(self, alpha_list_path: Path, limit: int) -> Path:
        if limit <= 0:
            return alpha_list_path
        rows = read_json(alpha_list_path)
        if not isinstance(rows, list) or len(rows) <= limit:
            return alpha_list_path
        candidates = self.repo.list_rows("candidates", self.run_id)
        selected, report = allocate_simulation_quota(rows, candidates, limit=limit)
        out = alpha_list_path.with_name(f"{alpha_list_path.stem}_quota{limit}{alpha_list_path.suffix}")
        report_path = alpha_list_path.with_name(f"{alpha_list_path.stem}_quota{limit}_report.json")
        write_json(out, selected)
        write_json(report_path, report)
        self.record_artifact("alpha_list_quota_allocated", out, "SIMULATE")
        self.record_artifact("alpha_list_quota_report", report_path, "SIMULATE")
        return out


class EnhanceTemplateAdapter(SkillAdapter):
    name = "enhanceTemplate"

    def dry_run(self, limit: int = 2) -> AdapterResult:
        rows = [
            r
            for r in self.repo.list_rows("candidates", self.run_id)
            if r.get("status") in {CandidateStatus.PROMISING.value, CandidateStatus.NEEDS_ENHANCE.value}
        ][:limit]
        path = self.artifacts_dir / "04_enhance" / "enhanced_expressions.json"
        enhanced = []
        for row in rows:
            expr = f"ts_decay_linear(({row['expression']}), 5)"
            enhanced.append({"expression": expr, "source_candidate_id": row["candidate_id"]})
        write_json(path, enhanced)
        filtered_path, rejected_path = self._filter_enhanced_expressions_by_complexity(
            path,
            _run_config_from_repo(self.repo, self.run_id),
        )
        result = MakeSomeGemAdapter(self.repo, self.run_id, self.run_dir).parse_final_expressions(
            filtered_path,
            source="dry_run_enhance",
            artifact_kind="enhanced_expressions",
            source_stage="ENHANCE",
        )
        if rejected_path:
            result.artifacts.append(self.record_artifact("enhanced_expressions_complexity_rejected", rejected_path, "ENHANCE"))
        return result

    def run_real(
        self,
        idea_files: list[Path],
        config: RunConfig,
        *,
        style: str = "balanced",
        candidates_context: list[dict[str, Any]] | None = None,
    ) -> AdapterResult:
        if not idea_files:
            return AdapterResult(status="ok")
        skill_root = SKILLS_ROOT / "brain-enhance-template"
        script = skill_root / "scripts" / "run.py"
        if not script.exists():
            return AdapterResult(status="failed", error_summary=f"enhance runner not found: {script}")
        env = _credential_env(require_brain=True, require_llm=True)
        env.update(prompt_env(config))
        complexity_budget = _enhance_complexity_budget(config)
        complexity_path = self.artifacts_dir / "04_enhance" / "enhance_complexity_budget.json"
        write_json(complexity_path, complexity_budget)
        self.record_artifact("enhance_complexity_budget", complexity_path, "ENHANCE")
        env["ENHANCE_COMPLEXITY_BUDGET_JSON"] = json.dumps(complexity_budget, ensure_ascii=False)
        env["APPROVED_FORUM_LESSONS_QUERY"] = " ".join(
            [config.dataset, config.region, config.data_type, config.neutralization, "enhance"]
        )
        if candidates_context:
            context_path = self.artifacts_dir / "04_enhance" / "enhance_diagnostics_context.json"
            context_payload = _compact_enhance_context(candidates_context)
            write_json(context_path, {"complexity_budget": complexity_budget, "candidates": context_payload})
            self.record_artifact("enhance_diagnostics_context", context_path, "ENHANCE")
            env["ENHANCE_DIAGNOSTICS_JSON"] = context_path.read_text(encoding="utf-8")
        copied_inputs = [copy_artifact_to_run(p, self.artifacts_dir / "04_enhance" / "input") for p in idea_files]
        copied_inputs = [p.resolve() for p in copied_inputs]
        if len(copied_inputs) == 1:
            cmd = [
                sys.executable,
                str(script),
                "--config",
                str(self.config_stub()),
                "--idea-json",
                str(copied_inputs[0]),
                "--data-type",
                config.data_type,
                "--universe",
                config.universe,
                "--idle-timeout-seconds",
                "900",
            ]
        else:
            cmd = [
                sys.executable,
                str(script),
                "--config",
                str(self.config_stub()),
                "--idea-json-list",
                *[str(p) for p in copied_inputs],
                "--cross-prompt-style",
                style,
                "--data-type",
                config.data_type,
                "--universe",
                config.universe,
                "--idle-timeout-seconds",
                "900",
            ]
        started = max((p.stat().st_mtime for p in copied_inputs if p.exists()), default=0)
        _, returncode = self.run_command(self.name, cmd, skill_root, env=env)
        enhanced_files = _newest_files(skill_root, "enhanced_final_expressions_*.json", started)
        if not enhanced_files:
            enhanced_files = _newest_files(self.artifacts_dir / "04_enhance", "enhanced_final_expressions_*.json", started)
        if returncode != 0 and not enhanced_files:
            return AdapterResult(
                status="ok",
                error_summary="enhance produced no enhanced expressions (non-critical; pipeline will continue without enhancement)",
            )
        result = AdapterResult(status="ok")
        for src in enhanced_files:
            dest = copy_artifact_to_run(src, self.artifacts_dir / "04_enhance")
            self.record_artifact("enhanced_expressions_raw", dest, "ENHANCE")
            filtered_dest, rejected_path = self._filter_enhanced_expressions_by_complexity(dest, config)
            if rejected_path:
                result.artifacts.append(self.record_artifact("enhanced_expressions_complexity_rejected", rejected_path, "ENHANCE"))
            filtered_dest, diversity_rejected_path, diversity_report_path = _filter_json_list_by_batch_diversity(
                filtered_dest,
                _expression_from_enhanced_item,
            )
            result.artifacts.append(self.record_artifact("enhanced_expressions_diversity_report", diversity_report_path, "ENHANCE"))
            if diversity_rejected_path:
                result.artifacts.append(
                    self.record_artifact("enhanced_expressions_diversity_rejected", diversity_rejected_path, "ENHANCE")
                )
            parsed = MakeSomeGemAdapter(self.repo, self.run_id, self.run_dir).parse_final_expressions(
                filtered_dest,
                source="enhanceTemplate",
                artifact_kind="enhanced_expressions",
                source_stage="ENHANCE",
            )
            result.artifacts.extend(parsed.artifacts)
            result.candidates_delta.extend(parsed.candidates_delta)
        return result

    def _filter_enhanced_expressions_by_complexity(self, path: Path, config: RunConfig) -> tuple[Path, Path | None]:
        rows = read_json(path)
        if not isinstance(rows, list):
            return path, None
        budget = _enhance_complexity_budget(config)
        accepted = []
        rejected = []
        for item in rows:
            expression = _expression_from_enhanced_item(item)
            if not expression:
                accepted.append(item)
                continue
            complexity = _expression_complexity(expression)
            over_ops = complexity["operator_count"] > int(budget["max_operators"])
            over_fields = complexity["datafield_count"] > int(budget["max_datafields"])
            if over_ops or over_fields:
                rejected.append({"item": item, "expression": expression, "complexity": complexity, "budget": budget})
            else:
                accepted.append(item)
        if not rejected:
            return path, None
        filtered_path = path.with_name(path.stem + "_complexity_filtered.json")
        rejected_path = path.with_name(path.stem + "_complexity_rejected.json")
        write_json(filtered_path, accepted)
        write_json(rejected_path, rejected)
        return filtered_path, rejected_path


class SubmissionGateAdapter(SkillAdapter):
    name = "submissionGate"

    def dry_run(self) -> AdapterResult:
        rows = [
            r
            for r in self.repo.list_rows("candidates", self.run_id)
            if r.get("status") in {CandidateStatus.MANUAL_REVIEW.value, CandidateStatus.PROMISING.value}
        ]
        ready = []
        for row in rows:
            passed = row.get("alpha_id", "").endswith("0") or row.get("status") == CandidateStatus.MANUAL_REVIEW.value
            gate = {
                "candidate_id": row["candidate_id"],
                "alpha_id": row.get("alpha_id") or "",
                "submission_check": "PASS" if passed else "PENDING",
                "self_corr_check": "PENDING",
                "prod_corr_check": "PENDING",
                "weight_check": "PASS" if passed else "PENDING",
                "subuniverse_check": "PASS" if passed else "PENDING",
                "passed": passed,
            }
            self.repo.add_gate_check(self.run_id, gate)
            if passed:
                self.repo.update_candidate_status(self.run_id, row["fingerprint"], CandidateStatus.SUBMIT_READY.value)
                ready.append(gate)
        path = self.artifacts_dir / "06_gate" / "gate_checks.json"
        write_json(path, ready)
        artifact = self.record_artifact("gate_checks", path, "SUBMIT_GATE")
        return AdapterResult(status="ok", artifacts=[artifact], metrics_delta=ready)

    def run_real(self) -> AdapterResult:
        rows = [
            r
            for r in self.repo.list_rows("candidates", self.run_id)
            if r.get("alpha_id") and r.get("status") in {
                CandidateStatus.SUBMIT_READY.value,
                CandidateStatus.MANUAL_REVIEW.value,
                CandidateStatus.PROMISING.value,
            }
        ]
        if not rows:
            path = self.artifacts_dir / "06_gate" / "gate_checks.json"
            write_json(path, [])
            artifact = self.record_artifact("gate_checks", path, "SUBMIT_GATE")
            return AdapterResult(status="ok", artifacts=[artifact], metrics_delta=[])

        env = _credential_env(require_brain=True, require_llm=False)
        old_env = os.environ.copy()
        os.environ.update(env)
        try:
            shared_scripts = SKILLS_ROOT / "brain-shared" / "scripts"
            if str(shared_scripts) not in sys.path:
                sys.path.insert(0, str(shared_scripts))
            import ace_lib  # type: ignore

            gate_rows = []
            try:
                session = ace_lib.start_session()
            except Exception as exc:
                for row in rows:
                    gate = _gate_error_row(row, exc, ["session"])
                    self.repo.add_gate_check(self.run_id, gate)
                    gate_rows.append(gate)
                path = self.artifacts_dir / "06_gate" / "gate_checks.json"
                write_json(path, gate_rows)
                artifact = self.record_artifact("gate_checks", path, "SUBMIT_GATE")
                return AdapterResult(status="ok", artifacts=[artifact], metrics_delta=gate_rows)

            latest_sim = self.repo.latest_sim_results_by_candidate(self.run_id)
            memory_context = _memory_context_for_run_dir(self.run_dir, _run_config_filters(self.repo, self.run_id))
            for row in rows:
                gate = _run_gate_for_alpha(ace_lib, session, row)
                self.repo.add_gate_check(self.run_id, gate)
                candidate_id = int(row.get("candidate_id") or 0)
                if candidate_id:
                    score = score_candidate(row, latest_sim.get(candidate_id), memory_context, gate)
                    self.repo.update_candidate_score(self.run_id, candidate_id, score.score, score.breakdown)
                if gate.get("passed"):
                    self.repo.update_candidate_status(self.run_id, row["fingerprint"], CandidateStatus.SUBMIT_READY.value)
                elif _should_revoke_submit_ready(row, gate):
                    self.repo.update_candidate_status(self.run_id, row["fingerprint"], CandidateStatus.MANUAL_REVIEW.value)
                gate_rows.append(gate)
        except Exception as exc:
            gate_rows = [_gate_error_row(row, exc, ["gate_runtime"]) for row in rows]
            for gate in gate_rows:
                self.repo.add_gate_check(self.run_id, gate)
        finally:
            os.environ.clear()
            os.environ.update(old_env)

        path = self.artifacts_dir / "06_gate" / "gate_checks.json"
        write_json(path, gate_rows)
        artifact = self.record_artifact("gate_checks", path, "SUBMIT_GATE")
        return AdapterResult(status="ok", artifacts=[artifact], metrics_delta=gate_rows)


def copy_artifact_to_run(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    if src.resolve() != dest.resolve():
        shutil.copy2(src, dest)
    return dest


def _limited_alpha_list(path: Path, limit: int) -> Path:
    if limit <= 0:
        return path
    rows = read_json(path)
    if not isinstance(rows, list) or len(rows) <= limit:
        return path
    limited = path.with_name(f"{path.stem}_first{limit}{path.suffix}")
    write_json(limited, rows[:limit])
    return limited


def _alpha_list_count(path: Path) -> int:
    rows = read_json(path)
    return len(rows) if isinstance(rows, list) else 0


def _enhance_complexity_budget(config: RunConfig) -> dict[str, Any]:
    return {
        "max_operators": int(config.max_operators or _ENHANCE_DEFAULT_MAX_OPERATORS),
        "max_datafields": _ENHANCE_DEFAULT_MAX_DATAFIELDS,
        "policy": (
            "Enhancement may clean, smooth, or repair an alpha, but should not keep adding fields/operators. "
            "Reject enhanced expressions over either budget before simulation. "
            "Across a batch, keep common operators from dominating and prefer coverage of multiple structural themes."
        ),
        "batch_diversity": {
            "common_operators": sorted(_COMMON_BATCH_OPERATORS),
            "max_common_operator_repeats": _BATCH_DIVERSITY_MAX_COMMON_OPERATOR_REPEATS,
            "target_min_structural_themes": _BATCH_DIVERSITY_MIN_STRUCTURAL_THEMES,
            "structural_themes": {theme: sorted(operators) for theme, operators in _STRUCTURAL_OPERATOR_THEMES.items()},
        },
    }


def _expression_from_enhanced_item(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("expression") or item.get("regular") or item.get("regular_expression") or "")
    return ""


def _expression_from_alpha_list_item(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        return str(item.get("regular") or item.get("regular_expression") or item.get("expression") or "")
    return ""


def _expression_complexity(expression: str) -> dict[str, Any]:
    operators = extract_operators(expression)
    datafields = _expression_datafield_tokens_for_complexity(expression)
    return {
        "operator_count": len(operators),
        "unique_operator_count": len(set(operators)),
        "operators": operators,
        "datafield_count": len(datafields),
        "datafields": sorted(datafields),
    }


def _filter_json_list_by_batch_diversity(path: Path, expression_getter: Any) -> tuple[Path, Path | None, Path]:
    rows = read_json(path)
    if not isinstance(rows, list):
        report_path = path.with_name(path.stem + "_diversity_report.json")
        write_json(report_path, _batch_diversity_report([], [], []))
        return path, None, report_path
    accepted_pairs: list[tuple[int, Any]] = []
    rejected: list[dict[str, Any]] = []
    accepted_common_counts: dict[str, int] = {}
    available_themes: set[str] = set()
    analyses: list[dict[str, Any]] = []
    for index, item in enumerate(rows):
        expression = expression_getter(item)
        analysis = _expression_diversity(expression)
        analysis["index"] = index
        analysis["expression"] = expression
        analyses.append(analysis)
        available_themes.update(analysis["structural_themes"])

    ordered = sorted(
        enumerate(rows),
        key=lambda pair: (
            -len(analyses[pair[0]]["structural_themes"]),
            analyses[pair[0]]["common_operator_count"],
            pair[0],
        ),
    )
    accepted_indices: set[int] = set()
    for index, item in ordered:
        expression = analyses[index]["expression"]
        if not expression:
            accepted_pairs.append((index, item))
            accepted_indices.add(index)
            continue
        common_ops = analyses[index]["common_operators"]
        overused = [
            op
            for op in sorted(set(common_ops))
            if accepted_common_counts.get(op, 0) + common_ops.count(op) > _BATCH_DIVERSITY_MAX_COMMON_OPERATOR_REPEATS
        ]
        if overused:
            rejected.append(
                {
                    "item": item,
                    "expression": expression,
                    "diversity": analyses[index],
                    "reason": "common_operator_repeat_limit",
                    "overused_operators": overused,
                    "max_repeats_per_batch": _BATCH_DIVERSITY_MAX_COMMON_OPERATOR_REPEATS,
                }
            )
            continue
        accepted_pairs.append((index, item))
        accepted_indices.add(index)
        for op in common_ops:
            accepted_common_counts[op] = accepted_common_counts.get(op, 0) + 1

    accepted = [item for _, item in sorted(accepted_pairs, key=lambda pair: pair[0])]
    accepted_themes = set()
    for index in accepted_indices:
        accepted_themes.update(analyses[index]["structural_themes"])
    report = _batch_diversity_report(analyses, sorted(accepted_themes), sorted(available_themes))
    report["accepted_count"] = len(accepted)
    report["rejected_count"] = len(rejected)
    report["accepted_common_operator_counts"] = dict(sorted(accepted_common_counts.items()))
    if len(accepted_themes) < min(_BATCH_DIVERSITY_MIN_STRUCTURAL_THEMES, len(available_themes)):
        report["warnings"].append(
            "Accepted batch did not reach available structural theme coverage; generation should add more diverse structures."
        )
    if len(available_themes) < _BATCH_DIVERSITY_MIN_STRUCTURAL_THEMES:
        report["warnings"].append(
            "Source batch does not contain enough structural themes to satisfy the target; regenerate with more theme coverage."
        )

    report_path = path.with_name(path.stem + "_diversity_report.json")
    write_json(report_path, report)
    if not rejected:
        return path, None, report_path
    filtered_path = path.with_name(path.stem + "_diversity_filtered.json")
    rejected_path = path.with_name(path.stem + "_diversity_rejected.json")
    write_json(filtered_path, accepted)
    write_json(rejected_path, rejected)
    return filtered_path, rejected_path, report_path


def _batch_diversity_report(
    analyses: list[dict[str, Any]],
    accepted_themes: list[str],
    available_themes: list[str],
) -> dict[str, Any]:
    return {
        "policy": {
            "common_operators": sorted(_COMMON_BATCH_OPERATORS),
            "max_common_operator_repeats": _BATCH_DIVERSITY_MAX_COMMON_OPERATOR_REPEATS,
            "target_min_structural_themes": _BATCH_DIVERSITY_MIN_STRUCTURAL_THEMES,
        },
        "input_count": len(analyses),
        "accepted_count": len(analyses),
        "rejected_count": 0,
        "available_structural_themes": available_themes,
        "accepted_structural_themes": accepted_themes,
        "warnings": [],
    }


def _expression_diversity(expression: str) -> dict[str, Any]:
    operators = _expression_operator_tokens(expression)
    themes = sorted(_structural_themes_for_operators(operators))
    common = [op for op in operators if op in _COMMON_BATCH_OPERATORS]
    return {
        "operators": operators,
        "structural_themes": themes,
        "common_operators": common,
        "common_operator_count": len(common),
    }


def _structural_themes_for_operators(operators: list[str]) -> set[str]:
    themes: set[str] = set()
    op_set = set(operators)
    for theme, theme_ops in _STRUCTURAL_OPERATOR_THEMES.items():
        if op_set & theme_ops:
            themes.add(theme)
    return themes


def _expression_operator_tokens(expression: str) -> list[str]:
    return re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", expression or "")


def _expression_datafield_tokens_for_complexity(expression: str) -> set[str]:
    tokens: set[str] = set()
    text = _strip_string_literals(str(expression or ""))
    for match in re.finditer(r"\b([A-Za-z][A-Za-z0-9_]*)\b", text):
        token = match.group(1)
        rest = text[match.end(1) :].lstrip()
        lowered = token.lower()
        if rest.startswith("(") or rest.startswith("="):
            continue
        if lowered in _LITERAL_TOKENS or lowered in _GROUPING_SYMBOLS:
            continue
        tokens.add(token)
    return tokens


def _field_ids_from_df(df: Any) -> set[str]:
    if df is None:
        return set()
    try:
        if bool(df.empty):
            return set()
    except Exception:
        pass
    columns_attr = getattr(df, "columns", None)
    columns = list(columns_attr) if columns_attr is not None else []
    id_col = next((col for col in ("id", "field_id", "fieldId") if col in columns), None)
    if not id_col:
        return set()
    try:
        values = df[id_col].dropna().astype(str).tolist()
    except Exception:
        return set()
    return {value.strip() for value in values if value and value.strip()}


def _expression_datafield_tokens(expression: str) -> set[str]:
    text = _strip_string_literals(str(expression or ""))
    tokens: set[str] = set()
    for match in re.finditer(r"\b([A-Za-z][A-Za-z0-9_]*)\b", text):
        token = match.group(1)
        rest = text[match.end(1) :].lstrip()
        if rest.startswith("("):
            continue
        if rest.startswith("="):
            continue
        lowered = token.lower()
        if lowered in _LITERAL_TOKENS:
            continue
        tokens.add(token)
    return tokens


def _strip_string_literals(text: str) -> str:
    return re.sub(r"'[^']*'|\"[^\"]*\"", " ", text)


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_obj(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        data = json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_idea_context_near(anchor_path: Path, idea_file: Any) -> dict[str, Any]:
    raw = str(idea_file or "").strip()
    if not raw:
        return {}
    path = Path(raw)
    if not path.is_absolute():
        near = anchor_path.parent / path
        if near.exists():
            path = near
    return load_idea_context(path)


def _settings_from_config(config: RunConfig) -> dict[str, Any]:
    return {
        "instrumentType": "EQUITY",
        "region": config.region,
        "universe": config.universe,
        "delay": config.delay,
        "decay": config.decay,
        "neutralization": config.neutralization,
        "truncation": config.truncation,
        "pasteurization": "ON",
        "testPeriod": "P0Y0M0D",
        "unitHandling": "VERIFY",
        "nanHandling": "OFF",
        "maxTrade": "ON" if config.max_trade else "OFF",
        "language": "FASTEXPR",
        "visualization": False,
    }


def _neutralization_candidates_for_region(region: str, current: str = "") -> list[str]:
    current_norm = str(current or "").upper()
    region_norm = str(region or "").upper()
    if region_norm in {"ASI", "CHN", "KOR", "TWN", "HKG", "JPN", "GLB"}:
        base = ["MARKET", "INDUSTRY", "SUBINDUSTRY", "SECTOR"]
    elif region_norm in {"USA", "EUR"}:
        base = ["INDUSTRY", "SUBINDUSTRY", "SECTOR", "MARKET"]
    else:
        base = ["INDUSTRY", "SUBINDUSTRY", "MARKET", "SECTOR"]
    if current_norm and current_norm not in base:
        base.insert(0, current_norm)
    elif current_norm in base:
        base = [current_norm] + [item for item in base if item != current_norm]
    return base


def _settings_variants_for_initial_generation(config: RunConfig) -> list[dict[str, Any]]:
    settings = _settings_from_config(config)
    variants = []
    for neutralization in _neutralization_candidates_for_region(config.region, config.neutralization):
        item = dict(settings)
        item["neutralization"] = neutralization
        variants.append(item)
    return variants or [settings]


def _settings_for_candidate_index(config: RunConfig, expression: str, index: int) -> dict[str, Any]:
    variants = _settings_variants_for_initial_generation(config)
    if not variants:
        return _settings_from_config(config)
    return dict(variants[max(0, int(index)) % len(variants)])


def _memory_context_for_run_dir(run_dir: Path, filters: dict[str, Any] | None = None) -> dict[str, Any]:
    path = memory_path_for_run_dir(run_dir)
    if not path.exists():
        return {}
    memory = AlphaMemory(path)
    try:
        filters = filters or {}
        return memory.scoring_context(
            dataset=filters.get("dataset"),
            region=filters.get("region"),
            universe=filters.get("universe"),
            delay=filters.get("delay"),
            data_type=filters.get("data_type"),
            neutralization=filters.get("neutralization"),
        )
    finally:
        memory.close()


def _run_config_filters(repo: Repository, run_id: str, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    run = repo.get_run(run_id)
    if run:
        try:
            config = json.loads(run.get("config_json") or "{}")
        except json.JSONDecodeError:
            config = {}
        filters.update(
            {
                "dataset": config.get("dataset"),
                "region": config.get("region"),
                "universe": config.get("universe"),
                "delay": config.get("delay"),
                "data_type": config.get("data_type"),
                "neutralization": config.get("neutralization"),
            }
        )
    if settings:
        for key in ("region", "universe", "delay", "neutralization"):
            if settings.get(key) not in (None, ""):
                filters[key] = settings.get(key)
    return filters


def _run_config_from_repo(repo: Repository, run_id: str) -> RunConfig:
    run = repo.get_run(run_id)
    if not run:
        raise ValueError(f"Run not found: {run_id}")
    return RunConfig(**json.loads(run["config_json"]))


def _settings_from_alpha_rows(rows: list[Any]) -> dict[str, Any]:
    for row in rows:
        if not isinstance(row, dict):
            continue
        settings = row.get("settings") if isinstance(row.get("settings"), dict) else {}
        if settings:
            return settings
    return {}


def _candidate_fingerprint(expression: str, settings: dict[str, Any] | None = None) -> str:
    return expression_fingerprint(expression, _canonical_settings(settings) if settings else None)


def _canonical_settings(settings: dict[str, Any] | None) -> dict[str, Any]:
    if not settings:
        return {}
    lowered = {str(k).lower(): v for k, v in settings.items()}
    aliases = {
        "instrumentType": ("instrumenttype", "instrument_type"),
        "region": ("region",),
        "universe": ("universe",),
        "delay": ("delay",),
        "decay": ("decay",),
        "neutralization": ("neutralization",),
        "truncation": ("truncation",),
        "pasteurization": ("pasteurization",),
        "testPeriod": ("testperiod", "test_period"),
        "unitHandling": ("unithandling", "unit_handling"),
        "nanHandling": ("nanhandling", "nan_handling"),
        "maxTrade": ("maxtrade", "max_trade"),
        "language": ("language",),
        "visualization": ("visualization",),
    }
    canonical: dict[str, Any] = {}
    for key, names in aliases.items():
        for name in names:
            if name in lowered:
                value = lowered[name]
                if key in {"region", "universe", "neutralization", "maxTrade", "language"} and isinstance(value, str):
                    value = value.upper()
                canonical[key] = value
                break
    return canonical


def _credential_env(*, require_brain: bool, require_llm: bool) -> dict[str, str]:
    env = os.environ.copy()
    creds = load_credentials(require_brain=require_brain, require_llm=require_llm)
    if creds.get("brain_email"):
        env["BRAIN_EMAIL"] = creds["brain_email"]
        env["BRAIN_USERNAME"] = creds["brain_email"]
        env["BRAIN_CREDENTIAL_EMAIL"] = creds["brain_email"]
    if creds.get("brain_password"):
        env["BRAIN_PASSWORD"] = creds["brain_password"]
        env["BRAIN_CREDENTIAL_PASSWORD"] = creds["brain_password"]
    if creds.get("llm_provider"):
        env["BRAIN_LLM_PROVIDER"] = creds["llm_provider"]
        env["LLM_PROVIDER"] = creds["llm_provider"]
    if creds.get("llm_api_key"):
        env["LLM_API_KEY"] = creds["llm_api_key"]
        env["MOONSHOT_API_KEY"] = creds["llm_api_key"]
        if creds.get("llm_provider") == "deepseek":
            env["DEEPSEEK_API_KEY"] = creds["llm_api_key"]
        if creds.get("llm_provider") == "openai":
            env["OPENAI_API_KEY"] = creds["llm_api_key"]
    if creds.get("llm_base_url"):
        env["LLM_BASE_URL"] = creds["llm_base_url"]
        env["MOONSHOT_BASE_URL"] = creds["llm_base_url"]
    if creds.get("llm_model"):
        env["LLM_MODEL"] = creds["llm_model"]
        env["MOONSHOT_MODEL"] = creds["llm_model"]
    return env


def _dataset_category(dataset: str) -> str:
    match = re.match(r"([A-Za-z_]+)", str(dataset))
    return match.group(1).strip("_").lower() if match else "analyst"


def _choose_settings(candidates_path: Path, config: RunConfig) -> dict[str, Any]:
    payload = read_json(candidates_path)
    rows = payload.get("valid_options") or []
    if not rows:
        raise ValueError(f"No settings candidates in {candidates_path}")
    row = rows[0]
    universes = [str(x) for x in row.get("Universe", [])]
    neutralizations = [str(x) for x in row.get("Neutralization", [])]
    universe = config.universe if config.universe in universes else (universes[0] if universes else config.universe)
    requested_neutralization = str(config.neutralization).upper()
    neutralization = requested_neutralization if requested_neutralization in neutralizations else next(
        (n for n in neutralizations if n and n.upper() != "NONE"),
        neutralizations[0] if neutralizations else requested_neutralization,
    )
    settings = _settings_from_config(config)
    settings["instrumentType"] = row.get("InstrumentType", "EQUITY")
    settings["universe"] = universe
    settings["neutralization"] = neutralization
    return settings


def _newest_files(root: Path, pattern: str, started_mtime: float) -> list[Path]:
    files = [p for p in root.rglob(pattern) if p.is_file() and p.stat().st_mtime >= started_mtime]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def _compact_enhance_context(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for candidate in candidates[:4]:
        latest = candidate.get("latest_sim_result") if isinstance(candidate.get("latest_sim_result"), dict) else {}
        diagnosis = candidate.get("diagnosis") if isinstance(candidate.get("diagnosis"), dict) else {}
        compact.append(
            {
                "candidate_id": candidate.get("candidate_id"),
                "status": candidate.get("status"),
                "alpha_id": candidate.get("alpha_id") or latest.get("alpha_id"),
                "expression": _truncate_for_prompt(str(candidate.get("expression") or ""), 500),
                "metrics": {
                    "sharpe": latest.get("sharpe"),
                    "fitness": latest.get("fitness"),
                    "turnover": latest.get("turnover"),
                    "pnl": latest.get("pnl"),
                    "drawdown": latest.get("drawdown"),
                },
                "failure_tags": candidate.get("failure_tags") or [],
                "repair_objectives": candidate.get("repair_objectives") or [],
                "diagnosis_reasons": [
                    _truncate_for_prompt(str(item), 240) for item in (diagnosis.get("diagnosis_reasons") or [])[:4]
                ],
                "repair_hints": [
                    _truncate_for_prompt(str(item), 240) for item in (diagnosis.get("repair_hints") or [])[:4]
                ],
                "short_flip_suggestions": [
                    _truncate_for_prompt(str(item), 500)
                    for item in (diagnosis.get("short_flip_suggestions") or [])[:2]
                ],
                "error": _truncate_for_prompt(str(latest.get("error") or ""), 500),
            }
        )
    return compact


def _truncate_for_prompt(value: str, max_chars: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 3)].rstrip() + "..."


def _latest_gate_by_candidate(repo: Repository, run_id: str) -> dict[int, dict[str, Any]]:
    latest: dict[int, dict[str, Any]] = {}
    for row in repo.list_rows("gate_checks", run_id):
        candidate_id = int(row.get("candidate_id") or 0)
        if candidate_id:
            latest[candidate_id] = row
    return latest


def _load_pnl_cache(run_dir: Path) -> dict[str, list[float]] | None:
    cache_path = run_dir / "pnl_cache.json"
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    result: dict[str, list[float]] = {}
    for key, value in data.items():
        if isinstance(value, list):
            result[str(key)] = [float(v) for v in value if v is not None]
    return result or None


def _update_pnl_cache_from_metrics(run_dir: Path, metrics: list[dict[str, Any]], env: dict[str, str], *, max_fetch: int = 80) -> None:
    alpha_ids = []
    for row in metrics:
        alpha_id = str(row.get("alpha_id") or "")
        if alpha_id and str(row.get("status") or "").upper() in {"COMPLETE", "COMPLETED"}:
            alpha_ids.append(alpha_id)
    if not alpha_ids:
        return
    cache_path = run_dir / "pnl_cache.json"
    cache = _load_pnl_cache(run_dir) or {}
    missing = [alpha_id for alpha_id in dict.fromkeys(alpha_ids) if alpha_id not in cache][:max_fetch]
    if not missing:
        return
    old_env = os.environ.copy()
    os.environ.update(env)
    try:
        shared_scripts = SKILLS_ROOT / "brain-shared" / "scripts"
        if str(shared_scripts) not in sys.path:
            sys.path.insert(0, str(shared_scripts))
        import ace_lib  # type: ignore

        session = ace_lib.start_session()
        for alpha_id in missing:
            try:
                series = _pnl_series_from_frame(ace_lib.get_alpha_pnl(session, alpha_id))
            except Exception:
                continue
            if series:
                cache[alpha_id] = series
    except Exception:
        return
    finally:
        os.environ.clear()
        os.environ.update(old_env)
    if cache:
        write_json(cache_path, cache)


def _pnl_series_from_frame(frame: Any) -> list[float]:
    try:
        if bool(frame.empty):
            return []
    except Exception:
        pass
    columns = list(getattr(frame, "columns", []) or [])
    preferred = next((col for col in ("pnl", "PnL", "dailyPnl", "daily_pnl") if col in columns), None)
    if preferred is None:
        preferred = next((col for col in columns if str(col).lower() not in {"alpha_id", "date"}), None)
    if preferred is None:
        return []
    try:
        values = frame[preferred].dropna().astype(float).tolist()
    except Exception:
        return []
    return [float(value) for value in values]


def _load_datafield_metadata(config: RunConfig, cache_dir: Path, env: dict[str, str]) -> list[dict[str, Any]]:
    cache = cache_dir / f"{config.dataset}_{config.region}_delay{config.delay}_{config.universe}_{config.data_type}.json"
    if cache.exists():
        try:
            data = read_json(cache)
        except (OSError, json.JSONDecodeError):
            data = {}
        if isinstance(data, dict) and isinstance(data.get("fields"), list):
            return [row for row in data["fields"] if isinstance(row, dict)]
    old_env = os.environ.copy()
    os.environ.update(env)
    try:
        shared_scripts = SKILLS_ROOT / "brain-shared" / "scripts"
        if str(shared_scripts) not in sys.path:
            sys.path.insert(0, str(shared_scripts))
        import ace_lib  # type: ignore

        session = ace_lib.start_session()
        df = ace_lib.get_datafields(
            session,
            instrument_type="EQUITY",
            region=config.region,
            delay=config.delay,
            universe=config.universe,
            dataset_id=config.dataset,
            data_type=config.data_type,
        )
        fields = _datafield_records_from_df(df)
    except Exception:
        return []
    finally:
        os.environ.clear()
        os.environ.update(old_env)
    write_json(
        cache,
        {
            "dataset": config.dataset,
            "region": config.region,
            "delay": config.delay,
            "universe": config.universe,
            "data_type": config.data_type,
            "field_ids": sorted(str(row.get("id") or "") for row in fields if row.get("id")),
            "fields": fields,
        },
    )
    return fields


def _datafield_records_from_df(df: Any) -> list[dict[str, Any]]:
    try:
        records = df.to_dict("records")
    except Exception:
        return []
    normalized = []
    for row in records:
        if not isinstance(row, dict):
            continue
        field_id = str(row.get("id") or row.get("field_id") or row.get("fieldId") or "").strip()
        if not field_id:
            continue
        normalized.append(
            {
                "id": field_id,
                "type": str(row.get("type") or row.get("dataType") or row.get("data_type") or "").upper(),
                "coverage": _float_or_none(row.get("coverage")),
                "userCount": _float_or_none(row.get("userCount") or row.get("user_count")),
                "alphaCount": _float_or_none(row.get("alphaCount") or row.get("alpha_count")),
            }
        )
    return normalized


def _field_factory_alpha_rows(config: RunConfig, fields: list[dict[str, Any]], *, max_rows: int | None = None) -> list[dict[str, Any]]:
    rows = []
    field_limit = max(1, min(int(config.max_fields or 8), 20))
    row_limit = max_rows if max_rows is not None else field_limit * 2
    compatible_fields = [row for row in fields if _is_field_compatible_for_data_type(row, config.data_type)]
    selected = sorted(compatible_fields, key=lambda row: (_field_coverage(row), str(row.get("id") or "")), reverse=True)[:field_limit]
    for field in selected:
        field_id = str(field.get("id") or "").strip()
        if not field_id:
            continue
        base = _field_factory_base_expression(field_id, str(field.get("type") or config.data_type).upper())
        coverage = _field_coverage(field)
        signal = f"ts_backfill({base}, 60)" if coverage and coverage < 0.70 else base
        expressions = [
            f"rank(ts_mean({signal}, 20))",
            f"rank(winsorize(ts_delta({signal}, 5), std=4))",
        ]
        for expression in expressions:
            settings = _settings_for_candidate_index(config, expression, len(rows))
            rows.append(
                {
                    "type": "REGULAR",
                    "settings": settings,
                    "regular": expression,
                    "lineage": {"variant_strategy": "field_factory", "field_id": field_id, "coverage": coverage},
                    "variant_strategy": "field_factory",
                    "variant_params": {"field_id": field_id, "coverage": coverage},
                }
            )
            if row_limit and len(rows) >= row_limit:
                return rows
    return rows


def _field_factory_base_expression(field_id: str, data_type: str) -> str:
    if str(data_type or "").upper() == "VECTOR":
        return f"vec_avg({field_id})"
    return field_id


def _is_field_compatible_for_data_type(field: dict[str, Any], data_type: str) -> bool:
    target = str(data_type or "").upper()
    field_type = str(field.get("type") or "").upper()
    if target == "VECTOR":
        return field_type in {"", "VECTOR"}
    return True


def _incompatible_datafield_tokens(
    tokens: list[str],
    field_types: dict[str, str],
    data_type: str,
) -> list[tuple[str, str]]:
    if str(data_type or "").upper() != "VECTOR":
        return []
    incompatible: list[tuple[str, str]] = []
    for token in tokens:
        field_type = str(field_types.get(token) or field_types.get(token.lower()) or "").upper()
        if field_type and field_type != "VECTOR":
            incompatible.append((token, field_type))
    return sorted(set(incompatible))


def _field_coverage(field: dict[str, Any]) -> float:
    value = _float_or_none(field.get("coverage"))
    return float(value) if value is not None else 0.0


def _short_flip_suggestions(expression: str) -> list[str]:
    expr = " ".join(str(expression or "").split())
    if not expr:
        return []
    return [f"multiply(-1, {expr})", f"-({expr})"]


def _run_gate_for_alpha(ace_lib: Any, session: Any, candidate: dict[str, Any]) -> dict[str, Any]:
    alpha_id = str(candidate.get("alpha_id") or "")
    submission_records, submission_error = _submission_gate_records(ace_lib, session, alpha_id)
    submission_records = _normalize_gate_records(submission_records)
    submission_scoring_records = _non_correlation_submission_records(submission_records)
    errors = {
        name: err
        for name, err in {
            "submission": submission_error,
        }.items()
        if err
    }
    checks = submission_records
    passed = (
        bool(submission_scoring_records)
        and not errors
        and _all_pass(submission_scoring_records)
    )

    submission_by_test = {str(item.get("test", "")).upper(): str(item.get("result", "")) for item in submission_scoring_records}
    error_type = _gate_error_type(list(errors.values()))
    return {
        "candidate_id": candidate.get("candidate_id"),
        "alpha_id": alpha_id,
        "submission_check": "ERROR" if submission_error else ("PASS" if submission_scoring_records and _all_pass(submission_scoring_records) else "FAIL"),
        "self_corr_check": "PENDING",
        "prod_corr_check": "PENDING",
        "weight_check": _first_check_result(
            submission_by_test,
            "WEIGHT",
            "WEIGHT_CONCENTRATION",
            "CONCENTRATED_WEIGHT",
        ),
        "subuniverse_check": _first_check_result(
            submission_by_test,
            "SUB_UNIVERSE_SHARPE",
            "SUBUNIVERSE",
            "LOW_SUB_UNIVERSE_SHARPE",
        ),
        "gate_status": "incomplete" if errors else "complete",
        "error_type": error_type,
        "incomplete_checks": sorted(errors),
        "error": "; ".join(f"{name}: {err}" for name, err in sorted(errors.items()))[:1000],
        "passed": passed,
        "checks": checks,
    }


def _submission_gate_records(ace_lib: Any, session: Any, alpha_id: str) -> tuple[list[dict[str, Any]], str]:
    primary_records, primary_error = _safe_gate_records(
        "submission", lambda: ace_lib.get_check_submission(session, alpha_id)
    )
    if primary_records:
        return primary_records, ""

    base_url = str(getattr(ace_lib, "brain_api_url", "https://api.worldquantbrain.com")).rstrip("/")
    detail_records, detail_error = _safe_gate_records(
        "alpha_detail_checks", lambda: _alpha_detail_submission_checks(session, alpha_id, base_url)
    )
    if detail_records:
        return detail_records, ""
    if primary_error:
        joined = "; ".join(part for part in [f"/check: {primary_error}", f"/alphas: {detail_error}" if detail_error else ""] if part)
        return [], joined[:500]
    return [], ""


def _should_revoke_submit_ready(candidate: dict[str, Any], gate: dict[str, Any]) -> bool:
    if str(candidate.get("status") or "") != CandidateStatus.SUBMIT_READY.value:
        return False
    if str(gate.get("gate_status") or "").lower() == "incomplete":
        return False
    return not bool(gate.get("passed"))


def _alpha_detail_submission_checks(session: Any, alpha_id: str, base_url: str) -> list[dict[str, Any]]:
    for _ in range(3):
        response = session.get(f"{base_url}/alphas/{alpha_id}")
        retry_after = _header_value(response, "Retry-After") or _header_value(response, "retry-after")
        if not retry_after:
            break
        time.sleep(float(retry_after))
    if hasattr(response, "raise_for_status"):
        response.raise_for_status()
    payload = response.json()
    checks = ((payload or {}).get("is") or {}).get("checks") or []
    return [dict(item, alpha_id=alpha_id) for item in checks if isinstance(item, dict)]


def _header_value(response: Any, name: str) -> str:
    headers = getattr(response, "headers", {}) or {}
    try:
        return str(headers.get(name) or "")
    except Exception:
        return ""


def _normalize_gate_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = []
    for item in records:
        if not isinstance(item, dict):
            continue
        record = dict(item)
        test = record.get("test") or record.get("name")
        if test:
            record["test"] = str(test).upper()
        result = record.get("result")
        if result:
            record["result"] = str(result).upper()
        normalized.append(record)
    return normalized


def _non_correlation_submission_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    correlation_tests = {"SELF_CORRELATION", "PROD_CORRELATION"}
    return [record for record in records if str(record.get("test") or "").upper() not in correlation_tests]


def _first_check_result(by_test: dict[str, str], *names: str) -> str:
    for name in names:
        result = by_test.get(name)
        if result:
            return result
    return "UNKNOWN"


def _safe_gate_records(name: str, func: Any) -> tuple[list[dict[str, Any]], str]:
    try:
        return _df_records(func()), ""
    except Exception as exc:
        return [], str(exc)[:500]


def _gate_error_row(candidate: dict[str, Any], exc: Exception, checks: list[str]) -> dict[str, Any]:
    error = str(exc)[:1000]
    return {
        "candidate_id": candidate.get("candidate_id"),
        "alpha_id": candidate.get("alpha_id") or "",
        "submission_check": "ERROR",
        "self_corr_check": "ERROR",
        "prod_corr_check": "ERROR",
        "weight_check": "ERROR",
        "subuniverse_check": "ERROR",
        "gate_status": "incomplete",
        "error_type": _gate_error_type([error]),
        "incomplete_checks": sorted(set(checks)),
        "passed": False,
        "error": error,
    }


def _platform_error_type(error: Any) -> str:
    return _gate_error_type([str(error or "")]) or "platform_error"


def _gate_error_type(errors: list[str]) -> str:
    text = " ".join(str(err or "").lower() for err in errors)
    if not text:
        return ""
    network_terms = (
        "proxy",
        "connection",
        "connect",
        "timeout",
        "timed out",
        "retry",
        "ssl",
        "tls",
        "dns",
        "name resolution",
        "temporarily unavailable",
        "remote disconnected",
        "connection aborted",
        "connection reset",
        "429",
        "rate limit",
        "retry-after",
    )
    if any(term in text for term in network_terms):
        return "network_error"
    return "gate_error"


def _df_records(df: Any) -> list[dict[str, Any]]:
    if df is None:
        return []
    if isinstance(df, list):
        return [dict(item) for item in df if isinstance(item, dict)]
    try:
        if bool(df.empty):
            return []
        return df.to_dict(orient="records")
    except Exception:
        return []


def _all_pass(records: list[dict[str, Any]]) -> bool:
    return bool(records) and all(str(item.get("result", "")).upper() == "PASS" for item in records)
