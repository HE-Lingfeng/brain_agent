from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any


NEUTRALIZATION_SETTING_SWEEP = ("INDUSTRY", "SUBINDUSTRY", "SECTOR", "MARKET", "SLOW_AND_FAST", "FAST", "SLOW", "NONE")


@dataclass(frozen=True)
class OptimizerContext:
    expression: str
    settings: dict[str, Any]
    metrics: dict[str, Any]
    failure_tags: set[str]


class AlphaOptimizer:
    name = "AlphaOptimizer"

    def applies(self, context: OptimizerContext) -> bool:
        raise NotImplementedError

    def variants(self, context: OptimizerContext) -> list[dict[str, Any]]:
        raise NotImplementedError

    def _variant(self, expression: str, context: OptimizerContext, operation: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        variant_params = {"optimizer": self.name, "operation": operation}
        if params:
            variant_params.update(params)
        return {
            "type": "REGULAR",
            "settings": copy.deepcopy(context.settings),
            "regular": expression,
            "variant_strategy": self.name,
            "variant_params": variant_params,
            "lineage": {"variant_strategy": self.name, "variant_params": variant_params},
        }

    def _settings_variant(
        self,
        context: OptimizerContext,
        operation: str,
        settings_patch: dict[str, Any],
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        variant = self._variant(context.expression, context, operation, params)
        variant["settings"].update(settings_patch)
        return variant


class LowFitnessOptimizer(AlphaOptimizer):
    name = "LowFitnessOptimizer"

    def applies(self, context: OptimizerContext) -> bool:
        sharpe = _float(context.metrics.get("sharpe"), 0.0)
        fitness = _float(context.metrics.get("fitness"), 0.0)
        return (
            "low_fitness" in context.failure_tags
            or "low_sharpe" in context.failure_tags
            or (abs(sharpe) <= 0.45 and abs(fitness) <= 0.35)
        )

    def variants(self, context: OptimizerContext) -> list[dict[str, Any]]:
        expr = context.expression
        variants = [
            self._variant(f"rank(({expr}))", context, "rank_normalize"),
            self._variant(f"winsorize(({expr}), std=4)", context, "winsorize", {"std": 4}),
            self._variant(f"group_neutralize(({expr}), industry)", context, "group_neutralize", {"group": "industry"}),
        ]
        if not re.search(r"\b(zscore|rank)\s*\(", expr):
            variants.append(self._variant(f"zscore(({expr}))", context, "zscore_normalize"))
        decay = max(int(_float(context.settings.get("decay"), 8) or 8), 4)
        variants.append(self._variant(f"ts_decay_linear(({expr}), {decay})", context, "ts_decay_linear", {"window": decay}))
        return variants


class LowTurnoverOptimizer(AlphaOptimizer):
    name = "LowTurnoverOptimizer"

    def applies(self, context: OptimizerContext) -> bool:
        turnover = _float(context.metrics.get("turnover"), 0.0)
        return "low_turnover" in context.failure_tags or (0.0 < turnover < 0.03)

    def variants(self, context: OptimizerContext) -> list[dict[str, Any]]:
        expr = context.expression
        variants: list[dict[str, Any]] = []
        current_decay = int(_float(context.settings.get("decay"), 0) or 0)
        if current_decay > 1:
            relaxed_decay = max(1, min(8, current_decay // 2))
            variants.append(
                self._settings_variant(
                    context,
                    "relax_decay_setting",
                    {"decay": relaxed_decay},
                    {"from": current_decay, "to": relaxed_decay},
                )
            )
        variants.extend(self._relax_trade_when(context))
        variants.extend(self._shrink_windows(context))
        variants.extend(self._unwrap_decay(context))
        return variants

    def _relax_trade_when(self, context: OptimizerContext) -> list[dict[str, Any]]:
        args = _call_args(context.expression, "trade_when")
        if len(args) != 3 or args[2].strip() not in {"-1", "1"}:
            return []
        return [self._variant(args[1].strip(), context, "relax_trade_when", {"removed_condition": _shorten(args[0].strip(), 120)})]

    def _shrink_windows(self, context: OptimizerContext) -> list[dict[str, Any]]:
        variants = []
        for match in list(re.finditer(r"\b(ts_[A-Za-z0-9_]+)\(([^()]*(?:\([^()]*\)[^()]*)*?),\s*(\d+)(\s*[,)]?)", context.expression))[:2]:
            current = int(match.group(3))
            if current <= 5:
                continue
            reduced = max(3, current // 2)
            replacement = f"{match.group(1)}({match.group(2)}, {reduced}{match.group(4)}"
            changed = context.expression[: match.start()] + replacement + context.expression[match.end() :]
            variants.append(self._variant(changed, context, "shrink_window", {"operator": match.group(1), "from": current, "to": reduced}))
        return variants

    def _unwrap_decay(self, context: OptimizerContext) -> list[dict[str, Any]]:
        match = re.match(r"\s*ts_decay_linear\s*\((.+),\s*\d+\s*\)\s*$", context.expression)
        if not match:
            return []
        return [self._variant(match.group(1).strip(), context, "remove_outer_decay")]


class HighTurnoverOptimizer(AlphaOptimizer):
    name = "HighTurnoverOptimizer"

    def applies(self, context: OptimizerContext) -> bool:
        turnover = _float(context.metrics.get("turnover"), 0.0)
        return "high_turnover" in context.failure_tags or "elevated_turnover" in context.failure_tags or turnover > 0.45

    def variants(self, context: OptimizerContext) -> list[dict[str, Any]]:
        expr = context.expression
        current_decay = int(_float(context.settings.get("decay"), 0) or 0)
        target_decay = max(current_decay, 12)
        variants: list[dict[str, Any]] = []
        if current_decay < 20:
            stronger_decay = min(20, max(8, current_decay * 2 if current_decay else target_decay))
            variants.append(
                self._settings_variant(
                    context,
                    "increase_decay_setting",
                    {"decay": stronger_decay},
                    {"from": current_decay, "to": stronger_decay},
                )
            )
        variants.extend(
            [
                self._variant(f"ts_decay_linear(({expr}), {target_decay})", context, "outer_ts_decay_linear", {"window": target_decay}),
                self._variant(f"trade_when(volume > adv20, ({expr}), -1)", context, "liquidity_trade_when", {"condition": "volume > adv20"}),
                self._variant(f"winsorize(ts_decay_linear(({expr}), {target_decay}), std=4)", context, "decay_winsorize", {"window": target_decay, "std": 4}),
            ]
        )
        variants.extend(self._expand_windows(context))
        return variants

    def _expand_windows(self, context: OptimizerContext) -> list[dict[str, Any]]:
        variants = []
        for match in list(re.finditer(r"\b(ts_[A-Za-z0-9_]+)\(([^()]*(?:\([^()]*\)[^()]*)*?),\s*(\d+)(\s*[,)]?)", context.expression))[:2]:
            current = int(match.group(3))
            if current >= 120:
                continue
            expanded = min(120, max(current + 5, current * 2))
            replacement = f"{match.group(1)}({match.group(2)}, {expanded}{match.group(4)}"
            changed = context.expression[: match.start()] + replacement + context.expression[match.end() :]
            variants.append(self._variant(changed, context, "expand_window", {"operator": match.group(1), "from": current, "to": expanded}))
        return variants


class ShortFlipOptimizer(AlphaOptimizer):
    name = "ShortFlipOptimizer"

    def applies(self, context: OptimizerContext) -> bool:
        sharpe = _float(context.metrics.get("sharpe"), 0.0)
        fitness = _float(context.metrics.get("fitness"), 0.0)
        return "cand_neg" in context.failure_tags or sharpe < -0.2 or fitness < -0.15

    def variants(self, context: OptimizerContext) -> list[dict[str, Any]]:
        expr = context.expression
        return [
            self._variant(f"multiply(-1, ({expr}))", context, "sign_flip"),
            self._variant(f"rank(multiply(-1, ({expr})))", context, "ranked_sign_flip"),
            self._variant(f"zscore(multiply(-1, ({expr})))", context, "zscore_sign_flip"),
        ]


class CoverageRepairer(AlphaOptimizer):
    name = "CoverageRepairer"

    def applies(self, context: OptimizerContext) -> bool:
        return bool(context.failure_tags.intersection({"coverage_issue", "subuniverse_issue", "datafield_unavailable"}))

    def variants(self, context: OptimizerContext) -> list[dict[str, Any]]:
        expr = context.expression
        variants = [
            self._variant(f"ts_backfill(({expr}), 20)", context, "ts_backfill", {"window": 20}),
            self._variant(f"winsorize(ts_backfill(({expr}), 20), std=4)", context, "backfill_winsorize", {"window": 20, "std": 4}),
            self._variant(f"group_neutralize(ts_backfill(({expr}), 20), subindustry)", context, "backfill_group_neutralize", {"window": 20, "group": "subindustry"}),
        ]
        if re.search(r"\btrade_when\s*\(", expr):
            variants.extend(LowTurnoverOptimizer()._relax_trade_when(context))
        return variants


class CorrelationOptimizer(AlphaOptimizer):
    name = "CorrelationOptimizer"

    def applies(self, context: OptimizerContext) -> bool:
        return bool(context.failure_tags.intersection({"self_corr_high", "prod_corr_high", "correlation_high", "corr_high"}))

    def variants(self, context: OptimizerContext) -> list[dict[str, Any]]:
        expr = context.expression
        variants = [
            self._variant(f"group_neutralize(({expr}), subindustry)", context, "subindustry_neutralize", {"group": "subindustry"}),
            self._variant(f"group_neutralize(({expr}), sector)", context, "sector_neutralize", {"group": "sector"}),
            self._variant(f"winsorize(rank(({expr})), std=4)", context, "rank_winsorize", {"std": 4}),
            self._variant(f"rank(ts_delta(({expr}), 5))", context, "delta_rank", {"window": 5}),
        ]
        current_neutralization = str(context.settings.get("neutralization") or "").upper()
        for neutralization in _neutralization_setting_sweep(context.settings):
            if neutralization == current_neutralization:
                continue
            variants.append(
                self._settings_variant(
                    context,
                    "neutralization_setting",
                    {"neutralization": neutralization},
                    {"from": current_neutralization, "to": neutralization},
                )
            )
            if len(variants) >= 6:
                break
        variants.extend(self._window_perturbations(context))
        return variants

    def _window_perturbations(self, context: OptimizerContext) -> list[dict[str, Any]]:
        variants = []
        for match in list(re.finditer(r"\b(ts_[A-Za-z0-9_]+)\(([^()]*(?:\([^()]*\)[^()]*)*?),\s*(\d+)(\s*[,)]?)", context.expression))[:2]:
            current = int(match.group(3))
            for window in _corr_windows(current):
                replacement = f"{match.group(1)}({match.group(2)}, {window}{match.group(4)}"
                changed = context.expression[: match.start()] + replacement + context.expression[match.end() :]
                variants.append(
                    self._variant(changed, context, "window_perturbation", {"operator": match.group(1), "from": current, "to": window})
                )
                break
        return variants


class SecondOrderOptimizer(AlphaOptimizer):
    """Wrap promising expressions with safe second-order operators.

    Only applied to PROMISING / MANUAL_REVIEW / high-score candidates.
    Skip candidates with hard failures or retryable/platform errors.
    """

    name = "second_order"
    ELIGIBLE_STATUSES = {"promising", "manual_review"}
    ELIGIBLE_SCORE_THRESHOLD = 0.6
    GROUPS = ("industry", "subindustry", "sector")

    def applies(self, context: OptimizerContext) -> bool:
        return True  # eligibility checked per-candidate in variant_search

    def variants(self, context: OptimizerContext) -> list[dict[str, Any]]:
        return self.optimize(context.expression, context.settings)

    def optimize(
        self,
        expression: str,
        settings: dict[str, Any],
        *,
        sim_result: dict[str, Any] | None = None,
        candidate: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        # Eligibility gate
        if candidate:
            status = str(candidate.get("status") or "")
            score = _float(candidate.get("selection_score"), 0.0)
            if status not in self.ELIGIBLE_STATUSES and score < self.ELIGIBLE_SCORE_THRESHOLD:
                return []
        if sim_result and self._has_hard_failure(sim_result):
            return []

        variants: list[dict[str, Any]] = []

        # Group wrapping — up to 2 groups
        for group in self.GROUPS[:2]:
            variants.append(
                self._variant(f"group_rank({expression}, {group})", OptimizerContext(expression, settings, {}, set()),
                              "group_rank", {"group": group})
            )
            variants.append(
                self._variant(f"group_zscore({expression}, {group})", OptimizerContext(expression, settings, {}, set()),
                              "group_zscore", {"group": group})
            )
            variants.append(
                self._variant(f"group_neutralize({expression}, {group})", OptimizerContext(expression, settings, {}, set()),
                              "group_neutralize", {"group": group})
            )
            variants.append(
                self._variant(f"group_mean({expression}, {group})", OptimizerContext(expression, settings, {}, set()),
                              "group_mean", {"group": group})
            )

        # Power wrapping
        variants.append(
            self._variant(f"signed_power({expression}, 2)", OptimizerContext(expression, settings, {}, set()),
                          "signed_power2", {"power": 2})
        )
        variants.append(
            self._variant(f"signed_power({expression}, 0.5)", OptimizerContext(expression, settings, {}, set()),
                          "signed_power05", {"power": 0.5})
        )

        # ts_decay_linear — only if turnover < 0.5
        turnover = _float(sim_result.get("turnover"), None) if sim_result else None
        if turnover is None or turnover < 0.5:
            for w in (10, 20):
                variants.append(
                    self._variant(f"ts_decay_linear({expression}, {w})", OptimizerContext(expression, settings, {}, set()),
                                  "ts_decay_linear", {"window": w})
                )

        return variants

    @staticmethod
    def _has_hard_failure(sim_result: dict[str, Any]) -> bool:
        tags = sim_result.get("failure_tags") or []
        if isinstance(tags, str):
            tags = [t.strip() for t in tags.split(",")]
        hard = {"hard_error", "syntax_error", "unknown_variable"}
        return bool(hard.intersection(str(t) for t in tags))


OPTIMIZERS: tuple[AlphaOptimizer, ...] = (
    ShortFlipOptimizer(),
    HighTurnoverOptimizer(),
    LowTurnoverOptimizer(),
    CoverageRepairer(),
    CorrelationOptimizer(),
    LowFitnessOptimizer(),
)


def build_optimizer_variants(expression: str, settings: dict[str, Any], metrics: dict[str, Any], failure_tags: set[str]) -> list[dict[str, Any]]:
    context = OptimizerContext(expression=expression, settings=settings, metrics=metrics, failure_tags=failure_tags)
    variants: list[dict[str, Any]] = []
    for optimizer in OPTIMIZERS:
        if optimizer.applies(context):
            variants.extend(optimizer.variants(context))
    return _dedupe(variants)


def _dedupe(variants: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
    return "|".join(f"{key}={settings.get(key)}" for key in sorted(settings))


def _neutralization_setting_sweep(settings: dict[str, Any]) -> list[str]:
    region = str(settings.get("region") or "").upper()
    values = list(NEUTRALIZATION_SETTING_SWEEP)
    if region in {"ASI", "CHN", "KOR", "TWN", "HKG", "JPN", "GLB"}:
        preferred = ["MARKET", "INDUSTRY", "SUBINDUSTRY", "SECTOR"]
        values = preferred + [item for item in values if item not in preferred]
    current = str(settings.get("neutralization") or "").upper()
    if current and current not in values:
        values.insert(0, current)
    return values


def _float(value: Any, default: float) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _call_args(expression: str, function_name: str) -> list[str]:
    prefix = f"{function_name}("
    stripped = expression.strip()
    if not stripped.startswith(prefix) or not stripped.endswith(")"):
        return []
    body = stripped[len(prefix) : -1]
    args: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(body):
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(0, depth - 1)
        elif char == "," and depth == 0:
            args.append(body[start:index].strip())
            start = index + 1
    args.append(body[start:].strip())
    return args


def _corr_windows(current: int) -> list[int]:
    choices = (5, 10, 20, 60, 120)
    return [item for item in sorted(choices, key=lambda value: (abs(value - current), value)) if item != current]
