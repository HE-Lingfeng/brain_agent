#!/usr/bin/env python3
"""Gate-check targeted variants: reduce self-correlation, boost Sharpe/Fitness."""
from __future__ import annotations

import copy, csv, json, os

OUTPUT = ".claude/skills/brain-simAlphasinBatch-and-track/data/gate_fix_expressions.json"
ALPHA_LIST_PATH = ".claude/skills/brain-simAlphasinBatch-and-track/data/alpha_list.json"

SETTINGS = {
    "instrument_type": "EQUITY", "region": "USA", "universe": "TOP3000",
    "delay": 1, "decay": 4, "neutralization": "INDUSTRY", "truncation": 0.08,
    "test_period": "P0Y0M", "unit_handling": "VERIFY", "nan_handling": "OFF",
    "language": "FASTEXPR", "pasteurization": "ON", "max_trade": "OFF",
    "visualization": False,
}

# Core expression
CORE = "divide(ts_backfill(inst6_total_shares_held_by_institutions, 20), ts_backfill(inst6_total_share_held_by_owners, 20))"

_already = set()
def load():
    for p in [ALPHA_LIST_PATH]:
        if not os.path.exists(p): continue
        with open(p) as f:
            for e in json.load(f):
                _already.add(e.get("regular","").strip())
    for path in [
        ".claude/skills/brain-simAlphasinBatch-and-track/outputs/simulation_status.csv",
        ".claude/skills/brain-simAlphasinBatch-and-track/outputs/variant_simulation_status_round2.csv",
        ".claude/skills/brain-simAlphasinBatch-and-track/outputs/novel_simulation_status.csv",
    ]:
        if not os.path.exists(path): continue
        with open(path) as f:
            for r in csv.DictReader(f):
                _already.add(r.get("regular_expression","").strip())

load()

exprs = []
def add(expr, note, settings_patch=None):
    if expr.strip() in _already:
        print(f"  SKIP: {note}")
        return
    _already.add(expr.strip())
    s = copy.deepcopy(SETTINGS)
    if settings_patch:
        s.update(settings_patch)
    exprs.append({"type": "REGULAR", "settings": s, "regular": expr})
    print(f"  ADD [{note}]: {expr[:100]}...")

# ═══════════════════════════════════════════════════
# STRATEGY 1: Explicit group_neutralize (reduces self-corr directly)
# ═══════════════════════════════════════════════════
# Instead of relying on setting-level neutralization, wrap explicitly
for group in ["industry", "subindustry", "sector"]:
    for decay in [4, 8, 10]:
        s = {"neutralization": "SLOW_AND_FAST", "decay": decay}
        add(f"group_neutralize(({CORE}), {group})", f"group_neut_{group}_d{decay}", s)

# ═══════════════════════════════════════════════════
# STRATEGY 2: ts_decay_linear to reduce serial correlation
# ═══════════════════════════════════════════════════
for d in [4, 8, 12, 20]:
    s = {"decay": d}
    add(f"ts_decay_linear(({CORE}), {d})", f"decay_linear_d{d}", s)
    # Rank + decay linear combo
    add(f"rank(ts_decay_linear(({CORE}), {d}))", f"rank_decay_linear_d{d}", s)

# ═══════════════════════════════════════════════════
# STRATEGY 3: winsorize for outlier control (reduces self-corr)
# ═══════════════════════════════════════════════════
for std in [4, 3, 5]:
    add(f"winsorize(({CORE}), std={std})", f"winsorize_std{std}")

# ═══════════════════════════════════════════════════
# STRATEGY 4: Combined: winsorize + group_neutralize + decay_linear
# ═══════════════════════════════════════════════════
for decay in [4, 8]:
    s = {"neutralization": "SLOW_AND_FAST", "decay": decay}
    add(f"group_neutralize(winsorize(ts_decay_linear(({CORE}), {decay}), std=4), industry)", f"triple_combo_d{decay}", s)

# ═══════════════════════════════════════════════════
# STRATEGY 5: ts_delta variant (momentum = lower self-corr)
# ═══════════════════════════════════════════════════
# ts_delta versions with best settings
for w in [10, 20, 60]:
    for d in [4, 8]:
        s = {"decay": d}
        add(f"ts_delta(({CORE}), {w})", f"ts_delta_w{w}_d{d}", s)
        add(f"group_neutralize(ts_delta(({CORE}), {w}), industry)", f"group_neut_ts_delta_w{w}_d{d}", s)

# Rank + ts_delta (rank helps gate scores)
for w in [20, 60]:
    s = {"decay": 4}
    add(f"rank(ts_delta(({CORE}), {w}))", f"rank_ts_delta_w{w}_d4", s)

# ═══════════════════════════════════════════════════
# STRATEGY 6: ts_av_diff (smoother than delta, lower corr)
# ═══════════════════════════════════════════════════
for w in [20, 60, 120]:
    for d in [4, 8]:
        s = {"decay": d}
        add(f"ts_av_diff(({CORE}), {w})", f"ts_av_diff_w{w}_d{d}", s)
        add(f"group_neutralize(ts_av_diff(({CORE}), {w}), industry)", f"group_neut_av_diff_w{w}_d{d}", s)

# ═══════════════════════════════════════════════════
# STRATEGY 7: Longer backfill window (slower changing = lower corr)
# ═══════════════════════════════════════════════════
CORE_60 = "divide(ts_backfill(inst6_total_shares_held_by_institutions, 60), ts_backfill(inst6_total_share_held_by_owners, 60))"
for d in [4, 8, 10]:
    s = {"decay": d}
    add(CORE_60, f"core_60d_d{d}", s)
    add(f"group_neutralize(({CORE_60}), industry)", f"group_neut_core60_d{d}", s)

CORE_120 = "divide(ts_backfill(inst6_total_shares_held_by_institutions, 120), ts_backfill(inst6_total_share_held_by_owners, 120))"
for d in [4, 8]:
    s = {"decay": d}
    add(CORE_120, f"core_120d_d{d}", s)

# ═══════════════════════════════════════════════════
# STRATEGY 8: zscore instead of raw (cross-sectional normalization)
# ═══════════════════════════════════════════════════
for d in [4, 8]:
    s = {"decay": d}
    add(f"zscore(({CORE}))", f"zscore_d{d}", s)
    add(f"group_neutralize(zscore(({CORE})), industry)", f"group_neut_zscore_d{d}", s)

# ═══════════════════════════════════════════════════
# STRATEGY 9: Trade when liquidity filter
# ═══════════════════════════════════════════════════
add(f"trade_when(volume > adv20, ({CORE}), -1)", "trade_when_liquidity")

os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
with open(OUTPUT, "w") as f:
    json.dump(exprs, f, indent=2)
print(f"\nGenerated {len(exprs)} gate-fix variants -> {OUTPUT}")
