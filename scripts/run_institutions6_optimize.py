#!/usr/bin/env python3
"""Optimize top institutions6 expressions via window sweeps, sign flips, and neutralization."""
from __future__ import annotations

import copy, csv, json, os, sys, subprocess
from pathlib import Path

BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "5"))
CONCURRENCY = int(os.environ.get("CONCURRENCY", "3"))
OUTPUT_DIR = Path(".brain_runtime/runs/institutions6_optimize/artifacts/03_simulate")
SKILL_ROOT = Path(".agents/skills/brain-simAlphasinBatch-and-track")
SIMULATOR = SKILL_ROOT / "scripts" / "batch_simulator.py"
CONFIG_STUB = OUTPUT_DIR / "config" / "empty_config.json"

BASE_SETTINGS = {
    "instrumentType": "EQUITY", "region": "USA", "universe": "TOP3000",
    "delay": 1, "decay": 4, "neutralization": "INDUSTRY", "truncation": 0.08,
    "testPeriod": "P0Y0M0D", "unitHandling": "VERIFY", "nanHandling": "OFF",
    "language": "FASTEXPR", "pasteurization": "ON", "maxTrade": "OFF",
    "visualization": False,
}

# Field shorthand
bf = lambda f, w=20: f"ts_backfill({f}, {w})"
H  = "inst6_total_shares_held_by_institutions"
O  = "inst6_total_share_held_by_owners"
CB = "count_institutional_buyers_security"
CS = "count_institutional_sellers_security"
CH = "count_institutional_holders_security"
VB = "inst6_value_of_institutional_shares_bought"
VS = "inst6_value_of_institutional_shares_sold"
VH = "inst6_value_held_by_institutions"
B  = "inst6_num_of_institutional_shares_bought"
S  = "inst6_num_of_institutional_shares_sold"
EV = "aggregate_equity_value_all_owners"
NB = "inst6_num_of_institutional_buyers"

expressions = []
def add(expr: str, note: str = "", settings_override: dict | None = None) -> None:
    s = copy.deepcopy(BASE_SETTINGS)
    if settings_override:
        s.update(settings_override)
    expressions.append({"type": "REGULAR", "settings": s, "regular": expr})
    tag = f" [{note}]" if note else ""
    print(f"  +{tag} {expr[:100]}...")

# ══════════════════════════════════════════════════════════════
# 1. Ownership Ratio — Window & Decay Sweep (top performer: Sharpe 1.24)
# ══════════════════════════════════════════════════════════════
ownership = f"divide({bf(H, '{}')}, {bf(O, '{}')})"

# Backfill window sweep
for w in [10, 20, 40, 60, 120]:
    expr = ownership.replace("{}", str(w))
    add(expr, f"ownership_ratio_bf{w}")

# Ownership ratio delta — window sweep
for w in [10, 20, 40, 60]:
    for d in [10, 20, 40, 63]:
        expr = f"ts_delta(divide({bf(H, w)}, {bf(O, w)}), {d})"
        add(expr, f"ownership_delta_w{w}_d{d}")

# Ownership ratio with decay sweep
for decay in [2, 6, 8, 10]:
    s = copy.deepcopy(BASE_SETTINGS)
    s["decay"] = decay
    expr = ownership.replace("{}", "20")
    expressions.append({"type": "REGULAR", "settings": s, "regular": expr})
    print(f"  + [ownership_decay{decay}] {expr[:100]}...")

# ══════════════════════════════════════════════════════════════
# 2. Net Buyer Momentum — Window Sweep (Sharpe 1.06)
# ══════════════════════════════════════════════════════════════
for w in [10, 20, 40, 60]:
    for d in [5, 10, 20, 40, 63]:
        expr = f"ts_delta(subtract({bf(CB, w)}, {bf(CS, w)}), {d})"
        add(expr, f"net_buyer_mom_w{w}_d{d}")

# Net buyer momentum with longer backfill
for d in [20, 40, 63]:
    expr = f"ts_delta(subtract({bf(CB, 60)}, {bf(CS, 60)}), {d})"
    add(expr, f"net_buyer_mom_long_w60_d{d}")

# ══════════════════════════════════════════════════════════════
# 3. Seller Decline — Window Sweep (Sharpe 0.92)
# ══════════════════════════════════════════════════════════════
for w in [10, 20, 40, 60]:
    for d in [10, 20, 40, 63]:
        expr = f"subtract(0, ts_delta({bf(CS, w)}, {d}))"
        add(expr, f"seller_decline_w{w}_d{d}")

# ══════════════════════════════════════════════════════════════
# 4. Sign-Flipped Negative Expressions (original run)
# ══════════════════════════════════════════════════════════════
flips = [
    # Net value flow × ownership (Sharpe -0.51)
    f"multiply(-1, multiply((divide(subtract({bf(VB)}, {bf(VS)}), {bf(EV)})), (divide({bf(H)}, {bf(O)}))))",
    # Net value flow / inst value (Sharpe -0.47)
    f"multiply(-1, divide(subtract({bf(VB)}, {bf(VS)}), {bf(VH)}))",
    # Net share flow / total shares (Sharpe -0.45)
    f"multiply(-1, divide(subtract({bf(B)}, {bf(S)}), {bf(O)}))",
    # Net share flow momentum (Sharpe -0.35)
    f"multiply(-1, ts_delta(subtract({bf(B)}, {bf(S)}), 20))",
]
for expr in flips:
    add(expr, "sign_flipped")

# ══════════════════════════════════════════════════════════════
# 5. Refined Ownership + Flow Combos
# ══════════════════════════════════════════════════════════════

# Ownership × buyer breadth (cleaner version of interaction)
add(f"multiply(divide({bf(H)}, {bf(O)}), divide({bf(CB)}, {bf(CH)}))", "ownership_x_buyer_breadth")

# Net buyer per holder (normalized breadth)
for w in [20, 60]:
    expr = f"ts_delta(divide({bf(CB, w)}, {bf(CH, w)}), 20)"
    add(expr, f"buyer_per_holder_delta_w{w}")

# Net flow over gross flow (directional conviction)
add(f"ts_delta(divide(subtract({bf(VB)}, {bf(VS)}), add({bf(VB)}, {bf(VS)})), 20)", "net_over_gross_flow_delta")

# ══════════════════════════════════════════════════════════════
# 6. Value-Based Flow Signals
# ══════════════════════════════════════════════════════════════

# Net value flow / total market value
for d in [10, 20, 40, 63]:
    expr = f"ts_delta(divide(subtract({bf(VB)}, {bf(VS)}), {bf(EV)}), {d})"
    add(expr, f"net_value_flow_over_mktcap_d{d}")

# Value flow momentum (buy value minus sell value)
for d in [20, 40, 63]:
    expr = f"ts_delta(subtract({bf(VB)}, {bf(VS)}), {d})"
    add(expr, f"value_flow_momentum_d{d}")

# ══════════════════════════════════════════════════════════════
# 7. Decay & Truncation Sweep on Top Performer
# ══════════════════════════════════════════════════════════════
for decay in [2, 4, 6, 8, 10]:
    for trunc in [0.05, 0.08, 0.10]:
        s = copy.deepcopy(BASE_SETTINGS)
        s["decay"] = decay
        s["truncation"] = trunc
        expr = f"divide({bf(H, 20)}, {bf(O, 20)})"
        expressions.append({"type": "REGULAR", "settings": s, "regular": expr})
        print(f"  + [ownership_decay{decay}_trunc{trunc}] {expr[:100]}...")

# ── Write alpha list & run batch simulator ───────────────────────────
input_dir = OUTPUT_DIR / "input"
input_dir.mkdir(parents=True, exist_ok=True)
(OUTPUT_DIR / "config").mkdir(parents=True, exist_ok=True)
CONFIG_STUB.write_text("{}")

alpha_json = input_dir / "institutions6_optimize.json"
with open(alpha_json, "w") as f:
    json.dump(expressions, f, indent=2)

print(f"\nTotal expressions: {len(expressions)}")
print(f"Alpha list: {alpha_json}")

output_csv = (OUTPUT_DIR / "simulation_status.csv").resolve()
cmd = [
    sys.executable,
    str(SIMULATOR.resolve()),
    "--config", str(CONFIG_STUB.resolve()),
    "--alpha-json", str(alpha_json.resolve()),
    "--output-csv", str(output_csv),
    "--batch-size", str(BATCH_SIZE),
    "--concurrency", str(CONCURRENCY),
]

print(f"\nRunning batch simulator...")
result = subprocess.run(cmd, cwd=str(SKILL_ROOT / "scripts"))

# ── Print results ────────────────────────────────────────────────────
if output_csv.exists():
    with open(output_csv) as f:
        reader = list(csv.DictReader(f))

    seen = set()
    completed = []
    for row in reader:
        expr = row.get("regular_expression", "")
        status = row.get("status", "")
        if status == "COMPLETE" and expr not in seen:
            seen.add(expr)
            completed.append(row)

    completed.sort(key=lambda r: abs(float(r.get("sharpe", 0) or 0)), reverse=True)

    print(f"\n=== Optimized Results: {len(completed)} unique COMPLETE ===\n")
    for i, row in enumerate(completed[:20]):
        sharpe = float(row.get("sharpe", 0) or 0)
        fitness = float(row.get("fitness", 0) or 0)
        turnover = float(row.get("turnover", 0) or 0)
        expr = row.get("regular_expression", "")
        print(f"{i+1}. Sharpe={sharpe:+.2f}  Fitness={fitness:+.2f}  Turnover={turnover:.4f}")
        print(f"   {expr[:130]}")
        print()
else:
    print(f"Output CSV not found: {output_csv}")
