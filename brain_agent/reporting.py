from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .diagnostics import summarize_failure_tags
from .memory import AlphaMemory, memory_path_for_run_dir
from .prompting import summarize_run_prompt_metrics
from .repository import Repository
from .selection import summarize_counts
from .utils import write_json


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

    config = result["config"]
    candidates = result["candidates"]
    sim_by_fp = _latest_by_key(result["sim_results"], "fingerprint")
    gates_by_candidate = _latest_by_key(result["gate_checks"], "candidate_id")
    ready = [c for c in candidates if c.get("status") == "submit_ready"]
    tag_counts = summarize_failure_tags(result["sim_results"])
    research_summary = _research_summary(result, ready, tag_counts)

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
            lines.append(f"- {item.get('alpha_id') or '<missing alpha_id>'} (candidate_id={item['candidate_id']})")
    else:
        lines.append("- None")
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
    lines.append("## Candidate Lifecycle")
    for item in candidates:
        sim = sim_by_fp.get(item.get("fingerprint")) or {}
        gate = gates_by_candidate.get(item.get("candidate_id")) or {}
        diagnosis = _json_obj(sim.get("diagnosis_json"))
        lines.append(f"### Candidate {item['candidate_id']} - {item.get('status')}")
        lines.append(f"- alpha_id: {item.get('alpha_id') or ''}")
        lines.append(f"- source: {item.get('source') or ''}")
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
            f"self_corr={gate.get('self_corr_check') or ''}, prod_corr={gate.get('prod_corr_check') or ''}"
        )
        lines.append(f"- lifecycle: generated_at={item.get('created_at')}, last_updated={item.get('updated_at')}")
        if sim:
            lines.append(f"- latest_sim: sim_id={sim.get('sim_id')}, created_at={sim.get('created_at')}, status={sim.get('status')}")
        if gate:
            lines.append(f"- latest_gate: created_at={gate.get('created_at')}, passed={gate.get('passed')}")
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
    lines.append("## Lessons And Next Steps")
    for item in _next_steps(result, ready, tag_counts):
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Reproduction")
    lines.append("- The full machine-readable run state is stored in `run_result.json` next to this report.")
    lines.append("- Artifacts listed above are the exact files used or produced by each stage.")
    lines.append("- No automatic submit is performed by this report.")

    md_path.write_text("\n".join(lines), encoding="utf-8")
    memory = AlphaMemory(memory_path_for_run_dir(run_dir))
    try:
        memory.ingest_run(repo, run_id)
    finally:
        memory.close()
    return md_path, json_path


def _latest_by_key(rows: list[dict[str, Any]], key: str) -> dict[Any, dict[str, Any]]:
    result: dict[Any, dict[str, Any]] = {}
    for row in rows:
        result[row.get(key)] = row
    return result


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
