from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

from .adapters import BatchSimAdapter, InspectRawTemplateAdapter, MakeSomeGemAdapter, SubmissionGateAdapter, copy_artifact_to_run
from .controller import BatchLoopController
from .credentials import load_credentials
from .forum import DEFAULT_DAILY_LEARNING_QUERIES, ForumService, render_markdown
from .knowledge import approve_forum_lesson
from .memory import AlphaMemory, render_memory_summary_markdown
from .models import CandidateStatus, RunConfig
from .prompting import (
    compare_prompt_runs,
    render_prompt_compare_markdown,
    summarize_run_prompt_metrics,
    write_prompt_compare_report,
)
from .progress import build_simulation_progress, render_simulation_progress
from .quality import build_research_quality_summary, render_research_quality_markdown
from .reporting import build_run_result, write_report
from .repository import Repository
from .runtime import ensure_runtime, get_runtime_paths, new_run_id
from .settings_presets import (
    SETTING_KEYS,
    find_settings_preset,
    load_settings_presets,
    prompt_dataset,
    render_preset_detail,
    render_settings_presets,
    parse_bool as parse_settings_bool,
    select_settings_preset,
    settings_command_fragment,
)
from .task_runner import TaskRunner
from .utils import write_json
from .worker import SimulationWorker


def _simulation_slot_policy(region: str) -> dict[str, int]:
    """Return the default BRAIN simulation slot policy for a region."""
    region_norm = str(region or "").upper()
    concurrency = 4 if region_norm == "GLB" else 8
    batch_size = 4 if region_norm == "GLB" else 10
    return {
        "batch_size": batch_size,
        "concurrency": concurrency,
        "capacity": batch_size * concurrency,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m brain_agent")
    parser.add_argument("--runtime-root", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run")
    _add_config_args(run_p)
    run_p.add_argument("--run-id")
    run_p.add_argument("--preset", default=None, help="Named dataset/settings preset")
    run_p.add_argument("--choose-settings", action="store_true", help="Interactively choose a dataset/settings preset")
    run_p.add_argument("--dry-run", action="store_true")

    status_p = sub.add_parser("status")
    status_p.add_argument("--run-id", required=True)

    resume_p = sub.add_parser("resume")
    resume_p.add_argument("--run-id", required=True)

    retry_sim_p = sub.add_parser("retry-sim")
    retry_sim_p.add_argument("--run-id", required=True)
    retry_sim_p.add_argument("--limit", type=int, default=None, help="Maximum retryable candidates to resimulate")
    retry_sim_p.add_argument("--batch-size", type=int, default=None)
    retry_sim_p.add_argument("--concurrency", type=int, default=None)
    retry_sim_p.add_argument("--dry-run", action="store_true", help="Write retry alpha_list and print summary without submitting")

    worker_p = sub.add_parser("worker")
    worker_p.add_argument("--run-id", required=True)
    worker_p.add_argument("--mode", choices=["drain", "once"], default="drain")
    worker_p.add_argument("--idle-sleep-seconds", type=int, default=60, help="Poll interval when queue is empty")
    worker_p.add_argument("--max-runtime-hours", type=float, default=None, help="Auto-shutdown after N hours")
    worker_p.add_argument("--max-batches", type=int, default=None, help="Stop after N batches")
    worker_p.add_argument("--max-total-alphas", type=int, default=5000, help="Stop after submitting N alphas total (default 5000, the BRAIN daily limit)")
    worker_p.add_argument("--max-retries", type=int, default=3, help="Max retries per candidate before marking SIM_FAILED")
    worker_p.add_argument("--batch-candidates-limit", type=int, default=0, help="Max candidates per batch (0 = batch_size * concurrency)")
    worker_p.add_argument("--batch-size", type=int, default=None, help="Override batch_size from run config")
    worker_p.add_argument("--concurrency", type=int, default=None, help="Override concurrency from run config")
    worker_p.add_argument("--refill-on-empty", action="store_true", help="Generate and inspect new candidates when the simulation queue is empty")
    worker_p.add_argument("--max-empty-refills", type=int, default=3, help="Maximum empty-queue refill attempts in drain mode; 0 means unlimited")

    report_p = sub.add_parser("report")
    report_p.add_argument("--run-id", required=True)

    export_p = sub.add_parser("export")
    export_p.add_argument("--run-id", required=True)
    export_p.add_argument("--format", choices=["json"], default="json")

    gate_p = sub.add_parser("gate")
    gate_p.add_argument("--run-id", required=True)
    gate_p.add_argument("--dry-run", action="store_true", help="Use local deterministic mock checks")

    tasks_p = sub.add_parser("tasks")
    tasks_p.add_argument("--run-id", required=True)
    tasks_p.add_argument("--task-id")
    tasks_p.add_argument("--refresh", action="store_true")
    tasks_p.add_argument("--cancel", action="store_true")
    tasks_p.add_argument("--retry", action="store_true")

    doctor_p = sub.add_parser("doctor")
    doctor_p.add_argument("--check-llm", action="store_true")

    forum_p = sub.add_parser("forum")
    forum_p.add_argument("--secret-path", default=None)
    forum_sub = forum_p.add_subparsers(dest="forum_command", required=True)

    forum_search = forum_sub.add_parser("search")
    forum_search.add_argument("query")
    forum_search.add_argument("--max-results", type=int, default=50)
    forum_search.add_argument("--locale", default="zh-cn")
    forum_search.add_argument("--format", choices=["json", "markdown"], default="json")

    forum_read = forum_sub.add_parser("read")
    forum_read.add_argument("post", help="Forum post URL or article/post ID")
    forum_read.add_argument("--no-comments", action="store_true")
    forum_read.add_argument("--format", choices=["json", "markdown"], default="json")

    forum_glossary = forum_sub.add_parser("glossary")
    forum_glossary.add_argument("--format", choices=["json", "markdown"], default="json")

    forum_learn = forum_sub.add_parser("learn")
    forum_learn.add_argument("query")
    forum_learn.add_argument("--max-results", type=int, default=10)
    forum_learn.add_argument("--read-top", type=int, default=3)
    forum_learn.add_argument("--locale", default="zh-cn")
    forum_learn.add_argument("--no-comments", action="store_true")
    forum_learn.add_argument("--format", choices=["json", "markdown"], default="markdown")
    forum_learn.add_argument("--output", default=None, help="Optional path to save the learning report")

    forum_daily = forum_sub.add_parser("daily-learn")
    forum_daily.add_argument("--query", action="append", dest="queries", help="Search query; can be passed multiple times")
    forum_daily.add_argument("--max-results-per-query", type=int, default=12)
    forum_daily.add_argument("--read-top", type=int, default=5)
    forum_daily.add_argument("--locale", default="zh-cn")
    forum_daily.add_argument("--no-comments", action="store_true")
    forum_daily.add_argument("--format", choices=["json", "markdown"], default="markdown")
    forum_daily.add_argument("--output", default=None, help="Optional path to save the daily learning report")
    forum_daily.add_argument(
        "--output-dir",
        default=None,
        help="Directory for generated reports when --output is omitted; defaults to <runtime-root>/forum_learning",
    )
    forum_daily.add_argument(
        "--history-path",
        default=None,
        help="JSONL file tracking previously read posts; defaults to <runtime-root>/forum_learning/read_history.jsonl",
    )
    forum_daily.add_argument("--reread-after-days", type=int, default=14)

    knowledge_p = sub.add_parser("knowledge")
    knowledge_sub = knowledge_p.add_subparsers(dest="knowledge_command", required=True)
    approve_forum = knowledge_sub.add_parser("approve-forum-lesson")
    approve_forum.add_argument("--report", required=True, help="Path to a forum learn JSON/Markdown report")
    approve_forum.add_argument("--title", default=None, help="Exact proposed update title to approve")
    approve_forum.add_argument("--approve-all", action="store_true", help="Approve all proposed updates in the report")
    approve_forum.add_argument("--approved-by", default="user")
    approve_forum.add_argument("--notes", default="")
    approve_forum.add_argument("--knowledge-dir", default=None)

    prompt_p = sub.add_parser("prompt")
    prompt_sub = prompt_p.add_subparsers(dest="prompt_command", required=True)
    prompt_compare = prompt_sub.add_parser("compare")
    prompt_compare.add_argument("--run-id", action="append", dest="run_ids", required=True, help="Compare runs; the first run id is treated as the baseline/control")
    prompt_compare.add_argument("--format", choices=["json", "markdown"], default="markdown")
    prompt_compare.add_argument("--output", default=None)

    memory_p = sub.add_parser("memory")
    memory_sub = memory_p.add_subparsers(dest="memory_command", required=True)
    memory_ingest = memory_sub.add_parser("ingest")
    memory_ingest.add_argument("--run-id", required=True)
    memory_summary = memory_sub.add_parser("summary")
    memory_summary.add_argument("--dataset", default=None)
    memory_summary.add_argument("--region", default=None)
    memory_summary.add_argument("--limit", type=int, default=10)
    memory_summary.add_argument("--format", choices=["json", "markdown"], default="markdown")

    research_p = sub.add_parser("research")
    research_sub = research_p.add_subparsers(dest="research_command", required=True)
    research_summary = research_sub.add_parser("summary")
    research_summary.add_argument("--run-id", action="append", dest="run_ids")
    research_summary.add_argument("--dataset", default=None)
    research_summary.add_argument("--region", default=None)
    research_summary.add_argument("--limit", type=int, default=20)
    research_summary.add_argument("--format", choices=["json", "markdown"], default="markdown")
    research_summary.add_argument("--output", default=None)

    settings_p = sub.add_parser("settings")
    settings_sub = settings_p.add_subparsers(dest="settings_command", required=True)
    settings_list = settings_sub.add_parser("list")
    settings_list.add_argument("--format", choices=["table", "json"], default="table")
    settings_show = settings_sub.add_parser("show")
    settings_show.add_argument("--preset", required=True)
    settings_show.add_argument("--format", choices=["text", "json"], default="text")
    settings_choose = settings_sub.add_parser("choose")
    settings_choose.add_argument("--print-command", action="store_true", help="Print a run command using the chosen preset")

    parse_p = sub.add_parser("parse-artifact")
    parse_p.add_argument("--run-id", required=True)
    parse_p.add_argument("--kind", choices=["final_expressions", "alpha_list", "simulation_status"], required=True)
    parse_p.add_argument("--path", required=True)

    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "resume":
        return cmd_resume(args)
    if args.command == "retry-sim":
        return cmd_retry_sim(args)
    if args.command == "worker":
        return cmd_worker(args)
    if args.command == "report":
        return cmd_report(args)
    if args.command == "export":
        return cmd_export(args)
    if args.command == "gate":
        return cmd_gate(args)
    if args.command == "tasks":
        return cmd_tasks(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "forum":
        return cmd_forum(args)
    if args.command == "knowledge":
        return cmd_knowledge(args)
    if args.command == "prompt":
        return cmd_prompt(args)
    if args.command == "memory":
        return cmd_memory(args)
    if args.command == "research":
        return cmd_research(args)
    if args.command == "settings":
        return cmd_settings(args)
    if args.command == "parse-artifact":
        return cmd_parse_artifact(args)
    return 2


def _add_config_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset")
    parser.add_argument("--region")
    parser.add_argument("--delay", type=int)
    parser.add_argument("--universe")
    parser.add_argument("--data-type")
    parser.add_argument("--decay", type=int)
    parser.add_argument("--truncation", type=float)
    parser.add_argument("--neutralization")
    parser.add_argument("--max-trade", type=_parse_bool)
    parser.add_argument("--target-ready", type=int, default=4)
    parser.add_argument("--max-iterations", type=int, default=6)
    parser.add_argument("--batch-size", type=int, default=None)
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--max-fields", type=int, default=None)
    parser.add_argument("--max-operators", type=int, default=None)
    parser.add_argument("--max-sim-alphas", type=int, default=None)
    parser.add_argument("--max-variant-alphas", type=int, default=20)
    parser.add_argument("--max-variants-per-alpha", type=int, default=4)
    parser.add_argument("--use-llm-decide", action="store_true")
    parser.add_argument("--max-enhance-actions", type=int, default=4)
    parser.add_argument("--make-prompt-version", default="make-v1")
    parser.add_argument("--enhance-prompt-version", default="enhance-v1")
    parser.add_argument("--decision-prompt-version", default="decision-v1")
    parser.add_argument("--prompt-experiment", default="")


def _parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"Expected boolean value, got {value!r}")


def cmd_run(args: argparse.Namespace) -> int:
    try:
        settings = _resolve_run_settings(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    region = str(settings["region"]).upper()
    slot_policy = _simulation_slot_policy(region)
    config = RunConfig(
        dataset=settings["dataset"],
        region=region,
        delay=settings["delay"],
        universe=settings["universe"],
        data_type=str(settings["data_type"]).upper(),
        decay=settings["decay"],
        truncation=settings["truncation"],
        neutralization=str(settings["neutralization"]).upper(),
        max_trade=bool(settings["max_trade"]),
        target_ready=args.target_ready,
        max_iterations=args.max_iterations,
        batch_size=args.batch_size if args.batch_size is not None else slot_policy["batch_size"],
        concurrency=args.concurrency if args.concurrency is not None else slot_policy["concurrency"],
        dry_run=args.dry_run,
        max_fields=args.max_fields,
        max_operators=args.max_operators,
        max_sim_alphas=args.max_sim_alphas,
        max_variant_alphas=args.max_variant_alphas,
        max_variants_per_alpha=args.max_variants_per_alpha,
        use_llm_decide=args.use_llm_decide,
        max_enhance_actions=args.max_enhance_actions,
        make_prompt_version=args.make_prompt_version,
        enhance_prompt_version=args.enhance_prompt_version,
        decision_prompt_version=args.decision_prompt_version,
        prompt_experiment=args.prompt_experiment,
    )
    run_id = args.run_id or new_run_id(config.dataset, config.region)
    paths = get_runtime_paths(run_id, args.runtime_root)
    ensure_runtime(paths)
    repo = Repository(paths.db_path)
    try:
        if repo.get_run(run_id):
            print(f"Run already exists: {run_id}", file=sys.stderr)
            return 1
        repo.create_run(run_id, config)
        reason = BatchLoopController(repo, run_id, paths).run(config)
        print(f"run_id: {run_id}")
        print(f"run_dir: {paths.run_dir}")
        print(f"stop_reason: {reason}")
        return 0
    except NotImplementedError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        repo.close()


def _resolve_run_settings(args: argparse.Namespace) -> dict[str, object]:
    settings: dict[str, object] = {}
    if args.preset and args.choose_settings:
        raise ValueError("Use either --preset or --choose-settings, not both.")
    if args.choose_settings:
        preset = select_settings_preset(args.runtime_root)
        settings.update(preset.normalized_settings())
        if not args.dataset and "dataset" not in settings:
            settings["dataset"] = prompt_dataset()
    elif args.preset:
        settings.update(find_settings_preset(args.preset, args.runtime_root).normalized_settings())

    cli_values = {
        "dataset": args.dataset,
        "region": args.region,
        "delay": args.delay,
        "universe": args.universe,
        "data_type": args.data_type,
        "decay": args.decay,
        "truncation": args.truncation,
        "neutralization": args.neutralization,
        "max_trade": args.max_trade,
    }
    for key, value in cli_values.items():
        if value is not None:
            settings[key] = value

    settings.setdefault("decay", 10)
    settings.setdefault("truncation", 0.08)
    settings.setdefault("neutralization", "INDUSTRY")
    settings.setdefault("max_trade", False)

    missing = [key for key in SETTING_KEYS if key not in settings or settings[key] in {None, ""}]
    if missing:
        missing_flags = ", ".join("--" + key.replace("_", "-") for key in missing)
        raise ValueError(
            f"Missing settings: {missing_flags}. Use --dataset with --preset, use --choose-settings, or pass the settings explicitly."
        )

    settings["dataset"] = str(settings["dataset"])
    settings["region"] = str(settings["region"]).upper()
    settings["delay"] = int(settings["delay"])
    settings["universe"] = str(settings["universe"]).upper()
    settings["data_type"] = str(settings["data_type"]).upper()
    settings["decay"] = int(settings["decay"])
    settings["truncation"] = float(settings["truncation"])
    settings["neutralization"] = str(settings["neutralization"]).upper()
    settings["max_trade"] = parse_settings_bool(settings["max_trade"])
    return settings


def cmd_status(args: argparse.Namespace) -> int:
    paths = get_runtime_paths(args.run_id, args.runtime_root)
    repo = Repository(paths.db_path)
    try:
        run = repo.get_run(args.run_id)
        if not run:
            print(f"Run not found: {args.run_id}", file=sys.stderr)
            return 1
        progress = build_simulation_progress(repo, args.run_id, paths.run_dir)
        payload = {
                "run_id": args.run_id,
                "stage": run["stage"],
                "stop_reason": run["stop_reason"],
                "counts": {
                    "candidates": repo.count_candidates(args.run_id),
                    "submit_ready": repo.count_candidates(args.run_id, "submit_ready"),
                    "promising": repo.count_candidates(args.run_id, "promising"),
                    "needs_enhance": repo.count_candidates(args.run_id, "needs_enhance"),
                },
                "run_dir": str(paths.run_dir),
                "simulation": progress,
            }
        worker_stats_path = paths.run_dir / "worker_stats" / "worker_stats.json"
        if worker_stats_path.exists():
            try:
                payload["worker_stats"] = json.loads(worker_stats_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        print(json.dumps(
            payload,
            ensure_ascii=False,
            indent=2,
        ))
        return 0
    finally:
        repo.close()


def cmd_resume(args: argparse.Namespace) -> int:
    paths = get_runtime_paths(args.run_id, args.runtime_root)
    ensure_runtime(paths)
    repo = Repository(paths.db_path)
    try:
        reason = BatchLoopController(repo, args.run_id, paths).resume()
        print(f"run_id: {args.run_id}")
        print(f"stop_reason: {reason}")
        return 0
    finally:
        repo.close()


def cmd_retry_sim(args: argparse.Namespace) -> int:
    paths = get_runtime_paths(args.run_id, args.runtime_root)
    ensure_runtime(paths)
    repo = Repository(paths.db_path)
    try:
        run = repo.get_run(args.run_id)
        if not run:
            print(f"Run not found: {args.run_id}", file=sys.stderr)
            return 1
        config = RunConfig(**json.loads(run["config_json"]))
        if args.batch_size is not None or args.concurrency is not None:
            config = replace(
                config,
                batch_size=args.batch_size if args.batch_size is not None else config.batch_size,
                concurrency=args.concurrency if args.concurrency is not None else config.concurrency,
            )
        candidates = [
            row
            for row in repo.list_rows("candidates", args.run_id)
            if row.get("status") == CandidateStatus.SIM_RETRYABLE.value
        ]
        candidates.sort(key=lambda row: (float(row.get("selection_score") or 0), int(row.get("candidate_id") or 0)), reverse=True)
        if args.limit is not None:
            candidates = candidates[: max(0, int(args.limit))]
        inspect = InspectRawTemplateAdapter(repo, args.run_id, paths.run_dir)
        alpha_list = inspect.write_alpha_list_for_candidates(
            candidates,
            config,
            name="alpha_list_retryable.json",
        )
        payload = {
            "run_id": args.run_id,
            "retryable_count": len(candidates),
            "alpha_list": str(alpha_list),
            "submitted": False,
        }
        if candidates and not args.dry_run:
            retry_config = replace(config, max_sim_alphas=None)
            result = BatchSimAdapter(repo, args.run_id, paths.run_dir).run_real(alpha_list, retry_config)
            payload["submitted"] = result.status == "ok"
            payload["status"] = result.status
            payload["error_summary"] = result.error_summary
            payload["metrics_count"] = len(result.metrics_delta)
            if result.status != "ok":
                print(json.dumps(payload, ensure_ascii=False, indent=2))
                return 1
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    finally:
        repo.close()


def cmd_worker(args: argparse.Namespace) -> int:
    paths = get_runtime_paths(args.run_id, args.runtime_root)
    ensure_runtime(paths)
    repo = Repository(paths.db_path)
    try:
        run = repo.get_run(args.run_id)
        if not run:
            print(f"Run not found: {args.run_id}", file=sys.stderr)
            return 1
        config = RunConfig(**json.loads(run["config_json"]))
        slot_policy = _simulation_slot_policy(config.region)
        if args.batch_size is not None:
            config = replace(config, batch_size=args.batch_size)
        elif config.batch_size != slot_policy["batch_size"]:
            config = replace(config, batch_size=slot_policy["batch_size"])
        if args.concurrency is not None:
            config = replace(config, concurrency=args.concurrency)
        elif config.concurrency != slot_policy["concurrency"]:
            config = replace(config, concurrency=slot_policy["concurrency"])
        config = replace(config, max_sim_alphas=None)

        limit = int(args.batch_candidates_limit)
        if limit <= 0:
            limit = max(config.batch_size * config.concurrency, 1)

        worker = SimulationWorker(repo, args.run_id, paths, config)

        if args.mode == "once":
            stats = worker.run_once(
                max_retries=args.max_retries,
                max_candidates_per_batch=limit,
                refill_on_empty=args.refill_on_empty,
            )
        else:
            max_empty_refills = None if int(args.max_empty_refills) <= 0 else int(args.max_empty_refills)
            stats = worker.run_drain(
                idle_sleep=args.idle_sleep_seconds,
                max_runtime_hours=args.max_runtime_hours,
                max_batches=args.max_batches,
                max_total_alphas=args.max_total_alphas,
                max_retries=args.max_retries,
                max_candidates_per_batch=limit,
                refill_on_empty=args.refill_on_empty,
                max_empty_refills=max_empty_refills,
            )

        if stats.total_submitted == 0:
            return 0
        return 0 if stats.total_succeeded > 0 else 1
    finally:
        repo.close()


def cmd_report(args: argparse.Namespace) -> int:
    paths = get_runtime_paths(args.run_id, args.runtime_root)
    repo = Repository(paths.db_path)
    try:
        md_path, json_path = write_report(repo, args.run_id, paths.run_dir)
        print(f"report: {md_path}")
        print(f"result: {json_path}")
        return 0
    finally:
        repo.close()


def cmd_export(args: argparse.Namespace) -> int:
    paths = get_runtime_paths(args.run_id, args.runtime_root)
    repo = Repository(paths.db_path)
    try:
        result = build_run_result(repo, args.run_id)
        out = paths.run_dir / "export.json"
        write_json(out, result)
        print(out)
        return 0
    finally:
        repo.close()


def cmd_gate(args: argparse.Namespace) -> int:
    paths = get_runtime_paths(args.run_id, args.runtime_root)
    repo = Repository(paths.db_path)
    try:
        if not repo.get_run(args.run_id):
            print(f"Run not found: {args.run_id}", file=sys.stderr)
            return 1
        adapter = SubmissionGateAdapter(repo, args.run_id, paths.run_dir)
        result = adapter.dry_run() if args.dry_run else adapter.run_real()
        if result.status != "ok":
            print(result.error_summary, file=sys.stderr)
            return 1
        write_report(repo, args.run_id, paths.run_dir)
        print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
        return 0
    finally:
        repo.close()


def cmd_parse_artifact(args: argparse.Namespace) -> int:
    paths = get_runtime_paths(args.run_id, args.runtime_root)
    ensure_runtime(paths)
    repo = Repository(paths.db_path)
    try:
        if not repo.get_run(args.run_id):
            print(f"Run not found: {args.run_id}", file=sys.stderr)
            return 1
        src = Path(args.path).expanduser().resolve()
        if args.kind == "final_expressions":
            dest = copy_artifact_to_run(src, paths.artifacts_dir / "imported" / "generate")
            result = MakeSomeGemAdapter(repo, args.run_id, paths.run_dir).parse_final_expressions(dest)
        elif args.kind == "alpha_list":
            dest = copy_artifact_to_run(src, paths.artifacts_dir / "imported" / "inspect")
            result = InspectRawTemplateAdapter(repo, args.run_id, paths.run_dir).parse_alpha_list(dest)
        else:
            dest = copy_artifact_to_run(src, paths.artifacts_dir / "imported" / "simulate")
            result = BatchSimAdapter(repo, args.run_id, paths.run_dir).parse_simulation_status(dest)
        print(json.dumps(result.__dict__, ensure_ascii=False, indent=2))
        return 0 if result.status == "ok" else 1
    finally:
        repo.close()


def cmd_forum(args: argparse.Namespace) -> int:
    import asyncio

    async def _run() -> dict:
        service = ForumService(secret_path=args.secret_path)
        if args.forum_command == "search":
            return await service.search(args.query, max_results=args.max_results, locale=args.locale)
        if args.forum_command == "read":
            return await service.read(args.post, include_comments=not args.no_comments)
        if args.forum_command == "glossary":
            return await service.glossary()
        if args.forum_command == "learn":
            return await service.learn(
                args.query,
                max_results=args.max_results,
                read_top=args.read_top,
                locale=args.locale,
                include_comments=not args.no_comments,
            )
        if args.forum_command == "daily-learn":
            return await service.daily_learn(
                queries=args.queries or DEFAULT_DAILY_LEARNING_QUERIES,
                max_results_per_query=args.max_results_per_query,
                read_top=args.read_top,
                locale=args.locale,
                include_comments=not args.no_comments,
                history_path=_daily_forum_history_path(args),
                reread_after_days=args.reread_after_days,
            )
        raise ValueError(f"Unknown forum command: {args.forum_command}")

    try:
        payload = asyncio.run(_run())
    except Exception as exc:
        print(f"forum {args.forum_command} failed: {exc}", file=sys.stderr)
        return 1
    if args.format == "markdown":
        text = render_markdown(payload, title=f"BRAIN Forum {args.forum_command.title()}")
    else:
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    output_path = getattr(args, "output", None)
    if not output_path and args.forum_command == "daily-learn":
        output_path = str(_daily_forum_report_path(args))
    if output_path:
        out = Path(output_path).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(str(out))
    else:
        print(text, end="")
    return 0


def _daily_forum_report_path(args: argparse.Namespace) -> Path:
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if getattr(args, "output_dir", None)
        else get_runtime_paths("_forum_learning", args.runtime_root).root / "forum_learning"
    )
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    suffix = "json" if getattr(args, "format", "markdown") == "json" else "md"
    return output_dir / f"daily_forum_learning_{stamp}.{suffix}"


def _daily_forum_history_path(args: argparse.Namespace) -> Path:
    if getattr(args, "history_path", None):
        return Path(args.history_path).expanduser().resolve()
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if getattr(args, "output_dir", None)
        else get_runtime_paths("_forum_learning", args.runtime_root).root / "forum_learning"
    )
    return output_dir / "read_history.jsonl"


def cmd_knowledge(args: argparse.Namespace) -> int:
    if args.knowledge_command != "approve-forum-lesson":
        print(f"Unknown knowledge command: {args.knowledge_command}", file=sys.stderr)
        return 2
    try:
        result = approve_forum_lesson(
            Path(args.report),
            title=args.title,
            approve_all=bool(args.approve_all),
            approved_by=str(args.approved_by),
            notes=str(args.notes or ""),
            knowledge_dir=Path(args.knowledge_dir) if args.knowledge_dir else None,
        )
    except Exception as exc:
        print(f"knowledge approve-forum-lesson failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_prompt(args: argparse.Namespace) -> int:
    if args.prompt_command != "compare":
        print(f"Unknown prompt command: {args.prompt_command}", file=sys.stderr)
        return 2
    metrics = []
    repos: list[Repository] = []
    try:
        for run_id in args.run_ids:
            paths = get_runtime_paths(run_id, args.runtime_root)
            repo = Repository(paths.db_path)
            repos.append(repo)
            metrics.append(summarize_run_prompt_metrics(repo, run_id))
        report = compare_prompt_runs(metrics)
    except Exception as exc:
        print(f"prompt compare failed: {exc}", file=sys.stderr)
        return 1
    finally:
        for repo in repos:
            repo.close()
    if args.format == "json":
        text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    else:
        text = render_prompt_compare_markdown(report)
    if args.output:
        out = Path(args.output).expanduser().resolve()
        write_prompt_compare_report(report, out)
        print(str(out))
    else:
        print(text, end="")
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    memory = AlphaMemory.for_runtime_root(get_runtime_paths("_memory", args.runtime_root).root)
    try:
        if args.memory_command == "ingest":
            paths = get_runtime_paths(args.run_id, args.runtime_root)
            repo = Repository(paths.db_path)
            try:
                result = memory.ingest_run(repo, args.run_id)
            finally:
                repo.close()
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.memory_command == "summary":
            summary = memory.summarize(
                dataset=str(args.dataset) if args.dataset else None,
                region=str(args.region).upper() if args.region else None,
                limit=int(args.limit),
            )
            if args.format == "json":
                print(json.dumps(summary, ensure_ascii=False, indent=2))
            else:
                print(render_memory_summary_markdown(summary), end="")
            return 0
        print(f"Unknown memory command: {args.memory_command}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"memory {args.memory_command} failed: {exc}", file=sys.stderr)
        return 1
    finally:
        memory.close()


def cmd_research(args: argparse.Namespace) -> int:
    if args.research_command != "summary":
        print(f"Unknown research command: {args.research_command}", file=sys.stderr)
        return 2
    try:
        runtime_root = get_runtime_paths("_research", args.runtime_root).root
        summary = build_research_quality_summary(
            runtime_root,
            run_ids=args.run_ids,
            dataset=str(args.dataset) if args.dataset else None,
            region=str(args.region).upper() if args.region else None,
            limit=int(args.limit),
        )
    except Exception as exc:
        print(f"research summary failed: {exc}", file=sys.stderr)
        return 1
    if args.format == "json":
        text = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
    else:
        text = render_research_quality_markdown(summary)
    if args.output:
        out = Path(args.output).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(str(out))
    else:
        print(text, end="")
    return 0


def cmd_settings(args: argparse.Namespace) -> int:
    if args.settings_command == "list":
        presets = load_settings_presets(args.runtime_root)
        if args.format == "json":
            payload = [
                {
                    "name": preset.name,
                    "description": preset.description,
                    "source": preset.source,
                    "settings": preset.normalized_settings(),
                }
                for preset in presets
            ]
            print(json.dumps(payload, ensure_ascii=False, indent=2))
        else:
            print(render_settings_presets(presets))
        return 0

    if args.settings_command == "show":
        try:
            preset = find_settings_preset(args.preset, args.runtime_root)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if args.format == "json":
            print(
                json.dumps(
                    {
                        "name": preset.name,
                        "description": preset.description,
                        "source": preset.source,
                        "settings": preset.normalized_settings(),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
        else:
            print(render_preset_detail(preset))
        return 0

    if args.settings_command == "choose":
        try:
            preset = select_settings_preset(args.runtime_root)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(render_preset_detail(preset))
        if args.print_command:
            print()
            print(f"PYTHONPATH=.. python3 -m brain_agent run --dataset <dataset_id> --preset {preset.name}")
            print(f"# explicit settings: {settings_command_fragment(preset.normalized_settings())}")
        return 0

    return 2


def cmd_tasks(args: argparse.Namespace) -> int:
    paths = get_runtime_paths(args.run_id, args.runtime_root)
    repo = Repository(paths.db_path)
    try:
        if not repo.get_run(args.run_id):
            print(f"Run not found: {args.run_id}", file=sys.stderr)
            return 1
        if args.task_id:
            rows = [repo.get_task(args.task_id)]
            rows = [r for r in rows if r]
        else:
            rows = repo.list_rows("tasks", args.run_id)
        if args.cancel:
            if not args.task_id:
                print("--cancel requires --task-id", file=sys.stderr)
                return 2
            if not rows:
                print(f"Task not found: {args.task_id}", file=sys.stderr)
                return 1
            ok = TaskRunner.cancel(rows[0].get("pid"))
            repo.update_task_status(str(args.task_id), "cancelled" if ok else "exited")
            rows[0]["status"] = "cancelled" if ok else "exited"
        if args.retry:
            if not args.task_id:
                print("--retry requires --task-id", file=sys.stderr)
                return 2
            if not rows:
                print(f"Task not found: {args.task_id}", file=sys.stderr)
                return 1
            retry_result = _retry_task(repo, args.run_id, paths, rows[0])
            print(json.dumps(retry_result, ensure_ascii=False, indent=2))
            return 0 if retry_result["returncode"] == 0 else 1
        if args.refresh:
            for row in rows:
                status = TaskRunner.infer_status(row.get("pid"), str(row.get("status") or ""))
                if status != row.get("status"):
                    repo.update_task_status(str(row["task_id"]), status)
                    row["status"] = status
        if any(str(row.get("adapter") or "") == "batchSim" for row in rows):
            print(render_simulation_progress(build_simulation_progress(repo, args.run_id, paths.run_dir)))
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    finally:
        repo.close()


def _retry_task(repo: Repository, run_id: str, paths, task: dict) -> dict:
    stdout_path = Path(str(task.get("stdout_path") or ""))
    meta_path = stdout_path.parent / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(f"Task meta not found: {meta_path}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    cmd = meta.get("cmd")
    cwd = meta.get("cwd")
    if not isinstance(cmd, list) or not cwd:
        raise ValueError(f"Task meta missing cmd/cwd: {meta_path}")

    env = _credential_env_for_retry()
    runner = TaskRunner(paths.tasks_dir)
    adapter = str(task.get("adapter") or "retry")

    def _record_running(handle):
        repo.create_task(
            run_id,
            handle.task_id,
            adapter,
            pid=handle.pid,
            status="running",
            stdout_path=handle.stdout_path,
            stderr_path=handle.stderr_path,
        )

    handle, returncode = runner.run_tracked(
        adapter,
        [str(x) for x in cmd],
        cwd,
        env=env,
        on_start=_record_running,
    )
    repo.update_task_status(handle.task_id, "completed" if returncode == 0 else "failed")
    return {
        "previous_task_id": task.get("task_id"),
        "new_task_id": handle.task_id,
        "returncode": returncode,
        "stdout_path": str(handle.stdout_path),
        "stderr_path": str(handle.stderr_path),
    }


def _credential_env_for_retry() -> dict[str, str]:
    import os

    env = os.environ.copy()
    creds = load_credentials(require_brain=False, require_llm=False)
    if creds.get("brain_email"):
        env["BRAIN_EMAIL"] = creds["brain_email"]
        env["BRAIN_USERNAME"] = creds["brain_email"]
        env["BRAIN_CREDENTIAL_EMAIL"] = creds["brain_email"]
    if creds.get("brain_password"):
        env["BRAIN_PASSWORD"] = creds["brain_password"]
        env["BRAIN_CREDENTIAL_PASSWORD"] = creds["brain_password"]
    if creds.get("llm_provider"):
        env["BRAIN_LLM_PROVIDER"] = creds["llm_provider"]
        env["LLM_PROVIDER"] = creds["llm_provider"]
    if creds.get("llm_api_key"):
        env["LLM_API_KEY"] = creds["llm_api_key"]
        env["MOONSHOT_API_KEY"] = creds["llm_api_key"]
        if creds.get("llm_provider") == "deepseek":
            env["DEEPSEEK_API_KEY"] = creds["llm_api_key"]
        if creds.get("llm_provider") == "openai":
            env["OPENAI_API_KEY"] = creds["llm_api_key"]
    if creds.get("llm_base_url"):
        env["LLM_BASE_URL"] = creds["llm_base_url"]
        env["MOONSHOT_BASE_URL"] = creds["llm_base_url"]
    if creds.get("llm_model"):
        env["LLM_MODEL"] = creds["llm_model"]
        env["MOONSHOT_MODEL"] = creds["llm_model"]
    return env


def cmd_doctor(args: argparse.Namespace) -> int:
    paths = get_runtime_paths("doctor", args.runtime_root)
    checks = []
    checks.append({"name": "python_version", "ok": sys.version_info >= (3, 11), "detail": sys.version.split()[0]})
    try:
        ensure_runtime(paths)
        checks.append({"name": "runtime_writable", "ok": paths.run_dir.exists(), "detail": str(paths.run_dir)})
    except Exception as exc:
        checks.append({"name": "runtime_writable", "ok": False, "detail": str(exc)})

    repo_root = Path(__file__).resolve().parent
    required = [
        repo_root / ".agents" / "skills" / "brain-makeSomeGem" / "scripts" / "headless_runner" / "run.py",
        repo_root / ".agents" / "skills" / "brain-inspectRawTemplate-create-Setting" / "scripts" / "process_template.py",
        repo_root / ".agents" / "skills" / "brain-simAlphasinBatch-and-track" / "scripts" / "batch_simulator.py",
        repo_root / ".agents" / "skills" / "brain-enhance-template" / "scripts" / "run.py",
        repo_root / ".agents" / "skills" / "brain-shared" / "scripts" / "ace_lib.py",
    ]
    for path in required:
        checks.append({"name": f"exists:{path.name}", "ok": path.exists(), "detail": str(path)})

    try:
        creds = load_credentials(require_brain=True, require_llm=bool(args.check_llm))
        checks.append({"name": "brain_credentials", "ok": bool(creds.get("brain_email") and creds.get("brain_password")), "detail": "loaded"})
        if args.check_llm:
            checks.append(
                {
                    "name": "llm_credentials",
                    "ok": bool(creds.get("llm_api_key")),
                    "detail": f"provider={creds.get('llm_provider', '')} model={creds.get('llm_model', '')}",
                }
            )
    except Exception as exc:
        checks.append({"name": "credentials", "ok": False, "detail": str(exc)})

    ok = all(item["ok"] for item in checks)
    print(json.dumps({"ok": ok, "checks": checks}, ensure_ascii=False, indent=2))
    return 0 if ok else 1
