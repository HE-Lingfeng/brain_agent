# AIWorker Claude Code Guide

This repository's long-term maintained automation layer is `brain_agent`.

## Low Token Startup

For optimization, debugging, and code maintenance requests, read `CODEX_CONTEXT.md` first. It is the compact shared context for Codex and Claude Code and should be used before broad repo scans. Only read additional files that are directly relevant to the requested change.

## Tool Routing

Use `brain_agent` as the default entry point for any end-to-end WorldQuant BRAIN alpha mining workflow:

- Generate alpha ideas and expressions
- Build alpha lists with canonical simulation settings
- Run batch simulations
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
```

Do not use `brain-mcp` in this repository. Route BRAIN platform queries, simulations, reports, retries, and gate checks through `brain_agent` so all activity is recorded under `.brain_runtime`, `brain_agent.sqlite3`, run reports, and candidate tracking.

## Change Approval

Before modifying any code — especially after learning from forum posts, external examples, or new ideas — first analyze the value and present your assessment. Do not apply the change until the user explicitly approves.

When evaluating a change, always consider:
- Whether it addresses a real problem in our current workflow
- Whether the scenario it solves actually applies to how we use the code
- Whether there are simpler alternatives

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

## Maintenance Preference

When adding durable functionality, add it to `brain_agent` first. Treat legacy implementations only as reference material unless the user explicitly asks to change them.

Keep `README.md` and `codex_pipeline.md` updated when changing CLI flags, run behavior, task handling, or artifact formats.

## Operator & Expression Guide

### Operator Documentation

The canonical operator reference is at `.agents/skills/brain-enhance-template/knowledge/template_final_enhance/op总结.md`. It covers all 7 operator categories with usage scenarios, safety patterns, and examples. Consult it when writing or reviewing expressions.

### Data Type Constraints

BRAIN data fields have two types: **MATRIX** (directly usable in expressions) and **VECTOR** (must be aggregated first).

**MATRIX data** (default) — supports all 84 operators returned by the operators API:
- **Arithmetic** (14): `add`, `subtract`, `multiply`, `divide`, `inverse`, `reverse`, `abs`, `log`, `sqrt`, `exp`, `power`, `signed_power`, `sign`, `max`, `min`
- **Logic/Filter** (13): `greater`, `less`, `if_else`, `trade_when`, `and`, `or`, `not`, `is_nan`, `equal`, `not_equal`, `greater_equal`, `less_equal`, `inst_pnl`
- **Time-Series** (30): `ts_mean`, `ts_delta`, `ts_delay`, `ts_sum`, `ts_std_dev`, `ts_av_diff`, `ts_zscore`, `ts_rank`, `ts_scale`, `ts_backfill`, `ts_decay_linear`, `ts_regression`, `ts_corr`, `ts_covariance`, `ts_quantile`, `ts_arg_max`, `ts_arg_min`, `ts_skewness`, `ts_entropy`, `ts_product`, `ts_step`, `ts_count_nans`, `ts_min_diff`, `ts_min_max_cps`, `ts_min_max_diff`, `days_from_last_change`, `last_diff_value`, `hump`, `kth_element`, `ts_target_tvr_decay`
- **Cross-Sectional / Group** (16): `rank`, `zscore`, `winsorize`, `normalize`, `quantile`, `truncate`, `scale`, `group_rank`, `group_zscore`, `group_neutralize`, `group_scale`, `group_mean`, `group_backfill`, `group_cartesian_product`, `group_extra`, `bucket`, `vector_proj`
- **Vector** (7): `vec_avg`, `vec_sum`, `vec_max`, `vec_min`, `vec_stddev`, `vec_range`, `vec_count` — only usable on VECTOR fields
- **Other** (4): `sigmoid`, `tanh`, `densify`, `regression_proj`

**VECTOR data** — fields are arrays, not scalars:
- Must be reduced to scalar via `vec_*` operators before use in arithmetic/time-series/cross-sectional operations
- Example: `ts_mean(vec_avg({vector_field}), 20)` — first aggregate, then smooth
- Never use a VECTOR field directly without a `vec_*` wrapper

**Key constraint**: The operators API may list all operators for a dataset, but the BRAIN simulation engine validates expressions at submission time. If an expression uses operators or syntax the engine rejects, it gets `SUBMISSION_FAILED`. Always verify the first batch of expressions before scaling up.

### Expression Writing Best Practices

1. **Start simple**. Time-series + arithmetic operators (`ts_delta`, `ts_mean`, `divide`, `subtract`) are the most reliable. Add cross-sectional operators (`rank`, `zscore`, `group_neutralize`) after basic expressions are confirmed to work.

2. **ts_backfill is essential for quarterly/infrequent data**. Datasets like institutions6 update quarterly. Without `ts_backfill(field, 20)` or similar, most values will be NaN. Apply backfill as the innermost operation.

3. **Avoid double-wrapping with rank/zscore in variants**. The variant search pipeline (optimizers.py) wraps expressions with `rank()`, `zscore()`, `group_neutralize()` etc. These can cause submission failures if the wrapped expression already has incompatible structure. Check variant simulation results and filter out failing patterns.

4. **Prefer `ts_mean` over `ts_decay_linear` for lower turnover**. `ts_mean(x, 20)` → ~16% turnover. `ts_decay_linear(x, 20)` → ~73% turnover. Use `ts_decay_linear` only when you need fast reaction and can afford the turnover.

5. **`multiply(-1, ...)` is preferred over `reverse(...)` or `-...`** for sign flipping — it's the most widely supported form.

### Known Issues

1. **Variant metadata leak regression check**: Variant submission payloads should strip internal metadata fields (`factorThesis`, `lineage`, `parentCandidateId`, `parentExpression`, `parentMetrics`, `variantParams`, `variantStrategy`) before calling BRAIN. If simulations fail with "Unexpected property", treat it as a regression and inspect the submission payload builder.

2. **Batch simulation queue delays**: During platform peak hours, batch simulations can wait 10+ minutes in the queue before children spawn. The BatchSimulator polls every ~60s with `[BRAIN wait]` messages. If wait exceeds 15 minutes, consider canceling and running individual simulations.
