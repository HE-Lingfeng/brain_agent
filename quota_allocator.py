from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any

from .memory import extract_field_families, extract_operators


DEFAULT_QUOTA_BUCKETS = {"exploit": 0.5, "explore": 0.3, "repair": 0.2}
HARD_FAILURE_TAGS = {"hard_error", "syntax_error", "unknown_variable", "coverage_issue"}


def allocate_simulation_quota(
    rows: list[Any],
    candidates: list[dict[str, Any]],
    *,
    limit: int | None,
) -> tuple[list[Any], dict[str, Any]]:
    """Select alpha-list rows for scarce simulation quota.

    The allocator consumes already-scored candidates. It keeps the old score as
    the primary signal, but splits quota across exploitation, exploration, and
    repairability so small runs do not collapse into near-duplicates.
    """

    if limit is None or limit <= 0 or len(rows) <= limit:
        return rows, _quota_report(rows, rows, [], int(limit or 0), "not_limited")

    candidate_index = _candidate_index(candidates)
    items = []
    for index, row in enumerate(rows):
        candidate = _candidate_for_row(row, candidate_index)
        item = _quota_item(index, row, candidate)
        items.append(item)

    bucket_limits = _bucket_limits(limit)
    selected: list[dict[str, Any]] = []
    selected_indices: set[int] = set()
    cluster_counts: Counter[str] = Counter()

    for bucket in ("exploit", "explore", "repair"):
        target = bucket_limits.get(bucket, 0)
        pool = [item for item in items if item["index"] not in selected_indices and item["bucket"] == bucket]
        for item in _rank_bucket(pool, bucket):
            if len([row for row in selected if row["bucket"] == bucket]) >= target:
                item["rejection_reason"] = f"bucket_full: {bucket} bucket exhausted (limit={target})"
                continue
            if _cluster_full(item, cluster_counts, limit):
                item["rejection_reason"] = f"duplicate_cluster: cluster {item['cluster']} at capacity"
                item["duplicate_cluster_penalty"] = 0.15
                continue
            _select_item(item, selected, selected_indices, cluster_counts)

    remaining_slots = limit - len(selected)
    if remaining_slots > 0:
        pool = [item for item in items if item["index"] not in selected_indices]
        for item in _rank_overflow(pool):
            if remaining_slots <= 0:
                item["rejection_reason"] = f"low_priority: allocator_priority={item['allocator_priority']:.4f}"
                continue
            if _cluster_full(item, cluster_counts, limit):
                item["rejection_reason"] = f"duplicate_cluster: cluster {item['cluster']} at capacity"
                item["duplicate_cluster_penalty"] = 0.15
                continue
            _select_item(item, selected, selected_indices, cluster_counts)
            remaining_slots -= 1

    remaining_slots = limit - len(selected)
    if remaining_slots > 0:
        for item in _rank_overflow([item for item in items if item["index"] not in selected_indices]):
            if remaining_slots <= 0:
                item["rejection_reason"] = f"low_priority: allocator_priority={item['allocator_priority']:.4f}"
                continue
            _select_item(item, selected, selected_indices, cluster_counts)
            remaining_slots -= 1

    selected = sorted(selected, key=lambda item: item["rank_order"])
    selected_rows = [item["row"] for item in selected[:limit]]
    rejected = [item for item in items if item["index"] not in {row["index"] for row in selected[:limit]}]
    # Fill fallback rejection reason for any remaining unannotated rejected items
    for item in rejected:
        if not item.get("rejection_reason"):
            item["rejection_reason"] = "not_selected: priority too low or all slots filled"
    return selected_rows, _quota_report(rows, selected_rows, selected[:limit], limit, "allocated", rejected)


def _candidate_index(candidates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        expression = str(candidate.get("expression") or "")
        fingerprint = str(candidate.get("fingerprint") or "")
        if expression:
            index[f"expr:{expression}"] = candidate
        if fingerprint:
            index[f"fp:{fingerprint}"] = candidate
    return index


def _candidate_for_row(row: Any, index: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    expression = _row_expression(row)
    candidate = index.get(f"expr:{expression}")
    return candidate or {}


def _quota_item(index: int, row: Any, candidate: dict[str, Any]) -> dict[str, Any]:
    expression = _row_expression(row)
    settings = _row_settings(row)
    breakdown = _json_obj(candidate.get("score_breakdown"))
    memory_score = _float(breakdown.get("memory_score"), 0.5)
    memory_risk = _float(breakdown.get("memory_risk_penalty"), 0.0)
    memory_evidence = breakdown.get("memory_evidence") if isinstance(breakdown.get("memory_evidence"), list) else []
    repairability = _float(breakdown.get("repairability_score"), 0.0)
    novelty = _float(breakdown.get("novelty_score"), 0.0)
    score = _float(candidate.get("selection_score"), _float(breakdown.get("score"), 0.0))
    failure_tags = _listish(breakdown.get("failure_tags"))
    repair_objectives = _listish(breakdown.get("repair_objectives"))
    bucket = _bucket_for_item(memory_score, memory_evidence, repairability, failure_tags, repair_objectives)
    cluster = _expression_cluster(expression, settings=settings, candidate=candidate)
    priority = score + 0.18 * (memory_score - 0.5) + 0.08 * novelty + 0.08 * repairability - memory_risk
    pattern_confidence = max((_float(e.get("confidence"), 0.0) for e in memory_evidence if isinstance(e, dict)), default=0.0)
    historical_success_boost = _float(breakdown.get("historical_success_boost"), 0.0)
    historical_failure_penalty = _float(breakdown.get("historical_failure_penalty"), 0.0)
    selection_reason = _build_selection_reason(bucket, memory_score, memory_risk, len(memory_evidence), repairability, novelty, cluster)
    return {
        "index": index,
        "rank_order": index,
        "row": row,
        "candidate_id": candidate.get("candidate_id"),
        "expression": expression,
        "bucket": bucket,
        "cluster": cluster,
        "selection_score": round(score, 4),
        "allocator_priority": round(priority, 4),
        "memory_score": round(memory_score, 4),
        "memory_risk_penalty": round(memory_risk, 4),
        "memory_evidence_count": len(memory_evidence),
        "repairability_score": round(repairability, 4),
        "novelty_score": round(novelty, 4),
        "failure_tags": failure_tags,
        "repair_objectives": repair_objectives,
        "pattern_confidence": round(pattern_confidence, 4),
        "historical_success_boost": round(historical_success_boost, 4),
        "historical_failure_penalty": round(historical_failure_penalty, 4),
        "duplicate_cluster_penalty": 0.0,
        "selection_reason": selection_reason,
        "rejection_reason": None,
    }


def _build_selection_reason(bucket: str, memory_score: float, memory_risk: float,
                            memory_evidence_count: int, repairability: float,
                            novelty: float, cluster: str) -> str:
    reasons = []
    if bucket == "exploit" and memory_evidence_count > 0 and memory_score >= 0.52:
        reasons.append(f"exploit: memory_score={memory_score:.3f} from {memory_evidence_count} evidence items")
    elif bucket == "repair" and repairability >= 0.45:
        reasons.append(f"repair: repairability={repairability:.3f}")
    else:
        reasons.append(f"explore: novel expression (novelty={novelty:.3f})")
    if memory_risk > 0.05:
        reasons.append(f"memory_risk={memory_risk:.3f}")
    return f"{', '.join(reasons)} [cluster={cluster}]"


def _bucket_for_item(
    memory_score: float,
    memory_evidence: list[Any],
    repairability: float,
    failure_tags: list[str],
    repair_objectives: list[str],
) -> str:
    hard_failure = any(tag in HARD_FAILURE_TAGS for tag in failure_tags)
    if repair_objectives and repairability >= 0.45 and not hard_failure:
        return "repair"
    if memory_evidence and memory_score >= 0.52:
        return "exploit"
    return "explore"


def _bucket_limits(limit: int) -> dict[str, int]:
    if limit <= 1:
        return {"exploit": 1, "explore": 0, "repair": 0}
    buckets = {key: int(limit * weight) for key, weight in DEFAULT_QUOTA_BUCKETS.items()}
    for key in ("exploit", "explore"):
        if buckets[key] == 0:
            buckets[key] = 1
    while sum(buckets.values()) < limit:
        for key in ("exploit", "explore", "repair"):
            buckets[key] += 1
            if sum(buckets.values()) >= limit:
                break
    while sum(buckets.values()) > limit:
        for key in ("repair", "explore", "exploit"):
            if buckets[key] > 0:
                buckets[key] -= 1
                break
    return buckets


def _rank_bucket(items: list[dict[str, Any]], bucket: str) -> list[dict[str, Any]]:
    if bucket == "explore":
        return sorted(
            items,
            key=lambda item: (
                item["memory_evidence_count"],
                -item["novelty_score"],
                -item["selection_score"],
                item["index"],
            ),
        )
    if bucket == "repair":
        return sorted(
            items,
            key=lambda item: (-item["repairability_score"], -item["selection_score"], item["index"]),
        )
    return sorted(
        items,
        key=lambda item: (-item["memory_score"], -item["selection_score"], item["index"]),
    )


def _rank_overflow(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        items,
        key=lambda item: (-item["allocator_priority"], -item["selection_score"], item["index"]),
    )


def _cluster_full(item: dict[str, Any], cluster_counts: Counter[str], limit: int) -> bool:
    max_per_cluster = 1 if limit <= 5 else max(2, limit // 4)
    return cluster_counts[item["cluster"]] >= max_per_cluster


def _select_item(
    item: dict[str, Any],
    selected: list[dict[str, Any]],
    selected_indices: set[int],
    cluster_counts: Counter[str],
) -> None:
    selected.append(item)
    selected_indices.add(int(item["index"]))
    cluster_counts[str(item["cluster"])] += 1


def _quota_report(
    input_rows: list[Any],
    selected_rows: list[Any],
    selected_items: list[dict[str, Any]],
    limit: int,
    mode: str,
    rejected_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    bucket_counts = Counter(str(item.get("bucket") or "") for item in selected_items)
    cluster_counts = Counter(str(item.get("cluster") or "") for item in selected_items)
    return {
        "mode": mode,
        "input_count": len(input_rows),
        "selected_count": len(selected_rows),
        "limit": limit,
        "bucket_policy": DEFAULT_QUOTA_BUCKETS,
        "selected_bucket_counts": dict(sorted(bucket_counts.items())),
        "selected_cluster_counts": dict(sorted(cluster_counts.items())),
        "selected": [_public_item(item) for item in selected_items],
        "rejected_count": len(rejected_items or []),
        "rejected_sample": [_public_item(item) for item in (rejected_items or [])[:20]],
    }


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "index": item.get("index"),
        "candidate_id": item.get("candidate_id"),
        "bucket": item.get("bucket"),
        "cluster": item.get("cluster"),
        "selection_score": item.get("selection_score"),
        "allocator_priority": item.get("allocator_priority"),
        "memory_score": item.get("memory_score"),
        "memory_evidence_count": item.get("memory_evidence_count"),
        "repairability_score": item.get("repairability_score"),
        "novelty_score": item.get("novelty_score"),
        "failure_tags": item.get("failure_tags"),
        "repair_objectives": item.get("repair_objectives"),
        "expression": _shorten(str(item.get("expression") or "")),
        "pattern_confidence": item.get("pattern_confidence"),
        "historical_success_boost": item.get("historical_success_boost"),
        "historical_failure_penalty": item.get("historical_failure_penalty"),
        "duplicate_cluster_penalty": item.get("duplicate_cluster_penalty"),
        "selection_reason": item.get("selection_reason"),
        "rejection_reason": item.get("rejection_reason"),
    }


def _expression_cluster(
    expression: str,
    *,
    settings: dict[str, Any] | None = None,
    candidate: dict[str, Any] | None = None,
) -> str:
    operators = extract_operators(expression)
    families = extract_field_families(expression)
    themes = []
    for prefix in ("ts_", "group_", "regression_"):
        if any(op.startswith(prefix) for op in operators):
            themes.append(prefix.rstrip("_"))
    if any(op in {"trade_when", "hump", "hump_decay"} for op in operators):
        themes.append("turnover_control")
    if any(op in {"group_rank", "group_neutralize", "group_zscore", "group_vector_neut"} for op in operators):
        themes.append("peer_relative")
    op_key = "+".join(sorted(set(themes or operators[:3] or ["plain"])))
    family_key = "+".join(families[:2]) if families else "nofamily"
    settings = settings or {}
    neutralization = str(settings.get("neutralization") or settings.get("neutralization".upper()) or "").upper() or "NEUTRAL_UNKNOWN"
    thesis = _json_obj((candidate or {}).get("thesis_json") or (candidate or {}).get("factor_thesis"))
    thesis_type = str(thesis.get("thesis_type") or thesis.get("type") or "no_thesis").lower()
    window_key = "+".join(_window_buckets(expression)[:3]) or "no_window"
    structure_key = "+".join(_structure_flags(expression, operators)) or "plain_structure"
    return f"{family_key}|{op_key}|{window_key}|{neutralization}|{structure_key}|{thesis_type}"


def _row_expression(row: Any) -> str:
    if isinstance(row, str):
        return row
    if isinstance(row, dict):
        return str(row.get("regular") or row.get("regular_expression") or row.get("expression") or "")
    return ""


def _row_settings(row: Any) -> dict[str, Any]:
    if not isinstance(row, dict):
        return {}
    settings = row.get("settings")
    return settings if isinstance(settings, dict) else {}


def _window_buckets(expression: str) -> list[str]:
    buckets: set[str] = set()
    for value in re.findall(r"\bts_[A-Za-z0-9_]+\([^,]+,\s*(\d+)", expression or ""):
        window = int(value)
        if window <= 10:
            bucket = "w_short"
        elif window <= 30:
            bucket = "w_medium"
        elif window <= 90:
            bucket = "w_long"
        else:
            bucket = "w_very_long"
        buckets.add(bucket)
    return sorted(buckets)


def _structure_flags(expression: str, operators: list[str]) -> list[str]:
    flags: list[str] = []
    expr = expression or ""
    if "trade_when" in operators:
        flags.append("conditional")
    if any(op.startswith("group_") for op in operators) or re.search(r"\b(industry|sector|subindustry|market)\b", expr):
        flags.append("grouped")
    if any(op in {"rank", "zscore", "winsorize", "normalize", "scale"} for op in operators):
        flags.append("normalized")
    if any(op in {"hump", "hump_decay", "ts_decay_linear"} for op in operators):
        flags.append("smoothed")
    return sorted(set(flags))


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        data = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def _listish(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,|]", value) if item.strip()]
    return []


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _shorten(value: str, limit: int = 240) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."
