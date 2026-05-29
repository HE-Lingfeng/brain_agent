# Low Token Project Context

Read this file first when optimizing or debugging this repository. It is the compact shared context for Codex and Claude Code, so avoid rescanning the whole repo unless the task requires it.

## Non-Negotiables

- Maintained entry point: `brain_agent`.
- Do not use `brain-mcp`.
- Never production-submit alphas unless the user explicitly confirms specific alpha IDs.
- Before changing code for a new idea, first explain value, applicability, and simpler alternatives. Apply changes only after user approval.
- Keep `README.md` and `codex_pipeline.md` updated when CLI flags, run behavior, task handling, or artifact formats change.
- The worktree is often dirty. Do not revert unrelated user changes.

## Fast Start

Use this order before broad file reads:

1. Read `CODEX_CONTEXT.md`.
2. Read only the relevant module(s) from the map below.
3. Check `git status --short` to avoid touching unrelated edits.
4. Run focused tests before broad tests.

Common commands:

```bash
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime doctor --check-llm
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime run ...
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime worker --run-id <run_id> --mode drain --max-total-alphas 5000
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime tasks --run-id <run_id> --refresh
PYTHONPATH=.. python3 -m brain_agent --runtime-root .brain_runtime retry-sim --run-id <run_id> --dry-run
PYTHONPATH=.. python3 -m unittest tests.test_brain_agent
```

## Module Map

- `cli.py`: CLI flags and subcommands.
- `controller.py`: end-to-end run/resume loop and phase orchestration.
- `worker.py`: non-interactive drain mode that keeps generating/submitting/resuming.
- `adapters.py`: bridges to legacy skills, BRAIN datafields, simulation artifacts, PnL cache, Field Factory.
- `variant_search.py`: variant generation, neutralization/decay sweep, PnL prune.
- `optimizers.py`: optimizer classes including second-order variants.
- `repository.py`: SQLite persistence and candidate/task/run queries.
- `models.py`: shared dataclasses/enums.
- `scoring.py`, `selection.py`, `decision.py`: candidate scoring, selection, and run decisions.
- `reporting.py`: run reports and summaries.
- `quota_allocator.py`: simulation quota allocation when max sim alphas is constrained.

## Current Throughput Features

- Worker/drain mode exists and is the preferred way to avoid repeated manual Claude/Codex interaction.
- Non-GLB default simulation slots are `batch_size=10`, `concurrency=8`; GLB remains more conservative.
- Retryable platform failures such as `BATCH_SPAWN_FAILED` and `TIMEOUT` can be requeued via `retry-sim`.
- Variant pipeline includes second-order wrapping, neutralization/decay cross sweep, lightweight window/rank/zscore perturbations, and PnL prune when `pnl_cache.json` is available.
- Real inspect runs add a conservative Field Factory list from datafield metadata: MATRIX fields direct, VECTOR fields wrapped with `vec_avg(...)`, low coverage fields backfilled.
- Simulation JSON metadata leak was fixed by stripping internal fields before BRAIN submission. If variant submissions fail with unexpected property errors, treat that as a regression.

## Expression Constraints

- MATRIX fields can be used directly.
- VECTOR fields must be reduced first with `vec_*`, usually `vec_avg(field)`.
- Put `ts_backfill` close to the field for sparse or quarterly data.
- Prefer simple time-series/arithmetic expressions before complex cross-sectional/group wrappers.
- Prefer `multiply(-1, expr)` for sign flip.
- Be careful with variant double-wrapping such as `rank(rank(...))` or incompatible `zscore/group_neutralize` combinations.

## Low Token Work Modes

For review-only requests:

```text
低 token 模式：先读 CODEX_CONTEXT.md，只检查 <files>，不要改代码，输出 P0/P1/P2。
```

For approved small fixes:

```text
低 token 模式：只修已确认的 P0/P1，限制在 <files>，跑 tests.test_brain_agent。
```

For BRAIN runs:

```text
低 token 模式：使用 brain_agent worker/drain，不调用 submit，报告 run_id 和 submit-ready alpha IDs。
```

## When To Read More

Escalate beyond this file only when:

- The user asks for architecture-level changes.
- A test failure points to a module not listed above.
- CLI behavior, artifact schema, or task state transitions are involved.
- A production BRAIN run is stuck and task logs must be inspected.
- The task involves external Brain_v2/reference code.
