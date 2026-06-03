from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..adapters import VariantSearchAdapter, _settings_from_config
from ..models import CandidateStatus
from ..repository import Repository
from ..models import RunConfig
from ..utils import write_json


HARD_OPTIMIZATION_FAILURE_TAGS = {"hard_error", "syntax_error", "unknown_variable", "sim_retryable"}
OPTIMIZABLE_STATUSES = {
    CandidateStatus.PROMISING.value,
    CandidateStatus.NEEDS_ENHANCE.value,
    CandidateStatus.MANUAL_REVIEW.value,
    CandidateStatus.REJECTED.value,
}


def select_optimization_parents(
    repo: Repository,
    run_id: str,
    *,
    max_parents: int = 20,
    min_score: float = 0.35,
    include_child_variants: bool = False,
    exclude_tagged: bool = False,
) -> list[dict[str, Any]]:
    latest_sim = repo.latest_sim_results_by_candidate(run_id)
    latest_gate = _latest_gate_by_candidate(repo, run_id)
    tagged_candidate_ids = _tagged_candidate_ids(repo, run_id) if exclude_tagged else set()
    parents: list[dict[str, Any]] = []
    for candidate in repo.list_rows("candidates", run_id):
        candidate_id = int(candidate.get("candidate_id") or 0)
        if not candidate_id:
            continue
        if candidate_id in tagged_candidate_ids:
            continue
        if candidate.get("parent_candidate_id") and not include_child_variants:
            continue
        sim = latest_sim.get(candidate_id) or {}
        if not sim:
            continue
        status = str(candidate.get("status") or "")
        score = float(candidate.get("selection_score") or 0.0)
        tags = optimization_tags(candidate, sim, latest_gate.get(candidate_id) or {})
        if not tags:
            continue
        failure_tags = set(_listish(sim.get("failure_tags")))
        if HARD_OPTIMIZATION_FAILURE_TAGS.intersection(failure_tags):
            continue
        if status not in OPTIMIZABLE_STATUSES and score < min_score:
            continue
        enriched = dict(candidate)
        enriched["latest_sim_result"] = sim
        enriched["latest_gate_check"] = latest_gate.get(candidate_id) or {}
        enriched["optimization_tags"] = tags
        enriched["failure_tags"] = sorted(failure_tags)
        enriched["repair_objectives"] = sorted(_listish(sim.get("repair_objectives")))
        parents.append(enriched)
    parents.sort(
        key=lambda row: (
            float(row.get("selection_score") or 0.0),
            _metric(row.get("latest_sim_result"), "fitness"),
            abs(_metric(row.get("latest_sim_result"), "sharpe")),
            int(row.get("candidate_id") or 0),
        ),
        reverse=True,
    )
    limit = max(0, int(max_parents))
    return parents[:limit] if limit else parents


def run_optimization_pass(
    repo: Repository,
    run_id: str,
    run_dir: Path,
    config: RunConfig,
    *,
    max_parents: int = 20,
    max_variants: int = 100,
    max_variants_per_alpha: int | None = None,
    min_score: float = 0.35,
    include_child_variants: bool = False,
    exclude_tagged: bool = False,
    source_prefix: str = "manual_optimize",
    dry_run: bool = False,
) -> dict[str, Any]:
    parents = select_optimization_parents(
        repo,
        run_id,
        max_parents=max_parents,
        min_score=min_score,
        include_child_variants=include_child_variants,
        exclude_tagged=exclude_tagged,
    )
    timestamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    payload: dict[str, Any] = {
        "run_id": run_id,
        "selected_parent_count": len(parents),
        "selected_parents": [_compact_parent(row) for row in parents],
        "dry_run": bool(dry_run),
    }
    if not parents or dry_run:
        payload.setdefault("status", "ok")
        payload.setdefault("variant_count", 0)
        return payload

    tag_source = f"{source_prefix}:{timestamp}"
    for parent in parents:
        metadata = {
            "latest_sim_result_id": (parent.get("latest_sim_result") or {}).get("sim_result_id"),
            "latest_gate_check_id": (parent.get("latest_gate_check") or {}).get("gate_check_id"),
            "selection_score": parent.get("selection_score"),
        }
        for tag in parent.get("optimization_tags") or []:
            repo.add_candidate_tag(run_id, int(parent["candidate_id"]), str(tag), source=tag_source, metadata=metadata)

    parent_alpha_list = _write_optimization_parent_alpha_list(run_dir, config, parents, timestamp)
    repo.add_artifact(run_id, "optimization_parent_alpha_list", parent_alpha_list, source_stage="DECIDE")
    repo.add_decision(
        run_id,
        int(timestamp),
        json.dumps(
            {
                "type": f"{source_prefix}_candidates",
                "max_parents": int(max_parents),
                "max_variants": int(max_variants),
                "max_variants_per_alpha": max_variants_per_alpha,
                "tag_source": tag_source,
                "parent_alpha_list": str(parent_alpha_list),
            },
            ensure_ascii=False,
        ),
        "Optimization pass tagged simulated parents and enqueued local-search variants.",
        parents,
    )

    variant_config = replace(
        config,
        max_variant_alphas=max(0, int(max_variants)),
        max_variants_per_alpha=(
            max(0, int(max_variants_per_alpha))
            if max_variants_per_alpha is not None
            else config.max_variants_per_alpha
        ),
    )
    result = VariantSearchAdapter(repo, run_id, run_dir).run(parent_alpha_list, variant_config, iteration=int(timestamp))
    variant_candidate_ids = [int(row.get("candidate_id") or 0) for row in result.candidates_delta if row.get("candidate_id")]
    for candidate_id in variant_candidate_ids:
        repo.update_candidate_queue_priority(run_id, candidate_id, 100)
    payload["status"] = result.status
    payload["error_summary"] = result.error_summary
    payload["variant_count"] = len(result.candidates_delta)
    payload["parent_alpha_list"] = str(parent_alpha_list)
    payload["tag_source"] = tag_source
    payload["variant_candidate_ids"] = variant_candidate_ids
    payload["variant_queue_priority"] = 100 if variant_candidate_ids else 0
    return payload


def optimization_tags(candidate: dict[str, Any], sim: dict[str, Any], gate: dict[str, Any] | None = None) -> list[str]:
    tags = set(_listish(sim.get("failure_tags")))
    objectives = set(_listish(sim.get("repair_objectives")))
    gate = gate or {}
    result: set[str] = set()

    sharpe = _metric(sim, "sharpe")
    fitness = _metric(sim, "fitness")
    turnover = _metric(sim, "turnover")
    status = str(candidate.get("status") or "")

    if "cand_neg" in tags or "test_short_flip" in objectives or sharpe < -0.2:
        result.add("short_flip_candidate")
    if "low_fitness" in tags or "improve_fitness" in objectives or 0 < fitness < 1.0:
        result.add("repair_low_fitness")
    if "low_sharpe" in tags or "improve_sharpe" in objectives or 0 < abs(sharpe) < 1.58:
        result.add("repair_low_sharpe")
    if "subuniverse_issue" in tags or _gate_failed(gate, "subuniverse_check"):
        result.add("repair_subuniverse")
    if "coverage_issue" in tags or "datafield_unavailable" in tags:
        result.add("coverage_repair_candidate")
    if "high_turnover" in tags or "elevated_turnover" in tags or "reduce_turnover" in objectives or turnover > 0.35:
        result.add("turnover_control_candidate")
    if "low_turnover" in tags or "increase_turnover" in objectives or (0 < turnover < 0.03):
        result.add("turnover_activation_candidate")
    if "self_corr_high" in tags or "prod_corr_high" in tags or "correlation_high" in tags:
        result.add("correlation_repair_candidate")
    if _gate_failed(gate, "weight_check") or "weight_concentration" in tags or "concentrated_weight" in tags:
        result.add("repair_weight_concentration")
    if not result and status in {CandidateStatus.PROMISING.value, CandidateStatus.MANUAL_REVIEW.value}:
        result.add("conservative_enhance_candidate")
    return sorted(result)


def _latest_gate_by_candidate(repo: Repository, run_id: str) -> dict[int, dict[str, Any]]:
    latest: dict[int, dict[str, Any]] = {}
    for row in repo.list_rows("gate_checks", run_id):
        candidate_id = int(row.get("candidate_id") or 0)
        if candidate_id:
            latest[candidate_id] = row
    return latest


def _tagged_candidate_ids(repo: Repository, run_id: str) -> set[int]:
    return {int(row.get("candidate_id") or 0) for row in repo.list_candidate_tags(run_id) if row.get("candidate_id")}


def _compact_parent(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": row.get("candidate_id"),
        "alpha_id": row.get("alpha_id"),
        "status": row.get("status"),
        "selection_score": row.get("selection_score"),
        "optimization_tags": row.get("optimization_tags") or [],
        "failure_tags": row.get("failure_tags") or [],
        "repair_objectives": row.get("repair_objectives") or [],
        "expression": row.get("expression"),
    }


def _write_optimization_parent_alpha_list(
    run_dir: Path,
    config: RunConfig,
    parents: list[dict[str, Any]],
    timestamp: str,
) -> Path:
    settings = _settings_from_config(config)
    rows = []
    for parent in parents:
        expression = str(parent.get("expression") or "")
        if not expression:
            continue
        rows.append(
            {
                "type": "REGULAR",
                "settings": settings,
                "regular": expression,
                "optimization_tags": parent.get("optimization_tags") or [],
                "parent_candidate_id": parent.get("candidate_id"),
            }
        )
    out = run_dir / "artifacts" / "04_variants" / f"optimization_parent_alpha_list_{timestamp}.json"
    write_json(out, rows)
    return out


def _listish(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    if isinstance(value, (tuple, set)):
        return [str(item) for item in value if str(item)]
    text = str(value)
    if not text:
        return []
    return [part.strip() for part in text.split(",") if part.strip()]


def _metric(row: Any, key: str) -> float:
    if not isinstance(row, dict):
        return 0.0
    try:
        return float(row.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _gate_failed(gate: dict[str, Any], key: str) -> bool:
    value = str(gate.get(key) or "").upper()
    return value == "FAIL" or value.startswith("FAIL:")
