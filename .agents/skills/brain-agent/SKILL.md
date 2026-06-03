---
name: brain-agent
description: >-
  Default orchestrator for WorldQuant BRAIN alpha mining in this repository.
  Use for end-to-end workflows and for pipeline management: generate ideas and
  expressions, build alpha lists, run or resume batch simulations, inspect tasks,
  retry failures, write reports, check gates, use settings presets, learn from
  forum, summarize memory/research quality, or import artifacts. Prefer this
  skill over the legacy atomic skills unless the user explicitly asks to debug
  or run one legacy step directly.
allowed-tools:
  - Read
  - Grep
  - Glob
  - RunTerminal
  - ManageTodoList
---

# Brain Agent

`brain_agent` is the maintained entry point for BRAIN alpha mining in this
repository. It records runs under `.brain_runtime`, persists state in
`brain_agent.sqlite3`, tracks artifacts/candidates/tasks, writes reports, and
keeps gate results auditable.

Use this command pattern from the repository root:

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime <command> ...
```

## Default Routing

Use `brain_agent` for normal user requests such as:

- generate alpha ideas or expressions
- build alpha lists with canonical simulation settings
- run, resume, retry, cancel, or inspect simulations
- check pipeline status or task logs
- produce reports, exports, research summaries, or memory summaries
- run submit gate checks
- import external `final_expressions`, `alpha_list`, or `simulation_status` artifacts

Use legacy atomic skills only when the user explicitly asks to debug or run a
single legacy component. Do not use `brain-mcp` in this repository.

## Source Layout

`cli.py` remains the package entry point. The implementation is grouped by
responsibility:

| Path | Purpose |
|---|---|
| `core/` | Runtime paths, SQLite repository, dataclasses/enums, settings presets, task runner, leases, daily usage, shared utilities, credential loading |
| `pipeline/` | Controller, worker/drain mode, legacy adapters, decision logic, quota allocation, variant search, optimization, thesis helpers |
| `analysis/` | Diagnostics, scoring, selection, memory, research quality, reports |
| `intelligence/` | Prompting, forum learning, approved knowledge |
| `legacy/` | Older standalone platform/forum integrations kept only for reference or explicit legacy debugging |
| `scripts/` | One-off local research and maintenance scripts |

Root-level modules such as `adapters.py`, `repository.py`, and `worker.py` are
compatibility aliases for old imports and tests. New durable code changes should
edit the grouped implementation file, for example `pipeline/adapters.py` or
`core/repository.py`.

## Common Commands

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime doctor --check-llm

PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime settings list
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime settings show --preset <name>
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime settings choose --print-command

PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime run \
  --dataset <dataset_id> --region <REGION> --delay <DELAY> --universe <UNIVERSE> \
  --data-type MATRIX --neutralization <NEUTRALIZATION>

PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime run \
  --preset <preset_name> --dataset <dataset_id>

PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime status --run-id <run_id>
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime tasks --run-id <run_id> --refresh
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime resume --run-id <run_id>
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime retry-sim --run-id <run_id> --dry-run
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime report --run-id <run_id>
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime usage
```

## Worker Mode

For long unattended runs, prefer worker/drain mode after creating a run:

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime worker \
  --run-id <run_id> --mode drain --max-total-alphas 5000
```

`--max-total-alphas` is a local worker-process cap, not a live BRAIN daily
quota query. Worker batches record their actual submitted alpha count in
`.brain_runtime/daily_simulation_usage.json`; use `brain_agent usage` or
`brain_agent usage --date YYYY-MM-DD` to inspect the local daily total across
`brain_agent` workers. This does not include manual BRAIN web submissions or
other tools.

## Artifact Import

Use this when a legacy or external step already produced artifacts and they need
to be tracked by `brain_agent`:

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime parse-artifact \
  --run-id <run_id> --kind final_expressions --path <path_to_final_expressions.json>

PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime parse-artifact \
  --run-id <run_id> --kind alpha_list --path <path_to_alpha_list.json>

PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime parse-artifact \
  --run-id <run_id> --kind simulation_status --path <path_to_simulation_status.csv>
```

## Forum, Memory, And Research

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime forum search "turnover submit" --max-results 10
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime forum learn "turnover submit fitness" --read-top 3
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime knowledge approve-forum-lesson --report <report_path>

PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime memory ingest --run-id <run_id>
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime memory summary --dataset <dataset_id> --region <REGION>
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime research summary --limit 20
```

## Gate And Submission Safety

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime gate --run-id <run_id>
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime gate --run-id <run_id> --dry-run
```

Never call any production submission path unless the user explicitly confirms
submission for specific alpha IDs. When the user says "不要自动 submit", only run
checks and reports, then list submit-ready alpha IDs for human review.

## Stuck Simulation Protocol

1. Run `brain_agent tasks --run-id <run_id> --refresh`.
2. Read the `batchSim` task stdout/stderr logs.
3. Look for `[BRAIN wait]` messages and retry-after waits.
4. Decide whether to keep waiting, cancel, retry, or resume.

Do not treat one transient status-fetch failure as a failed alpha. Parent, child,
and single-simulation polling should retry transient HTTP, JSON parsing, or
empty-response reads before recording a simulation failure.
