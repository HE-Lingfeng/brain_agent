from __future__ import annotations

import csv
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from .credentials import load_credentials
from .diagnostics import diagnose_sim_result
from .memory import AlphaMemory, extract_operators, memory_path_for_run_dir
from .models import AdapterResult, CandidateStatus, RunConfig
from .prompting import prompt_env
from .repository import Repository
from .scoring import score_candidate, score_candidates
from .selection import classify_candidate
from .task_runner import TaskRunner
from .utils import expression_fingerprint, file_sha256, read_json, write_json


REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = REPO_ROOT / ".claude" / "skills"


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

_ENHANCE_DEFAULT_MAX_OPERATORS = 10
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

        handle, returncode = runner.run_tracked(adapter, cmd, cwd, env=env, on_start=_record_running)
        status = "completed" if returncode == 0 else "failed"
        self.repo.update_task_status(handle.task_id, status)
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
        data = read_json(path)
        if isinstance(data, dict):
            expressions = data.get("expressions") or data.get("expression_list") or []
        else:
            expressions = data
        if not isinstance(expressions, list):
            return AdapterResult(status="failed", error_summary=f"Unsupported final_expressions format: {path}")

        candidates = []
        for item in expressions:
            if isinstance(item, str):
                expression = item
                idea_file = ""
            elif isinstance(item, dict):
                expression = item.get("expression") or item.get("regular") or item.get("regular_expression") or ""
                idea_file = item.get("idea_file") or item.get("source") or ""
            else:
                continue
            if not expression:
                continue
            fp = expression_fingerprint(expression)
            cid = self.repo.upsert_candidate(
                self.run_id,
                expression,
                fp,
                status=CandidateStatus.GENERATED.value,
                idea_file=idea_file,
                source=source,
            )
            candidates.append({"candidate_id": cid, "expression": expression, "fingerprint": fp, "idea_file": idea_file})
        artifact = self.record_artifact(artifact_kind, path, source_stage)
        return AdapterResult(status="ok", artifacts=[artifact], candidates_delta=candidates)

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
        data = read_json(path)
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
            cid = self.repo.upsert_candidate(
                self.run_id,
                expression,
                fp,
                status=CandidateStatus.SIM_PENDING.value,
                idea_file=item.get("idea_file") or "",
                source=source,
            )
            score = score_candidate(
                {
                    "candidate_id": cid,
                    "expression": expression,
                    "status": CandidateStatus.SIM_PENDING.value,
                    "source": source,
                },
                memory_context=_memory_context_for_run_dir(self.run_dir, _run_config_filters(self.repo, self.run_id, settings)),
            )
            self.repo.update_candidate_score(self.run_id, cid, score.score, score.breakdown)
            candidates.append({"candidate_id": cid, "expression": expression, "fingerprint": fp, "settings": settings})
        artifact = self.record_artifact("alpha_list", path, "INSPECT")
        return AdapterResult(status="ok", artifacts=[artifact], candidates_delta=candidates)

    def dry_run(self, config: RunConfig) -> AdapterResult:
        generated = self.repo.list_rows("candidates", self.run_id)
        path = self.artifacts_dir / "02_inspect" / "alpha_list.json"
        rows = []
        settings = _settings_from_config(config)
        for row in generated:
            rows.append({"type": "REGULAR", "settings": settings, "regular": row["expression"]})
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

    def write_alpha_list_for_candidates(
        self,
        candidates: list[dict[str, Any]],
        config: RunConfig,
        *,
        name: str = "alpha_list_candidates.json",
    ) -> Path:
        settings = _settings_from_config(config)
        rows = [
            {"type": "REGULAR", "settings": settings, "regular": str(candidate.get("expression") or "")}
            for candidate in candidates
            if candidate.get("expression")
        ]
        out = self.artifacts_dir / "02_inspect" / name
        write_json(out, rows)
        self.record_artifact("alpha_list_generated", out, "INSPECT")
        self.parse_alpha_list(out, source="generated_alpha_list")
        return out


class BatchSimAdapter(SkillAdapter):
    name = "batchSim"

    def parse_simulation_status(self, path: Path) -> AdapterResult:
        metrics = []
        with path.open("r", encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
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
            alpha_dest = _limited_alpha_list(alpha_dest, int(config.max_sim_alphas))
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
        if _alpha_list_count(alpha_dest) == 0:
            return AdapterResult(status="ok", artifacts=preflight_artifacts, metrics_delta=preflight_metrics)
        alpha_dest = alpha_dest.resolve()
        output_csv = (self.artifacts_dir / "03_simulate" / "simulation_status.csv").resolve()
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
        ]
        _, returncode = self.run_command(self.name, cmd, skill_root, env=env)
        if returncode != 0 and not output_csv.exists():
            return AdapterResult(status="failed", error_summary=f"batch simulation failed; CSV missing: {output_csv}")
        if not output_csv.exists():
            return AdapterResult(status="failed", error_summary=f"batch simulation CSV missing: {output_csv}")
        parsed = self.parse_simulation_status(output_csv)
        parsed.artifacts = preflight_artifacts + parsed.artifacts
        parsed.metrics_delta = preflight_metrics + parsed.metrics_delta
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
            return field_result
        available_fields = set(field_result.candidates_delta[0].get("field_ids") or [])
        allowed = available_fields | _BUILTIN_EXPRESSION_SYMBOLS
        keep_rows = []
        skipped_rows = []
        for row in rows:
            if not isinstance(row, dict):
                keep_rows.append(row)
                continue
            expression = str(row.get("regular") or row.get("regular_expression") or row.get("expression") or "")
            missing = sorted(
                token
                for token in _expression_datafield_tokens(expression)
                if token not in allowed and token.lower() not in allowed
            )
            if missing:
                skipped_rows.append((row, expression, missing))
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
            return AdapterResult(
                status="ok",
                artifacts=[self.record_artifact("datafields_preflight", cache, "SIMULATE")],
                candidates_delta=[{"field_ids": list(field_ids or [])}],
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
            },
        )
        artifact = self.record_artifact("datafields_preflight", cache, "SIMULATE")
        return AdapterResult(status="ok", artifacts=[artifact], candidates_delta=[{"field_ids": sorted(field_ids)}])

    def _write_precheck_status(self, path: Path, skipped_rows: list[tuple[dict[str, Any], str, list[str]]]) -> None:
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
            for idx, (row, expression, missing) in enumerate(skipped_rows):
                settings = row.get("settings") if isinstance(row.get("settings"), dict) else {}
                writer.writerow(
                    {
                        "fingerprint": _candidate_fingerprint(expression, settings),
                        "regular_expression": expression,
                        "settings_json": json.dumps(settings, ensure_ascii=False),
                        "sim_id": f"precheck_missing_fields_{idx}",
                        "status": "PRECHECK_FAILED",
                        "alpha_id": "",
                        "pnl": "",
                        "sharpe": "",
                        "turnover": "",
                        "fitness": "",
                        "error": f"Datafield not available in target universe: {', '.join(missing)}",
                        "error_details": json.dumps({"missing_datafields": missing}, ensure_ascii=False),
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
                "self_corr_check": "PASS" if passed else "PENDING",
                "prod_corr_check": "PASS" if passed else "PENDING",
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

            session = ace_lib.start_session()
            gate_rows = []
            for row in rows:
                try:
                    gate = _run_gate_for_alpha(ace_lib, session, row)
                except Exception as exc:
                    gate = {
                        "candidate_id": row.get("candidate_id"),
                        "alpha_id": row.get("alpha_id") or "",
                        "submission_check": "ERROR",
                        "self_corr_check": "ERROR",
                        "prod_corr_check": "ERROR",
                        "weight_check": "ERROR",
                        "subuniverse_check": "ERROR",
                        "passed": False,
                        "error": str(exc)[:500],
                    }
                self.repo.add_gate_check(self.run_id, gate)
                if gate.get("passed"):
                    self.repo.update_candidate_status(self.run_id, row["fingerprint"], CandidateStatus.SUBMIT_READY.value)
                gate_rows.append(gate)
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
    try:
        data = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


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
    if creds.get("moonshot_api_key"):
        env["MOONSHOT_API_KEY"] = creds["moonshot_api_key"]
    if creds.get("moonshot_base_url"):
        env["MOONSHOT_BASE_URL"] = creds["moonshot_base_url"]
    if creds.get("moonshot_model"):
        env["MOONSHOT_MODEL"] = creds["moonshot_model"]
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


def _short_flip_suggestions(expression: str) -> list[str]:
    expr = " ".join(str(expression or "").split())
    if not expr:
        return []
    return [f"multiply(-1, {expr})", f"-({expr})"]


def _run_gate_for_alpha(ace_lib: Any, session: Any, candidate: dict[str, Any]) -> dict[str, Any]:
    alpha_id = str(candidate.get("alpha_id") or "")
    submission_df = ace_lib.get_check_submission(session, alpha_id)
    self_df = ace_lib.check_self_corr_test(session, alpha_id)
    prod_df = ace_lib.check_prod_corr_test(session, alpha_id)

    submission_records = _df_records(submission_df)
    self_records = _df_records(self_df)
    prod_records = _df_records(prod_df)
    checks = submission_records + self_records + prod_records
    passed = bool(checks) and all(str(item.get("result", "")).upper() == "PASS" for item in checks)

    by_test = {str(item.get("test", "")).upper(): str(item.get("result", "")) for item in checks}
    return {
        "candidate_id": candidate.get("candidate_id"),
        "alpha_id": alpha_id,
        "submission_check": "PASS" if submission_records and _all_pass(submission_records) else "FAIL",
        "self_corr_check": by_test.get("SELF_CORRELATION", "UNKNOWN"),
        "prod_corr_check": by_test.get("PROD_CORRELATION", "UNKNOWN"),
        "weight_check": by_test.get("WEIGHT", by_test.get("WEIGHT_CONCENTRATION", "UNKNOWN")),
        "subuniverse_check": by_test.get("SUB_UNIVERSE_SHARPE", by_test.get("SUBUNIVERSE", "UNKNOWN")),
        "passed": passed,
        "checks": checks,
    }


def _df_records(df: Any) -> list[dict[str, Any]]:
    if df is None:
        return []
    try:
        if bool(df.empty):
            return []
        return df.to_dict(orient="records")
    except Exception:
        return []


def _all_pass(records: list[dict[str, Any]]) -> bool:
    return bool(records) and all(str(item.get("result", "")).upper() == "PASS" for item in records)
