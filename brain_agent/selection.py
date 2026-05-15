from __future__ import annotations

from typing import Any

from .models import CandidateStatus


def classify_candidate(
    *,
    sim_status: str,
    sharpe: float | None,
    fitness: float | None,
    turnover: float | None,
    gate_passed: bool = False,
    hard_error: str = "",
) -> CandidateStatus:
    if gate_passed:
        return CandidateStatus.SUBMIT_READY
    if hard_error:
        return CandidateStatus.REJECTED
    if sim_status and sim_status.upper() not in {"COMPLETE", "COMPLETED", "SUCCESS"}:
        return CandidateStatus.SIM_FAILED

    s = float(sharpe or 0)
    f = float(fitness or 0)
    t = turnover
    if s <= -1.20 and f <= -0.50 and (t is None or 0.01 <= float(t) <= 0.70):
        return CandidateStatus.NEEDS_ENHANCE
    if t is not None and (float(t) < 0.01 or float(t) > 0.70):
        if s >= 1.0 and f >= 0.7:
            return CandidateStatus.NEEDS_ENHANCE
        return CandidateStatus.REJECTED
    if s >= 1.25 and f >= 1.0:
        return CandidateStatus.MANUAL_REVIEW
    if s >= 0.8 and f >= 0.5:
        return CandidateStatus.PROMISING
    if s >= 0.3 or f >= 0.25:
        return CandidateStatus.NEEDS_ENHANCE
    return CandidateStatus.REJECTED


def summarize_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "")
        counts[status] = counts.get(status, 0) + 1
    return counts
