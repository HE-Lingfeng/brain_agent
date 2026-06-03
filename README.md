# brain_agent

`brain_agent` is a local WorldQuant BRAIN alpha-mining orchestrator. It coordinates idea generation, inspection, simulation, variant search, enhancement, reporting, memory, and submission-readiness checks while keeping credentials outside the repository.

For Codex/Claude Code maintenance, start with `CODEX_CONTEXT.md` — a compact project map with current invariants, common commands, and the minimal set of files to inspect for common tasks.

## Repository Layout

The Python package is organized by responsibility:

| Path | Purpose |
|------|---------|
| `cli.py` | CLI entry point and subcommand wiring |
| `core/` | Runtime paths, persistence, models, settings, task runner, leases, shared utilities |
| `pipeline/` | End-to-end controller, legacy adapters, worker/drain mode, quota allocation, variants, optimization |
| `analysis/` | Diagnostics, candidate scoring/selection, memory, reporting, research quality |
| `intelligence/` | Prompting, forum learning, approved knowledge handling |
| `legacy/` | Older standalone platform/forum integrations kept for reference or direct debugging |
| `scripts/` | One-off local research and maintenance scripts |

Root-level modules such as `adapters.py`, `repository.py`, and `worker.py` are compatibility wrappers. Existing imports like `from brain_agent.adapters import BatchSimAdapter` still work, but new code should prefer the grouped implementation modules when it is editing internals.

If this repository is cloned as `brain_agent`, run it from the parent directory:

```bash
python3 -m brain_agent --help
```

From inside the repository directory, run it with the parent directory on `PYTHONPATH`:

```bash
PYTHONPATH=.. python3 -m brain_agent --help
```

## Credentials

Do not commit credentials to this repository. The agent reads credentials with precedence: **provider defaults < secret file < environment variables**.

### Environment Variables

```bash
export BRAIN_EMAIL=<brain-email>
export BRAIN_PASSWORD=<brain-password>
export LLM_PROVIDER=moonshot
export LLM_API_KEY=<llm-api-key>
export LLM_BASE_URL=https://api.moonshot.cn/v1
export LLM_MODEL=kimi-k2.5
```

### Secret File (optional)

Path: `~/secrets/worldquant-brain.json`

```json
{
  "brain": {
    "email": "your_email",
    "password": "your_password"
  },
  "llm": {
    "provider": "moonshot",
    "api_key": "your-api-key",
    "base_url": "https://api.moonshot.cn/v1",
    "model": "kimi-k2.5"
  }
}
```

Supported `provider` values: `moonshot`, `deepseek`, `openai`, or any OpenAI-compatible endpoint when `base_url` and `model` are set.

Legacy `MOONSHOT_API_KEY`, `MOONSHOT_BASE_URL`, `MOONSHOT_MODEL`, and `moonshot_*` secret keys still work for older local scripts, but `LLM_*` env vars and `llm.*` secret keys are preferred.

The repository `.gitignore` uses a default-deny policy so runtime data, documents, caches, and secret-like files stay local.

## Quick Start

### Doctor Check

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime doctor --check-llm
```

### Settings Presets

List or interactively choose reusable dataset/settings presets:

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime settings list
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime settings show --preset eur_top2500_slow_fast
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime settings choose --print-command
```

Built-in presets:

| Name | Region | Universe | Neutralization |
|------|--------|----------|----------------|
| `eur_top2500_slow_fast` | EUR | TOP2500 | SLOW_AND_FAST |
| `usa_top3000_industry` | USA | TOP3000 | INDUSTRY |
| `usa_top3000_subindustry` | USA | TOP3000 | SUBINDUSTRY |
| `glb_top3000_market` | GLB | TOP3000 | MARKET |

All presets use `delay=1`, `data_type=MATRIX`, `decay=10`, `truncation=0.08`, `max_trade=false`. Built-in presets intentionally omit dataset ids because BRAIN datasets change over time; pass `--dataset` explicitly or use `--choose-settings` to enter it interactively.

Add personal presets in `<runtime-root>/settings_presets.json`:

```json
{
  "presets": [
    {
      "name": "my_usa_setting",
      "description": "Personal default USA research setting",
      "settings": {
        "region": "USA",
        "delay": 1,
        "universe": "TOP3000",
        "data_type": "MATRIX",
        "decay": 10,
        "truncation": 0.08,
        "neutralization": "INDUSTRY",
        "max_trade": false
      }
    }
  ]
}
```

Explicit CLI flags (`--dataset`, `--neutralization`, `--decay`, etc.) override preset values.

## Core Commands

### Run

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime run \
  --dataset fundamental31 \
  --preset eur_top2500_slow_fast \
  --target-ready 1 \
  --max-iterations 1 \
  --max-sim-alphas 1 \
  --max-variant-alphas 6 \
  --max-variants-per-alpha 3
```

Each run progresses through stages: **GENERATE → INSPECT → SIMULATE → VARIANT_SEARCH → DECIDE → ENHANCE → SUBMIT_GATE → REPORT**.

Use `--dry-run` to validate the local pipeline without calling BRAIN or LLM APIs.

### Resume

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime resume --run-id <run_id>
```

### Status

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime status --run-id <run_id>
```

### Tasks

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime tasks --run-id <run_id> --refresh
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime tasks --run-id <run_id> --task-id <task_id> --cancel
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime tasks --run-id <run_id> --task-id <task_id> --retry
```

### Report & Export

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime report --run-id <run_id>
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime export --run-id <run_id>
```

`report` writes `run_report.md` and `run_result.json`. `export` writes a full JSON dump of the run result.

## Simulation & Quota

### Simulation Progress

During batch simulation, the foreground CLI prints a compact progress line:

```text
simulation: [########............] 8/20 (40.0%) | running=4 | slots=4x10 | capacity=40 | remaining=12 | completed=7 | failed=1
```

### Simulation Defaults

Defaults are intentionally conservative to reduce BRAIN rate limits:

| Region | Concurrency | Batch Size | Capacity |
|--------|-------------|------------|----------|
| Non-GLB | 8 | 10 | 80 |
| GLB | 4 | 4 | 16 |

Override with `--concurrency` and `--batch-size`. Real batchSim runs also use a runtime-wide lease file at `.brain_runtime/simulation_leases.json` so multiple worker or retry processes share the same platform budget and never exceed 80 active simulation slots (`8 * 10`). If another process is already using part of the budget, the next batch is automatically trimmed to the remaining slots or waits when all slots are full. Batch polling waits up to 30 minutes for parent batches to spawn children and up to 60 minutes for children to finish. During long platform waits, the simulator refreshes BRAIN authentication every 15 minutes and keeps polling; this is a defensive health check, not a failed-alpha signal.

### Retry Failed Simulations

Rebuild a clean retry list from `TIMEOUT`, `BATCH_SPAWN_FAILED`, and other retryable platform failures:

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime retry-sim \
  --run-id <run_id> \
  --batch-size 10 \
  --concurrency 8
```

Use `--dry-run` to write `alpha_list_retryable.json` without submitting.

### Worker (Non-Interactive Drain)

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime worker \
  --run-id <run_id> \
  --mode drain \
  --max-total-alphas 5000 \
  --refill-on-empty
```

Each batch is capped to `batch_size * concurrency` candidates by default, then capped again by the shared runtime simulation lease. `--max-total-alphas` is a local cap for that worker process and is enforced against the worker's own submitted count before each batch starts; it is not a live query of BRAIN's per-account daily usage. Worker submissions are also recorded in `.brain_runtime/daily_simulation_usage.json` so multiple brain_agent workers can be summed by local date. Use `--mode once` for a single batch.

Review the local daily submission ledger with:

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime usage
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime usage --date 2026-06-03
```

`--refill-on-empty` turns the worker into a closer approximation of a daemon queue: when no `sim_pending` or `sim_retryable` candidates remain, it runs a new `GENERATE -> INSPECT -> field_factory` refill and then keeps draining. `--max-empty-refills` defaults to 3 in drain mode; set it to `0` for unlimited refills during a supervised long run.

Drain mode intentionally does not run variant search or enhancement after every batch. It does run a light periodic optimization checkpoint every 500 submitted alphas by default: the worker scans simulated parents, skips parents already tagged by prior optimization passes, and enqueues repair variants only when useful candidates exist. Existing pending candidates stay in the queue; optimization variants receive higher `queue_priority`, so the next worker batch drains them before ordinary pending expressions. Set `--optimize-every-alphas 0` to disable, or tune `--optimize-max-parents` / `--optimize-max-variants`.

When you want to review recent simulation evidence yourself and enqueue targeted repairs on demand, run a manual optimization pass:

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime optimize-candidates \
  --run-id <run_id> \
  --max-parents 20 \
  --max-variants 100
```

`optimize-candidates` selects simulated parents with repairable evidence, writes durable tags such as `repair_low_fitness`, `repair_subuniverse`, `repair_weight_concentration`, `short_flip_candidate`, and `turnover_control_candidate`, then generates lineage-preserving variants as higher-priority `sim_pending` candidates for the drain worker. Use `--dry-run` to inspect the selected parents and tags without modifying the queue.

### Simulation Quota Allocator

When `--max-sim-alphas` is set, the quota allocator splits scarce simulation budget across three strategies:

- **Exploit**: memory-backed candidates with historically successful patterns.
- **Explore**: new field families and low-evidence structures to avoid local loops.
- **Repair**: candidates with clear `repair_objectives` and high repairability.

The allocator also applies duplicate-cluster penalties and writes `alpha_list_quota_report` for review.

## Variant Search

After the initial simulation pass, the pipeline spends a small bounded budget on deterministic local variants of alphas with weak or repairable signal. Use `--max-variant-alphas 0` to disable.

### Optimizer Modules

Six named optimizer modules target specific failure modes:

| Optimizer | Trigger |
|-----------|---------|
| `LowFitnessOptimizer` | Near-zero Sharpe/Fitness |
| `LowTurnoverOptimizer` | Over-smoothed or over-filtered signals |
| `HighTurnoverOptimizer` | Excessive turnover |
| `ShortFlipOptimizer` | Inverse/negative signals |
| `CoverageRepairer` | Coverage, subuniverse, or unavailable-field failures |
| `CorrelationOptimizer` | Self/prod correlation failures (requires gate diagnostics) |

Each optimizer generates multiple variants, preserves parent lineage, and reports original-vs-variant metric deltas in `run_report.md`.

### Generic Variants

The pipeline also generates close generic variants: window sweep, decay sweep, rank/zscore swap, neutralization/decay cross sweep, turnover-control wrappers, and coverage repair wrappers.

### PnL Pruning

Completed simulations best-effort populate `pnl_cache.json`. When present, variant search uses cached PnL series to prune highly correlated parents before generating variants.

Variants are stored as candidates with lineage fields (`parent_candidate_id`, `variant_strategy`, `variant_params`, `lineage_json`) and simulated through the same batch pipeline as original alphas.

## Alpha Memory

Cross-run memory tracks dataset, field family, operator pattern, factor thesis, prompt version, and failure mode performance. It lives at `<runtime_root>/alpha_memory.sqlite3`.

### Ingest

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime memory ingest --run-id <run_id>
```

Run reports auto-ingest on `write_report`. Only simulated/promising/gate-passed samples enter the learnable evidence pool; generated-but-untested candidates are tracked separately and do not pollute success-rate denominators.

### Summary

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime memory summary --dataset institutions6 --region USA
```

The summary shows current layers: operators, field families, factor thesis types, expected failure modes, learned repair methods, and recent runs.

### Scoring Integration

Candidate scoring reads memory to compute `memory_score` (historical success rate with recency decay) and `memory_risk_penalty` (hard failure tag history). Memory weights are intentionally small; when samples are scarce, behavior remains close to the no-memory baseline.

## Prompt A/B Comparison

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime prompt compare \
  --run-id prompt_baseline \
  --run-id prompt_experiment \
  --format markdown \
  --output .brain_runtime/prompt_ab/comparison.md
```

The first `--run-id` is treated as the baseline. Comparison is only valid when runs share the same dataset, region, delay, universe, data type, decay, truncation, neutralization, and max trade settings. A prompt is marked eligible for promotion only when it improves valid rate, promising rate, average fitness, or hard-failure rate versus the baseline without introducing hard-failure regression.

## Forum & Knowledge

### Forum Search & Learning

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime forum search "turnover" --max-results 10 --format markdown
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime forum read "<post-id-or-url>" --format markdown
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime forum learn "turnover submit fitness" \
  --max-results 10 --read-top 3 --format markdown --output .brain_runtime/forum_learning/turnover.md
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime forum daily-learn --output-dir .brain_runtime/forum_learning
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime forum glossary --format markdown
```

### Knowledge Approval

`forum learn` and `daily-learn` produce review reports only — they do not modify system behavior. To ingest approved lessons into the knowledge base:

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime knowledge approve-forum-lesson \
  --report .brain_runtime/forum_learning/turnover.md \
  --title "turnover reduction"
```

Approved lessons are injected into make/enhance prompts with compact summaries. The knowledge base lives at `brain_agent/knowledge/approved_forum_lessons/`.

Template-construction lessons are also represented as structured research policy (`BRAIN_AGENT_RESEARCH_POLICY_JSON`) in the maintained generation path. This steers the generator toward reusable template structures, dataset-category template routing, operator prechecks, meaningful trading windows, neutralization hints, sparse-data handling, and diversified 8+ variant batches without granting any automatic submission permission.

Template-library reports with a generic `## Machine Readable (JSON)` block can be approved through the same command. They are normalized into compact approved lessons instead of copying the full forum template text into prompts.

## Submit Gate

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime gate --run-id <run_id>
```

Gate checks read platform-backed checks (`/alphas/{id}/check`, falling back to `/alphas/{id}` `is.checks`) and normalize page check names such as `CONCENTRATED_WEIGHT`, `LOW_SUB_UNIVERSE_SHARPE`, and `LOW_2Y_SHARPE`. Dedicated self/prod correlation endpoints are intentionally skipped because they can take minutes per alpha; `self_corr_check` and `prod_corr_check` remain `PENDING`. Existing `submit_ready` candidates are rechecked by real gate runs; a complete submission-check failure revokes stale submit-ready status. Reports only list manual-submission candidates whose latest gate row is complete and passed. Gate failures from transient network/proxy errors are recorded as `gate_status=incomplete` rather than alpha quality failures.

Use `--dry-run` for local deterministic mock checks.

## Research Quality

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime research summary --limit 20
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime research summary \
  --dataset institutions6 --region USA --format markdown \
  --output .brain_runtime/research_quality/institutions6_usa.md
```

Reports aggregate the quality funnel (generated → simulated → complete → promising → submit-ready → gate-passed), failure tags, operator signals, field family signals, and recent run summaries with recommendations.

## Artifact Import

Import legacy artifacts from external or manual pipeline runs:

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime parse-artifact \
  --run-id <run_id> --kind final_expressions --path <path/to/final_expressions.json>
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime parse-artifact \
  --run-id <run_id> --kind alpha_list --path <path/to/alpha_list.json>
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime parse-artifact \
  --run-id <run_id> --kind simulation_status --path <path/to/simulation_status.csv>
```

## Key Run Flags

| Flag | Default | Description |
|------|---------|-------------|
| `--dataset` | — | BRAIN dataset id (required) |
| `--preset` | — | Named settings preset |
| `--region` | — | BRAIN region (USA, EUR, GLB, etc.) |
| `--delay` | — | Data delay |
| `--universe` | — | Universe (TOP2500, TOP3000, etc.) |
| `--data-type` | — | MATRIX or VECTOR |
| `--decay` | 10 | BRAIN simulation decay |
| `--truncation` | 0.08 | BRAIN simulation truncation |
| `--neutralization` | INDUSTRY | BRAIN neutralization setting |
| `--max-trade` | false | BRAIN maxTrade setting |
| `--target-ready` | 4 | Stop after N submit-ready alphas |
| `--max-iterations` | 6 | Max generate/simulate/enhance cycles |
| `--batch-size` | 10 (4 GLB) | Alphas per batch |
| `--concurrency` | 8 (4 GLB) | Concurrent simulation slots |
| `--max-fields` | — | Limit datafields sent to LLM |
| `--max-operators` | — | Limit operators sent to LLM |
| `--max-sim-alphas` | — | Cap real simulation submissions |
| `--max-variant-alphas` | 20 | Max variant alphas per iteration (0 to disable) |
| `--max-variants-per-alpha` | 4 | Max variants per parent alpha |
| `--use-llm-decide` | false | Use LLM for DECIDE phase |
| `--max-enhance-actions` | 4 | Max enhancement actions per iteration |
| `--make-prompt-version` | make-v1 | Prompt version for GENERATE |
| `--enhance-prompt-version` | enhance-v1 | Prompt version for ENHANCE |
| `--decision-prompt-version` | decision-v1 | Prompt version for DECIDE |
| `--dry-run` | false | Local mock pipeline, no BRAIN/LLM calls |

## Run Artifacts

```text
.brain_runtime/runs/<run_id>/
  brain_agent.sqlite3       # SQLite database (runs, tasks, artifacts, candidates, sim_results, gate_checks)
  run_report.md             # Research log with executive summary, decisions, failure diagnostics, recommendations
  run_result.json           # Machine-readable run result
  artifacts/                # Staged artifacts per pipeline phase
  tasks/                    # Subprocess stdout/stderr logs
```

## Expression Constraints

- **MATRIX** fields can be used directly in expressions.
- **VECTOR** fields must be reduced first with `vec_*` (usually `vec_avg(field)`).
- Dataset-level `--data-type VECTOR` does not guarantee every returned field is a VECTOR field. EVENT fields are incompatible with VECTOR expressions and are rejected by local simulation preflight; use `--data-type MATRIX` for datasets like analyst69 when VECTOR metadata returns EVENT fields.
- Put `ts_backfill` close to the field for sparse or quarterly data.
- Prefer `multiply(-1, expr)` for sign flipping.
- Avoid double-wrapping with `rank`/`zscore`/`group_neutralize` in variant search, as these can cause submission failures.

## Field Factory

Real inspect runs generate a small auxiliary alpha list from target datafield metadata: MATRIX fields are used directly, VECTOR fields are wrapped with `vec_avg(...)`, EVENT fields are skipped for VECTOR runs, and low-coverage fields are backfilled. The result is recorded as `alpha_list_field_factory` and merged into `alpha_list_combined`.

Initial generated alpha lists rotate simulation neutralization settings instead of using only one setting. USA/EUR runs start with the configured setting and sweep `INDUSTRY`, `SUBINDUSTRY`, `SECTOR`, and `MARKET`; multi-country regions such as ASI/GLB prefer `MARKET` early. Enhance and variant-search sweeps also try wider setting-level neutralizations including `SLOW_AND_FAST`, `FAST`, `SLOW`, and `NONE` when budget allows.

## Factor Thesis

Every candidate carries a `factor_thesis` object. If the generator emits thesis fields, they are preserved; otherwise `brain_agent` infers a conservative thesis from the expression. Memory learns thesis types, expected failure modes, and intended repair methods. Reports include a Factor Thesis summary.

## Concurrency Safety

Avoid running another BRAIN simulation tool concurrently with `brain_agent` batch simulations for the same BRAIN account. They share platform quota and can increase 429 rate limits, platform queueing, and `Retry-After` waits.

If a batch simulation appears stuck:
1. Run `brain_agent tasks --refresh`.
2. Read the `batchSim` task stdout/stderr logs.
3. Look for `[BRAIN wait]` and `[BRAIN healthcheck]` messages.
4. Decide whether to keep waiting, cancel, retry, or resume.

## Submission Safety

Never call any production submission path unless the user explicitly confirms submission for specific alpha IDs. When the user says "不要自动 submit", only run checks and reports — list submit-ready alpha IDs for human review.
