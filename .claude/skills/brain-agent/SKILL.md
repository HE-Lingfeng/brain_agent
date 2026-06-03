---
name: brain-agent
description: >-
  Full pipeline orchestrator for BRAIN alpha mining. Covers the complete workflow:
  run, resume, retry, worker, report, gate, tasks, status, doctor, parse-artifact,
  settings, forum, memory, research, prompt-compare.
  Use when user asks to run the alpha pipeline, check status, resume, manage tasks,
  generate reports, check gates, learn from forum, or manage alpha memory.
  Triggers on: "跑流水线", "run pipeline", "brain_agent run", "resume", "gate",
  "alpha memory", "forum learn", "research quality".
user-invocable: true
---

# Brain Agent — Pipeline Orchestrator

The central CLI for end-to-end BRAIN alpha mining. All commands use the pattern:

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime <command> ...
```

`--runtime-root` defaults to `.brain_runtime`. All runs are tracked there.

## Environment Check

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime doctor --check-llm
```

Verifies: Python version, runtime writable, tool scripts exist, credentials loaded.

## Settings Presets

```bash
# List available presets
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime settings list

# Show a preset's details
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime settings show --preset <name>

# Interactive choose
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime settings choose --print-command
```

## Run Full Pipeline

```bash
# With a preset
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime run --preset <preset_name> --dataset <dataset_id>

# With explicit settings
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime run \
  --dataset fundamental28 --region GLB --delay 1 --universe TOP3000 \
  --data-type MATRIX --neutralization INDUSTRY --decay 10 --truncation 0.08

# Dry run (validate settings only)
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime run --preset <name> --dataset <id> --dry-run
```

The `run` command orchestrates a loop: generate → enhance → simulate → evaluate → (repeat or stop). Key tuning params:

| Param | Default | Description |
|-------|---------|-------------|
| `--target-ready` | 4 | Stop when N candidates are submit-ready |
| `--max-iterations` | 6 | Max pipeline loops |
| `--max-sim-alphas` | - | Cap on total simulated alphas |
| `--max-variant-alphas` | 20 | Max variants per iteration |
| `--max-enhance-actions` | 4 | Max enhance actions per iteration |
| `--use-llm-decide` | false | Let LLM decide which alphas to keep/enhance |

## Status & Tasks

```bash
# Run status (JSON with stage, counts, simulation progress)
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime status --run-id <run_id>

# List tasks for a run
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime tasks --run-id <run_id>

# Refresh task statuses (poll PID state)
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime tasks --run-id <run_id> --refresh

# Cancel a task
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime tasks --run-id <run_id> --task-id <task_id> --cancel

# Retry a failed task
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime tasks --run-id <run_id> --task-id <task_id> --retry
```

## Resume & Retry

```bash
# Resume a stopped/paused run from where it left off
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime resume --run-id <run_id>

# Retry failed simulations (regenerates alpha_list from retryable candidates)
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime retry-sim --run-id <run_id>
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime retry-sim --run-id <run_id> --limit 10 --dry-run
```

## Worker Mode

Long-running simulation worker that polls for submit-ready candidates and simulates them:

```bash
# Drain mode (keep polling until stopped)
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime worker --run-id <run_id> --mode drain \
  --max-runtime-hours 8 --max-total-alphas 5000

# Once mode (process one batch and exit)
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime worker --run-id <run_id> --mode once
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime usage
```

`--max-total-alphas` is a local worker-process cap, not a live BRAIN daily
quota query. Worker batches record their actual submitted alpha count in
`.brain_runtime/daily_simulation_usage.json`; use `brain_agent usage` or
`brain_agent usage --date YYYY-MM-DD` to inspect the local daily total across
`brain_agent` workers. This does not include manual BRAIN web submissions or
other tools.

## Report & Export

```bash
# Generate run report (markdown + JSON)
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime report --run-id <run_id>

# Export full run result as JSON
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime export --run-id <run_id>
```

## Gate Check

Check if submit-ready alphas pass submission gates:

```bash
# Real gate check (uses BRAIN API)
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime gate --run-id <run_id>

# Dry run with local mock checks (no API calls)
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime gate --run-id <run_id> --dry-run
```

## Import External Artifacts

Bring external alpha outputs into a run for tracking:

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime parse-artifact --run-id <run_id> \
  --kind final_expressions --path <path_to_final_expressions.json>

PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime parse-artifact --run-id <run_id> \
  --kind alpha_list --path <path_to_alpha_list.json>

PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime parse-artifact --run-id <run_id> \
  --kind simulation_status --path <path_to_simulation_status.csv>
```

## Forum Learning

```bash
# Search forum
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime forum search "alpha decay optimization"

# Read a specific post
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime forum read <post_url_or_id>

# Learn from search results (LLM summary)
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime forum learn "mean reversion alpha" --read-top 3

# Daily automated learning
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime forum daily-learn

# Approve a forum lesson to knowledge base
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime knowledge approve-forum-lesson --report <report_path>
```

Forum learning reports under `.brain_runtime/forum_learning/` are review
artifacts until the user approves them. Template-library reports with a generic
`## Machine Readable (JSON)` block can be approved through the same command; the
maintained `brain_agent` path normalizes them into compact approved lessons.

Approved template lessons are injected as soft guidance through compact
knowledge and `BRAIN_AGENT_RESEARCH_POLICY_JSON`. Use them to steer generation
toward dataset-category template routing, operator prechecks, meaningful
trading windows, neutralization hints, sparse-data handling, single-dataset
economic theses, and diversified 8+ variant batches. Do not paste forum text
directly into prompts or treat approved lessons as submission permission.

## Alpha Memory

Track alpha performance across runs for pattern learning:

```bash
# Ingest a run into memory
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime memory ingest --run-id <run_id>

# Summarize memory by dataset/region
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime memory summary --dataset fundamental28 --region GLB --limit 10
```

## Research Quality

```bash
# Summarize research quality across runs
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime research summary --dataset fundamental28 --region USA

# Compare specific runs
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime research summary --run-id <id1> --run-id <id2>
```

## Prompt Comparison

```bash
# Compare prompt experiments (first run-id = baseline)
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime prompt compare --run-id <baseline_id> --run-id <experiment_id>
```

## Typical Workflow

1. `doctor --check-llm` — verify environment
2. `settings list` + `settings choose` — pick dataset/settings
3. `run --preset ... --dataset ...` — start pipeline
4. `status --run-id ...` — monitor progress
5. `tasks --run-id ... --refresh` — check task details
6. `report --run-id ...` — review results
7. `gate --run-id ...` — submission gate
8. `resume --run-id ...` or `retry-sim --run-id ...` — if needed

## Guardrails

- Never call production submission without explicit user confirmation.
- Avoid concurrent `brain_agent` batch sims for the same BRAIN account.
- `--runtime-root .brain_runtime` should be consistent across sessions for the same project.
- For stuck simulations, inspect task logs for `[BRAIN wait]` and `[BRAIN healthcheck]`; one wait or healthcheck is not an alpha failure.
