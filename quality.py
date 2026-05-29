from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .diagnostics import summarize_failure_tags
from .memory import extract_field_families, extract_operators


SUCCESS_SHARPE = 0.8
SUCCESS_FITNESS = 0.5
COMPLETE_STATUSES = {"COMPLETE", "COMPLETED", "SUCCESS"}


def build_research_quality_summary(
    runtime_root: str | Path,
    *,
    run_ids: list[str] | None = None,
    dataset: str | None = None,
    region: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    root = Path(runtime_root)
    run_db_paths = _select_run_db_paths(root, run_ids=run_ids, limit=limit)
    runs: list[dict[str, Any]] = []
    for db_path in run_db_paths:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            run = _first_run(conn)
            if not run:
                continue
            config = _json_obj(run.get("config_json"))
            if dataset and str(config.get("dataset") or "") != dataset:
                continue
            if region and str(config.get("region") or "").upper() != region.upper():
                continue
            runs.append(_run_quality(conn, str(run["run_id"]), db_path, config))
        finally:
            conn.close()

    aggregate = _aggregate_runs(runs)
    return {
        "runtime_root": str(root),
        "filters": {
            "run_ids": run_ids or [],
            "dataset": dataset or "",
            "region": region.upper() if region else "",
            "limit": limit,
        },
        "run_count": len(runs),
        "aggregate": aggregate,
        "dataset_settings": _dataset_setting_rows(runs),
        "recent_runs": [_compact_run(row) for row in runs],
        "recommendations": _recommendations(aggregate, runs),
    }


def render_research_quality_markdown(summary: dict[str, Any]) -> str:
    lines = ["# Research Quality Summary", ""]
    filters = summary.get("filters") if isinstance(summary.get("filters"), dict) else {}
    lines.append(f"- runtime_root: {summary.get('runtime_root')}")
    lines.append(f"- run_count: {summary.get('run_count')}")
    if filters.get("dataset") or filters.get("region") or filters.get("run_ids"):
        lines.append(
            "- filters: "
            f"dataset={filters.get('dataset') or '*'}, "
            f"region={filters.get('region') or '*'}, "
            f"run_ids={len(filters.get('run_ids') or []) or '*'}"
        )
    lines.append(
        f"- successish: complete result with Sharpe >= {SUCCESS_SHARPE} and Fitness >= {SUCCESS_FITNESS}, "
        "or a candidate already marked manual_review/submit_ready/gate-passed"
    )
    lines.append("")

    aggregate = summary.get("aggregate") if isinstance(summary.get("aggregate"), dict) else {}
    funnel = aggregate.get("funnel") if isinstance(aggregate.get("funnel"), dict) else {}
    lines.append("## Overall Funnel")
    for key in (
        "candidates",
        "simulated_candidates",
        "sim_results",
        "complete_results",
        "successish_results",
        "promising",
        "manual_review",
        "submit_ready",
        "gate_passed",
        "gate_incomplete",
    ):
        lines.append(f"- {key}: {funnel.get(key, 0)}")
    lines.append(f"- simulated_candidate_rate: {funnel.get('simulated_candidate_rate')}")
    lines.append(f"- sim_complete_rate: {funnel.get('sim_complete_rate')}")
    lines.append(f"- successish_rate: {funnel.get('successish_rate')}")
    lines.append(f"- avg_sharpe: {funnel.get('avg_sharpe')}")
    lines.append(f"- avg_fitness: {funnel.get('avg_fitness')}")
    lines.append(f"- avg_turnover: {funnel.get('avg_turnover')}")
    lines.append("")

    lines.append("## Dataset Settings")
    setting_rows = summary.get("dataset_settings") if isinstance(summary.get("dataset_settings"), list) else []
    if setting_rows:
        for row in setting_rows[:20]:
            lines.append(
                "- "
                f"{row.get('dataset')} {row.get('region')} {row.get('universe')} "
                f"delay={row.get('delay')} neutralization={row.get('neutralization')}: "
                f"runs={row.get('run_count')}, simulated={row.get('simulated_candidates')}, "
                f"successish={row.get('successish_results')}, submit_ready={row.get('submit_ready')}, "
                f"avg_sharpe={row.get('avg_sharpe')}, avg_fitness={row.get('avg_fitness')}"
            )
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Failure Tags")
    lines.extend(_render_counter(aggregate.get("failure_tags"), limit=15))
    lines.append("")

    lines.append("## Candidate Statuses")
    lines.extend(_render_counter(aggregate.get("candidate_statuses"), limit=15))
    lines.append("")

    lines.append("## Operator Signals")
    lines.extend(_render_signal_rows(aggregate.get("operator_signals"), limit=15))
    lines.append("")

    lines.append("## Field Family Signals")
    lines.extend(_render_signal_rows(aggregate.get("field_family_signals"), limit=15))
    lines.append("")

    lines.append("## Recent Runs")
    recent = summary.get("recent_runs") if isinstance(summary.get("recent_runs"), list) else []
    if recent:
        for row in recent[:20]:
            f = row.get("funnel") if isinstance(row.get("funnel"), dict) else {}
            cfg = row.get("config") if isinstance(row.get("config"), dict) else {}
            lines.append(
                "- "
                f"{row.get('run_id')}: {cfg.get('dataset')} {cfg.get('region')} "
                f"delay={cfg.get('delay')} neutralization={cfg.get('neutralization')} "
                f"stage={row.get('stage')} candidates={f.get('candidates')} "
                f"simulated={f.get('simulated_candidates')} successish={f.get('successish_results')} "
                f"submit_ready={f.get('submit_ready')} avg_sharpe={f.get('avg_sharpe')} avg_fitness={f.get('avg_fitness')}"
            )
    else:
        lines.append("- None")
    lines.append("")

    lines.append("## Recommendations")
    recommendations = summary.get("recommendations") if isinstance(summary.get("recommendations"), list) else []
    lines.extend(f"- {item}" for item in recommendations) if recommendations else lines.append("- None")
    return "\n".join(lines).rstrip() + "\n"


def _select_run_db_paths(root: Path, *, run_ids: list[str] | None, limit: int) -> list[Path]:
    if run_ids:
        return [root / "runs" / run_id / "brain_agent.sqlite3" for run_id in run_ids if (root / "runs" / run_id / "brain_agent.sqlite3").exists()]
    paths = [path for path in (root / "runs").glob("*/brain_agent.sqlite3") if path.exists()]
    return sorted(paths, key=lambda path: path.stat().st_mtime, reverse=True)[: max(1, int(limit))]


def _first_run(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def _run_quality(conn: sqlite3.Connection, run_id: str, db_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    candidates = _list_rows(conn, "candidates", run_id)
    sim_results = _list_rows(conn, "sim_results", run_id)
    gate_checks = _list_rows(conn, "gate_checks", run_id)
    run = _get_run(conn, run_id) or {}
    candidate_by_id = {int(c["candidate_id"]): c for c in candidates if c.get("candidate_id") is not None}
    latest_sim = _latest_sim_by_candidate(sim_results)
    complete = [row for row in sim_results if _is_complete(row)]
    successish = [row for row in sim_results if _is_successish(row, candidate_by_id, gate_checks)]
    simulated_candidate_ids = {int(row["candidate_id"]) for row in sim_results if row.get("candidate_id") is not None}
    status_counts = Counter(str(row.get("status") or "") for row in candidates)
    funnel = {
        "candidates": len(candidates),
        "simulated_candidates": len(simulated_candidate_ids),
        "sim_results": len(sim_results),
        "complete_results": len(complete),
        "successish_results": len(successish),
        "promising": status_counts.get("promising", 0),
        "manual_review": status_counts.get("manual_review", 0),
        "submit_ready": status_counts.get("submit_ready", 0),
        "gate_checks": len(gate_checks),
        "gate_passed": sum(1 for row in gate_checks if int(row.get("passed") or 0) == 1),
        "gate_incomplete": sum(1 for row in gate_checks if str(row.get("gate_status") or "").lower() == "incomplete"),
        "simulated_candidate_rate": _rate(len(simulated_candidate_ids), len(candidates)),
        "sim_complete_rate": _rate(len(complete), len(sim_results)),
        "successish_rate": _rate(len(successish), len(sim_results)),
        "avg_sharpe": _avg(complete, "sharpe"),
        "avg_fitness": _avg(complete, "fitness"),
        "avg_turnover": _avg(complete, "turnover"),
    }
    operator_signals, family_signals = _signals(candidate_by_id, latest_sim, gate_checks)
    return {
        "run_id": run_id,
        "run_dir": str(db_path.parent),
        "stage": run.get("stage", ""),
        "stop_reason": run.get("stop_reason", ""),
        "created_at": run.get("created_at", ""),
        "updated_at": run.get("updated_at", ""),
        "config": config,
        "funnel": funnel,
        "candidate_statuses": dict(status_counts),
        "failure_tags": summarize_failure_tags(sim_results),
        "operator_signals": operator_signals,
        "field_family_signals": family_signals,
    }


def _get_run(conn: sqlite3.Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def _list_rows(conn: sqlite3.Connection, table: str, run_id: str) -> list[dict[str, Any]]:
    if table not in {"candidates", "sim_results", "gate_checks"}:
        raise ValueError(f"Unsupported table: {table}")
    if not _table_exists(conn, table):
        return []
    rows = conn.execute(f"SELECT * FROM {table} WHERE run_id = ? ORDER BY 1", (run_id,)).fetchall()
    return [dict(row) for row in rows]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1", (table,)).fetchone()
    return row is not None


def _aggregate_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    funnel_counts = Counter()
    status_counts = Counter()
    failure_tags = Counter()
    complete_metric_rows: list[dict[str, Any]] = []
    operator_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    family_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        funnel = run.get("funnel") if isinstance(run.get("funnel"), dict) else {}
        for key in (
            "candidates",
            "simulated_candidates",
            "sim_results",
            "complete_results",
            "successish_results",
            "promising",
            "manual_review",
            "submit_ready",
            "gate_checks",
            "gate_passed",
            "gate_incomplete",
        ):
            funnel_counts[key] += int(funnel.get(key) or 0)
        status_counts.update(run.get("candidate_statuses") or {})
        failure_tags.update(run.get("failure_tags") or {})
        for row in run.get("operator_signals") or []:
            operator_buckets[str(row.get("key") or "")].append(row)
        for row in run.get("field_family_signals") or []:
            family_buckets[str(row.get("key") or "")].append(row)
        if funnel.get("complete_results"):
            complete_metric_rows.append(
                {
                    "sharpe": funnel.get("avg_sharpe"),
                    "fitness": funnel.get("avg_fitness"),
                    "turnover": funnel.get("avg_turnover"),
                    "weight": funnel.get("complete_results"),
                }
            )
    funnel = dict(funnel_counts)
    funnel["simulated_candidate_rate"] = _rate(funnel_counts["simulated_candidates"], funnel_counts["candidates"])
    funnel["sim_complete_rate"] = _rate(funnel_counts["complete_results"], funnel_counts["sim_results"])
    funnel["successish_rate"] = _rate(funnel_counts["successish_results"], funnel_counts["sim_results"])
    funnel["avg_sharpe"] = _weighted_avg(complete_metric_rows, "sharpe")
    funnel["avg_fitness"] = _weighted_avg(complete_metric_rows, "fitness")
    funnel["avg_turnover"] = _weighted_avg(complete_metric_rows, "turnover")
    return {
        "funnel": funnel,
        "candidate_statuses": dict(status_counts.most_common()),
        "failure_tags": dict(failure_tags.most_common()),
        "operator_signals": _merge_signal_buckets(operator_buckets),
        "field_family_signals": _merge_signal_buckets(family_buckets),
    }


def _compact_run(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run.get("run_id"),
        "run_dir": run.get("run_dir"),
        "stage": run.get("stage"),
        "stop_reason": run.get("stop_reason"),
        "created_at": run.get("created_at"),
        "updated_at": run.get("updated_at"),
        "config": run.get("config"),
        "funnel": run.get("funnel"),
        "candidate_statuses": run.get("candidate_statuses"),
        "failure_tags": run.get("failure_tags"),
    }


def _dataset_setting_rows(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        cfg = run.get("config") if isinstance(run.get("config"), dict) else {}
        key = (
            cfg.get("dataset"),
            cfg.get("region"),
            cfg.get("universe"),
            cfg.get("delay"),
            cfg.get("data_type"),
            cfg.get("neutralization"),
        )
        grouped[key].append(run)
    rows = []
    for key, group in grouped.items():
        aggregate = _aggregate_runs(group)
        funnel = aggregate["funnel"]
        rows.append(
            {
                "dataset": key[0],
                "region": key[1],
                "universe": key[2],
                "delay": key[3],
                "data_type": key[4],
                "neutralization": key[5],
                "run_count": len(group),
                **funnel,
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            int(row.get("submit_ready") or 0),
            int(row.get("successish_results") or 0),
            float(row.get("avg_fitness") or 0),
            float(row.get("avg_sharpe") or 0),
        ),
        reverse=True,
    )


def _signals(
    candidate_by_id: dict[int, dict[str, Any]],
    latest_sim: dict[int, dict[str, Any]],
    gate_checks: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    gate_by_candidate = _gate_pass_by_candidate(gate_checks)
    operator_stats: dict[str, dict[str, Any]] = defaultdict(_empty_signal)
    family_stats: dict[str, dict[str, Any]] = defaultdict(_empty_signal)
    for candidate_id, sim in latest_sim.items():
        candidate = candidate_by_id.get(candidate_id)
        if not candidate:
            continue
        expression = str(candidate.get("expression") or "")
        success = _is_successish(sim, candidate_by_id, gate_checks)
        gate_passed = gate_by_candidate.get(candidate_id, False)
        for op in extract_operators(expression):
            _update_signal(operator_stats[op], sim, success, gate_passed)
        for family in extract_field_families(expression):
            _update_signal(family_stats[family], sim, success, gate_passed)
    return _finalize_signals(operator_stats), _finalize_signals(family_stats)


def _empty_signal() -> dict[str, Any]:
    return {"observations": 0, "complete": 0, "successish": 0, "gate_passed": 0, "sharpe": [], "fitness": [], "turnover": []}


def _update_signal(row: dict[str, Any], sim: dict[str, Any], success: bool, gate_passed: bool) -> None:
    row["observations"] += 1
    if _is_complete(sim):
        row["complete"] += 1
    if success:
        row["successish"] += 1
    if gate_passed:
        row["gate_passed"] += 1
    for key in ("sharpe", "fitness", "turnover"):
        value = _float_or_none(sim.get(key))
        if value is not None:
            row[key].append(value)


def _finalize_signals(stats: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for key, row in stats.items():
        observations = int(row["observations"])
        rows.append(
            {
                "key": key,
                "observations": observations,
                "complete": int(row["complete"]),
                "successish": int(row["successish"]),
                "gate_passed": int(row["gate_passed"]),
                "successish_rate": _rate(int(row["successish"]), observations),
                "avg_sharpe": _avg_values(row["sharpe"]),
                "avg_fitness": _avg_values(row["fitness"]),
                "avg_turnover": _avg_values(row["turnover"]),
            }
        )
    return sorted(
        rows,
        key=lambda item: (
            int(item.get("successish") or 0),
            float(item.get("successish_rate") or 0),
            int(item.get("observations") or 0),
        ),
        reverse=True,
    )


def _merge_signal_buckets(buckets: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    rows = []
    for key, values in buckets.items():
        observations = sum(int(row.get("observations") or 0) for row in values)
        complete = sum(int(row.get("complete") or 0) for row in values)
        successish = sum(int(row.get("successish") or 0) for row in values)
        gate_passed = sum(int(row.get("gate_passed") or 0) for row in values)
        weighted_rows = [
            {"weight": int(row.get("complete") or 0), **row}
            for row in values
            if int(row.get("complete") or 0)
        ]
        rows.append(
            {
                "key": key,
                "observations": observations,
                "complete": complete,
                "successish": successish,
                "gate_passed": gate_passed,
                "successish_rate": _rate(successish, observations),
                "avg_sharpe": _weighted_avg(weighted_rows, "avg_sharpe"),
                "avg_fitness": _weighted_avg(weighted_rows, "avg_fitness"),
                "avg_turnover": _weighted_avg(weighted_rows, "avg_turnover"),
            }
        )
    return sorted(
        rows,
        key=lambda item: (
            int(item.get("successish") or 0),
            float(item.get("successish_rate") or 0),
            int(item.get("observations") or 0),
        ),
        reverse=True,
    )


def _latest_sim_by_candidate(sim_results: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    latest: dict[int, dict[str, Any]] = {}
    for row in sim_results:
        if row.get("candidate_id") is not None:
            latest[int(row["candidate_id"])] = row
    return latest


def _is_complete(row: dict[str, Any]) -> bool:
    return str(row.get("status") or "").upper() in COMPLETE_STATUSES


def _is_successish(
    sim: dict[str, Any],
    candidate_by_id: dict[int, dict[str, Any]],
    gate_checks: list[dict[str, Any]],
) -> bool:
    candidate_id = sim.get("candidate_id")
    if candidate_id is not None:
        candidate = candidate_by_id.get(int(candidate_id), {})
        if str(candidate.get("status") or "") in {"submit_ready", "manual_review"}:
            return True
    if candidate_id is not None and _gate_pass_by_candidate(gate_checks).get(int(candidate_id), False):
        return True
    sharpe = _float_or_none(sim.get("sharpe")) or 0.0
    fitness = _float_or_none(sim.get("fitness")) or 0.0
    return _is_complete(sim) and sharpe >= SUCCESS_SHARPE and fitness >= SUCCESS_FITNESS


def _gate_pass_by_candidate(gate_checks: list[dict[str, Any]]) -> dict[int, bool]:
    out: dict[int, bool] = {}
    for row in gate_checks:
        if row.get("candidate_id") is not None:
            out[int(row["candidate_id"])] = bool(int(row.get("passed") or 0))
    return out


def _recommendations(aggregate: dict[str, Any], runs: list[dict[str, Any]]) -> list[str]:
    funnel = aggregate.get("funnel") if isinstance(aggregate.get("funnel"), dict) else {}
    tags = aggregate.get("failure_tags") if isinstance(aggregate.get("failure_tags"), dict) else {}
    recommendations: list[str] = []
    if funnel.get("candidates") and float(funnel.get("simulated_candidate_rate") or 0) < 0.2:
        recommendations.append("Simulation coverage is thin; prioritize quota allocation and candidate clustering before drawing strong conclusions.")
    if tags:
        top_tag = next(iter(tags))
        recommendations.append(f"Top failure pattern is {top_tag}; add or tune a targeted optimizer for this failure mode.")
    if tags.get("low_sharpe") or tags.get("low_fitness"):
        recommendations.append("Weak-signal failures dominate; add local variant search for direction, windows, normalization, and field-family alternatives.")
    if tags.get("low_turnover"):
        recommendations.append("Low-turnover appears frequently; test variants that relax entry filters or reduce excessive smoothing.")
    if funnel.get("successish_results", 0) and not funnel.get("submit_ready", 0):
        recommendations.append("Some candidates clear the local quality threshold but not submit readiness; inspect gate/correlation failures separately.")
    if runs and not recommendations:
        recommendations.append("No dominant failure pattern detected; compare prompt versions and dataset settings before changing generation rules.")
    return recommendations


def _render_counter(raw: Any, *, limit: int) -> list[str]:
    if not isinstance(raw, dict) or not raw:
        return ["- None"]
    return [f"- {key}: {value}" for key, value in list(raw.items())[:limit]]


def _render_signal_rows(raw: Any, *, limit: int) -> list[str]:
    if not isinstance(raw, list) or not raw:
        return ["- None"]
    lines = []
    for row in raw[:limit]:
        lines.append(
            "- "
            f"{row.get('key')}: observations={row.get('observations')}, "
            f"successish={row.get('successish')}, successish_rate={row.get('successish_rate')}, "
            f"avg_sharpe={row.get('avg_sharpe')}, avg_fitness={row.get('avg_fitness')}"
        )
    return lines


def _json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _float_or_none(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _avg(rows: list[dict[str, Any]], key: str) -> float | None:
    return _avg_values([_float_or_none(row.get(key)) for row in rows])


def _avg_values(values: list[Any]) -> float | None:
    nums = [float(value) for value in values if value is not None]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 4)


def _weighted_avg(rows: list[dict[str, Any]], key: str) -> float | None:
    total = 0.0
    weight_sum = 0
    for row in rows:
        value = _float_or_none(row.get(key))
        weight = int(row.get("weight") or 0)
        if value is None or weight <= 0:
            continue
        total += value * weight
        weight_sum += weight
    if not weight_sum:
        return None
    return round(total / weight_sum, 4)


def _rate(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0
