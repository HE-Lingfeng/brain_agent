from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from ..analysis.diagnostics import summarize_failure_tags
from ..analysis.memory import AlphaMemory, memory_path_for_run_dir
from ..intelligence.prompting import summarize_run_prompt_metrics
from ..core.repository import Repository
from ..analysis.selection import summarize_counts
from ..core.utils import write_json


def build_run_result(repo: Repository, run_id: str) -> dict[str, Any]:
    run = repo.get_run(run_id)
    if not run:
        raise ValueError(f"Run not found: {run_id}")
    candidates = repo.list_rows("candidates", run_id)
    return {
        "run": run,
        "config": json.loads(run["config_json"]),
        "counts": summarize_counts(candidates),
        "candidates": candidates,
        "sim_results": repo.list_rows("sim_results", run_id),
        "gate_checks": repo.list_rows("gate_checks", run_id),
        "decisions": repo.list_rows("decisions", run_id),
        "tasks": repo.list_rows("tasks", run_id),
        "artifacts": repo.list_rows("artifacts", run_id),
        "prompt_metrics": summarize_run_prompt_metrics(repo, run_id),
    }


def write_report(repo: Repository, run_id: str, run_dir: Path) -> tuple[Path, Path]:
    result = build_run_result(repo, run_id)
    json_path = run_dir / "run_result.json"
    md_path = run_dir / "run_report.md"
    write_json(json_path, result)
    memory = AlphaMemory(memory_path_for_run_dir(run_dir))
    try:
        memory.ingest_run(repo, run_id)
    finally:
        memory.close()

    config = result["config"]
    candidates = result["candidates"]
    sim_by_fp = _latest_by_key(result["sim_results"], "fingerprint")
    gates_by_candidate = _latest_by_key(result["gate_checks"], "candidate_id")
    ready = _latest_gate_passed_candidates(candidates, gates_by_candidate)
    ready_candidate_ids = {item.get("candidate_id") for item in ready}
    stale_ready = [
        c
        for c in candidates
        if c.get("status") == "submit_ready" and c.get("candidate_id") not in ready_candidate_ids
    ]
    tag_counts = summarize_failure_tags(result["sim_results"])
    research_summary = _research_summary(result, ready, tag_counts)
    variant_comparisons = _variant_comparisons(candidates, result["sim_results"])
    gate_incomplete_counts = _gate_incomplete_counts(result["gate_checks"])

    lines: list[str] = []
    lines.append(f"# BRAIN Research Log: {run_id}")
    lines.append("")
    lines.append("## Executive Summary")
    for item in research_summary:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Experiment Setup")
    for key in (
        "dataset",
        "region",
        "delay",
        "universe",
        "data_type",
        "decay",
        "truncation",
        "neutralization",
        "max_trade",
        "max_sim_alphas",
        "make_prompt_version",
        "enhance_prompt_version",
        "decision_prompt_version",
        "prompt_experiment",
        "target_ready",
        "max_iterations",
        "dry_run",
    ):
        lines.append(f"- {key}: {config.get(key)}")
    lines.append(f"- created_at: {result['run'].get('created_at')}")
    lines.append(f"- updated_at: {result['run'].get('updated_at')}")
    lines.append("")
    lines.append("## Run Outcome")
    lines.append(f"- stage: {result['run'].get('stage')}")
    lines.append(f"- stop_reason: {result['run'].get('stop_reason') or ''}")
    lines.append(f"- generated/simulated/enhanced candidates: {len(candidates)}")
    for status, count in sorted(result["counts"].items()):
        lines.append(f"- {status}: {count}")
    lines.append("")
    lines.append("## Prompt Metrics")
    prompt_metrics = result.get("prompt_metrics") if isinstance(result.get("prompt_metrics"), dict) else {}
    if prompt_metrics:
        lines.append(f"- experiment_score: {prompt_metrics.get('score')}")
        lines.append(f"- valid_rate: {prompt_metrics.get('valid_rate')}")
        lines.append(f"- sim_success_rate: {prompt_metrics.get('sim_success_rate')}")
        lines.append(f"- promising_rate: {prompt_metrics.get('promising_rate')}")
        lines.append(f"- submit_ready_rate: {prompt_metrics.get('submit_ready_rate')}")
        lines.append(f"- avg_sharpe: {prompt_metrics.get('avg_sharpe')}")
        lines.append(f"- avg_fitness: {prompt_metrics.get('avg_fitness')}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("## Research Timeline")
    for event in _timeline_events(result):
        lines.append(
            f"- {event.get('time') or '<unknown time>'} [{event.get('stage')}] "
            f"{event.get('kind')}: {event.get('summary')}"
        )
    lines.append("")
    lines.append("## Decision Journal")
    if result["decisions"]:
        for decision in result["decisions"]:
            action = _json_obj_or_list(decision.get("action"))
            lines.append(f"### Iteration {decision.get('iteration')} Decision {decision.get('decision_id')}")
            lines.append(f"- created_at: {decision.get('created_at')}")
            lines.append(f"- reason: {decision.get('reason') or ''}")
            if isinstance(action, list):
                for item in action:
                    if not isinstance(item, dict):
                        continue
                    lines.append(
                        "- action: "
                        f"mode={item.get('mode')}, style={item.get('style')}, "
                        f"candidate_ids={item.get('candidate_ids')}, source={item.get('source')}, "
                        f"reason={item.get('reason')}"
                    )
            elif isinstance(action, dict):
                lines.append(f"- action_type: {action.get('type') or 'object'}")
                if action.get("action"):
                    lines.append(f"- action: {json.dumps(action.get('action'), ensure_ascii=False)}")
                if action.get("produced_artifacts"):
                    lines.append(f"- produced_artifacts: {len(action.get('produced_artifacts') or [])}")
            else:
                lines.append(f"- action: {decision.get('action')}")
            inputs = _json_list(decision.get("input_candidates_json"))
            if inputs:
                top_inputs = sorted(inputs, key=lambda x: float(x.get("selection_score") or 0), reverse=True)[:5]
                lines.append("- top_input_candidates:")
                for item in top_inputs:
                    lines.append(
                        f"  - candidate_id={item.get('candidate_id')}, status={item.get('status')}, "
                        f"score={item.get('selection_score')}, objectives={item.get('repair_objectives') or []}"
                    )
            lines.append("")
    else:
        lines.append("- No enhance/decision records were created.")
        lines.append("")
    lines.append("## Submit Ready Alpha IDs")
    if ready:
        for item in ready:
            gate = gates_by_candidate.get(item.get("candidate_id")) or {}
            lines.append(
                f"- {item.get('alpha_id') or '<missing alpha_id>'} "
                f"(candidate_id={item['candidate_id']}, gate_checked_at={gate.get('created_at') or '<unknown>'})"
            )
    else:
        lines.append("- None")
    lines.append("")
    if stale_ready:
        lines.append("## Stale Submit Ready Excluded")
        for item in stale_ready:
            gate = gates_by_candidate.get(item.get("candidate_id")) or {}
            lines.append(
                f"- {item.get('alpha_id') or '<missing alpha_id>'} "
                f"(candidate_id={item['candidate_id']}, latest_gate_passed={gate.get('passed')}, "
                f"latest_gate_status={gate.get('gate_status') or '<missing>'})"
            )
        lines.append("")
    lines.append("## Suggested Manual Submission Order")
    ordered = sorted(
        ready,
        key=lambda c: (
            float((sim_by_fp.get(c.get("fingerprint")) or {}).get("fitness") or 0),
            float((sim_by_fp.get(c.get("fingerprint")) or {}).get("sharpe") or 0),
        ),
        reverse=True,
    )
    if ordered:
        for idx, item in enumerate(ordered, start=1):
            sim = sim_by_fp.get(item.get("fingerprint")) or {}
            lines.append(
                f"{idx}. {item.get('alpha_id')} - fitness={sim.get('fitness')}, "
                f"sharpe={sim.get('sharpe')}, turnover={sim.get('turnover')}"
            )
    else:
        lines.append("No candidates are ready for manual submission.")
    lines.append("")
    lines.append("## Failure Diagnostics")
    if tag_counts:
        for tag, count in tag_counts.items():
            lines.append(f"- {tag}: {count}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("## Quota Waste Analysis")
    waste = _quota_waste_analysis(candidates, result["sim_results"], sim_by_fp)
    if waste["total_simulated"] > 0:
        lines.append(f"- total simulated: {waste['total_simulated']}")
        lines.append(f"- hard_failures: {waste['hard_failures']['count']} ({waste['hard_failures']['pct']}%)")
        lines.append(f"- low_sharpe (<0.3): {waste['low_sharpe']['count']} ({waste['low_sharpe']['pct']}%)")
        lines.append(f"- low_fitness (<0.25): {waste['low_fitness']['count']} ({waste['low_fitness']['pct']}%)")
        lines.append(f"- extreme_turnover (<0.01 or >0.70): {waste['extreme_turnover']['count']} ({waste['extreme_turnover']['pct']}%)")
        lines.append(f"- estimated_waste: {waste['wasted_count']} / {waste['total_simulated']} ({waste['estimated_waste_pct']}%)")
        if waste["estimated_waste_pct"] > 30:
            lines.append("- CAUTION: Waste rate exceeds 30%; tighten candidate pre-filtering or adjust generation constraints.")
    else:
        lines.append("- No simulation results to analyze.")
    lines.append("")
    lines.append("## Gate Incomplete Summary")
    if gate_incomplete_counts:
        for key, count in gate_incomplete_counts.items():
            lines.append(f"- {key}: {count}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("## Factor Thesis Summary")
    thesis_counts = _thesis_counts(candidates)
    if thesis_counts:
        for key, count in thesis_counts[:15]:
            lines.append(f"- {key}: {count}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("## Variant Search")
    if variant_comparisons:
        lines.append(f"- compared_lineages: {len(variant_comparisons)}")
        for item in variant_comparisons[:30]:
            lines.append(
                "- "
                f"parent={item.get('parent_candidate_id')} variant={item.get('variant_candidate_id')} "
                f"strategy={item.get('variant_strategy')} "
                f"delta_sharpe={item.get('delta_sharpe')} delta_fitness={item.get('delta_fitness')} "
                f"delta_turnover={item.get('delta_turnover')}"
            )
            lines.append(
                f"  original: sharpe={item.get('parent_sharpe')}, fitness={item.get('parent_fitness')}, "
                f"turnover={item.get('parent_turnover')}"
            )
            lines.append(
                f"  variant:  sharpe={item.get('variant_sharpe')}, fitness={item.get('variant_fitness')}, "
                f"turnover={item.get('variant_turnover')}"
            )
    else:
        lines.append("- No simulated variants were available for original-vs-variant comparison.")
    lines.append("")
    lines.append("## Variant Strategy Effectiveness")
    strategy_effectiveness = _variant_strategy_effectiveness(variant_comparisons)
    if strategy_effectiveness:
        lines.append(f"- distinct_strategies: {len(strategy_effectiveness)}")
        for item in strategy_effectiveness:
            pos = item.get("net_positive_count", 0)
            neg = item.get("net_negative_count", 0)
            lines.append(
                f"- {item['variant_strategy']}: count={item['count']}, "
                f"avg_delta_sharpe={item.get('avg_delta_sharpe')}, "
                f"avg_delta_fitness={item.get('avg_delta_fitness')}, "
                f"avg_delta_turnover={item.get('avg_delta_turnover')}, "
                f"net_positive={pos}, net_negative={neg}"
            )
    else:
        lines.append("- No simulated variant comparisons available.")
    lines.append("")
    lines.append("## Candidate Lifecycle")
    for item in candidates:
        sim = sim_by_fp.get(item.get("fingerprint")) or {}
        gate = gates_by_candidate.get(item.get("candidate_id")) or {}
        diagnosis = _json_obj(sim.get("diagnosis_json"))
        lines.append(f"### Candidate {item['candidate_id']} - {item.get('status')}")
        lines.append(f"- alpha_id: {item.get('alpha_id') or ''}")
        lines.append(f"- source: {item.get('source') or ''}")
        if item.get("parent_candidate_id"):
            lines.append(
                f"- lineage: parent_candidate_id={item.get('parent_candidate_id')}, "
                f"strategy={item.get('variant_strategy') or ''}, params={item.get('variant_params') or '{}'}"
            )
        thesis = _json_obj(item.get("thesis_json"))
        if thesis:
            lines.append(
                "- factor_thesis: "
                f"type={thesis.get('thesis_type') or ''}, "
                f"families={thesis.get('field_families') or []}, "
                f"themes={thesis.get('operator_themes') or []}"
            )
            if thesis.get("thesis_text"):
                lines.append(f"- thesis_text: {thesis.get('thesis_text')}")
            if thesis.get("expected_failure_modes"):
                lines.append(f"- expected_failure_modes: {thesis.get('expected_failure_modes')}")
            if thesis.get("intended_repair_methods"):
                lines.append(f"- intended_repair_methods: {thesis.get('intended_repair_methods')}")
        lines.append(f"- idea_file: {item.get('idea_file') or ''}")
        lines.append(f"- selection_score: {item.get('selection_score') or 0}")
        score_breakdown = _json_obj(item.get("score_breakdown"))
        if score_breakdown:
            lines.append(
                "- score_breakdown: "
                f"quality={score_breakdown.get('quality_score')}, "
                f"repairability={score_breakdown.get('repairability_score')}, "
                f"novelty={score_breakdown.get('novelty_score')}, "
                f"coverage={score_breakdown.get('coverage_score')}, "
                f"risk_penalty={score_breakdown.get('risk_penalty')}, "
                f"memory={score_breakdown.get('memory_score')}, "
                f"memory_risk={score_breakdown.get('memory_risk_penalty')}"
            )
        lines.append(
            f"- metrics: sharpe={sim.get('sharpe')}, fitness={sim.get('fitness')}, "
            f"turnover={sim.get('turnover')}, pnl={sim.get('pnl')}"
        )
        lines.append(f"- failure_tags: {sim.get('failure_tags') or ''}")
        lines.append(f"- repair_objectives: {sim.get('repair_objectives') or ''}")
        if diagnosis.get("diagnosis_reasons"):
            lines.append("- diagnosis_reasons:")
            for reason in diagnosis.get("diagnosis_reasons", []):
                lines.append(f"  - {reason}")
        if diagnosis.get("repair_hints"):
            lines.append("- repair_hints:")
            for hint in diagnosis.get("repair_hints", []):
                lines.append(f"  - {hint}")
        lines.append(
            f"- checks: submission={gate.get('submission_check') or ''}, "
            f"self_corr={gate.get('self_corr_check') or ''}, prod_corr={gate.get('prod_corr_check') or ''}, "
            f"subuniverse={_gate_check_value(gate, 'subuniverse_check', ('SUB_UNIVERSE_SHARPE', 'SUB_UNIVERSE', 'SUBUNIVERSE', 'LOW_SUB_UNIVERSE_SHARPE'))}, "
            f"two_year={_gate_check_value(gate, 'two_year_check', ('LOW_2Y_SHARPE', '2Y_SHARPE', 'TWO_YEAR_SHARPE', 'IS_2Y_SHARPE'))}"
        )
        lines.append(f"- lifecycle: generated_at={item.get('created_at')}, last_updated={item.get('updated_at')}")
        if sim:
            lines.append(f"- latest_sim: sim_id={sim.get('sim_id')}, created_at={sim.get('created_at')}, status={sim.get('status')}")
        if gate:
            lines.append(f"- latest_gate: created_at={gate.get('created_at')}, passed={gate.get('passed')}")
            if gate.get("gate_status") == "incomplete":
                lines.append(
                    f"- gate_incomplete: error_type={gate.get('error_type') or ''}, "
                    f"checks={gate.get('incomplete_checks') or ''}"
                )
        lines.append("")
        lines.append("```text")
        lines.append(str(item.get("expression") or ""))
        lines.append("```")
        lines.append("")
    lines.append("## Artifact Ledger")
    if result["artifacts"]:
        for artifact in result["artifacts"]:
            lines.append(
                f"- artifact_id={artifact.get('artifact_id')} stage={artifact.get('source_stage') or ''} "
                f"kind={artifact.get('kind')} created_at={artifact.get('created_at')} "
                f"path={artifact.get('path')}"
            )
    else:
        lines.append("- None")
    lines.append("")
    lines.append("## Task Ledger")
    if result["tasks"]:
        for task in result["tasks"]:
            lines.append(
                f"- task_id={task.get('task_id')} adapter={task.get('adapter')} status={task.get('status')} "
                f"pid={task.get('pid')} created_at={task.get('created_at')} updated_at={task.get('updated_at')}"
            )
            if task.get("stdout_path"):
                lines.append(f"  stdout: {task.get('stdout_path')}")
            if task.get("stderr_path"):
                lines.append(f"  stderr: {task.get('stderr_path')}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("## Next Research Recommendations")
    memory = AlphaMemory(memory_path_for_run_dir(run_dir))
    try:
        memory_context = memory.scoring_context(
            dataset=config.get("dataset"),
            region=config.get("region"),
            universe=config.get("universe"),
            delay=config.get("delay"),
            data_type=config.get("data_type"),
            neutralization=config.get("neutralization"),
        )
        recommendations = _next_research_recommendations(memory_context, config, candidates)
        for rec in recommendations:
            lines.append(f"- {rec}")
    finally:
        memory.close()
    lines.append("")
    lines.append("## Lessons And Next Steps")
    for item in _next_steps(result, ready, tag_counts):
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Reproduction")
    lines.append("- The full machine-readable run state is stored in `run_result.json` next to this report.")
    lines.append("- Artifacts listed above are the exact files used or produced by each stage.")
    lines.append("- No automatic submit is performed by this report.")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, json_path


def _latest_by_key(rows: list[dict[str, Any]], key: str) -> dict[Any, dict[str, Any]]:
    result: dict[Any, dict[str, Any]] = {}
    for row in rows:
        result[row.get(key)] = row
    return result


def _latest_gate_passed_candidates(
    candidates: list[dict[str, Any]],
    gates_by_candidate: dict[Any, dict[str, Any]],
) -> list[dict[str, Any]]:
    ready = []
    for candidate in candidates:
        if candidate.get("status") != "submit_ready":
            continue
        gate = gates_by_candidate.get(candidate.get("candidate_id")) or {}
        if int(gate.get("passed") or 0) != 1:
            continue
        if str(gate.get("gate_status") or "complete").lower() != "complete":
            continue
        if not _gate_hard_checks_pass(gate):
            continue
        ready.append(candidate)
    return ready


def _gate_hard_checks_pass(gate: dict[str, Any]) -> bool:
    return (
        _check_passed(_gate_check_value(gate, "submission_check", ()))
        and _check_passed(_gate_check_value(gate, "weight_check", ("WEIGHT", "WEIGHT_CONCENTRATION", "CONCENTRATED_WEIGHT")))
        and _check_passed(_gate_check_value(gate, "subuniverse_check", ("SUB_UNIVERSE_SHARPE", "SUB_UNIVERSE", "SUBUNIVERSE", "LOW_SUB_UNIVERSE_SHARPE")))
        and _check_passed(_gate_check_value(gate, "two_year_check", ("LOW_2Y_SHARPE", "2Y_SHARPE", "TWO_YEAR_SHARPE", "IS_2Y_SHARPE")))
    )


def _check_passed(value: Any) -> bool:
    return str(value or "").upper() in {"PASS", "PASSED"}


def _gate_check_value(gate: dict[str, Any], column: str, aliases: tuple[str, ...]) -> str:
    value = str(gate.get(column) or "")
    if value:
        return value.upper()
    raw = _json_obj(gate.get("raw_json"))
    checks = raw.get("checks") if isinstance(raw.get("checks"), list) else []
    by_test = {}
    for item in checks:
        if not isinstance(item, dict):
            continue
        test = str(item.get("test") or item.get("name") or "").upper()
        result = str(item.get("result") or "").upper()
        if test and result:
            by_test[test] = _worst_check_result(by_test.get(test), result)
    for alias in aliases:
        result = by_test.get(alias)
        if result:
            return result
    return "UNKNOWN"


def _worst_check_result(left: Any, right: Any) -> str:
    left_s = str(left or "").upper()
    right_s = str(right or "").upper()
    return right_s if _check_result_rank(right_s) > _check_result_rank(left_s) else left_s


def _check_result_rank(value: Any) -> int:
    result = str(value or "").upper()
    if result in {"FAIL", "FAILED"}:
        return 4
    if result == "ERROR":
        return 3
    if result in {"UNKNOWN", "PENDING", ""}:
        return 2
    if result in {"PASS", "PASSED"}:
        return 1
    return 2


def _research_summary(result: dict[str, Any], ready: list[dict[str, Any]], tag_counts: dict[str, int]) -> list[str]:
    metrics = result.get("prompt_metrics") if isinstance(result.get("prompt_metrics"), dict) else {}
    counts = result.get("counts") if isinstance(result.get("counts"), dict) else {}
    candidates = result.get("candidates") if isinstance(result.get("candidates"), list) else []
    top_tags = ", ".join(f"{k}={v}" for k, v in list(tag_counts.items())[:5]) or "none"
    return [
        f"Outcome: stage={result['run'].get('stage')}, stop_reason={result['run'].get('stop_reason') or ''}",
        f"Candidate funnel: total={len(candidates)}, promising={counts.get('promising', 0)}, needs_enhance={counts.get('needs_enhance', 0)}, submit_ready={len(ready)}",
        f"Prompt experiment score: {metrics.get('score') if metrics else 'n/a'}",
        f"Simulation quality: valid_rate={metrics.get('valid_rate') if metrics else 'n/a'}, sim_success_rate={metrics.get('sim_success_rate') if metrics else 'n/a'}, avg_sharpe={metrics.get('avg_sharpe') if metrics else 'n/a'}, avg_fitness={metrics.get('avg_fitness') if metrics else 'n/a'}",
        f"Top failure patterns: {top_tags}",
    ]


def _gate_incomplete_counts(gate_checks: list[dict[str, Any]]) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for gate in gate_checks:
        if str(gate.get("gate_status") or "").lower() != "incomplete":
            continue
        error_type = str(gate.get("error_type") or "gate_error")
        checks = str(gate.get("incomplete_checks") or "")
        if checks:
            for check in [part.strip() for part in checks.split(",") if part.strip()]:
                counts[f"{error_type}:{check}"] += 1
        else:
            counts[error_type] += 1
    return dict(counts.most_common())


def _timeline_events(result: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    run = result.get("run") if isinstance(result.get("run"), dict) else {}
    if run.get("created_at"):
        events.append(
            {
                "time": run.get("created_at"),
                "stage": "INIT",
                "kind": "run",
                "summary": "Run created.",
            }
        )
    for task in result.get("tasks", []) or []:
        events.append(
            {
                "time": task.get("created_at"),
                "stage": task.get("adapter") or "TASK",
                "kind": "task",
                "summary": f"{task.get('adapter')} task {task.get('task_id')} started with status={task.get('status')}",
            }
        )
        if task.get("updated_at") and task.get("updated_at") != task.get("created_at"):
            events.append(
                {
                    "time": task.get("updated_at"),
                    "stage": task.get("adapter") or "TASK",
                    "kind": "task_update",
                    "summary": f"{task.get('adapter')} task {task.get('task_id')} status={task.get('status')}",
                }
            )
    for artifact in result.get("artifacts", []) or []:
        events.append(
            {
                "time": artifact.get("created_at"),
                "stage": artifact.get("source_stage") or "ARTIFACT",
                "kind": "artifact",
                "summary": f"{artifact.get('kind')} -> {artifact.get('path')}",
            }
        )
    for sim in result.get("sim_results", []) or []:
        events.append(
            {
                "time": sim.get("created_at"),
                "stage": "SIMULATE",
                "kind": "sim_result",
                "summary": (
                    f"candidate={sim.get('candidate_id')} alpha={sim.get('alpha_id') or ''} "
                    f"status={sim.get('status')} sharpe={sim.get('sharpe')} fitness={sim.get('fitness')} "
                    f"turnover={sim.get('turnover')}"
                ),
            }
        )
    for decision in result.get("decisions", []) or []:
        events.append(
            {
                "time": decision.get("created_at"),
                "stage": "DECIDE",
                "kind": "decision",
                "summary": f"iteration={decision.get('iteration')} reason={decision.get('reason') or ''}",
            }
        )
    for gate in result.get("gate_checks", []) or []:
        events.append(
            {
                "time": gate.get("created_at"),
                "stage": "SUBMIT_GATE",
                "kind": "gate_check",
                "summary": f"candidate={gate.get('candidate_id')} alpha={gate.get('alpha_id')} passed={gate.get('passed')}",
            }
        )
    return sorted(events, key=lambda item: str(item.get("time") or ""))


def _next_steps(result: dict[str, Any], ready: list[dict[str, Any]], tag_counts: dict[str, int]) -> list[str]:
    steps: list[str] = []
    if ready:
        steps.append("Review submit-ready alpha IDs manually in the suggested submission order; this report did not submit anything.")
    else:
        steps.append("No submit-ready alpha was produced; continue with targeted enhance or a new generation run.")
    if tag_counts:
        most_common = next(iter(tag_counts))
        steps.append(f"Prioritize the top failure pattern next run: {most_common}.")
    if tag_counts.get("high_turnover") or tag_counts.get("elevated_turnover"):
        steps.append("For turnover issues, try repair objectives such as decay/hump/trade_when/target_tvr before broadening generation.")
    if tag_counts.get("low_fitness") or tag_counts.get("low_sharpe"):
        steps.append("For weak metrics, prefer cleaner preprocessing, peer-relative normalization, and field-family alternatives before adding complexity.")
    if tag_counts.get("unknown_variable") or tag_counts.get("syntax_error"):
        steps.append("For syntax issues, inspect final expressions and tighten placeholder/operator constraints before spending more simulation quota.")
    metrics = result.get("prompt_metrics") if isinstance(result.get("prompt_metrics"), dict) else {}
    if metrics.get("score") is not None:
        steps.append("Use `python3 -m brain_agent prompt compare` against comparable runs before promoting this prompt version.")
    if not result.get("decisions"):
        steps.append("No decision records were created; if this was a short run, consider increasing max_iterations to observe enhance behavior.")
    return steps


HARD_FAILURE_TAGS = {"hard_error", "syntax_error", "unknown_variable", "coverage_issue"}


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _next_research_recommendations(
    memory_context: dict[str, Any] | None,
    config: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> list[str]:
    if not memory_context:
        return ["Alpha memory not yet available (first run or empty database)."]
    recommendations: list[str] = []
    family_stats = memory_context.get("field_family_stats") if isinstance(memory_context.get("field_family_stats"), dict) else {}
    for family, info in sorted(
        family_stats.items(),
        key=lambda kv: (float(kv[1].get("confidence", 0)), float(kv[1].get("effective_observations", 0))),
        reverse=True,
    ):
        obs = int(info.get("observations", 0))
        conf = float(info.get("confidence", 0))
        sr = float(info.get("success_rate", 0))
        if obs < 3:
            continue
        if sr >= 0.6 and conf >= 0.3:
            recommendations.append(
                f"Continue investing in field_family={family}: success_rate={sr:.2f} "
                f"across {obs} observations (avg_sharpe={info.get('avg_sharpe')}, confidence={conf:.2f})"
            )
        elif sr <= 0.25 and conf >= 0.5:
            recommendations.append(
                f"Deprioritize field_family={family}: success_rate={sr:.2f} "
                f"across {obs} observations (too low to justify further investment)"
            )
    variant_stats = memory_context.get("variant_strategy_stats") if isinstance(memory_context.get("variant_strategy_stats"), dict) else {}
    for strategy, info in sorted(
        variant_stats.items(),
        key=lambda kv: (float(kv[1].get("confidence", 0)), float(kv[1].get("effective_observations", 0))),
        reverse=True,
    ):
        obs = int(info.get("observations", 0))
        conf = float(info.get("confidence", 0))
        sr = float(info.get("success_rate", 0))
        if obs < 3:
            continue
        if sr >= 0.6 and conf >= 0.3:
            recommendations.append(
                f"Continue applying variant_strategy={strategy}: success_rate={sr:.2f} "
                f"across {obs} applications"
            )
        elif sr <= 0.25 and conf >= 0.5:
            recommendations.append(
                f"Deprioritize variant_strategy={strategy}: success_rate={sr:.2f} "
                f"across {obs} applications"
            )
    if not recommendations:
        recommendations.append("Insufficient historical data for pattern-level recommendations; more runs needed.")
    return recommendations


def _variant_strategy_effectiveness(
    variant_comparisons: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for comp in variant_comparisons:
        strategy = str(comp.get("variant_strategy") or "unknown")
        groups[strategy].append(comp)
    results: list[dict[str, Any]] = []
    for strategy, items in sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True):
        delta_sharpes = [_float_or_none(comp.get("delta_sharpe")) for comp in items]
        delta_fitnesses = [_float_or_none(comp.get("delta_fitness")) for comp in items]
        delta_turnovers = [_float_or_none(comp.get("delta_turnover")) for comp in items]

        def _safe_avg(values: list[float | None]) -> float | None:
            valid = [v for v in values if v is not None and v != -999.0]
            return round(sum(valid) / len(valid), 4) if valid else None

        net_positive = sum(
            1 for comp in items
            if _float_or_none(comp.get("delta_fitness")) is not None
            and _float_or_none(comp.get("delta_fitness")) != -999.0
            and float(comp.get("delta_fitness") or 0) > 0
        )
        net_negative = sum(
            1 for comp in items
            if _float_or_none(comp.get("delta_fitness")) is not None
            and _float_or_none(comp.get("delta_fitness")) != -999.0
            and float(comp.get("delta_fitness") or 0) <= 0
        )
        results.append({
            "variant_strategy": strategy,
            "count": len(items),
            "avg_delta_sharpe": _safe_avg(delta_sharpes),
            "avg_delta_fitness": _safe_avg(delta_fitnesses),
            "avg_delta_turnover": _safe_avg(delta_turnovers),
            "net_positive_count": net_positive,
            "net_negative_count": net_negative,
        })
    return sorted(results, key=lambda r: (float(r.get("avg_delta_fitness") or -999), r["count"]), reverse=True)


def _quota_waste_analysis(
    candidates: list[dict[str, Any]],
    sim_results: list[dict[str, Any]],
    sim_by_fp: dict[Any, dict[str, Any]],
) -> dict[str, Any]:
    if not sim_results:
        return {"total_simulated": 0, "estimated_waste_pct": 0.0, "wasted_count": 0}
    simulated = list(sim_by_fp.values())
    total = len(simulated)
    if total == 0:
        return {"total_simulated": 0, "estimated_waste_pct": 0.0, "wasted_count": 0}
    hard_failures = 0
    low_sharpe = 0
    low_fitness = 0
    extreme_turnover = 0
    wasted_ids: set[str] = set()
    for sim in simulated:
        sim_fp = str(sim.get("fingerprint") or sim.get("simulation_fingerprint") or "")
        tags = [t.strip() for t in str(sim.get("failure_tags") or "").split(",") if t.strip()]
        sharpe = _float_or_none(sim.get("sharpe"))
        fitness = _float_or_none(sim.get("fitness"))
        turnover = _float_or_none(sim.get("turnover"))
        is_hard_failure = any(t in HARD_FAILURE_TAGS for t in tags)
        if is_hard_failure:
            hard_failures += 1
            wasted_ids.add(sim_fp or str(id(sim)))
        if sharpe is not None and sharpe < 0.3:
            low_sharpe += 1
            wasted_ids.add(sim_fp or str(id(sim)))
        if fitness is not None and fitness < 0.25:
            low_fitness += 1
            wasted_ids.add(sim_fp or str(id(sim)))
        if turnover is not None and (turnover < 0.01 or turnover > 0.70):
            extreme_turnover += 1
            wasted_ids.add(sim_fp or str(id(sim)))
    wasted_count = len(wasted_ids)
    estimated_waste_pct = round(wasted_count / total * 100, 1) if total else 0.0
    return {
        "total_simulated": total,
        "hard_failures": {"count": hard_failures, "pct": round(hard_failures / total * 100, 1) if total else 0.0},
        "low_sharpe": {"count": low_sharpe, "pct": round(low_sharpe / total * 100, 1) if total else 0.0},
        "low_fitness": {"count": low_fitness, "pct": round(low_fitness / total * 100, 1) if total else 0.0},
        "extreme_turnover": {"count": extreme_turnover, "pct": round(extreme_turnover / total * 100, 1) if total else 0.0},
        "estimated_waste_pct": estimated_waste_pct,
        "wasted_count": wasted_count,
    }


def _variant_comparisons(candidates: list[dict[str, Any]], sim_results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    candidates_by_id = {int(row.get("candidate_id") or 0): row for row in candidates if row.get("candidate_id")}
    sim_by_candidate = _latest_by_key(sim_results, "candidate_id")
    comparisons: list[dict[str, Any]] = []
    for variant in candidates:
        parent_id = int(variant.get("parent_candidate_id") or 0)
        variant_id = int(variant.get("candidate_id") or 0)
        if not parent_id or not variant_id:
            continue
        parent = candidates_by_id.get(parent_id)
        parent_sim = sim_by_candidate.get(parent_id) or {}
        variant_sim = sim_by_candidate.get(variant_id) or {}
        if not parent or not parent_sim or not variant_sim:
            continue
        comparisons.append(
            {
                "parent_candidate_id": parent_id,
                "variant_candidate_id": variant_id,
                "variant_strategy": variant.get("variant_strategy") or "",
                "variant_params": variant.get("variant_params") or "{}",
                "parent_sharpe": parent_sim.get("sharpe"),
                "parent_fitness": parent_sim.get("fitness"),
                "parent_turnover": parent_sim.get("turnover"),
                "variant_sharpe": variant_sim.get("sharpe"),
                "variant_fitness": variant_sim.get("fitness"),
                "variant_turnover": variant_sim.get("turnover"),
                "delta_sharpe": _delta(variant_sim.get("sharpe"), parent_sim.get("sharpe")),
                "delta_fitness": _delta(variant_sim.get("fitness"), parent_sim.get("fitness")),
                "delta_turnover": _delta(variant_sim.get("turnover"), parent_sim.get("turnover")),
                "parent_expression": parent.get("expression") or "",
                "variant_expression": variant.get("expression") or "",
            }
        )
    return sorted(
        comparisons,
        key=lambda row: (
            _float(row.get("delta_fitness")),
            _float(row.get("delta_sharpe")),
        ),
        reverse=True,
    )


def _thesis_counts(candidates: list[dict[str, Any]]) -> list[tuple[str, int]]:
    counter: Counter[str] = Counter()
    for candidate in candidates:
        thesis = _json_obj(candidate.get("thesis_json"))
        thesis_type = str(thesis.get("thesis_type") or "").strip()
        if thesis_type:
            counter[thesis_type] += 1
    return counter.most_common()


def _json_obj_or_list(raw: Any) -> dict[str, Any] | list[Any] | None:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw or ""))
    except json.JSONDecodeError:
        return None


def _json_list(raw: Any) -> list[dict[str, Any]]:
    parsed = _json_obj_or_list(raw)
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []


def _json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _delta(new_value: Any, old_value: Any) -> float | None:
    if new_value is None or old_value is None:
        return None
    try:
        return round(float(new_value) - float(old_value), 4)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float:
    try:
        if value is None:
            return -999.0
        return float(value)
    except (TypeError, ValueError):
        return -999.0
