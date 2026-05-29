from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .diagnostics import summarize_failure_tags
from .prompting import summarize_run_prompt_metrics
from .repository import Repository
from .selection import summarize_counts
from .utils import now_iso


MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_run_summaries (
  run_id TEXT PRIMARY KEY,
  dataset TEXT,
  region TEXT,
  universe TEXT,
  delay INTEGER,
  data_type TEXT,
  neutralization TEXT,
  decay INTEGER,
  truncation REAL,
  max_trade INTEGER,
  make_prompt_version TEXT,
  enhance_prompt_version TEXT,
  decision_prompt_version TEXT,
  prompt_experiment TEXT,
  stage TEXT,
  stop_reason TEXT,
  candidate_count INTEGER,
  sim_result_count INTEGER,
  sim_success_count INTEGER,
  promising_count INTEGER,
  needs_enhance_count INTEGER,
  submit_ready_count INTEGER,
  manual_review_count INTEGER,
  rejected_count INTEGER,
  avg_sharpe REAL,
  avg_fitness REAL,
  avg_turnover REAL,
  experiment_score REAL,
  failure_tags_json TEXT,
  source_run_created_at TEXT,
  source_run_updated_at TEXT,
  ingested_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_candidate_observations (
  run_id TEXT NOT NULL,
  candidate_id INTEGER NOT NULL,
  dataset TEXT,
  region TEXT,
  universe TEXT,
  delay INTEGER,
  data_type TEXT,
  neutralization TEXT,
  expression TEXT,
  status TEXT,
  source TEXT,
  parent_candidate_id INTEGER,
  variant_strategy TEXT,
  thesis_id TEXT,
  thesis_type TEXT,
  thesis_json TEXT,
  selection_score REAL,
  score_breakdown_json TEXT,
  operators_json TEXT,
  field_families_json TEXT,
  alpha_id TEXT,
  sim_status TEXT,
  sharpe REAL,
  fitness REAL,
  turnover REAL,
  pnl REAL,
  failure_tags_json TEXT,
  repair_objectives_json TEXT,
  gate_passed INTEGER,
  ingested_at TEXT NOT NULL,
  PRIMARY KEY(run_id, candidate_id)
);
"""


OPERATORS = {
    "abs",
    "add",
    "and",
    "bucket",
    "divide",
    "equal",
    "group_backfill",
    "group_mean",
    "group_neutralize",
    "group_normalize",
    "group_rank",
    "group_scale",
    "group_zscore",
    "hump",
    "hump_decay",
    "inverse",
    "is_nan",
    "multiply",
    "normalize",
    "not",
    "not_equal",
    "or",
    "quantile",
    "rank",
    "regression_neut",
    "regression_proj",
    "reverse",
    "signed_power",
    "subtract",
    "trade_when",
    "ts_arg_max",
    "ts_arg_min",
    "ts_backfill",
    "ts_corr",
    "ts_count_nans",
    "ts_covariance",
    "ts_decay_linear",
    "ts_delta",
    "ts_mean",
    "ts_median",
    "ts_product",
    "ts_quantile",
    "ts_rank",
    "ts_regression",
    "ts_scale",
    "ts_std_dev",
    "ts_sum",
    "ts_zscore",
    "winsorize",
    "zscore",
}


LEARNABLE_MEMORY_LAYERS = {"simulated", "promising", "gate_passed"}
MEMORY_DECAY_HALF_LIFE_DAYS = 120.0


class AlphaMemory:
    def __init__(self, memory_path: str | Path):
        self.memory_path = Path(memory_path)
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.memory_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(MEMORY_SCHEMA)
        self._ensure_column("memory_candidate_observations", "parent_candidate_id", "INTEGER")
        self._ensure_column("memory_candidate_observations", "variant_strategy", "TEXT")
        self._ensure_column("memory_candidate_observations", "thesis_id", "TEXT")
        self._ensure_column("memory_candidate_observations", "thesis_type", "TEXT")
        self._ensure_column("memory_candidate_observations", "thesis_json", "TEXT")
        self.conn.commit()

    @classmethod
    def for_runtime_root(cls, runtime_root: str | Path) -> "AlphaMemory":
        return cls(Path(runtime_root) / "alpha_memory.sqlite3")

    def close(self) -> None:
        self.conn.close()

    def _ensure_column(self, table: str, column: str, ddl: str) -> None:
        rows = self.conn.execute(f"PRAGMA table_info({table})").fetchall()
        if column not in {str(row["name"]) for row in rows}:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def ingest_run(self, repo: Repository, run_id: str) -> dict[str, Any]:
        run = repo.get_run(run_id)
        if not run:
            raise ValueError(f"Run not found: {run_id}")
        config = json.loads(run["config_json"])
        candidates = repo.list_rows("candidates", run_id)
        sim_results = repo.list_rows("sim_results", run_id)
        gate_checks = repo.list_rows("gate_checks", run_id)
        prompt_metrics = summarize_run_prompt_metrics(repo, run_id)
        counts = summarize_counts(candidates)
        failure_tags = summarize_failure_tags(sim_results)
        latest_sim = _latest_by_key(sim_results, "candidate_id")
        latest_gate = _latest_by_key(gate_checks, "candidate_id")
        ts = now_iso()

        self.conn.execute("DELETE FROM memory_candidate_observations WHERE run_id = ?", (run_id,))
        self.conn.execute("DELETE FROM memory_run_summaries WHERE run_id = ?", (run_id,))
        self.conn.execute(
            """
            INSERT INTO memory_run_summaries(
              run_id, dataset, region, universe, delay, data_type, neutralization, decay, truncation, max_trade,
              make_prompt_version, enhance_prompt_version, decision_prompt_version, prompt_experiment,
              stage, stop_reason, candidate_count, sim_result_count, sim_success_count,
              promising_count, needs_enhance_count, submit_ready_count, manual_review_count, rejected_count,
              avg_sharpe, avg_fitness, avg_turnover, experiment_score, failure_tags_json,
              source_run_created_at, source_run_updated_at, ingested_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                config.get("dataset", ""),
                config.get("region", ""),
                config.get("universe", ""),
                config.get("delay"),
                config.get("data_type", ""),
                config.get("neutralization", ""),
                config.get("decay"),
                config.get("truncation"),
                1 if config.get("max_trade") else 0,
                config.get("make_prompt_version", ""),
                config.get("enhance_prompt_version", ""),
                config.get("decision_prompt_version", ""),
                config.get("prompt_experiment", ""),
                run.get("stage", ""),
                run.get("stop_reason", ""),
                len(candidates),
                len(sim_results),
                prompt_metrics.get("sim_success_count"),
                counts.get("promising", 0),
                counts.get("needs_enhance", 0),
                counts.get("submit_ready", 0),
                counts.get("manual_review", 0),
                counts.get("rejected", 0),
                prompt_metrics.get("avg_sharpe"),
                prompt_metrics.get("avg_fitness"),
                prompt_metrics.get("avg_turnover"),
                prompt_metrics.get("score"),
                json.dumps(failure_tags, ensure_ascii=False, sort_keys=True),
                run.get("created_at", ""),
                run.get("updated_at", ""),
                ts,
            ),
        )

        for candidate in candidates:
            candidate_id = int(candidate.get("candidate_id") or 0)
            sim = latest_sim.get(candidate_id) or {}
            gate = latest_gate.get(candidate_id) or {}
            expression = str(candidate.get("expression") or "")
            thesis = _json_obj(candidate.get("thesis_json"))
            self.conn.execute(
                """
                INSERT INTO memory_candidate_observations(
                  run_id, candidate_id, dataset, region, universe, delay, data_type, neutralization,
                  expression, status, source, parent_candidate_id, variant_strategy, thesis_id, thesis_type, thesis_json,
                  selection_score, score_breakdown_json,
                  operators_json, field_families_json, alpha_id, sim_status, sharpe, fitness, turnover, pnl,
                  failure_tags_json, repair_objectives_json, gate_passed, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    candidate_id,
                    config.get("dataset", ""),
                    config.get("region", ""),
                    config.get("universe", ""),
                    config.get("delay"),
                    config.get("data_type", ""),
                    config.get("neutralization", ""),
                    expression,
                    candidate.get("status", ""),
                    candidate.get("source", ""),
                    candidate.get("parent_candidate_id"),
                    candidate.get("variant_strategy", ""),
                    thesis.get("thesis_id") or "",
                    thesis.get("thesis_type") or "",
                    json.dumps(thesis, ensure_ascii=False, sort_keys=True),
                    candidate.get("selection_score") or 0,
                    candidate.get("score_breakdown") or "{}",
                    json.dumps(extract_operators(expression), ensure_ascii=False),
                    json.dumps(extract_field_families(expression), ensure_ascii=False),
                    sim.get("alpha_id") or candidate.get("alpha_id") or "",
                    sim.get("status", ""),
                    sim.get("sharpe"),
                    sim.get("fitness"),
                    sim.get("turnover"),
                    sim.get("pnl"),
                    json.dumps(_split_csv(sim.get("failure_tags")), ensure_ascii=False),
                    json.dumps(_split_csv(sim.get("repair_objectives")), ensure_ascii=False),
                    int(gate.get("passed") or 0),
                    ts,
                ),
            )
        self.conn.commit()
        return {"run_id": run_id, "candidate_observations": len(candidates), "memory_path": str(self.memory_path)}

    def summarize(self, *, dataset: str | None = None, region: str | None = None, limit: int = 10) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if dataset:
            clauses.append("dataset = ?")
            params.append(dataset)
        if region:
            clauses.append("region = ?")
            params.append(region)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        runs = [dict(row) for row in self.conn.execute("SELECT * FROM memory_run_summaries" + where, params).fetchall()]
        observations = [
            dict(row) for row in self.conn.execute("SELECT * FROM memory_candidate_observations" + where, params).fetchall()
        ]
        layer_counts = _layer_counts(observations)
        learnable_observations = [row for row in observations if _memory_learnable(row)]
        return {
            "memory_path": str(self.memory_path),
            "filters": {"dataset": dataset or "", "region": region or ""},
            "run_count": len(runs),
            "candidate_observation_count": len(observations),
            "generated_observation_count": layer_counts.get("generated", 0),
            "precheck_failed_observation_count": layer_counts.get("precheck_failed", 0),
            "simulated_observation_count": layer_counts.get("simulated", 0),
            "gate_passed_observation_count": layer_counts.get("gate_passed", 0),
            "memory_layer_counts": layer_counts,
            "learnable_observation_count": len(learnable_observations),
            "dataset_settings": _dataset_settings_summary(runs, limit),
            "top_failure_tags": _counter_from_json_dicts(runs, "failure_tags_json", limit),
            "top_operators": _counter_from_json_lists(observations, "operators_json", limit),
            "top_learned_operators": _counter_from_json_lists(learnable_observations, "operators_json", limit),
            "top_field_families": _counter_from_json_lists(observations, "field_families_json", limit),
            "top_learned_field_families": _counter_from_json_lists(
                learnable_observations, "field_families_json", limit
            ),
            "top_thesis_types": _counter_from_column(observations, "thesis_type", limit),
            "top_learned_thesis_types": _counter_from_column(learnable_observations, "thesis_type", limit),
            "top_expected_failure_modes": _counter_from_thesis_lists(observations, "expected_failure_modes", limit),
            "top_learned_repair_methods": _counter_from_thesis_lists(
                learnable_observations, "intended_repair_methods", limit
            ),
            "recent_runs": sorted(
                [
                    {
                        "run_id": row.get("run_id"),
                        "dataset": row.get("dataset"),
                        "region": row.get("region"),
                        "submit_ready_count": row.get("submit_ready_count"),
                        "avg_sharpe": row.get("avg_sharpe"),
                        "avg_fitness": row.get("avg_fitness"),
                        "experiment_score": row.get("experiment_score"),
                        "ingested_at": row.get("ingested_at"),
                    }
                    for row in runs
                ],
                key=lambda row: str(row.get("ingested_at") or ""),
                reverse=True,
            )[:limit],
        }

    def scoring_context(
        self,
        *,
        dataset: str | None = None,
        region: str | None = None,
        universe: str | None = None,
        delay: int | None = None,
        data_type: str | None = None,
        neutralization: str | None = None,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        for key, value in (
            ("dataset", dataset),
            ("region", region),
            ("universe", universe),
            ("delay", delay),
            ("data_type", data_type),
            ("neutralization", neutralization),
        ):
            if value is not None and value != "":
                clauses.append(f"{key} = ?")
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        observations = [
            dict(row) for row in self.conn.execute("SELECT * FROM memory_candidate_observations" + where, params).fetchall()
        ]
        return build_scoring_context(observations)


def memory_path_for_run_dir(run_dir: str | Path) -> Path:
    run_dir = Path(run_dir)
    if run_dir.parent.name == "runs":
        return run_dir.parent.parent / "alpha_memory.sqlite3"
    return run_dir.parent / "alpha_memory.sqlite3"


def render_memory_summary_markdown(summary: dict[str, Any]) -> str:
    lines = ["# Alpha Memory Summary", ""]
    lines.append(f"- memory_path: {summary.get('memory_path')}")
    lines.append(f"- run_count: {summary.get('run_count')}")
    lines.append(f"- candidate_observation_count: {summary.get('candidate_observation_count')}")
    lines.append(f"- learnable_observation_count: {summary.get('learnable_observation_count')}")
    lines.append(f"- simulated_observation_count: {summary.get('simulated_observation_count')}")
    lines.append(f"- gate_passed_observation_count: {summary.get('gate_passed_observation_count')}")
    filters = summary.get("filters") if isinstance(summary.get("filters"), dict) else {}
    if filters.get("dataset") or filters.get("region"):
        lines.append(f"- filters: dataset={filters.get('dataset') or '*'}, region={filters.get('region') or '*'}")
    lines.append("")
    lines.append("## Memory Layers")
    layer_counts = summary.get("memory_layer_counts") if isinstance(summary.get("memory_layer_counts"), dict) else {}
    for layer in ("generated", "precheck_failed", "simulated", "promising", "gate_passed"):
        lines.append(f"- {layer}: {layer_counts.get(layer, 0)}")
    lines.append("")
    lines.append("## Dataset Settings")
    for row in summary.get("dataset_settings", []) or []:
        lines.append(
            f"- {row.get('dataset')} {row.get('region')} {row.get('universe')} delay={row.get('delay')} "
            f"neutralization={row.get('neutralization')}: runs={row.get('run_count')}, "
            f"submit_ready={row.get('submit_ready_count')}, avg_sharpe={row.get('avg_sharpe')}, "
            f"avg_fitness={row.get('avg_fitness')}"
        )
    if not summary.get("dataset_settings"):
        lines.append("- None")
    lines.append("")
    lines.append("## Top Failure Tags")
    lines.extend(_render_counter(summary.get("top_failure_tags")))
    lines.append("")
    lines.append("## Top Operators")
    lines.extend(_render_counter(summary.get("top_operators")))
    lines.append("")
    lines.append("## Top Learned Operators")
    lines.extend(_render_counter(summary.get("top_learned_operators")))
    lines.append("")
    lines.append("## Top Field Families")
    lines.extend(_render_counter(summary.get("top_field_families")))
    lines.append("")
    lines.append("## Top Learned Field Families")
    lines.extend(_render_counter(summary.get("top_learned_field_families")))
    lines.append("")
    lines.append("## Top Thesis Types")
    lines.extend(_render_counter(summary.get("top_thesis_types")))
    lines.append("")
    lines.append("## Top Learned Thesis Types")
    lines.extend(_render_counter(summary.get("top_learned_thesis_types")))
    lines.append("")
    lines.append("## Expected Failure Modes")
    lines.extend(_render_counter(summary.get("top_expected_failure_modes")))
    lines.append("")
    lines.append("## Learned Repair Methods")
    lines.extend(_render_counter(summary.get("top_learned_repair_methods")))
    lines.append("")
    lines.append("## Recent Runs")
    for row in summary.get("recent_runs", []) or []:
        lines.append(
            f"- {row.get('run_id')}: dataset={row.get('dataset')}, region={row.get('region')}, "
            f"submit_ready={row.get('submit_ready_count')}, score={row.get('experiment_score')}"
        )
    if not summary.get("recent_runs"):
        lines.append("- None")
    return "\n".join(lines).rstrip() + "\n"


def extract_operators(expression: str) -> list[str]:
    names = set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", expression or ""))
    return sorted(name for name in names if name in OPERATORS)


def extract_field_families(expression: str) -> list[str]:
    tokens = set(re.findall(r"\b([a-z][a-z0-9_]*\d[a-z0-9_]*|[a-z]+\d+_[a-z0-9_]+)\b", (expression or "").lower()))
    families: set[str] = set()
    for token in tokens:
        match = re.match(r"([a-z]+\d+)", token)
        if match:
            families.add(match.group(1))
    return sorted(families)


def build_scoring_context(observations: list[dict[str, Any]]) -> dict[str, Any]:
    operator_stats: dict[str, dict[str, Any]] = {}
    field_family_stats: dict[str, dict[str, Any]] = {}
    thesis_type_stats: dict[str, dict[str, Any]] = {}
    repair_method_stats: dict[str, dict[str, Any]] = {}
    variant_strategy_stats: dict[str, dict[str, Any]] = {}
    layer_counts = _layer_counts(observations)
    learnable_count = 0
    for row in observations:
        if not _memory_learnable(row):
            continue
        learnable_count += 1
        success = _memory_success(row)
        failure_tags = _json_list(row.get("failure_tags_json"))
        weight = _recency_weight(row.get("ingested_at"))
        sharpe = _float_or_none(row.get("sharpe"))
        fitness = _float_or_none(row.get("fitness"))
        turnover = _float_or_none(row.get("turnover"))
        for op in _json_list(row.get("operators_json")):
            _update_memory_stat(operator_stats, str(op), success, failure_tags, weight, sharpe=sharpe, fitness=fitness, turnover=turnover)
        for family in _json_list(row.get("field_families_json")):
            _update_memory_stat(field_family_stats, str(family), success, failure_tags, weight, sharpe=sharpe, fitness=fitness, turnover=turnover)
        thesis = _json_obj(row.get("thesis_json"))
        thesis_type = str(row.get("thesis_type") or thesis.get("thesis_type") or "")
        _update_memory_stat(thesis_type_stats, thesis_type, success, failure_tags, weight, sharpe=sharpe, fitness=fitness, turnover=turnover)
        for method in _json_list(thesis.get("intended_repair_methods") or []):
            _update_memory_stat(repair_method_stats, str(method), success, failure_tags, weight, sharpe=sharpe, fitness=fitness, turnover=turnover)
        variant_strategy = str(row.get("variant_strategy") or "")
        if variant_strategy:
            _update_memory_stat(variant_strategy_stats, variant_strategy, success, failure_tags, weight, sharpe=sharpe, fitness=fitness, turnover=turnover)
    return {
        "observation_count": len(observations),
        "learnable_observation_count": learnable_count,
        "memory_layer_counts": layer_counts,
        "recency_half_life_days": MEMORY_DECAY_HALF_LIFE_DAYS,
        "operator_stats": _finalize_memory_stats(operator_stats),
        "field_family_stats": _finalize_memory_stats(field_family_stats),
        "thesis_type_stats": _finalize_memory_stats(thesis_type_stats),
        "repair_method_stats": _finalize_memory_stats(repair_method_stats),
        "variant_strategy_stats": _finalize_memory_stats(variant_strategy_stats),
    }


def _latest_by_key(rows: list[dict[str, Any]], key: str) -> dict[Any, dict[str, Any]]:
    result: dict[Any, dict[str, Any]] = {}
    for row in rows:
        value = row.get(key)
        if value is not None:
            result[value] = row
    return result


def _split_csv(value: Any) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        data = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else []


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        data = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _memory_success(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "")
    if status in {"submit_ready", "manual_review"}:
        return True
    if int(row.get("gate_passed") or 0):
        return True
    try:
        sharpe = float(row.get("sharpe") or 0)
        fitness = float(row.get("fitness") or 0)
    except (TypeError, ValueError):
        return False
    return sharpe >= 0.8 and fitness >= 0.5


def _memory_layer(row: dict[str, Any]) -> str:
    if int(row.get("gate_passed") or 0):
        return "gate_passed"
    status = str(row.get("status") or "")
    if status in {"submit_ready", "manual_review", "promising", "needs_enhance"}:
        return "promising"
    if _has_sim_evidence(row):
        return "simulated"
    if status in {"sim_failed", "rejected"}:
        return "precheck_failed"
    return "generated"


def _memory_learnable(row: dict[str, Any]) -> bool:
    return _memory_layer(row) in LEARNABLE_MEMORY_LAYERS or _memory_success(row)


def _has_sim_evidence(row: dict[str, Any]) -> bool:
    if str(row.get("sim_status") or "").strip():
        return True
    if str(row.get("alpha_id") or "").strip():
        return True
    return any(row.get(key) not in (None, "") for key in ("sharpe", "fitness", "turnover", "pnl"))


def _layer_counts(observations: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in observations:
        counter[_memory_layer(row)] += 1
    return {
        layer: counter.get(layer, 0)
        for layer in ("generated", "precheck_failed", "simulated", "promising", "gate_passed")
    }


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _recency_weight(value: Any) -> float:
    if not value:
        return 1.0
    raw = str(value).strip()
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return 1.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_days = max((datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 86400.0, 0.0)
    return round(0.5 ** (age_days / MEMORY_DECAY_HALF_LIFE_DAYS), 6)


def _update_memory_stat(
    stats: dict[str, dict[str, Any]],
    key: str,
    success: bool,
    failure_tags: list[Any],
    weight: float,
    sharpe: float | None = None,
    fitness: float | None = None,
    turnover: float | None = None,
) -> None:
    if not key:
        return
    row = stats.setdefault(
        key,
        {
            "observations": 0,
            "successes": 0,
            "weighted_observations": 0.0,
            "weighted_successes": 0.0,
            "failure_tags": Counter(),
            "weighted_failure_tags": Counter(),
            "sharpe_values": [],
            "fitness_values": [],
            "turnover_values": [],
        },
    )
    row["observations"] += 1
    row["weighted_observations"] += float(weight)
    if success:
        row["successes"] += 1
        row["weighted_successes"] += float(weight)
    for tag in failure_tags:
        row["failure_tags"][str(tag)] += 1
        row["weighted_failure_tags"][str(tag)] += float(weight)
    if sharpe is not None:
        row["sharpe_values"].append(sharpe)
    if fitness is not None:
        row["fitness_values"].append(fitness)
    if turnover is not None:
        row["turnover_values"].append(turnover)


HARD_FAILURE_TAGS = {"syntax_error", "unknown_variable", "hard_error", "coverage_issue"}


def _hard_failure_rate_from_tags(failure_tags: dict[str, Any], weighted_observations: float) -> float:
    if not weighted_observations:
        return 0.0
    hard_count = sum(count for tag, count in dict(failure_tags).items() if tag in HARD_FAILURE_TAGS)
    return round(min(hard_count / weighted_observations, 1.0), 4)


def _finalize_memory_stats(stats: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    finalized: dict[str, dict[str, Any]] = {}
    for key, row in stats.items():
        observations = int(row.get("observations") or 0)
        successes = int(row.get("successes") or 0)
        weighted_observations = float(row.get("weighted_observations") or observations)
        weighted_successes = float(row.get("weighted_successes") or successes)
        failure_tags = dict(row.get("failure_tags") or {})
        weighted_failure_tags = dict(row.get("weighted_failure_tags") or failure_tags)
        hard_failure_rate = _hard_failure_rate_from_tags(weighted_failure_tags, weighted_observations)
        finalized[key] = {
            "observations": observations,
            "successes": successes,
            "effective_observations": round(weighted_observations, 4),
            "success_rate": round(weighted_successes / weighted_observations, 4) if weighted_observations else 0.0,
            "raw_success_rate": round(successes / observations, 4) if observations else 0.0,
            "confidence": round(min(weighted_observations / 20.0, 1.0), 4),
            "failure_tags": failure_tags,
            "non_success_rate": round(1.0 - (weighted_successes / weighted_observations), 4) if weighted_observations else 0.0,
            "failure_rate": hard_failure_rate,
            "hard_failure_rate": hard_failure_rate,
            "avg_sharpe": _avg(row.get("sharpe_values") or []),
            "avg_fitness": _avg(row.get("fitness_values") or []),
            "avg_turnover": _avg(row.get("turnover_values") or []),
        }
    return finalized


def _avg(values: list[Any]) -> float | None:
    nums = []
    for value in values:
        if value in (None, ""):
            continue
        try:
            nums.append(float(value))
        except (TypeError, ValueError):
            continue
    return round(sum(nums) / len(nums), 4) if nums else None


def _dataset_settings_summary(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        key = (
            row.get("dataset"),
            row.get("region"),
            row.get("universe"),
            row.get("delay"),
            row.get("data_type"),
            row.get("neutralization"),
        )
        grouped[key].append(row)
    out = []
    for key, group in grouped.items():
        out.append(
            {
                "dataset": key[0],
                "region": key[1],
                "universe": key[2],
                "delay": key[3],
                "data_type": key[4],
                "neutralization": key[5],
                "run_count": len(group),
                "candidate_count": sum(int(row.get("candidate_count") or 0) for row in group),
                "submit_ready_count": sum(int(row.get("submit_ready_count") or 0) for row in group),
                "avg_sharpe": _avg([row.get("avg_sharpe") for row in group]),
                "avg_fitness": _avg([row.get("avg_fitness") for row in group]),
                "avg_turnover": _avg([row.get("avg_turnover") for row in group]),
                "experiment_score": _avg([row.get("experiment_score") for row in group]),
            }
        )
    return sorted(out, key=lambda row: (int(row.get("submit_ready_count") or 0), float(row.get("experiment_score") or 0)), reverse=True)[:limit]


def _counter_from_json_dicts(rows: list[dict[str, Any]], key: str, limit: int) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for row in rows:
        try:
            data = json.loads(row.get(key) or "{}")
        except json.JSONDecodeError:
            data = {}
        if isinstance(data, dict):
            for item, count in data.items():
                counter[str(item)] += int(count or 0)
    return [{"key": item, "count": count} for item, count in counter.most_common(limit)]


def _counter_from_json_lists(rows: list[dict[str, Any]], key: str, limit: int) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for row in rows:
        try:
            data = json.loads(row.get(key) or "[]")
        except json.JSONDecodeError:
            data = []
        if isinstance(data, list):
            for item in data:
                counter[str(item)] += 1
    return [{"key": item, "count": count} for item, count in counter.most_common(limit)]


def _counter_from_column(rows: list[dict[str, Any]], key: str, limit: int) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for row in rows:
        value = str(row.get(key) or "").strip()
        if value:
            counter[value] += 1
    return [{"key": item, "count": count} for item, count in counter.most_common(limit)]


def _counter_from_thesis_lists(rows: list[dict[str, Any]], key: str, limit: int) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for row in rows:
        data = _json_obj(row.get("thesis_json"))
        values = data.get(key)
        if isinstance(values, list):
            for value in values:
                item = str(value).strip()
                if item:
                    counter[item] += 1
    return [{"key": item, "count": count} for item, count in counter.most_common(limit)]


def _render_counter(rows: Any) -> list[str]:
    if not rows:
        return ["- None"]
    return [f"- {item.get('key')}: {item.get('count')}" for item in rows if isinstance(item, dict)]
