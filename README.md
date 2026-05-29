# brain_agent

`brain_agent` is a local WorldQuant BRAIN alpha-mining orchestrator. It coordinates idea generation, inspection, simulation, enhancement, reporting, memory, and submission-readiness checks while keeping credentials outside the repository.

## Repository Layout

The Python package files live at the repository root. If this repository is cloned as `brain_agent`, run it from the parent directory:

For Codex/Claude Code maintenance, start with `CODEX_CONTEXT.md`. It is a compact project map with current invariants, high-throughput features, common commands, and the smallest set of files to inspect for common tasks.

```bash
python3 -m brain_agent --help
```

From inside the repository directory, run it with the parent directory on `PYTHONPATH`:

```bash
PYTHONPATH=.. python3 -m brain_agent --help
```

## Credentials

Do not commit credentials to this repository. The agent reads credentials from environment variables or a local secret file.

Environment variables:

```bash
export BRAIN_EMAIL=<brain-email>
export BRAIN_PASSWORD=<brain-password>
export LLM_PROVIDER=moonshot
export LLM_API_KEY=<llm-api-key>
export LLM_BASE_URL=https://api.moonshot.cn/v1
export LLM_MODEL=kimi-k2.5
```

Optional local secret file path:

```text
~/secrets/worldquant-brain.json
```

Expected shape:

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

`provider` can be `moonshot`, `deepseek`, `openai`, or any OpenAI-compatible endpoint when `base_url` and `model` are set. Legacy `MOONSHOT_API_KEY`, `MOONSHOT_BASE_URL`, `MOONSHOT_MODEL`, and `moonshot_*` secret keys still work for older local scripts, but `LLM_*` and `llm.api_key/base_url/model` are preferred.

The repository `.gitignore` uses a default-deny policy so runtime data, documents, caches, and secret-like files stay local.

## Basic Commands

```bash
PYTHONPATH=.. python3 -m brain_agent doctor --check-llm
```

List or choose reusable dataset/settings presets:

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime settings list
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime settings choose --print-command
```

```bash
PYTHONPATH=.. python3 -m brain_agent run \
  --dataset fundamental31 \
  --preset eur_top2500_slow_fast \
  --target-ready 1 \
  --max-iterations 1 \
  --max-sim-alphas 1 \
  --max-variant-alphas 6 \
  --max-variants-per-alpha 3
```

```bash
PYTHONPATH=.. python3 -m brain_agent report --run-id <run_id>
```

During real batch simulation, the foreground CLI prints a compact progress line like:

```text
simulation: [########............] 8/20 (40.0%) | running=4 | slots=4x10 | capacity=40 | remaining=12 | completed=7 | failed=1
```

Simulation defaults are intentionally conservative to reduce BRAIN rate limits while still filling quota: non-GLB regions use `--concurrency 8 --batch-size 10` by default, while GLB uses `--concurrency 4 --batch-size 4`. You can still override them manually; the batch simulator clamps requests above the regional safety limits (`8x10` outside GLB, `4x10` for GLB). Batch polling now waits up to 30 minutes for parent batches to spawn children and up to 60 minutes for children to finish, because BRAIN queueing during peak hours can otherwise look like a failed batch. Parent, child, and single-simulation status fetches use bounded retries for transient HTTP, JSON, or empty-response reads before recording a failure.

You can also refresh the latest task and simulation snapshot:

```bash
PYTHONPATH=.. python3 -m brain_agent tasks --run-id <run_id> --refresh
PYTHONPATH=.. python3 -m brain_agent status --run-id <run_id>
```

If a run contains retryable platform failures such as `TIMEOUT` or `BATCH_SPAWN_FAILED`, rebuild a clean retry list and resimulate only those candidates:

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime retry-sim \
  --run-id <run_id> \
  --batch-size 10 \
  --concurrency 8
```

Use `--dry-run` to write `alpha_list_retryable.json` without submitting.

For non-interactive quota draining, run the worker against an existing run. By default each worker batch is capped to `batch_size * concurrency` candidates, and `--max-total-alphas` is enforced against the remaining quota before each batch starts:

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime worker \
  --run-id <run_id> \
  --mode drain \
  --max-total-alphas 5000
```

To inspect research quality across recent runs:

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime research summary --limit 20
```

You can filter by dataset/region, specific run ids, switch to JSON, or write the report:

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime research summary \
  --dataset institutions6 \
  --region USA \
  --format markdown \
  --output .brain_runtime/research_quality/institutions6_usa.md
```

`run` supports `--preset <name>` and `--choose-settings` so frequent BRAIN settings do not need to be typed every session. Built-in presets intentionally do not include dataset ids because BRAIN datasets change over time; pass `--dataset <dataset_id>` for scripted runs, or use `--choose-settings` and enter the dataset interactively. Explicit flags such as `--neutralization` or `--decay` override the preset. Add personal presets in `<runtime-root>/settings_presets.json` if you want to maintain your own fixed choices:

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

Alpha Memory keeps generated candidates separate from simulated learning evidence. Pattern scoring uses only simulated/promising/gate-passed samples, tracks confidence and recency, and separates non-success from hard-failure risk so weak but syntactically valid patterns do not get treated like parser or coverage failures. Use the summary to inspect the current layers and learned operator/field-family/factor-thesis signals:

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime memory summary --dataset institutions6 --region USA
```

When `--max-sim-alphas` is set, batch simulation uses a quota allocator instead of taking the first N scored candidates. The allocator splits scarce quota across memory-backed exploitation, new-pattern exploration, and repairable failures, then writes `alpha_list_quota_report` under the run artifacts for review. The report includes selected/rejected candidate explanations, pattern confidence, historical success/failure adjustments, and duplicate-cluster penalties.

The make/inspect path now attaches a `factor_thesis` object to every candidate. If the generator emits thesis fields, they are preserved; otherwise `brain_agent` infers a conservative thesis from the expression. Candidate rows store `thesis_json`, generated artifacts include `factor_theses`, memory learns `thesis_type`, expected failure modes, and intended repair methods, and reports show a Factor Thesis summary.

Real inspect runs also add a small Field Factory alpha list from target datafield metadata. MATRIX fields are used directly, VECTOR fields are wrapped with `vec_avg(...)`, and low-coverage fields are backfilled before simple rank/mean/delta variants are emitted. The generated rows are recorded as `alpha_list_field_factory` and merged into `alpha_list_combined`.

Prompt A/B comparison is settings-aware. `prompt compare` only declares a direct winner when compared runs share the same dataset, region, delay, universe, data type, decay, truncation, neutralization, and max trade settings. The first `--run-id` is treated as the baseline; default prompt promotion is marked eligible only when valid rate, promising rate, average fitness, or hard-failure rate improves enough versus that baseline without hard-failure regression.

Submit gate checks read the same platform-backed checks exposed on alpha detail pages. The primary path uses `/alphas/{id}/check`, then falls back to `/alphas/{id}` `is.checks` when the check endpoint returns no rows, and normalizes platform check names such as `CONCENTRATED_WEIGHT` and `LOW_SUB_UNIVERSE_SHARPE`. Correlation rows inside `/check` can remain `PENDING`, so submit readiness ignores those rows and uses the dedicated self/prod correlation endpoints instead. Gate checks are non-fatal when the BRAIN API, proxy, or correlation endpoints fail mid-check. These rows are stored as `gate_status=incomplete` with an `error_type` such as `network_error`; reports show a Gate Incomplete summary, and the run can still finish without turning transient gate evidence gaps into alpha quality failures.

Run stability is defensive around common transient failures. Malformed LLM/legacy JSON artifacts are converted into structured adapter failures instead of raw tracebacks, unexpected controller errors are recorded on the run as `FAILED` with a report, and BRAIN datafield preflight API failures are stored as `datafields_preflight_incomplete` while batch simulation continues so per-alpha simulator evidence can still be collected.

After an initial simulation pass, local Variant Search can spend a small bounded budget on deterministic variants of alphas with weak or repairable signal. It now starts with named optimizer modules: `LowFitnessOptimizer` for near-zero Sharpe/Fitness, `LowTurnoverOptimizer` for over-smoothed or over-filtered alphas, `HighTurnoverOptimizer` for excessive turnover, `ShortFlipOptimizer` for inverse signals, `CoverageRepairer` for coverage/subuniverse/unavailable-field style failures, and `CorrelationOptimizer` for self/prod correlation failures when gate diagnostics are available. It then supplements them with close generic variants such as window sweep, decay sweep, rank/zscore swap, neutralization/decay cross sweep, turnover-control wrappers, and coverage repair wrappers. Completed simulations best-effort populate `pnl_cache.json`; when present, Variant Search uses the cached PnL series to prune highly correlated parents before generating variants. Variants are stored as normal candidates with lineage fields (`parent_candidate_id`, `variant_strategy`, `variant_params`, `lineage_json`), and `run_report.md` includes original-vs-variant deltas, variant-strategy effectiveness, quota-waste analysis, and next research recommendations after the current run has been ingested into memory.

Use `--max-variant-alphas 0` to disable local variant search. `--max-variants-per-alpha` controls how many close variants can be generated for one parent alpha.
