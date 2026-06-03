from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


THESIS_TEXT_KEYS = (
    "factor_thesis",
    "thesis",
    "hypothesis",
    "rationale",
    "research_hypothesis",
    "idea",
    "description",
)


def build_factor_thesis(
    expression: str,
    *,
    item: dict[str, Any] | None = None,
    idea_context: dict[str, Any] | None = None,
    source: str = "",
) -> dict[str, Any]:
    """Create stable candidate-level research hypothesis metadata.

    The generator may already emit thesis fields. When it does not, this creates
    a conservative thesis from expression structure so memory can still learn at
    the hypothesis layer instead of only the operator layer.
    """

    item = item or {}
    idea_context = idea_context or {}
    text = _first_text(item, idea_context)
    thesis_type = _first_text_value(("thesis_type", "hypothesis_type", "signal_type", "theme"), item, idea_context)
    families = sorted(set(_listish(item.get("field_families") or item.get("field_family")) + extract_field_families(expression)))
    operators = extract_operators(expression)
    operator_themes = _operator_themes(operators)
    if not thesis_type:
        thesis_type = _infer_thesis_type(expression, operators, families, operator_themes)
    failure_modes = _listish(
        item.get("expected_failure_modes")
        or item.get("failure_modes")
        or item.get("risks")
        or idea_context.get("expected_failure_modes")
        or idea_context.get("failure_modes")
    )
    if not failure_modes:
        failure_modes = _expected_failure_modes(operators, operator_themes, families)
    repair_methods = _listish(
        item.get("intended_repair_methods")
        or item.get("repair_methods")
        or item.get("repairs")
        or idea_context.get("intended_repair_methods")
        or idea_context.get("repair_methods")
    )
    if not repair_methods:
        repair_methods = _repair_methods(failure_modes, operator_themes)
    if not text:
        text = _default_thesis_text(thesis_type, families, operator_themes)
    thesis = {
        "thesis_id": _thesis_id(thesis_type, families, operator_themes, text),
        "thesis_text": text,
        "thesis_type": thesis_type,
        "field_families": families,
        "operator_themes": operator_themes,
        "expected_failure_modes": sorted(set(failure_modes)),
        "intended_repair_methods": sorted(set(repair_methods)),
        "source": source,
    }
    return {key: value for key, value in thesis.items() if value not in ("", [], {}, None)}


def thesis_from_json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def thesis_lineage_from_row(row: dict[str, Any]) -> dict[str, Any]:
    direct = row.get("factor_thesis") or row.get("thesis")
    if isinstance(direct, dict):
        return direct
    lineage = row.get("lineage") if isinstance(row.get("lineage"), dict) else {}
    if isinstance(lineage.get("factor_thesis"), dict):
        return lineage["factor_thesis"]
    if isinstance(row.get("thesis_json"), dict):
        return row["thesis_json"]
    return {}


def load_idea_context(path: Path | str | None) -> dict[str, Any]:
    if not path:
        return {}
    try:
        p = Path(path)
        if not p.exists() or not p.is_file():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return _compact_idea_context(data)


def extract_operators(expression: str) -> list[str]:
    return sorted(set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", expression or "")))


def extract_field_families(expression: str) -> list[str]:
    tokens = set(re.findall(r"\b([a-z][a-z0-9_]*\d[a-z0-9_]*|[a-z]+\d+_[a-z0-9_]+)\b", (expression or "").lower()))
    families: set[str] = set()
    for token in tokens:
        match = re.match(r"([a-z]+\d+)", token)
        if match:
            families.add(match.group(1))
    return sorted(families)


def _compact_idea_context(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    context: dict[str, Any] = {}
    for key in (
        *THESIS_TEXT_KEYS,
        "theme",
        "signal_type",
        "thesis_type",
        "field_family",
        "field_families",
        "failure_modes",
        "expected_failure_modes",
        "repair_methods",
        "intended_repair_methods",
    ):
        if key in data:
            context[key] = data[key]
    for nested_key in ("idea", "template", "metadata", "context"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            for key in THESIS_TEXT_KEYS + ("theme", "signal_type", "thesis_type"):
                if key in nested and key not in context:
                    context[key] = nested[key]
    return context


def _first_text(item: dict[str, Any], context: dict[str, Any]) -> str:
    for key in THESIS_TEXT_KEYS:
        for source in (item, context):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return _clean_text(value)
    return ""


def _first_text_value(keys: tuple[str, ...], item: dict[str, Any], context: dict[str, Any]) -> str:
    for key in keys:
        for source in (item, context):
            value = source.get(key)
            if isinstance(value, str) and value.strip():
                return _slug(value)
    return ""


def _listish(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_slug(str(item)) for item in value if str(item).strip()]
    if isinstance(value, str):
        return [_slug(part) for part in re.split(r"[,;/|]", value) if part.strip()]
    return []


def _operator_themes(operators: list[str]) -> list[str]:
    ops = set(operators)
    themes: set[str] = set()
    if any(op.startswith("ts_") for op in ops):
        themes.add("time_series")
    if ops.intersection({"rank", "zscore", "normalize", "quantile"}):
        themes.add("cross_sectional")
    if any(op.startswith("group_") for op in ops) or "group_neutralize" in ops:
        themes.add("group_adjusted")
    if ops.intersection({"winsorize", "ts_backfill", "group_backfill", "densify", "kth_element"}):
        themes.add("coverage_cleaning")
    if ops.intersection({"trade_when", "hump", "hump_decay"}):
        themes.add("turnover_control")
    if ops.intersection({"ts_corr", "ts_covariance", "ts_regression", "regression_neut", "regression_proj"}):
        themes.add("relation_or_residual")
    if not themes:
        themes.add("plain_cross_sectional")
    return sorted(themes)


def _infer_thesis_type(expression: str, operators: list[str], families: list[str], themes: list[str]) -> str:
    lower = expression.lower()
    ops = set(operators)
    if ops.intersection({"ts_delta", "ts_rank", "ts_arg_max", "ts_arg_min"}):
        return "momentum_or_reversal"
    if ops.intersection({"ts_mean", "ts_sum", "ts_zscore", "ts_std_dev"}):
        return "mean_reversion_or_smoothing"
    if "relation_or_residual" in themes:
        return "relative_value_or_residual"
    if re.search(r"\b(volume|adv\d*|turnover|liquidity)\b", lower):
        return "liquidity_microstructure"
    if any(family.startswith(("pv", "volume", "price")) for family in families):
        return "price_volume"
    if families:
        return "fundamental_cross_section"
    return "generic_cross_section"


def _expected_failure_modes(operators: list[str], themes: list[str], families: list[str]) -> list[str]:
    modes = {"low_fitness", "low_sharpe"}
    ops = set(operators)
    if ops.intersection({"ts_delta", "rank", "zscore"}) and "turnover_control" not in themes:
        modes.add("high_turnover")
    if families or "coverage_cleaning" in themes:
        modes.add("coverage_issue")
    if "cross_sectional" in themes or "group_adjusted" in themes:
        modes.add("self_corr_high")
    return sorted(modes)


def _repair_methods(failure_modes: list[str], themes: list[str]) -> list[str]:
    repairs = set()
    modes = set(failure_modes)
    if modes.intersection({"high_turnover", "elevated_turnover"}):
        repairs.update({"increase_decay", "ts_decay_linear", "liquidity_trade_when"})
    if modes.intersection({"coverage_issue", "subuniverse_issue", "datafield_unavailable"}):
        repairs.update({"ts_backfill", "winsorize", "field_family_replacement"})
    if modes.intersection({"self_corr_high", "prod_corr_high"}):
        repairs.update({"group_neutralize", "window_perturbation", "neutralization_sweep"})
    if modes.intersection({"low_fitness", "low_sharpe"}):
        repairs.update({"rank_or_zscore_normalize", "group_neutralize", "window_sweep"})
    if "turnover_control" in themes:
        repairs.add("relax_entry_condition")
    return sorted(repairs)


def _default_thesis_text(thesis_type: str, families: list[str], themes: list[str]) -> str:
    family_text = ", ".join(families) if families else "available dataset fields"
    theme_text = ", ".join(themes) if themes else "expression structure"
    return f"{thesis_type} signal using {family_text} with {theme_text} transformations."


def _thesis_id(thesis_type: str, families: list[str], themes: list[str], text: str) -> str:
    payload = json.dumps(
        {"type": thesis_type, "families": families, "themes": themes, "text": text[:160]},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()[:500]


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.strip().lower()).strip("_")
