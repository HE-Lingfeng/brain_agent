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
COMPARABLE_SETTING_KEYS = (
    "dataset",
    "region",
    "delay",
    "universe",
    "data_type",
    "decay",
    "truncation",
    "neutralization",
    "max_trade",
)
HARD_FAILURE_TAGS = ("hard_error", "syntax_error", "unknown_variable", "coverage_issue", "datafield_unavailable")
PROMOTION_THRESHOLDS = {
    "valid_rate": 0.02,
    "promising_rate": 0.02,
    "avg_fitness": 0.05,
    "hard_failure_rate": 0.05,
}


def prompt_versions_from_config(config: Any) -> dict[str, str]:
    return {
        "make_prompt_version": str(getattr(config, "make_prompt_version", "") or DEFAULT_MAKE_PROMPT_VERSION),
        "enhance_prompt_version": str(getattr(config, "enhance_prompt_version", "") or DEFAULT_ENHANCE_PROMPT_VERSION),
        "decision_prompt_version": str(getattr(config, "decision_prompt_version", "") or DEFAULT_DECISION_PROMPT_VERSION),
        "prompt_experiment": str(getattr(config, "prompt_experiment", "") or ""),
    }


def prompt_env(config: Any) -> dict[str, str]:
    versions = prompt_versions_from_config(config)
    research_policy = {
        "mode": "template_guided",
        "policy_version": "forum-template-v1",
        "template_discipline": [
            "Start from reusable alpha template structures, then fill them with target-dataset fields.",
            "Generate several related variants per thesis instead of one-off raw signals.",
            "Keep each expression tied to one economic hypothesis and one target dataset unless the user explicitly asks for cross-dataset work.",
            "Pre-check operator availability and field compatibility before relying on a template family.",
            "Prefer trading-calendar windows with economic meaning: 5, 22, 66, 120, 252, 504.",
        ],
        "dataset_template_routing": {
            "price_volume": ["turnover_stability", "short_reversal", "volume_price_correlation", "decayed_momentum"],
            "fundamental": ["profit_to_size", "time_series_then_group_rank", "standardized_backfill", "double_neutralization"],
            "analyst": ["vector_aggregation", "analyst_revision", "momentum_residualized_analyst", "coverage_filter"],
            "news_sentiment": ["positive_minus_negative_sentiment", "sentiment_regression_residual", "conditional_sentiment_filter"],
            "option": ["put_call_greek_spread", "option_price_to_close", "implied_vs_realized_volatility"],
            "macro": ["to_nan_backfill_quantile", "time_series_then_cross_section"],
        },
        "diversity_targets": {
            "min_variants_per_batch": 8,
            "avoid_dominating_operator_family": True,
            "prefer_multiple_structural_themes": True,
            "vary_field_operator_window_and_neutralization": True,
        },
        "quality_floor": {
            "do_not_lower_standards_after_failures": True,
            "treat_low_margin_or_low_fitness_as_repair_objectives": True,
            "direction_may_need_sign_flip_after_empirical_test": True,
            "reject_factory_shaped_or_long_flat_pnl_candidates": True,
        },
        "data_handling": {
            "sparse_or_macro_fields": ["to_nan", "ts_backfill", "ts_quantile before winsorize when appropriate"],
            "low_frequency_fields": ["ts_backfill", "winsorize_or_quantile", "time_series_then_cross_section"],
            "vector_fields": ["vec_avg", "vec_sum", "vec_count before ts/group operations"],
        },
        "neutralization_hints": {
            "fundamental_or_analyst": ["SLOW", "STATISTICAL"],
            "news_or_sentiment": ["FAST", "SLOW_AND_FAST"],
            "option_or_macro": ["MARKET", "SLOW_AND_FAST"],
            "institutions": ["CROWDING", "REVERSION_AND_MOMENTUM"],
            "short_interest": ["SLOW", "CROWDING"],
        },
        "high_signal_template_archetypes": [
            "profit_to_size_ratio",
            "market_cap_bucket_then_industry_neutralization",
            "turnover_stability_reversal",
            "macro_to_nan_backfill_quantile",
            "news_sentiment_regression_residual",
        ],
    }
    return {
        "BRAIN_AGENT_MAKE_PROMPT_VERSION": versions["make_prompt_version"],
        "BRAIN_AGENT_ENHANCE_PROMPT_VERSION": versions["enhance_prompt_version"],
        "BRAIN_AGENT_DECISION_PROMPT_VERSION": versions["decision_prompt_version"],
        "BRAIN_AGENT_PROMPT_EXPERIMENT": versions["prompt_experiment"],
        "BRAIN_AGENT_RESEARCH_POLICY_JSON": json.dumps(research_policy, ensure_ascii=False),
        "BRAIN_AGENT_FACTOR_THESIS_FIRST": "1",
        "BRAIN_AGENT_FACTOR_THESIS_SCHEMA": json.dumps(
            {
                "factor_thesis": "Plain-language factor hypothesis before writing the expression.",
                "thesis_type": "Stable class such as momentum_or_reversal, fundamental_cross_section, liquidity_microstructure.",
                "field_families": ["families or dataset slices intentionally used"],
                "expected_failure_modes": ["likely failure tags before simulation"],
                "intended_repair_methods": ["preferred local repairs if the alpha weakly works or fails"],
                "expression": "Final FASTEXPR expression generated from the thesis.",
            },
            ensure_ascii=False,
        ),
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
    comparable_settings = _comparable_settings(config)
    failure_tags = summarize_failure_tags(sim_results)
    hard_failure_count = _hard_failure_count(failure_tags)
    metrics = {
        "run_id": run_id,
        "comparable_settings": comparable_settings,
        "settings_signature": settings_signature(comparable_settings),
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
        "failure_tags": failure_tags,
        "hard_failure_count": hard_failure_count,
        "hard_failure_rate": _rate(hard_failure_count, len(sim_results)),
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
    hard_failures = int(metrics.get("hard_failure_count") or 0)
    if int(metrics.get("sim_result_count") or 0):
        score -= min(hard_failures / int(metrics["sim_result_count"]), 1.0) * 0.18
    return round(max(0.0, score), 4)


def compare_prompt_runs(run_metrics: list[dict[str, Any]]) -> dict[str, Any]:
    baseline = run_metrics[0] if run_metrics else {}
    groups = _settings_groups(run_metrics)
    comparable = len(groups) <= 1
    ranked = sorted(run_metrics, key=lambda row: float(row.get("score") or 0), reverse=True) if comparable else []
    promotion = _promotion_decision(baseline, ranked[0] if ranked else None, comparable=comparable)
    comparisons = []
    for row in (ranked or run_metrics):
        deltas = _metric_deltas(row, baseline)
        comparisons.append(
            {
                "run_id": row.get("run_id"),
                "comparison_status": "comparable" if comparable else "not_comparable",
                "score": row.get("score"),
                "delta_vs_baseline": deltas,
                "comparable_settings": row.get("comparable_settings"),
                "settings_signature": row.get("settings_signature"),
                "prompt_versions": row.get("prompt_versions"),
                "valid_rate": row.get("valid_rate"),
                "sim_success_rate": row.get("sim_success_rate"),
                "promising_rate": row.get("promising_rate"),
                "submit_ready_rate": row.get("submit_ready_rate"),
                "avg_sharpe": row.get("avg_sharpe"),
                "avg_fitness": row.get("avg_fitness"),
                "hard_failure_rate": row.get("hard_failure_rate"),
                "hard_failure_count": row.get("hard_failure_count"),
                "failure_tags": row.get("failure_tags"),
            }
        )
    return {
        "comparison_status": "comparable" if comparable else "not_comparable",
        "comparable": comparable,
        "baseline_run_id": baseline.get("run_id") if baseline else None,
        "settings_groups": groups,
        "winner": ranked[0] if ranked else None,
        "ranked_runs": ranked,
        "comparisons": comparisons,
        "promotion": promotion,
        "promotion_note": (
            "Direct prompt comparison is valid only for runs with the same dataset/settings signature. "
            "Promote a prompt version only when it improves valid rate, promising rate, avg fitness, "
            "or hard-failure rate versus the baseline without introducing hard-failure regression."
        ),
    }


def render_prompt_compare_markdown(report: dict[str, Any]) -> str:
    lines = ["# Prompt A/B Report", ""]
    lines.extend(
        [
            "## Comparability",
            f"- status: {report.get('comparison_status')}",
            f"- baseline_run_id: {report.get('baseline_run_id') or ''}",
        ]
    )
    groups = report.get("settings_groups") if isinstance(report.get("settings_groups"), dict) else {}
    for signature, run_ids in groups.items():
        lines.append(f"- settings_signature: `{signature}` runs={run_ids}")
    lines.append("")
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
    else:
        lines.extend(["## Winner", "- None; runs are not directly comparable." if not report.get("comparable") else "- None", ""])
    promotion = report.get("promotion") if isinstance(report.get("promotion"), dict) else {}
    lines.extend(
        [
            "## Promotion Decision",
            f"- eligible: {promotion.get('eligible')}",
            f"- candidate_run_id: {promotion.get('candidate_run_id') or ''}",
            f"- baseline_run_id: {promotion.get('baseline_run_id') or ''}",
        ]
    )
    for reason in promotion.get("reasons", []) or []:
        lines.append(f"- reason: {reason}")
    for evidence in promotion.get("evidence", []) or []:
        lines.append(f"- evidence: {evidence}")
    lines.append("")
    lines.append("## Comparison")
    for row in report.get("comparisons", []) or []:
        versions = row.get("prompt_versions") if isinstance(row.get("prompt_versions"), dict) else {}
        deltas = row.get("delta_vs_baseline") if isinstance(row.get("delta_vs_baseline"), dict) else {}
        lines.extend(
            [
                f"### {row.get('run_id')}",
                f"- status: {row.get('comparison_status')}",
                f"- settings_signature: `{row.get('settings_signature')}`",
                f"- score: {row.get('score')} (delta_vs_baseline={deltas.get('score')})",
                f"- versions: make={versions.get('make_prompt_version')}, enhance={versions.get('enhance_prompt_version')}, decision={versions.get('decision_prompt_version')}",
                f"- rates: valid={row.get('valid_rate')}, sim_success={row.get('sim_success_rate')}, promising={row.get('promising_rate')}, submit_ready={row.get('submit_ready_rate')}",
                f"- avg: sharpe={row.get('avg_sharpe')}, fitness={row.get('avg_fitness')}",
                f"- hard_failures: count={row.get('hard_failure_count')}, rate={row.get('hard_failure_rate')}",
                f"- deltas: valid={deltas.get('valid_rate')}, promising={deltas.get('promising_rate')}, avg_fitness={deltas.get('avg_fitness')}, hard_failure_rate={deltas.get('hard_failure_rate')}",
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


def _comparable_settings(config: dict[str, Any]) -> dict[str, Any]:
    return {key: config.get(key) for key in COMPARABLE_SETTING_KEYS}


def settings_signature(settings: dict[str, Any]) -> str:
    return "|".join(f"{key}={settings.get(key)}" for key in COMPARABLE_SETTING_KEYS)


def _settings_groups(run_metrics: list[dict[str, Any]]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {}
    for row in run_metrics:
        signature = str(row.get("settings_signature") or "")
        groups.setdefault(signature, []).append(str(row.get("run_id") or ""))
    return groups


def _metric_deltas(row: dict[str, Any], baseline: dict[str, Any]) -> dict[str, float | None]:
    keys = ("score", "valid_rate", "sim_success_rate", "promising_rate", "submit_ready_rate", "avg_sharpe", "avg_fitness", "hard_failure_rate")
    return {key: _delta(row.get(key), baseline.get(key)) for key in keys}


def _promotion_decision(
    baseline: dict[str, Any],
    candidate: dict[str, Any] | None,
    *,
    comparable: bool,
) -> dict[str, Any]:
    if not comparable:
        return {
            "eligible": False,
            "candidate_run_id": None,
            "baseline_run_id": baseline.get("run_id") if baseline else None,
            "evidence": [],
            "reasons": ["Runs use different dataset/settings signatures; direct prompt promotion is blocked."],
            "thresholds": PROMOTION_THRESHOLDS,
        }
    if not candidate:
        return {
            "eligible": False,
            "candidate_run_id": None,
            "baseline_run_id": baseline.get("run_id") if baseline else None,
            "evidence": [],
            "reasons": ["No candidate run was available."],
            "thresholds": PROMOTION_THRESHOLDS,
        }
    if candidate.get("run_id") == baseline.get("run_id"):
        return {
            "eligible": False,
            "candidate_run_id": candidate.get("run_id"),
            "baseline_run_id": baseline.get("run_id"),
            "evidence": [],
            "reasons": ["The baseline run is still the top-ranked prompt; no promotion candidate."],
            "thresholds": PROMOTION_THRESHOLDS,
        }

    deltas = _metric_deltas(candidate, baseline)
    evidence: list[str] = []
    if _positive(deltas.get("valid_rate"), PROMOTION_THRESHOLDS["valid_rate"]):
        evidence.append(f"valid_rate improved by {deltas.get('valid_rate')}")
    if _positive(deltas.get("promising_rate"), PROMOTION_THRESHOLDS["promising_rate"]):
        evidence.append(f"promising_rate improved by {deltas.get('promising_rate')}")
    if _positive(deltas.get("avg_fitness"), PROMOTION_THRESHOLDS["avg_fitness"]):
        evidence.append(f"avg_fitness improved by {deltas.get('avg_fitness')}")
    hard_failure_delta = deltas.get("hard_failure_rate")
    if hard_failure_delta is not None and hard_failure_delta <= -PROMOTION_THRESHOLDS["hard_failure_rate"]:
        evidence.append(f"hard_failure_rate improved by {abs(hard_failure_delta)}")

    reasons: list[str] = []
    if not evidence:
        reasons.append("No promotion evidence crossed the minimum thresholds.")
    if hard_failure_delta is not None and hard_failure_delta > 0:
        reasons.append(f"hard_failure_rate regressed by {hard_failure_delta}")
    score_delta = _delta(candidate.get("score"), baseline.get("score"))
    if score_delta is not None and score_delta <= 0:
        reasons.append("Overall prompt score did not improve versus baseline.")

    return {
        "eligible": bool(evidence) and not reasons,
        "candidate_run_id": candidate.get("run_id"),
        "baseline_run_id": baseline.get("run_id"),
        "evidence": evidence,
        "reasons": reasons or ["Promotion evidence passed."],
        "deltas": deltas,
        "thresholds": PROMOTION_THRESHOLDS,
    }


def _hard_failure_count(tags: dict[str, int]) -> int:
    return sum(int(tags.get(key, 0) or 0) for key in HARD_FAILURE_TAGS)


def _delta(value: Any, baseline: Any) -> float | None:
    if value is None or baseline is None:
        return None
    try:
        return round(float(value) - float(baseline), 4)
    except (TypeError, ValueError):
        return None


def _positive(value: float | None, threshold: float) -> bool:
    return value is not None and value >= threshold
