from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from typing import Any

from ..diagnostics import diagnose_gate_check, diagnose_sim_result
from ..optimizers import SecondOrderOptimizer, build_optimizer_variants


DEFAULT_SIGNAL_SCORE = 0.42
HARD_FAILURE_TAGS = {"hard_error", "syntax_error", "unknown_variable"}
NEUTRALIZATION_SWEEP = ("INDUSTRY", "SUBINDUSTRY", "SECTOR", "MARKET", "SLOW_AND_FAST", "FAST", "SLOW", "NONE")
CROSS_SWEEP_NEUTRALIZATION = ["INDUSTRY", "SUBINDUSTRY", "SECTOR", "MARKET", "SLOW_AND_FAST", "FAST", "SLOW", "NONE"]
DECAY_SWEEP = (4, 8, 12, 20)
WINDOW_SWEEP = (5, 10, 20, 60, 120)
GROUPS = ("industry", "subindustry", "sector", "market")


def build_variant_search(
    alpha_rows: list[Any],
    candidates: list[dict[str, Any]],
    latest_sim_by_candidate: dict[int, dict[str, Any]],
    *,
    max_variant_alphas: int,
    max_variants_per_alpha: int,
    latest_gate_by_candidate: dict[int, dict[str, Any]] | None = None,
    pnl_series_by_alpha_id: dict[str, list[float]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Generate a small local-search alpha list from weak positive evidence.

    This is deliberately bounded and deterministic: it does not call an LLM,
    and it keeps each variant close enough to its parent that metric deltas are
    meaningful after simulation.
    """

    if max_variant_alphas <= 0 or max_variants_per_alpha <= 0:
        return [], _report([], [], "disabled", max_variant_alphas, max_variants_per_alpha)

    row_by_expression = _row_index(alpha_rows)
    latest_gate_by_candidate = latest_gate_by_candidate or {}

    pruned_count = 0
    if pnl_series_by_alpha_id:
        before = len(candidates)
        candidates = _pnl_prune(candidates, pnl_series_by_alpha_id, latest_sim_by_candidate=latest_sim_by_candidate)
        pruned_count = before - len(candidates)

    selected_parents = _signal_candidates(candidates, latest_sim_by_candidate, latest_gate_by_candidate, row_by_expression)
    variants: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    existing_variant_expressions = {
        str(candidate.get("expression") or "")
        for candidate in candidates
        if candidate.get("parent_candidate_id") or str(candidate.get("source") or "") == "variant_search"
    }

    for parent in selected_parents:
        parent_id = int(parent["candidate_id"])
        row = row_by_expression.get(str(parent.get("expression") or "")) or {}
        settings = copy.deepcopy(row.get("settings")) if isinstance(row.get("settings"), dict) else {}
        parent_variants = _variants_for_parent(
            parent,
            latest_sim_by_candidate.get(parent_id) or {},
            latest_gate_by_candidate.get(parent_id) or {},
            settings,
        )
        added_for_parent = 0
        for variant in parent_variants:
            if added_for_parent >= max_variants_per_alpha or len(variants) >= max_variant_alphas:
                break
            expression = str(variant.get("regular") or variant.get("expression") or "")
            variant_settings = variant.get("settings") if isinstance(variant.get("settings"), dict) else settings
            dedupe_key = (expression, _settings_key(variant_settings))
            if not expression or dedupe_key in seen or expression in existing_variant_expressions:
                continue
            if expression == str(parent.get("expression") or "") and _settings_key(variant_settings) == _settings_key(settings):
                continue
            seen.add(dedupe_key)
            variant["parent_candidate_id"] = parent_id
            variant["parent_expression"] = str(parent.get("expression") or "")
            variant["parent_status"] = str(parent.get("status") or "")
            variant["parent_selection_score"] = _float(parent.get("selection_score"), 0.0)
            variant["parent_metrics"] = _metric_summary(latest_sim_by_candidate.get(parent_id) or {})
            variants.append(variant)
            added_for_parent += 1
        if len(variants) >= max_variant_alphas:
            break

    report = _report(variants, selected_parents, "generated", max_variant_alphas, max_variants_per_alpha)
    if pruned_count:
        report["pnl_pruned"] = pruned_count
    return _alpha_rows_from_variants(variants), report


def variant_lineage_from_row(row: dict[str, Any]) -> dict[str, Any]:
    lineage = row.get("lineage") if isinstance(row.get("lineage"), dict) else {}
    if lineage:
        return lineage
    if not row.get("parent_candidate_id") and not row.get("variant_strategy"):
        return {}
    return {
        "parent_candidate_id": row.get("parent_candidate_id"),
        "variant_strategy": row.get("variant_strategy"),
        "variant_params": row.get("variant_params") if isinstance(row.get("variant_params"), dict) else {},
        "parent_expression": row.get("parent_expression") or "",
    }


def _signal_candidates(
    candidates: list[dict[str, Any]],
    latest_sim_by_candidate: dict[int, dict[str, Any]],
    latest_gate_by_candidate: dict[int, dict[str, Any]],
    row_by_expression: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    pool = []
    for candidate in candidates:
        candidate_id = int(candidate.get("candidate_id") or 0)
        expression = str(candidate.get("expression") or "")
        if not candidate_id or not expression or expression not in row_by_expression:
            continue
        if candidate.get("parent_candidate_id"):
            continue
        sim = latest_sim_by_candidate.get(candidate_id) or {}
        tags = _failure_tags(candidate, sim, latest_gate_by_candidate.get(candidate_id) or {})
        if HARD_FAILURE_TAGS.intersection(tags):
            continue
        signal = _signal_strength(candidate, sim, tags)
        if signal < DEFAULT_SIGNAL_SCORE:
            continue
        enriched = dict(candidate)
        enriched["_variant_signal_strength"] = signal
        enriched["_variant_failure_tags"] = sorted(tags)
        pool.append(enriched)
    return sorted(
        pool,
        key=lambda item: (
            -_float(item.get("_variant_signal_strength"), 0.0),
            -_float(item.get("selection_score"), 0.0),
            int(item.get("candidate_id") or 0),
        ),
    )


def _signal_strength(candidate: dict[str, Any], sim: dict[str, Any], tags: set[str]) -> float:
    sharpe = _float(sim.get("sharpe"), 0.0)
    fitness = _float(sim.get("fitness"), 0.0)
    turnover = _float(sim.get("turnover"), 0.0)
    score = _float(candidate.get("selection_score"), 0.0)
    strength = min(abs(sharpe) / 1.5, 0.34)
    strength += min(max(fitness, 0.0) / 1.0, 0.22)
    strength += min(score, 1.0) * 0.28
    if 0.01 <= turnover <= 0.70:
        strength += 0.08
    if tags.intersection(
        {
            "cand_neg",
            "low_sharpe",
            "low_fitness",
            "high_turnover",
            "elevated_turnover",
            "low_turnover",
            "coverage_issue",
            "subuniverse_issue",
            "datafield_unavailable",
            "self_corr_high",
            "prod_corr_high",
            "correlation_high",
            "corr_high",
        }
    ):
        strength += 0.12
    if tags.intersection({"coverage_issue", "subuniverse_issue", "datafield_unavailable"}):
        strength += 0.04
    if tags.intersection({"self_corr_high", "prod_corr_high", "correlation_high", "corr_high"}):
        strength += 0.10
    return round(strength, 4)


def _variants_for_parent(parent: dict[str, Any], sim: dict[str, Any], gate: dict[str, Any], settings: dict[str, Any]) -> list[dict[str, Any]]:
    expression = str(parent.get("expression") or "")
    tags = _failure_tags(parent, sim, gate)
    fingerprint = str(parent.get("fingerprint") or "")
    turnover = _float(sim.get("turnover"), None)

    all_variants: list[dict[str, Any]] = []

    # Bucket 1: repair optimizers (up to 2)
    repair = build_optimizer_variants(expression, settings, sim, tags)
    all_variants.extend(repair[:2])

    if not any(item.get("variant_strategy") == "ShortFlipOptimizer" for item in repair) and (
        "cand_neg" in tags or _float(sim.get("sharpe"), 0.0) < -0.2
    ):
        all_variants.append(_variant("sign_flip", f"multiply(-1, ({expression}))", settings, {"reason": "negative_or_inverse_signal"}))

    # Bucket 2: second-order operator wrapping (up to 2, only eligible candidates)
    if _eligible_for_second_order(parent, sim):
        so_optimizer = SecondOrderOptimizer()
        so_variants = so_optimizer.optimize(expression, settings, sim_result=sim, candidate=parent)
        all_variants.extend(so_variants[:2])

    # Bucket 3: settings cross sweep (up to 4)
    cross = _neutralization_decay_cross_sweep(
        expression, settings, parent_fingerprint=fingerprint, turnover=turnover, max_variants=8,
    )
    all_variants.extend(cross[:4])

    # Bucket 4: window / rank-zscore swap (up to 2)
    all_variants.extend(_window_sweep(expression, settings)[:1])
    all_variants.extend(_rank_zscore_swap(expression, settings)[:1])

    # Bucket 5: existing conditional strategies (up to 2)
    if tags.intersection({"high_turnover", "elevated_turnover"}) or (turnover is not None and turnover > 0.35):
        all_variants.extend(_turnover_control(expression, settings)[:1])
    elif not any(item.get("variant_strategy") == "LowTurnoverOptimizer" for item in repair):
        all_variants.extend(_decay_sweep(expression, settings)[:1])

    all_variants.extend(_group_neutralization_sweep(expression, settings)[:1])

    if not any(item.get("variant_strategy") == "CoverageRepairer" for item in repair) and (
        tags.intersection({"coverage_issue", "subuniverse_issue", "datafield_unavailable", "low_turnover"})
        or (turnover is not None and turnover < 0.03)
    ):
        all_variants.extend(_coverage_repair(expression, settings)[:1])

    parent_thesis = _json_obj(parent.get("thesis_json"))
    variants = _dedupe_variants(all_variants)
    for variant in variants:
        if parent_thesis:
            variant["factor_thesis"] = _variant_thesis(parent_thesis, variant)
    return variants


def _eligible_for_second_order(parent: dict[str, Any], sim: dict[str, Any]) -> bool:
    status = str(parent.get("status") or "")
    score = _float(parent.get("selection_score"), 0.0)
    if status in ("rejected", "sim_failed", "sim_retryable"):
        return False
    if status not in SecondOrderOptimizer.ELIGIBLE_STATUSES and score < SecondOrderOptimizer.ELIGIBLE_SCORE_THRESHOLD:
        return False
    if SecondOrderOptimizer._has_hard_failure(sim):
        return False
    return True


def _window_sweep(expression: str, settings: dict[str, Any]) -> list[dict[str, Any]]:
    variants = []
    matches = list(re.finditer(r"\b(ts_[A-Za-z0-9_]+)\(([^()]*(?:\([^()]*\)[^()]*)*?),\s*(\d+)(\s*[,)]?)", expression))
    for match in matches[:2]:
        current = int(match.group(3))
        for window in _neighbor_windows(current)[:2]:
            if window == current:
                continue
            replacement = f"{match.group(1)}({match.group(2)}, {window}{match.group(4)}"
            changed = expression[: match.start()] + replacement + expression[match.end() :]
            variants.append(_variant("window_sweep", changed, settings, {"from": current, "to": window, "operator": match.group(1)}))
    return variants


def _decay_sweep(expression: str, settings: dict[str, Any]) -> list[dict[str, Any]]:
    current = int(_float(settings.get("decay"), 0) or 0)
    variants = []
    for decay in _neighbor_decays(current)[:2]:
        if decay == current:
            continue
        variant_settings = copy.deepcopy(settings)
        variant_settings["decay"] = decay
        variants.append(_variant("decay_sweep", expression, variant_settings, {"from": current, "to": decay}))
    return variants


def _rank_zscore_swap(expression: str, settings: dict[str, Any]) -> list[dict[str, Any]]:
    variants = []
    if re.search(r"\brank\s*\(", expression):
        variants.append(_variant("rank_zscore_swap", re.sub(r"\brank\s*\(", "zscore(", expression, count=1), settings, {"from": "rank", "to": "zscore"}))
    if re.search(r"\bzscore\s*\(", expression):
        variants.append(_variant("rank_zscore_swap", re.sub(r"\bzscore\s*\(", "rank(", expression, count=1), settings, {"from": "zscore", "to": "rank"}))
    return variants


def _group_neutralization_sweep(expression: str, settings: dict[str, Any]) -> list[dict[str, Any]]:
    variants = []
    current_setting = str(settings.get("neutralization") or "").upper()
    for neutralization in _neutralization_sweep_for_settings(settings):
        if neutralization == current_setting:
            continue
        variant_settings = copy.deepcopy(settings)
        variant_settings["neutralization"] = neutralization
        variants.append(_variant("neutralization_sweep", expression, variant_settings, {"from": current_setting, "to": neutralization}))
        if len(variants) >= 2:
            break
    group_match = re.search(r"\bgroup_neutralize\s*\((.*),\s*(industry|subindustry|sector|market)\s*\)\s*$", expression)
    if group_match:
        current_group = group_match.group(2)
        for group in GROUPS:
            if group == current_group:
                continue
            changed = expression[: group_match.start(2)] + group + expression[group_match.end(2) :]
            variants.append(_variant("group_neutralization_sweep", changed, settings, {"from": current_group, "to": group}))
            if len(variants) >= 4:
                break
    return variants


def _deterministic_hash(seed: str) -> int:
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest(), 16)


def _neutralization_decay_cross_sweep(
    expression: str,
    settings: dict[str, Any],
    *,
    parent_fingerprint: str = "",
    turnover: float | None = None,
    max_variants: int = 16,
) -> list[dict[str, Any]]:
    """Cross-product sweep: neutralization × decay settings variants.

    Deterministic selection via parent fingerprint hash — same fingerprint
    produces the same sweep every time. Only varies settings, never the expression.
    """
    if not parent_fingerprint:
        parent_fingerprint = expression
    neut_list = sorted(
        _neutralization_sweep_for_settings(settings),
        key=lambda n: _deterministic_hash(parent_fingerprint + "|neut|" + n),
    )
    current_neut = str(settings.get("neutralization") or "").upper()
    max_neutralizations = max(1, min(len(neut_list), max_variants // 2 if max_variants >= 2 else 1))
    neuts = [n for n in neut_list if n != current_neut][:max_neutralizations]

    if turnover is not None and turnover > 0.15:
        decay_pool = [5, 10, 22]
    else:
        decay_pool = [0, 5, 10]
    decay_list = sorted(decay_pool, key=lambda d: _deterministic_hash(parent_fingerprint + "|decay|" + str(d)))
    current_decay = int(_float(settings.get("decay"), 0) or 0)
    decays = [d for d in decay_list if d != current_decay][:2]

    variants: list[dict[str, Any]] = []
    for n in neuts:
        for d in decays:
            vs = copy.deepcopy(settings)
            vs["neutralization"] = n
            vs["decay"] = d
            variants.append(_variant("neut_decay_cross_sweep", expression, vs, {"neutralization": n, "decay": d}))
    return variants[:max_variants]


def _neutralization_sweep_for_settings(settings: dict[str, Any]) -> list[str]:
    region = str(settings.get("region") or "").upper()
    values = list(CROSS_SWEEP_NEUTRALIZATION)
    if region in {"ASI", "CHN", "KOR", "TWN", "HKG", "JPN", "GLB"}:
        preferred = ["MARKET", "INDUSTRY", "SUBINDUSTRY", "SECTOR"]
        values = preferred + [item for item in values if item not in preferred]
    current = str(settings.get("neutralization") or "").upper()
    if current and current not in values:
        values.insert(0, current)
    return values


def _pnl_prune(
    candidates: list[dict[str, Any]],
    pnl_series_by_alpha_id: dict[str, list[float]],
    threshold: float = 0.8,
    latest_sim_by_candidate: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Greedy dedup by PnL correlation.

    pnl_series_by_alpha_id is supplied by the caller. If empty, pruning is skipped.
    Keeps the highest-sharpe alpha in each correlated cluster.
    """
    if len(candidates) <= 1 or not pnl_series_by_alpha_id:
        return list(candidates)

    alpha_ids = [str(c.get("alpha_id") or "") for c in candidates]
    series_list: list[tuple[int, list[float]]] = []
    for i, aid in enumerate(alpha_ids):
        pnl = pnl_series_by_alpha_id.get(aid)
        if pnl:
            series_list.append((i, pnl))

    if len(series_list) <= 1:
        return list(candidates)

    # Build correlation matrix from aligned PnL series
    n = len(series_list)
    indices = [idx for idx, _ in series_list]
    # Simple Pearson on equal-length series; skip if lengths differ
    keep = set(range(len(candidates)))
    skipped: set[int] = set()

    for a in range(n):
        ia = series_list[a][0]
        if ia in skipped:
            continue
        for b in range(a + 1, n):
            ib = series_list[b][0]
            if ib in skipped:
                continue
            sa, sb = series_list[a][1], series_list[b][1]
            if len(sa) != len(sb):
                continue
            corr = _pearson_corr(sa, sb)
            if corr is not None and abs(corr) > threshold:
                # Keep the one with higher sharpe (fallback: lower index)
                sha = _candidate_sharpe(candidates[ia], latest_sim_by_candidate)
                shb = _candidate_sharpe(candidates[ib], latest_sim_by_candidate)
                if sha >= shb:
                    skipped.add(ib)
                else:
                    skipped.add(ia)
                    break  # ia is removed, stop comparing it

    keep.difference_update(skipped)
    return [candidates[i] for i in sorted(keep)]


def _candidate_sharpe(candidate: dict[str, Any], latest_sim_by_candidate: dict[int, dict[str, Any]] | None) -> float:
    candidate_id = int(candidate.get("candidate_id") or 0)
    sim = (latest_sim_by_candidate or {}).get(candidate_id) or {}
    return _float(candidate.get("_sharpe") or candidate.get("sharpe") or sim.get("sharpe"), 0.0)


def _pearson_corr(x: list[float], y: list[float]) -> float | None:
    if len(x) < 2:
        return None
    n = len(x)
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    cov = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    std_x = sum((xi - mean_x) ** 2 for xi in x) ** 0.5
    std_y = sum((yi - mean_y) ** 2 for yi in y) ** 0.5
    if std_x == 0 or std_y == 0:
        return None
    return cov / (std_x * std_y)


def _turnover_control(expression: str, settings: dict[str, Any]) -> list[dict[str, Any]]:
    decay = max(int(_float(settings.get("decay"), 10) or 10), 8)
    return [
        _variant("turnover_control", f"ts_decay_linear(({expression}), {decay})", settings, {"operator": "ts_decay_linear", "window": decay}),
        _variant("turnover_control", f"trade_when(volume > adv20, ({expression}), -1)", settings, {"operator": "trade_when", "condition": "volume > adv20"}),
    ]


def _coverage_repair(expression: str, settings: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _variant("coverage_repair", f"ts_backfill(({expression}), 20)", settings, {"operator": "ts_backfill", "window": 20}),
        _variant("coverage_repair", f"winsorize(({expression}), std=4)", settings, {"operator": "winsorize", "std": 4}),
    ]


def _variant(strategy: str, expression: str, settings: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "REGULAR",
        "settings": copy.deepcopy(settings),
        "regular": expression,
        "variant_strategy": strategy,
        "variant_params": params,
        "lineage": {"variant_strategy": strategy, "variant_params": params},
    }


def _alpha_rows_from_variants(variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for variant in variants:
        row = {
            "type": "REGULAR",
            "settings": variant.get("settings") if isinstance(variant.get("settings"), dict) else {},
            "regular": variant.get("regular") or variant.get("expression") or "",
            "parent_candidate_id": variant.get("parent_candidate_id"),
            "variant_strategy": variant.get("variant_strategy"),
            "variant_params": variant.get("variant_params") if isinstance(variant.get("variant_params"), dict) else {},
            "parent_expression": variant.get("parent_expression") or "",
            "parent_metrics": variant.get("parent_metrics") if isinstance(variant.get("parent_metrics"), dict) else {},
        }
        if isinstance(variant.get("factor_thesis"), dict):
            row["factor_thesis"] = variant["factor_thesis"]
        row["lineage"] = {
            "parent_candidate_id": row["parent_candidate_id"],
            "parent_expression": row["parent_expression"],
            "variant_strategy": row["variant_strategy"],
            "variant_params": row["variant_params"],
            "parent_metrics": row["parent_metrics"],
        }
        if isinstance(row.get("factor_thesis"), dict):
            row["lineage"]["factor_thesis"] = row["factor_thesis"]
        rows.append(row)
    return rows


def _report(
    variants: list[dict[str, Any]],
    parents: list[dict[str, Any]],
    mode: str,
    max_variant_alphas: int,
    max_variants_per_alpha: int,
) -> dict[str, Any]:
    strategies = Counter(str(v.get("variant_strategy") or "") for v in variants)
    parent_counts = Counter(int(v.get("parent_candidate_id") or 0) for v in variants)
    return {
        "mode": mode,
        "max_variant_alphas": max_variant_alphas,
        "max_variants_per_alpha": max_variants_per_alpha,
        "parent_count": len(parents),
        "variant_count": len(variants),
        "strategy_counts": dict(sorted(strategies.items())),
        "variants_per_parent": {str(k): v for k, v in sorted(parent_counts.items()) if k},
        "parents": [
            {
                "candidate_id": p.get("candidate_id"),
                "signal_strength": p.get("_variant_signal_strength"),
                "failure_tags": p.get("_variant_failure_tags"),
                "selection_score": p.get("selection_score"),
                "expression": _shorten(str(p.get("expression") or "")),
            }
            for p in parents[:20]
        ],
        "variants": [
            {
                "parent_candidate_id": v.get("parent_candidate_id"),
                "variant_strategy": v.get("variant_strategy"),
                "variant_params": v.get("variant_params"),
                "expression": _shorten(str(v.get("regular") or "")),
            }
            for v in variants[:50]
        ],
    }


def _row_index(rows: list[Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        expression = str(row.get("regular") or row.get("regular_expression") or row.get("expression") or "")
        if expression:
            index.setdefault(expression, row)
    return index


def _failure_tags(candidate: dict[str, Any], sim: dict[str, Any], gate: dict[str, Any] | None = None) -> set[str]:
    tags = _split_tags(candidate.get("failure_tags"))
    tags.update(_split_tags(sim.get("failure_tags")))
    if sim:
        tags = set(diagnose_sim_result(sim).get("failure_tags") or [])
        tags.update(_split_tags(candidate.get("failure_tags")))
        tags.update(_split_tags(sim.get("failure_tags")))
    if gate:
        tags.update(diagnose_gate_check(gate).get("failure_tags") or [])
    return tags


def _json_obj(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _variant_thesis(parent_thesis: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    thesis = dict(parent_thesis)
    repairs = set(thesis.get("intended_repair_methods") or [])
    params = variant.get("variant_params") if isinstance(variant.get("variant_params"), dict) else {}
    operation = str(params.get("operation") or variant.get("variant_strategy") or "")
    if operation:
        repairs.add(operation)
    thesis["intended_repair_methods"] = sorted(str(item) for item in repairs if str(item).strip())
    thesis["parent_thesis_id"] = parent_thesis.get("thesis_id") or ""
    thesis["variant_strategy"] = variant.get("variant_strategy") or ""
    return thesis


def _split_tags(value: Any) -> set[str]:
    if isinstance(value, list):
        return {str(item) for item in value if str(item)}
    if isinstance(value, str):
        return {item.strip() for item in re.split(r"[,|]", value) if item.strip()}
    return set()


def _metric_summary(sim: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": sim.get("status") or "",
        "alpha_id": sim.get("alpha_id") or "",
        "sharpe": sim.get("sharpe"),
        "fitness": sim.get("fitness"),
        "turnover": sim.get("turnover"),
        "pnl": sim.get("pnl"),
    }


def _neighbor_windows(current: int) -> list[int]:
    return sorted(WINDOW_SWEEP, key=lambda item: (abs(item - current), item))


def _neighbor_decays(current: int) -> list[int]:
    return sorted(DECAY_SWEEP, key=lambda item: (abs(item - current), item))


def _dedupe_variants(variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    seen: set[tuple[str, str]] = set()
    for variant in variants:
        key = (str(variant.get("regular") or ""), _settings_key(variant.get("settings") or {}))
        if key in seen:
            continue
        seen.add(key)
        result.append(variant)
    return result


def _settings_key(settings: Any) -> str:
    if not isinstance(settings, dict):
        return ""
    parts = []
    for key in ("region", "universe", "delay", "decay", "neutralization", "truncation", "maxTrade"):
        if key in settings:
            parts.append(f"{key}={settings.get(key)}")
    return "|".join(parts)


def _float(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _shorten(value: str, limit: int = 220) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."
