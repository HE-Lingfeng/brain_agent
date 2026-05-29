from __future__ import annotations

import re
from typing import Any


def diagnose_sim_result(result: dict[str, Any]) -> dict[str, Any]:
    """Convert raw simulation metrics/errors into repair-oriented diagnostics."""

    tags: list[str] = []
    reasons: list[str] = []
    objectives: list[str] = []
    error = str(result.get("error") or "")
    status = str(result.get("status") or "")
    sharpe = _float_or_none(result.get("sharpe"))
    fitness = _float_or_none(result.get("fitness"))
    turnover = _float_or_none(result.get("turnover"))
    drawdown = _float_or_none(result.get("drawdown"))

    lower_error = error.lower()
    upper_status = status.upper()

    retryable = is_retryable_sim_failure(status=status, error=error)

    if error:
        if any(
            token in lower_error
            for token in (
                "datafield not available",
                "data field not available",
                "field not available",
                "missing datafield",
                "missing data field",
                "unavailable datafield",
            )
        ):
            _add(tags, "datafield_unavailable")
            _add(objectives, "use_available_fields")
            reasons.append("Expression uses data fields that are unavailable for the selected region/universe.")
        elif "unknown variable" in lower_error or "attempted to use unknown variable" in lower_error:
            _add(tags, "unknown_variable")
            _add(objectives, "fix_syntax")
            reasons.append("Simulation used a variable not recognized by BRAIN.")
        elif any(token in lower_error for token in ("syntax", "parse", "invalid expression", "invalid operator")):
            _add(tags, "syntax_error")
            _add(objectives, "fix_syntax")
            reasons.append("Simulation failed because the expression/operator syntax is invalid.")
        elif "subuniverse" in lower_error or "sub-universe" in lower_error:
            _add(tags, "subuniverse_issue")
            _add(objectives, "improve_coverage")
            reasons.append("Simulation error suggests the alpha does not cover enough of the target subuniverse.")
        elif any(token in lower_error for token in ("coverage", "insufficient", "empty universe", "no stocks")):
            _add(tags, "coverage_issue")
            _add(objectives, "improve_coverage")
            reasons.append("Simulation error suggests poor field coverage or too few tradable stocks.")
        elif retryable or any(token in lower_error for token in ("timeout", "rate limit", "429", "retry-after")):
            _add(tags, "platform_or_rate_limit")
            reasons.append("Failure appears related to platform timeout or rate limiting.")
        else:
            _add(tags, "hard_error")
            reasons.append("Simulation returned a hard error.")

    if upper_status and upper_status not in {"COMPLETE", "COMPLETED", "SUCCESS"}:
        _add(tags, "sim_failed")
        reasons.append(f"Simulation status is {status}.")
        if retryable:
            _add(tags, "sim_retryable")
            _add(objectives, "retry_simulation")
            reasons.append("Failure appears transient; retry the same simulation payload before judging alpha quality.")

    if turnover is not None:
        if turnover > 0.70:
            _add(tags, "high_turnover")
            _add(objectives, "reduce_turnover")
            reasons.append(f"Turnover {turnover:.3f} is above the high-turnover threshold.")
        elif turnover < 0.01:
            _add(tags, "low_turnover")
            _add(objectives, "improve_signal_activity")
            reasons.append(f"Turnover {turnover:.3f} is too low; the signal may barely trade.")
        elif turnover > 0.45:
            _add(tags, "elevated_turnover")
            _add(objectives, "reduce_turnover")
            reasons.append(f"Turnover {turnover:.3f} is elevated and may hurt margin or submission quality.")

    if sharpe is not None and sharpe < 0.80:
        _add(tags, "low_sharpe")
        _add(objectives, "improve_sharpe")
        reasons.append(f"Sharpe {sharpe:.3f} is below the promising threshold.")

    if fitness is not None and fitness < 0.50:
        _add(tags, "low_fitness")
        _add(objectives, "improve_fitness")
        reasons.append(f"Fitness {fitness:.3f} is below the promising threshold.")

    if _is_short_flip_candidate(sharpe=sharpe, fitness=fitness, turnover=turnover, status=upper_status, error=error):
        _add(tags, "cand_neg")
        _add(objectives, "test_short_flip")
        reasons.append(
            "Sharpe and Fitness are strongly negative without an obvious hard failure; treat this as a possible inverse signal."
        )

    if drawdown is not None and drawdown < -0.20:
        _add(tags, "high_drawdown")
        _add(objectives, "improve_robustness")
        reasons.append(f"Drawdown {drawdown:.3f} suggests weak robustness.")

    if not tags and upper_status in {"COMPLETE", "COMPLETED", "SUCCESS"}:
        _add(tags, "no_obvious_failure")
        reasons.append("No obvious simulation failure pattern detected.")

    repair_hints = _repair_hints(tags)
    return {
        "failure_tags": tags,
        "repair_objectives": objectives,
        "diagnosis_reasons": reasons,
        "repair_hints": repair_hints,
    }


def is_retryable_sim_failure(*, status: str = "", error: str = "") -> bool:
    """Return true for platform/queue failures that should be retried unchanged."""
    upper_status = str(status or "").upper()
    lower_error = str(error or "").lower()
    if upper_status in {"TIMEOUT", "BATCH_SPAWN_FAILED"}:
        return True
    if upper_status == "SUBMISSION_FAILED":
        return any(
            token in lower_error
            for token in (
                "429",
                "rate limit",
                "retry-after",
                "too many",
                "temporarily",
                "timeout",
                "timed out",
                "platform",
                "queue",
                "try again",
                "service unavailable",
                "502",
                "503",
                "504",
            )
        )
    if any(token in lower_error for token in ("parent status: none", "batch parent", "spawn children")):
        return True
    return False


def diagnose_gate_check(result: dict[str, Any]) -> dict[str, Any]:
    """Convert submit-gate checks into repair-oriented diagnostics."""

    tags: list[str] = []
    reasons: list[str] = []
    objectives: list[str] = []
    self_corr = str(result.get("self_corr_check") or "").upper()
    prod_corr = str(result.get("prod_corr_check") or "").upper()
    subuniverse = str(result.get("subuniverse_check") or "").upper()
    submission = str(result.get("submission_check") or "").upper()
    error = str(result.get("error") or "")
    gate_status = str(result.get("gate_status") or "").lower()
    error_type = str(result.get("error_type") or "").lower()

    if gate_status == "incomplete":
        _add(tags, "gate_incomplete")
        reasons.append("Submit gate check was incomplete; treat this as missing gate evidence rather than an alpha quality failure.")
    if error_type:
        _add(tags, error_type)
        if error_type == "network_error":
            reasons.append("Submit gate check hit a network/proxy/rate-limit style error.")
    if _is_failed_check(self_corr):
        _add(tags, "self_corr_high")
        _add(objectives, "reduce_self_correlation")
        reasons.append("Submit gate self-correlation check did not pass.")
    if _is_failed_check(prod_corr):
        _add(tags, "prod_corr_high")
        _add(objectives, "reduce_prod_correlation")
        reasons.append("Submit gate production-correlation check did not pass.")
    if _is_failed_check(subuniverse):
        _add(tags, "subuniverse_issue")
        _add(objectives, "improve_coverage")
        reasons.append("Submit gate subuniverse check did not pass.")
    if _is_failed_check(submission) and not tags:
        _add(tags, "submission_gate_failed")
        reasons.append("Submit gate failed without a more specific local category.")
    if error and gate_status != "incomplete":
        _add(tags, "gate_error")
        reasons.append("Submit gate check returned an error.")

    return {
        "failure_tags": tags,
        "repair_objectives": objectives,
        "diagnosis_reasons": reasons,
        "repair_hints": _repair_hints(tags),
    }


def summarize_failure_tags(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        tags = row.get("failure_tags")
        if isinstance(tags, str):
            tags = [x.strip() for x in re.split(r"[,|]", tags) if x.strip()]
        if not isinstance(tags, list):
            continue
        for tag in tags:
            counts[str(tag)] = counts.get(str(tag), 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _repair_hints(tags: list[str]) -> list[str]:
    hints: list[str] = []
    if "high_turnover" in tags or "elevated_turnover" in tags:
        hints.extend(
            [
                "Try stronger decay, hump, ts_target_tvr_decay, ts_target_tvr_hump, ts_target_tvr_delta_limit, or trade_when filters.",
                "Preserve the economic signal while reducing unnecessary rebalancing.",
            ]
        )
    if "low_fitness" in tags:
        hints.append("Improve signal-to-noise via backfill, winsorize, group normalization, or cleaner regime filters.")
    if "low_sharpe" in tags:
        hints.append("Consider stronger cross-sectional ranking, time-series smoothing, or peer-relative normalization.")
    if "unknown_variable" in tags or "syntax_error" in tags:
        hints.append("Keep placeholders unchanged and use only dataset fields/operators available to this run.")
    if "datafield_unavailable" in tags:
        hints.append("Switch to fields returned by the target region/universe datafields API, or rerun generation for that target universe.")
    if "coverage_issue" in tags or "subuniverse_issue" in tags:
        hints.append("Prefer higher-coverage fields, add ts_backfill, or avoid overly restrictive trade_when conditions.")
    if "low_turnover" in tags:
        hints.append("Relax entry conditions or reduce excessive smoothing so the alpha trades enough.")
    if "cand_neg" in tags:
        hints.append("Generate short-flip variants such as multiply(-1, expr) before discarding this signal.")
    if "self_corr_high" in tags or "prod_corr_high" in tags:
        hints.append("Reduce correlation via alternate neutralization, group-neutralization, window perturbation, or stronger signal de-correlation.")
    if "gate_incomplete" in tags or "network_error" in tags:
        hints.append("Retry gate checks later; do not treat incomplete gate evidence as a failed research result.")
    if "sim_retryable" in tags:
        hints.append("Retry the same simulation payload later; this looks like platform queueing, timeout, or rate limiting.")
    return hints


def _add(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_short_flip_candidate(
    *,
    sharpe: float | None,
    fitness: float | None,
    turnover: float | None,
    status: str,
    error: str,
) -> bool:
    if error:
        return False
    if status and status not in {"COMPLETE", "COMPLETED", "SUCCESS"}:
        return False
    if sharpe is None or fitness is None:
        return False
    if sharpe > -1.20 or fitness > -0.50:
        return False
    if turnover is not None and (turnover < 0.01 or turnover > 0.70):
        return False
    return True


def _is_failed_check(value: str) -> bool:
    if not value:
        return False
    return value.upper() not in {"PASS", "PASSED", "PENDING", "UNKNOWN", "ERROR", "N/A", "NA"}
