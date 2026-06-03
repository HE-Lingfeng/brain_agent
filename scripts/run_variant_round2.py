#!/usr/bin/env python3
"""Cross-combination variant sweep for top institutions6 expressions.

Round 2: cross neutralization × decay, truncation sweep, negative signal.
"""
from __future__ import annotations

import copy
import csv
import json
import os

VARIANT_OUTPUT = ".claude/skills/brain-simAlphasinBatch-and-track/data/variant_alpha_list_round2.json"
ALPHA_LIST_PATH = ".claude/skills/brain-simAlphasinBatch-and-track/data/alpha_list.json"
CSV_PATH = ".claude/skills/brain-simAlphasinBatch-and-track/outputs/variant_simulation_status.csv"

BASE_SETTINGS = {
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

# Core expressions
OWNERSHIP = "divide(ts_backfill(inst6_total_shares_held_by_institutions, 20), ts_backfill(inst6_total_share_held_by_owners, 20))"
TS_DELTA_OWNERSHIP = f"ts_delta({OWNERSHIP}, 20)"
TS_DELTA_OWNERSHIP_60 = f"ts_delta({OWNERSHIP}, 60)"
TS_AV_DIFF_OWNERSHIP = f"ts_av_diff({OWNERSHIP}, 20)"
TS_AV_DIFF_OWNERSHIP_60 = f"ts_av_diff({OWNERSHIP}, 60)"

BEST_NEUTS = ["INDUSTRY", "MARKET", "SECTOR"]
DECAYS = [4, 8, 12, 20]
TRUNCATIONS = [0.05, 0.10, 0.12]


def main():
    # Build (expr, settings_key) for all existing — from original alpha_list + round1 CSV
    existing_keys = set()
    with open(ALPHA_LIST_PATH) as f:
        for e in json.load(f):
            s = e.get("settings", {})
            sk = json.dumps({k: s[k] for k in sorted(s)}, sort_keys=True)
            existing_keys.add((e["regular"], sk))

    # Also load expressions already in round1 results
    if os.path.exists(CSV_PATH):
        with open(CSV_PATH) as f:
            for r in csv.DictReader(f):
                expr = r.get("regular_expression", "").strip()
                try:
                    s = json.loads(r.get("settings_json", "{}"))
                except:
                    s = {}
                sk = json.dumps({k: s[k] for k in sorted(s)}, sort_keys=True) if s else "{}"
                existing_keys.add((expr, sk))

    variants = []
    seen = set()

    def add(expr, settings_patch=None):
        nonlocal count
        s = copy.deepcopy(BASE_SETTINGS)
        if settings_patch:
            s.update(settings_patch)
        sk = json.dumps(s, sort_keys=True)
        key = (expr, sk)
        if key in seen or key in existing_keys:
            return
        seen.add(key)
        existing_keys.add(key)  # avoid intra-run dupes too
        count += 1
        variants.append({"type": "REGULAR", "settings": s, "regular": expr})

    count = 0
    for neut in BEST_NEUTS:
        for decay in DECAYS:
            count += 1
            add(OWNERSHIP, {"neutralization": neut, "decay": decay})

            if decay in (4, 8):  # Only for faster decays
                count += 1
                add(TS_DELTA_OWNERSHIP, {"neutralization": neut, "decay": decay})

            # ts_av_diff variants — fewer combos
            if decay == 8:
                count += 1
                add(TS_AV_DIFF_OWNERSHIP, {"neutralization": neut, "decay": decay})
                count += 1
                add(TS_AV_DIFF_OWNERSHIP_60, {"neutralization": neut, "decay": decay})

    # Truncation sweep on top performer (ownership + INDUSTRY, decay=10)
    for trunc in TRUNCATIONS:
        count += 1
        add(OWNERSHIP, {"neutralization": "INDUSTRY", "decay": 10, "truncation": trunc})
        add(OWNERSHIP, {"neutralization": "MARKET", "decay": 10, "truncation": trunc})

    # Negative signal (short side)
    for neut in BEST_NEUTS:
        count += 1
        add(f"multiply(-1, ({OWNERSHIP}))", {"neutralization": neut, "decay": 10})

    # ts_delta 60 + INDUSTRY (window sweep × best neut)
    add(TS_DELTA_OWNERSHIP_60, {"neutralization": "INDUSTRY", "decay": 10})
    add(TS_DELTA_OWNERSHIP_60, {"neutralization": "MARKET", "decay": 10})

    # ts_delta with wider ts_delta window (10, 60)
    add(f"ts_delta({OWNERSHIP}, 10)", {"neutralization": "INDUSTRY", "decay": 8})
    add(f"ts_delta({OWNERSHIP}, 60)", {"neutralization": "MARKET", "decay": 8})

    # 5-day backfill ownership + INDUSTRY (shorter lookback)
    OWNERSHIP_5 = "divide(ts_backfill(inst6_total_shares_held_by_institutions, 5), ts_backfill(inst6_total_share_held_by_owners, 5))"
    add(OWNERSHIP_5, {"neutralization": "INDUSTRY", "decay": 10})
    add(OWNERSHIP_5, {"neutralization": "MARKET", "decay": 10})

    print(f"Generated {len(variants)} cross-combination variants")
    print(f"(estimated {count} attempted, {len(variants)} after dedup)")

    # Summary
    from collections import Counter
    neuts = Counter(v["settings"]["neutralization"] for v in variants)
    decays = Counter(v["settings"]["decay"] for v in variants)
    print(f"Neutralizations: {dict(neuts)}")
    print(f"Decays: {dict(decays)}")

    os.makedirs(os.path.dirname(VARIANT_OUTPUT), exist_ok=True)
    with open(VARIANT_OUTPUT, "w") as f:
        json.dump(variants, f, indent=2)
    print(f"\nWritten to {VARIANT_OUTPUT}")


if __name__ == "__main__":
    main()
