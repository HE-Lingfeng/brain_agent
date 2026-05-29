from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any

from .diagnostics import diagnose_gate_check
from .models import CandidateStatus


@dataclass(frozen=True)
class CandidateScore:
    score: float
    breakdown: dict[str, Any]


def score_candidate(
    candidate: dict[str, Any],
    latest_sim: dict[str, Any] | None = None,
    memory_context: dict[str, Any] | None = None,
    latest_gate: dict[str, Any] | None = None,
) -> CandidateScore:
    """Score candidates for scarce simulation/enhancement quota.

    The score is deliberately heuristic and transparent. Before simulation it
    mostly rewards valid-looking, moderately rich, non-duplicate expressions.
    After simulation it shifts weight toward metrics plus repairability.
    """

    latest = latest_sim or _latest_sim_from_candidate(candidate)
    expression = str(candidate.get("expression") or "")
    status = str(candidate.get("status") or "")
    gate = latest_gate or _latest_gate_from_candidate(candidate)
    gate_diagnosis = diagnose_gate_check(gate) if gate else {}
    tags = _merge_unique(
        _csv_or_list(candidate.get("failure_tags") or latest.get("failure_tags")),
        _csv_or_list((gate_diagnosis or {}).get("failure_tags")),
    )
    objectives = _merge_unique(
        _csv_or_list(candidate.get("repair_objectives") or latest.get("repair_objectives")),
        _csv_or_list((gate_diagnosis or {}).get("repair_objectives")),
    )

    has_metrics = any(latest.get(key) is not None for key in ("sharpe", "fitness", "turnover", "drawdown"))
    quality = _quality_score(latest) if has_metrics else _pre_sim_quality(expression, status)
    repairability = _repairability_score(status, tags, objectives, latest)
    novelty = _novelty_score(expression, str(candidate.get("source") or ""))
    coverage = _coverage_score(tags, objectives, latest)
    risk_penalty = _risk_penalty(status, tags, latest, expression)
    memory = _memory_adjustment(expression, memory_context, candidate)

    if has_metrics:
        score = (
            0.44 * quality
            + 0.22 * repairability
            + 0.14 * novelty
            + 0.10 * coverage
            + 0.10 * memory["memory_score"]
            - risk_penalty
            - memory["memory_risk_penalty"]
        )
    else:
        score = (
            0.38 * quality
            + 0.30 * novelty
            + 0.14 * coverage
            + 0.08 * repairability
            + 0.10 * memory["memory_score"]
            - risk_penalty
            - memory["memory_risk_penalty"]
        )
    score = _clamp(score, 0.0, 1.0)
    breakdown = {
        "score": round(score, 4),
        "quality_score": round(quality, 4),
        "repairability_score": round(repairability, 4),
        "novelty_score": round(novelty, 4),
        "coverage_score": round(coverage, 4),
        "risk_penalty": round(risk_penalty, 4),
        "memory_score": round(memory["memory_score"], 4),
        "memory_risk_penalty": round(memory["memory_risk_penalty"], 4),
        "memory_evidence": memory["memory_evidence"],
        "historical_success_boost": round(_float(memory.get("historical_success_boost"), 0.0), 4),
        "historical_failure_penalty": round(_float(memory.get("historical_failure_penalty"), 0.0), 4),
        "pattern_confidence": max((e.get("confidence", 0) for e in memory.get("memory_evidence", [])), default=0.0),
        "gate_status": gate.get("gate_status") if gate else "",
        "gate_passed": _boolish(gate.get("passed")) if gate else False,
        "has_metrics": has_metrics,
        "failure_tags": tags,
        "repair_objectives": objectives,
        "rationale": _rationale(status, tags, objectives, latest, expression, has_metrics, gate),
    }
    return CandidateScore(score=score, breakdown=breakdown)


def score_candidates(
    candidates: list[dict[str, Any]],
    latest_by_candidate: dict[int, dict[str, Any]] | None = None,
    memory_context: dict[str, Any] | None = None,
    latest_gate_by_candidate: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    latest_by_candidate = latest_by_candidate or {}
    latest_gate_by_candidate = latest_gate_by_candidate or {}
    scored: list[dict[str, Any]] = []
    for candidate in candidates:
        cid = _int_or_none(candidate.get("candidate_id"))
        latest = latest_by_candidate.get(cid or -1)
        latest_gate = latest_gate_by_candidate.get(cid or -1)
        score = score_candidate(candidate, latest, memory_context, latest_gate)
        item = dict(candidate)
        item["selection_score"] = score.score
        item["score_breakdown"] = score.breakdown
        scored.append(item)
    return sorted(
        scored,
        key=lambda c: (
            float(c.get("selection_score") or 0),
            _status_priority(str(c.get("status") or "")),
            int(c.get("candidate_id") or 0),
        ),
        reverse=True,
    )


def parse_score_breakdown(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _quality_score(latest: dict[str, Any]) -> float:
    sharpe = _float(latest.get("sharpe"), 0.0)
    fitness = _float(latest.get("fitness"), 0.0)
    turnover = latest.get("turnover")
    drawdown = latest.get("drawdown")
    sharpe_score = _clamp(sharpe / 1.6, 0.0, 1.0)
    fitness_score = _clamp(fitness / 1.2, 0.0, 1.0)
    turnover_score = _turnover_score(turnover)
    drawdown_score = _drawdown_score(drawdown)
    return 0.42 * sharpe_score + 0.38 * fitness_score + 0.14 * turnover_score + 0.06 * drawdown_score


def _pre_sim_quality(expression: str, status: str) -> float:
    operators = _operators(expression)
    depth = expression.count("(")
    has_timeseries = any(op.startswith("ts_") for op in operators)
    has_group = any(op.startswith("group_") for op in operators)
    has_cleaning = any(op in {"winsorize", "ts_backfill", "densify", "kth_element"} for op in operators)
    base = 0.35
    base += min(len(set(operators)) * 0.045, 0.22)
    base += 0.08 if has_timeseries else 0.0
    base += 0.06 if has_group else 0.0
    base += 0.05 if has_cleaning else 0.0
    if 2 <= depth <= 8:
        base += 0.12
    elif depth > 14:
        base -= 0.12
    if status == CandidateStatus.SIM_PENDING.value:
        base += 0.05
    return _clamp(base, 0.0, 1.0)


def _repairability_score(
    status: str,
    tags: list[str],
    objectives: list[str],
    latest: dict[str, Any],
) -> float:
    if status == CandidateStatus.PROMISING.value:
        base = 0.72
    elif status == CandidateStatus.NEEDS_ENHANCE.value:
        base = 0.62
    elif status == CandidateStatus.SIM_PENDING.value:
        base = 0.45
    elif status == CandidateStatus.SIM_RETRYABLE.value:
        base = 0.40
    else:
        base = 0.25
    if "reduce_turnover" in objectives or "high_turnover" in tags or "elevated_turnover" in tags:
        sharpe = _float(latest.get("sharpe"), 0.0)
        fitness = _float(latest.get("fitness"), 0.0)
        base += 0.16 if sharpe >= 0.8 or fitness >= 0.5 else 0.05
    if "improve_sharpe" in objectives or "improve_fitness" in objectives:
        base += 0.08
    if "test_short_flip" in objectives or "cand_neg" in tags:
        base += 0.18
    if "fix_syntax" in objectives or "unknown_variable" in tags or "syntax_error" in tags:
        base -= 0.22
    if "platform_or_rate_limit" in tags:
        base += 0.05
    if "sim_retryable" in tags or "retry_simulation" in objectives:
        base += 0.08
    if "hard_error" in tags:
        base -= 0.28
    return _clamp(base, 0.0, 1.0)


def _novelty_score(expression: str, source: str) -> float:
    ops = set(_operators(expression))
    placeholders = set(re.findall(r"\{[A-Za-z0-9_]+\}", expression))
    chars = len(expression)
    score = 0.28 + min(len(ops) * 0.045, 0.28) + min(len(placeholders) * 0.035, 0.14)
    if 45 <= chars <= 260:
        score += 0.18
    elif chars > 420:
        score -= 0.16
    if "enhance" in source.lower():
        score += 0.04
    if "variant" in source.lower():
        score += 0.03
    return _clamp(score, 0.0, 1.0)


def _coverage_score(tags: list[str], objectives: list[str], latest: dict[str, Any]) -> float:
    score = 0.72
    if "coverage_issue" in tags or "improve_coverage" in objectives:
        score -= 0.32
    if latest.get("status") and str(latest.get("status")).upper() not in {"COMPLETE", "COMPLETED", "SUCCESS"}:
        score -= 0.18
    if "no_obvious_failure" in tags:
        score += 0.08
    return _clamp(score, 0.0, 1.0)


def _risk_penalty(status: str, tags: list[str], latest: dict[str, Any], expression: str) -> float:
    penalty = 0.0
    turnover = latest.get("turnover")
    if turnover is not None:
        t = _float(turnover, 0.0)
        if t > 0.7:
            penalty += 0.22
        elif t > 0.5:
            penalty += 0.1
        elif 0 < t < 0.01:
            penalty += 0.18
    if status in {CandidateStatus.REJECTED.value, CandidateStatus.SIM_FAILED.value}:
        penalty += 0.22
    if "hard_error" in tags:
        penalty += 0.25
    if "syntax_error" in tags or "unknown_variable" in tags:
        penalty += 0.18
    if "self_corr_high" in tags:
        penalty += 0.2
    if "prod_corr_high" in tags:
        penalty += 0.16
    if "correlation_high" in tags or "corr_high" in tags:
        penalty += 0.16
    if expression.count("(") > 16:
        penalty += 0.1
    return _clamp(penalty, 0.0, 0.7)


def _memory_adjustment(expression: str, memory_context: dict[str, Any] | None, candidate: dict[str, Any] | None = None) -> dict[str, Any]:
    if not memory_context:
        return {"memory_score": 0.5, "memory_risk_penalty": 0.0, "memory_evidence": [], "historical_success_boost": 0.0, "historical_failure_penalty": 0.0}
    operator_stats = memory_context.get("operator_stats") if isinstance(memory_context.get("operator_stats"), dict) else {}
    family_stats = (
        memory_context.get("field_family_stats") if isinstance(memory_context.get("field_family_stats"), dict) else {}
    )
    thesis_stats = memory_context.get("thesis_type_stats") if isinstance(memory_context.get("thesis_type_stats"), dict) else {}
    repair_stats = memory_context.get("repair_method_stats") if isinstance(memory_context.get("repair_method_stats"), dict) else {}
    variant_strategy_stats = memory_context.get("variant_strategy_stats") if isinstance(memory_context.get("variant_strategy_stats"), dict) else {}
    thesis = _json_obj((candidate or {}).get("thesis_json") or (candidate or {}).get("factor_thesis"))
    evidence: list[dict[str, Any]] = []
    weighted_success = 0.0
    weight_sum = 0.0
    risk_penalty = 0.0
    historical_success_boost = 0.0
    historical_failure_penalty = 0.0
    evidence_inputs = [
        *[("operator", op, 1.0, operator_stats.get(op)) for op in _operators(expression)],
        *[("field_family", family, 1.35, family_stats.get(family)) for family in _field_families(expression)],
        ("thesis_type", str(thesis.get("thesis_type") or ""), 1.5, thesis_stats.get(str(thesis.get("thesis_type") or ""))),
        *[
            ("repair_method", str(method), 0.75, repair_stats.get(str(method)))
            for method in _json_list(thesis.get("intended_repair_methods") or [])
        ],
    ]
    if candidate and candidate.get("variant_strategy"):
        evidence_inputs.append(
            ("variant_strategy", str(candidate.get("variant_strategy") or ""), 1.2,
             variant_strategy_stats.get(str(candidate.get("variant_strategy") or "")))
        )
    for kind, key, base_weight, stats in evidence_inputs:
        if not isinstance(stats, dict):
            continue
        observations = int(stats.get("observations") or 0)
        if observations < 2:
            continue
        effective_observations = _float(stats.get("effective_observations"), float(observations))
        success_rate = _clamp(float(stats.get("success_rate") or 0), 0.0, 1.0)
        confidence = _clamp(_float(stats.get("confidence"), effective_observations / 20), 0.0, 1.0)
        weight = base_weight * confidence
        failure_rate = _clamp(_float(stats.get("hard_failure_rate"), _float(stats.get("failure_rate"), 0.0)), 0.0, 1.0)
        # Small boost for high-success patterns with sufficient samples
        success_boost = 1.0
        if success_rate > 0.7 and observations >= 5:
            success_boost = 1.0 + 0.1 * confidence
            historical_success_boost += (success_boost - 1.0) * success_rate * weight
        weighted_success += success_rate * weight * success_boost
        weight_sum += weight
        failure_tags = stats.get("failure_tags") if isinstance(stats.get("failure_tags"), dict) else {}
        hard_failures = 0
        for tag in (
            "syntax_error",
            "unknown_variable",
            "coverage_issue",
            "hard_error",
            "high_turnover",
            "self_corr_high",
            "prod_corr_high",
        ):
            hard_failures += int(failure_tags.get(tag, 0) or 0)
        hard_failure_rate = _clamp(_float(stats.get("hard_failure_rate"), hard_failures / observations), 0.0, 1.0)
        # Amplify risk penalty for high-failure patterns
        failure_penalty_mult = 1.0
        if failure_rate > 0.5 and confidence > 0.3:
            failure_penalty_mult = 1.0 + failure_rate
            historical_failure_penalty += (failure_penalty_mult - 1.0) * hard_failure_rate * confidence * base_weight * 0.08
        risk_penalty += hard_failure_rate * confidence * base_weight * 0.08 * failure_penalty_mult
        evidence.append(
            {
                "kind": kind,
                "key": key,
                "observations": observations,
                "effective_observations": round(effective_observations, 4),
                "confidence": round(confidence, 4),
                "success_rate": round(success_rate, 4),
                "hard_failure_rate": round(hard_failure_rate, 4),
                "failure_rate": round(failure_rate, 4),
                "avg_sharpe": stats.get("avg_sharpe"),
                "avg_fitness": stats.get("avg_fitness"),
                "avg_turnover": stats.get("avg_turnover"),
            }
        )
    if not weight_sum:
        return {"memory_score": 0.5, "memory_risk_penalty": 0.0, "memory_evidence": [], "historical_success_boost": 0.0, "historical_failure_penalty": 0.0}
    return {
        "memory_score": _clamp(weighted_success / weight_sum, 0.0, 1.0),
        "memory_risk_penalty": _clamp(risk_penalty, 0.0, 0.18),
        "memory_evidence": sorted(evidence, key=lambda row: int(row.get("observations") or 0), reverse=True)[:8],
        "historical_success_boost": round(historical_success_boost, 4),
        "historical_failure_penalty": round(historical_failure_penalty, 4),
    }


def _turnover_score(value: Any) -> float:
    if value is None:
        return 0.55
    t = _float(value, 0.0)
    if t <= 0:
        return 0.2
    # Prefer the usual BRAIN-friendly middle band, while not killing low-turnover ideas.
    distance = abs(math.log10(max(t, 0.001)) - math.log10(0.18))
    return _clamp(1.0 - distance / 1.6, 0.0, 1.0)


def _drawdown_score(value: Any) -> float:
    if value is None:
        return 0.55
    d = abs(_float(value, 0.0))
    return _clamp(1.0 - d / 0.35, 0.0, 1.0)


def _rationale(
    status: str,
    tags: list[str],
    objectives: list[str],
    latest: dict[str, Any],
    expression: str,
    has_metrics: bool,
    gate: dict[str, Any] | None = None,
) -> list[str]:
    notes: list[str] = []
    if has_metrics:
        notes.append(
            "metrics considered: "
            f"sharpe={latest.get('sharpe')}, fitness={latest.get('fitness')}, turnover={latest.get('turnover')}"
        )
    else:
        notes.append("pre-simulation score uses expression structure and source quality")
    if status:
        notes.append(f"status={status}")
    if objectives:
        notes.append("repair objectives: " + ", ".join(objectives))
    if tags:
        notes.append("failure tags: " + ", ".join(tags))
    if gate:
        notes.append(
            "gate considered: "
            f"passed={_boolish(gate.get('passed'))}, "
            f"self_corr={gate.get('self_corr_check')}, prod_corr={gate.get('prod_corr_check')}"
        )
    ops = _operators(expression)
    if ops:
        notes.append("operators: " + ", ".join(sorted(set(ops))[:8]))
    return notes


def _latest_sim_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    latest = candidate.get("latest_sim_result")
    return latest if isinstance(latest, dict) else {}


def _latest_gate_from_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    latest = candidate.get("latest_gate_check")
    return latest if isinstance(latest, dict) else {}


def _operators(expression: str) -> list[str]:
    return re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", expression)


def _field_families(expression: str) -> list[str]:
    tokens = set(re.findall(r"\b([a-z][a-z0-9_]*\d[a-z0-9_]*|[a-z]+\d+_[a-z0-9_]+)\b", (expression or "").lower()))
    families: set[str] = set()
    for token in tokens:
        match = re.match(r"([a-z]+\d+)", token)
        if match:
            families.add(match.group(1))
    return sorted(families)


def _csv_or_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _merge_unique(*values: list[str]) -> list[str]:
    merged: list[str] = []
    for items in values:
        for item in items:
            if item and item not in merged:
                merged.append(item)
    return merged


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _float(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "pass", "passed"}


def _status_priority(status: str) -> int:
    order = {
        CandidateStatus.PROMISING.value: 4,
        CandidateStatus.NEEDS_ENHANCE.value: 3,
        CandidateStatus.SIM_PENDING.value: 2,
        CandidateStatus.MANUAL_REVIEW.value: 2,
        CandidateStatus.SIM_RETRYABLE.value: 2,
        CandidateStatus.SIM_FAILED.value: 1,
        CandidateStatus.REJECTED.value: 0,
    }
    return order.get(status, 0)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))
