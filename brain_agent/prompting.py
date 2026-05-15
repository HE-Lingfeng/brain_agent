from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .diagnostics import summarize_failure_tags
from .repository import Repository
from .selection import summarize_counts
from .utils import json_dumps


DEFAULT_MAKE_PROMPT_VERSION = "make-v1"
DEFAULT_ENHANCE_PROMPT_VERSION = "enhance-v1"
DEFAULT_DECISION_PROMPT_VERSION = "decision-v1"


def prompt_versions_from_config(config: Any) -> dict[str, str]:
    return {
        "make_prompt_version": str(getattr(config, "make_prompt_version", "") or DEFAULT_MAKE_PROMPT_VERSION),
        "enhance_prompt_version": str(getattr(config, "enhance_prompt_version", "") or DEFAULT_ENHANCE_PROMPT_VERSION),
        "decision_prompt_version": str(getattr(config, "decision_prompt_version", "") or DEFAULT_DECISION_PROMPT_VERSION),
        "prompt_experiment": str(getattr(config, "prompt_experiment", "") or ""),
    }


def prompt_env(config: Any) -> dict[str, str]:
    versions = prompt_versions_from_config(config)
    return {
        "BRAIN_AGENT_MAKE_PROMPT_VERSION": versions["make_prompt_version"],
        "BRAIN_AGENT_ENHANCE_PROMPT_VERSION": versions["enhance_prompt_version"],
        "BRAIN_AGENT_DECISION_PROMPT_VERSION": versions["decision_prompt_version"],
        "BRAIN_AGENT_PROMPT_EXPERIMENT": versions["prompt_experiment"],
    }


def summarize_run_prompt_metrics(repo: Repository, run_id: str) -> dict[str, Any]:
    run = repo.get_run(run_id)
    if not run:
        raise ValueError(f"Run not found: {run_id}")
    config = json.loads(run["config_json"])
    candidates = repo.list_rows("candidates", run_id)
    sim_results = repo.list_rows("sim_results", run_id)
    gates = repo.list_rows("gate_checks", run_id)
    counts = summarize_counts(candidates)
    complete = [r for r in sim_results if str(r.get("status") or "").upper() in {"COMPLETE", "COMPLETED", "SUCCESS"}]
    passed_gates = [g for g in gates if int(g.get("passed") or 0) == 1]
    prompt_versions = {
        "make_prompt_version": config.get("make_prompt_version", DEFAULT_MAKE_PROMPT_VERSION),
        "enhance_prompt_version": config.get("enhance_prompt_version", DEFAULT_ENHANCE_PROMPT_VERSION),
        "decision_prompt_version": config.get("decision_prompt_version", DEFAULT_DECISION_PROMPT_VERSION),
        "prompt_experiment": config.get("prompt_experiment", ""),
    }
    metrics = {
        "run_id": run_id,
        "prompt_versions": prompt_versions,
        "candidate_count": len(candidates),
        "sim_result_count": len(sim_results),
        "sim_success_count": len(complete),
        "sim_success_rate": _rate(len(complete), len(sim_results)),
        "valid_rate": _rate(len(sim_results), len(candidates)),
        "promising_count": counts.get("promising", 0),
        "needs_enhance_count": counts.get("needs_enhance", 0),
        "submit_ready_count": counts.get("submit_ready", 0),
        "manual_review_count": counts.get("manual_review", 0),
        "rejected_count": counts.get("rejected", 0),
        "promising_rate": _rate(counts.get("promising", 0) + counts.get("manual_review", 0), len(candidates)),
        "submit_ready_rate": _rate(counts.get("submit_ready", 0), len(candidates)),
        "avg_sharpe": _avg(complete, "sharpe"),
        "avg_fitness": _avg(complete, "fitness"),
        "avg_turnover": _avg(complete, "turnover"),
        "passed_gate_count": len(passed_gates),
        "failure_tags": summarize_failure_tags(sim_results),
        "stage": run.get("stage"),
        "stop_reason": run.get("stop_reason") or "",
    }
    metrics["score"] = prompt_experiment_score(metrics)
    return metrics


def prompt_experiment_score(metrics: dict[str, Any]) -> float:
    score = 0.0
    score += float(metrics.get("sim_success_rate") or 0) * 0.2
    score += float(metrics.get("promising_rate") or 0) * 0.32
    score += float(metrics.get("submit_ready_rate") or 0) * 0.28
    score += min(max(float(metrics.get("avg_sharpe") or 0), 0.0), 2.0) / 2.0 * 0.1
    score += min(max(float(metrics.get("avg_fitness") or 0), 0.0), 1.5) / 1.5 * 0.1
    hard_failures = 0
    tags = metrics.get("failure_tags") if isinstance(metrics.get("failure_tags"), dict) else {}
    for key in ("hard_error", "syntax_error", "unknown_variable", "coverage_issue"):
        hard_failures += int(tags.get(key, 0) or 0)
    if int(metrics.get("sim_result_count") or 0):
        score -= min(hard_failures / int(metrics["sim_result_count"]), 1.0) * 0.18
    return round(max(0.0, score), 4)


def compare_prompt_runs(run_metrics: list[dict[str, Any]]) -> dict[str, Any]:
    ranked = sorted(run_metrics, key=lambda row: float(row.get("score") or 0), reverse=True)
    baseline = ranked[-1] if ranked else {}
    comparisons = []
    for row in ranked:
        comparisons.append(
            {
                "run_id": row.get("run_id"),
                "score": row.get("score"),
                "delta_vs_lowest": round(float(row.get("score") or 0) - float(baseline.get("score") or 0), 4),
                "prompt_versions": row.get("prompt_versions"),
                "sim_success_rate": row.get("sim_success_rate"),
                "promising_rate": row.get("promising_rate"),
                "submit_ready_rate": row.get("submit_ready_rate"),
                "avg_sharpe": row.get("avg_sharpe"),
                "avg_fitness": row.get("avg_fitness"),
                "failure_tags": row.get("failure_tags"),
            }
        )
    return {
        "winner": ranked[0] if ranked else None,
        "ranked_runs": ranked,
        "comparisons": comparisons,
        "promotion_note": (
            "Promote a prompt version only if it improves score and does not increase hard failures "
            "on comparable dataset/settings samples."
        ),
    }


def render_prompt_compare_markdown(report: dict[str, Any]) -> str:
    lines = ["# Prompt A/B Report", ""]
    winner = report.get("winner") if isinstance(report.get("winner"), dict) else None
    if winner:
        versions = winner.get("prompt_versions") or {}
        lines.extend(
            [
                "## Winner",
                f"- run_id: {winner.get('run_id')}",
                f"- score: {winner.get('score')}",
                f"- make_prompt_version: {versions.get('make_prompt_version')}",
                f"- enhance_prompt_version: {versions.get('enhance_prompt_version')}",
                f"- decision_prompt_version: {versions.get('decision_prompt_version')}",
                f"- prompt_experiment: {versions.get('prompt_experiment') or ''}",
                "",
            ]
        )
    lines.append("## Comparison")
    for row in report.get("comparisons", []) or []:
        versions = row.get("prompt_versions") if isinstance(row.get("prompt_versions"), dict) else {}
        lines.extend(
            [
                f"### {row.get('run_id')}",
                f"- score: {row.get('score')} (delta_vs_lowest={row.get('delta_vs_lowest')})",
                f"- versions: make={versions.get('make_prompt_version')}, enhance={versions.get('enhance_prompt_version')}, decision={versions.get('decision_prompt_version')}",
                f"- rates: sim_success={row.get('sim_success_rate')}, promising={row.get('promising_rate')}, submit_ready={row.get('submit_ready_rate')}",
                f"- avg: sharpe={row.get('avg_sharpe')}, fitness={row.get('avg_fitness')}",
                f"- failure_tags: {json.dumps(row.get('failure_tags') or {}, ensure_ascii=False, sort_keys=True)}",
                "",
            ]
        )
    lines.extend(["## Promotion Rule", str(report.get("promotion_note") or "")])
    lines.extend(["", "## Machine Readable", "```json brain_agent_prompt_ab_report", json_dumps(report), "```"])
    return "\n".join(lines).rstrip() + "\n"


def write_prompt_compare_report(report: dict[str, Any], output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.suffix.lower() == ".json":
        output.write_text(json_dumps(report), encoding="utf-8")
    else:
        output.write_text(render_prompt_compare_markdown(report), encoding="utf-8")
    return output


def _rate(num: int, den: int) -> float:
    return round(num / den, 4) if den else 0.0


def _avg(rows: list[dict[str, Any]], key: str) -> float | None:
    values = []
    for row in rows:
        value = row.get(key)
        if value is None or value == "":
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    if not values:
        return None
    return round(sum(values) / len(values), 4)
