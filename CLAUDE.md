# AIWorker Claude Code Guide

This repository's long-term maintained automation layer is `brain_agent`.

## Skills

For BRAIN alpha mining workflows, use the project skills under `.claude/skills/`:

| Skill | Use When |
|---|---|
| `/brain-agent` | Default for full pipeline: generate, run, resume, report, gate, status, tasks, worker, settings, forum, memory, research |
| `/brain-make-some-gem` | Legacy single-step generator debugging only; ordinary generation should use `/brain-agent` |
| `/brain-enhance-template` | Legacy single/cross enhancer debugging only; ordinary enhancement should use `/brain-agent` |
| `/brain-sim-alphas-batch-track` | Legacy CSV batch simulator debugging only; ordinary simulation/resume/retry should use `/brain-agent` |
| `/brain-inspect-raw-template` | Legacy raw-template settings debugging only; ordinary alpha-list creation should use `/brain-agent` |
| `brain-shared` | Internal library (ace_lib, validator, llm_client) — not user-invocable |

Prefer `/brain-agent` as the default orchestrator for end-to-end workflows. Use the atomic skills only when the user explicitly asks to run or debug a legacy step directly.

## Tool Routing

Use `brain_agent` as the entry point for BRAIN alpha mining, pipeline management, and reporting:

- Generate alpha ideas and expressions
- Build alpha lists with canonical simulation settings
- Run batch simulations orchestrated by brain_agent
- Resume, retry, cancel, or inspect pipeline tasks
- Track artifacts, candidates, metrics, reports, and gate results
- Maintain or improve the pipeline code

Prefer commands like:

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime doctor --check-llm
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime run ...
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime tasks --run-id <run_id> --refresh
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime resume --run-id <run_id>
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime report --run-id <run_id>
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime worker --run-id <run_id> --mode drain --max-total-alphas 5000 --refill-on-empty
```

Do not use `brain-mcp` in this repository. Route BRAIN platform queries, simulations, reports, retries, and gate checks through `brain_agent` so all activity is recorded under `.brain_runtime`, `brain_agent.sqlite3`, run reports, and candidate tracking.

## Long-Run Simulation Mode

For unattended quota-draining runs, prefer `/brain-agent` with `worker --mode drain` over repeatedly resuming short interactive runs:

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime worker \
  --run-id <run_id> \
  --mode drain \
  --max-total-alphas 5000 \
  --refill-on-empty \
  --max-empty-refills 0
```

`--refill-on-empty` makes the worker generate and inspect another candidate batch when no `sim_pending` or `sim_retryable` candidates remain, then continue draining through the existing batch simulator. Keep `--batch-candidates-limit 0` unless there is a reason to cap each worker cycle below `batch_size * concurrency`.

## Submission Safety

Never call any production submission path unless the user explicitly confirms submission for specific alpha IDs.

When the user says "不要自动 submit", only run checks and reports. List submit-ready alpha IDs for human review.

## Concurrency Safety

Avoid running any other BRAIN simulation tool at the same time as `brain_agent` batch simulations for the same BRAIN account. They share platform quota and can increase 429 rate limits, platform queueing, and `Retry-After` waits.

If a batch simulation appears stuck:

1. Use `brain_agent tasks --refresh`.
2. Read the `batchSim` task stdout/stderr logs.
3. Look for `[BRAIN wait]` messages.
4. Decide whether to keep waiting, cancel, retry, or resume.

Batch simulation status fetches are intentionally defensive: parent, child, and single-simulation polling should retry transient HTTP, JSON parsing, or empty-response reads before recording a simulation failure. Do not treat one failed status fetch as a failed alpha.

## Maintenance Preference

When adding durable functionality, add it to `brain_agent` first. Treat legacy implementations only as reference material unless the user explicitly asks to change them.

Keep `README.md` and `codex_pipeline.md` updated when changing CLI flags, run behavior, task handling, or artifact formats.
