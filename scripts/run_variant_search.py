#!/usr/bin/env python3
"""Generate variants for top institutions6 expressions and submit for backtest.

Variants for the two best expressions (#5 ts_delta ownership, #2 ownership ratio):
- Window sweeps (5, 10, 60, 120 replacing 20)
- Decay sweeps (4, 8, 12, 20)
- Neutralization sweeps (INDUSTRY, SUBINDUSTRY, SECTOR, MARKET)
- Rank wrapping (for expressions without it)
- ts_av_diff as alternative to ts_delta (smoother)
- ts_regression for trend detection
"""
from __future__ import annotations

import copy
import csv
import json
import os
import re

CSV_PATH = ".claude/skills/brain-simAlphasinBatch-and-track/outputs/simulation_status.csv"
ALPHA_LIST_PATH = ".claude/skills/brain-simAlphasinBatch-and-track/data/alpha_list.json"
VARIANT_OUTPUT_PATH = ".claude/skills/brain-simAlphasinBatch-and-track/data/variant_alpha_list.json"

# Parent expressions (the two strongest)
P5 = "ts_delta(divide(ts_backfill(inst6_total_shares_held_by_institutions, 20), ts_backfill(inst6_total_share_held_by_owners, 20)), 20)"
P2 = "divide(ts_backfill(inst6_total_shares_held_by_institutions, 20), ts_backfill(inst6_total_share_held_by_owners, 20))"

# Base settings
SETTINGS = {
    "instrument_type": "EQUITY",
    "region": "USA",
    "universe": "TOP3000",
    "delay": 1,
    "decay": 10,
    "neutralization": "SLOW_AND_FAST",
    "truncation": 0.08,
    "test_period": "P0Y0M",
    "unit_handling": "VERIFY",
    "nan_handling": "OFF",
    "language": "FASTEXPR",
    "pasteurization": "ON",
    "max_trade": "OFF",
    "visualization": False,
}

WINDOWS = [5, 10, 60, 120]
DECAYS = [4, 8, 12, 20]
NEUTRALIZATIONS = ["INDUSTRY", "SUBINDUSTRY", "SECTOR", "MARKET"]


def _all_windows(expr: str) -> list[int]:
    """Find all window arguments in the expression."""
    return [int(m.group(3)) for m in re.finditer(r"\b(ts_[A-Za-z0-9_]+)\(([^()]*(?:\([^()]*\)[^()]*)*?),\s*(\d+)", expr)]


def _replace_window(expr: str, target_win: int, new_win: int, count: int = 99) -> str:
    """Replace window values in ts_* calls."""
    pattern = r"\b(ts_[A-Za-z0-9_]+)\(([^()]*(?:\([^()]*\)[^()]*)*?),\s*(\d+)"
    replaced = 0
    def _repl(m):
        nonlocal replaced
        if int(m.group(3)) == target_win and replaced < count:
            replaced += 1
            return f"{m.group(1)}({m.group(2)}, {new_win}"
        return m.group(0)
    return re.sub(pattern, _repl, expr)


def _replace_all_windows(expr: str, new_win: int) -> str:
    """Replace ALL window values with new_win."""
    def _repl(m):
        return f"{m.group(1)}({m.group(2)}, {new_win}"
    return re.sub(r"\b(ts_[A-Za-z0-9_]+)\(([^()]*(?:\([^()]*\)[^()]*)*?),\s*(\d+)", _repl, expr)


def _replace_tsdelta_with_tsavdiff(expr: str) -> str | None:
    """Replace ts_delta with ts_av_diff for smoother trend detection."""
    if "ts_delta" not in expr:
        return None
    return re.sub(r"\bts_delta\(", "ts_av_diff(", expr)


def _shorten(expr: str, limit: int = 90) -> str:
    return expr[:limit] + ("..." if len(expr) > limit else "")


def main():
    # Read existing expressions to avoid duplicates
    with open(ALPHA_LIST_PATH) as f:
        alpha_list = json.load(f)
    # Build (expr, settings_key) for existing alphas
    existing_keys = set()
    for e in alpha_list:
        s = e.get("settings", {})
        sk = json.dumps({k: s.get(k) for k in ["decay","delay","instrument_type","language","nan_handling","neutralization","pasteurization","region","test_period","truncation","universe","unit_handling"] if k in s}, sort_keys=True)
        existing_keys.add((e["regular"], sk))

    variants = []
    seen = set()

    def _settings_key(s: dict) -> str:
        return json.dumps({k: s[k] for k in sorted(s)}, sort_keys=True)

    def add(expr: str, strategy: str, params: dict | None = None, settings_patch: dict | None = None):
        settings = copy.deepcopy(SETTINGS)
        if settings_patch:
            settings.update(settings_patch)
        sk = _settings_key(settings)
        key = (expr, sk)
        if key in seen or key in existing_keys:
            return
        seen.add(key)
        variants.append({
            "type": "REGULAR",
            "settings": settings,
            "regular": expr,
            "variant_strategy": strategy,
            "variant_params": params or {},
        })
        print(f"  [{strategy}] {_shorten(expr)}")

    parents = [P5, P2]

    for parent in parents:
        parent_name = "P5_ts_delta" if "ts_delta" in parent else "P2_ownership"
        print(f"\n=== Variants for {parent_name} ===")
        print(f"  Parent: {_shorten(parent)}")

        # 1. Rank wrapping (key variant - neither has rank())
        add(f"rank({parent})", "rank_normalize", {"operation": "rank"})

        # 2. Window sweeps
        current_windows = _all_windows(parent)
        for cw in set(current_windows):
            for w in WINDOWS:
                if w != cw:
                    new_expr = _replace_window(parent, cw, w, count=1)
                    add(new_expr, "window_sweep", {"from": cw, "to": w, "operator": "ts_window"})

            # Replace ALL windows uniformly
            for w in WINDOWS:
                if w != cw:
                    new_expr = _replace_all_windows(parent, w)
                    add(new_expr, "window_sweep_all", {"from": cw, "to": w, "all_windows": True})

        # 3. Decay sweeps
        for d in DECAYS:
            if d != SETTINGS["decay"]:
                add(parent, "decay_sweep", {"from": SETTINGS["decay"], "to": d}, {"decay": d})

        # 4. Neutralization sweeps
        for n in NEUTRALIZATIONS:
            if n != SETTINGS["neutralization"]:
                add(parent, "neutralization_sweep", {"from": SETTINGS["neutralization"], "to": n}, {"neutralization": n})

        # 5. ts_av_diff instead of ts_delta (only for P5)
        if "ts_delta" in parent:
            av_diff_expr = _replace_tsdelta_with_tsavdiff(parent)
            if av_diff_expr:
                add(av_diff_expr, "ts_av_diff", {"from": "ts_delta", "to": "ts_av_diff"})

        # 6. Combined: rank + window optimization
        if "ts_delta" in parent:
            # P5: ts_delta(ownership_pct, 20) at window 60
            for w in [5, 10, 60]:
                delta_swapped = _replace_window(parent, 20, w, count=1)
                add(f"rank({delta_swapped})", "rank_window_sweep", {"window": w, "operation": "rank"})

        # 7. P2: static ownership ratio with ts_regression (trend detection)
        if "ts_delta" not in parent and "divide" in parent:
            # Static ownership with momentum overlay via regression
            add(f"ts_regression({parent}, {parent}, 60)", "ts_regression", {"window": 60, "operation": "regression"})
            add(f"rank(ts_regression({parent}, {parent}, 60))", "rank_ts_regression", {"window": 60})

        # 8. Additional creative variant: net buying pressure normalized (only for P5)
        if "ts_delta" in parent:
            # The core expression inside ts_delta
            core = "divide(ts_backfill(inst6_total_shares_held_by_institutions, 20), ts_backfill(inst6_total_share_held_by_owners, 20))"
            # ts_av_diff with different windows
            add(f"ts_av_diff({core}, 60)", "ts_av_diff_window60", {"from": 20, "to": 60})
            add(f"ts_av_diff({core}, 120)", "ts_av_diff_window120", {"from": 20, "to": 120})
            # rank(ts_av_diff(...)) variants
            for w in [20, 60]:
                add(f"rank(ts_av_diff({core}, {w}))", "rank_ts_av_diff", {"window": w})

    print(f"\n=== Summary ===")
    print(f"Total variants generated: {len(variants)}")

    # Count by strategy
    from collections import Counter
    strat_counts = Counter(v["variant_strategy"] for v in variants)
    for s, c in strat_counts.most_common():
        print(f"  {s}: {c}")

    # Write output
    os.makedirs(os.path.dirname(VARIANT_OUTPUT_PATH), exist_ok=True)
    with open(VARIANT_OUTPUT_PATH, "w") as f:
        json.dump(variants, f, indent=2)
    print(f"\nWritten to {VARIANT_OUTPUT_PATH}")


if __name__ == "__main__":
    main()
