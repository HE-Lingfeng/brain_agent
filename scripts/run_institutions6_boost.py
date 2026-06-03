#!/usr/bin/env python3
"""Push institutions6 Sharpe >1.5 via signal combinations, ts-normalization, and fine neutralization."""
from __future__ import annotations

import copy, csv, json, os, sys, subprocess
from pathlib import Path

BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "5"))
CONCURRENCY = int(os.environ.get("CONCURRENCY", "3"))
OUTPUT_DIR = Path(".brain_runtime/runs/institutions6_boost/artifacts/03_simulate")
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
NS = "inst6_num_of_institutional_sellers"

expressions = []
def add(expr: str, note: str = "", **kwargs) -> None:
    s = copy.deepcopy(BASE_SETTINGS)
    for k, v in kwargs.items():
        if k in s: s[k] = v
    expressions.append({"type": "REGULAR", "settings": s, "regular": expr})
    print(f"  + [{note}] {expr[:100]}...")

# ══════════════════════════════════════════════════════════════
# 1. SIGNAL COMBINATIONS — multiply for double confirmation
# ══════════════════════════════════════════════════════════════

# Core signals
S_OWN = f"divide({bf(H)}, {bf(O)})"                    # Ownership ratio (Sharpe 1.25)
S_NET = f"ts_delta(subtract({bf(CB)}, {bf(CS)}), 20)"  # Net buyer momentum (Sharpe 1.06)
S_SEL = f"subtract(0, ts_delta({bf(CS)}, 20))"         # Seller decline (Sharpe 0.92)
S_BPH = f"divide({bf(CB)}, {bf(CH)})"                   # Buyer per holder
S_NFV = f"divide(subtract({bf(VB)}, {bf(VS)}), {bf(EV)})"  # Net value flow / mkt cap

# Ownership × Net buyer momentum
add(f"multiply(({S_OWN}), ({S_NET}))", "own_x_netmom")

# Ownership × Seller decline
add(f"multiply(({S_OWN}), ({S_SEL}))", "own_x_selldec")

# Net buyer × Buyer breadth
add(f"multiply(({S_NET}), ({S_BPH}))", "netmom_x_breadth")

# Ownership × Buyer breadth
add(f"multiply(({S_OWN}), ({S_BPH}))", "own_x_breadth")

# Triple: Ownership × Net buyer × (1 - seller/holder)
add(f"multiply(multiply(({S_OWN}), ({S_NET})), divide(subtract({bf(CH)}, {bf(CS)}), {bf(CH)}))", "triple_own_net_nonsell")

# ══════════════════════════════════════════════════════════════
# 2. TIME-SERIES NORMALIZATION — ts_zscore / ts_rank of core signals
# ══════════════════════════════════════════════════════════════

# ts_zscore of ownership ratio — how extreme is ownership relative to its own history?
for d in [252, 504, 126, 63]:
    add(f"ts_zscore(divide({bf(H)}, {bf(O)}), {d})", f"own_ts_zscore_{d}")

# ts_zscore of net buyer momentum
for d in [252, 504, 126]:
    add(f"ts_zscore(ts_delta(subtract({bf(CB)}, {bf(CS)}), 20), {d})", f"netmom_ts_zscore_{d}")

# ts_rank of ownership ratio
for d in [252, 504]:
    add(f"ts_rank(divide({bf(H)}, {bf(O)}), {d})", f"own_ts_rank_{d}")

# ══════════════════════════════════════════════════════════════
# 3. NEUTRALIZATION SWEEP — try different neutralization methods
# ══════════════════════════════════════════════════════════════

for neut in ["SUBINDUSTRY", "SECTOR", "MARKET", "NONE"]:
    s = copy.deepcopy(BASE_SETTINGS)
    s["neutralization"] = neut
    expr = f"divide({bf(H)}, {bf(O)})"
    expressions.append({"type": "REGULAR", "settings": s, "regular": expr})
    print(f"  + [own_neut_{neut}] {expr[:100]}...")

for neut in ["SUBINDUSTRY", "SECTOR"]:
    s = copy.deepcopy(BASE_SETTINGS)
    s["neutralization"] = neut
    expr = f"ts_delta(subtract({bf(CB)}, {bf(CS)}), 20)"
    expressions.append({"type": "REGULAR", "settings": s, "regular": expr})
    print(f"  + [netmom_neut_{neut}] {expr[:100]}...")

# ══════════════════════════════════════════════════════════════
# 4. CONVICTION / SMART MONEY INDICATORS
# ══════════════════════════════════════════════════════════════

# Avg value per buyer (conviction) — higher = institutions betting bigger
buy_conviction = f"divide({bf(VB)}, add({bf(NB)}, 1))"
sell_conviction = f"divide({bf(VS)}, add({bf(NS)}, 1))"

# Buyer conviction minus seller conviction
add(f"subtract(({buy_conviction}), ({sell_conviction}))", "conviction_spread")

# Conviction spread × ownership
add(f"multiply(({S_OWN}), subtract(({buy_conviction}), ({sell_conviction})))", "own_x_conviction_spread")

# Conviction ratio (buy/sell)
add(f"divide(({buy_conviction}), add(({sell_conviction}), 1))", "conviction_ratio")

# ts_delta of conviction spread
add(f"ts_delta(subtract(({buy_conviction}), ({sell_conviction})), 20)", "conviction_spread_delta")

# ══════════════════════════════════════════════════════════════
# 5. VALUE-WEIGHTED vs COUNT-WEIGHTED DIVERGENCE
# ══════════════════════════════════════════════════════════════

# Dollar-weighted net flow minus share-weighted net flow (smart money indicator)
dollar_net_flow = f"divide(subtract({bf(VB)}, {bf(VS)}), {bf(EV)})"
share_net_flow = f"divide(subtract({bf(B)}, {bf(S)}), {bf(O)})"
add(f"subtract(({dollar_net_flow}), ({share_net_flow}))", "dollar_vs_share_flow_divergence")

# Ownership × dollar-share divergence (ownership validates the divergence)
add(f"multiply(({S_OWN}), subtract(({dollar_net_flow}), ({share_net_flow})))", "own_x_dollar_share_div")

# ══════════════════════════════════════════════════════════════
# 6. EXTREME WINDOW EXPLORATION
# ══════════════════════════════════════════════════════════════

# Ultra-long backfill
for w in [252]:
    add(f"divide({bf(H, w)}, {bf(O, w)})", f"own_bf{w}")

# Ownership rate of change over very long windows
for d in [126, 252]:
    add(f"ts_delta(divide({bf(H)}, {bf(O)}), {d})", f"own_delta_{d}")

# Very slow net buyer momentum
for d in [63, 126]:
    add(f"ts_delta(subtract({bf(CB)}, {bf(CS)}), {d})", f"netmom_d{d}")

# ══════════════════════════════════════════════════════════════
# 7. DISPERSION / HETEROGENEITY
# ══════════════════════════════════════════════════════════════

# Std dev of buyer count over time (institutional attention volatility)
add(f"divide({bf(CB)}, add(ts_std_dev({bf(CB)}, 60), 1))", "buyer_count_over_volatility")

# Buyer count volatility relative to holder count
add(f"divide(ts_std_dev({bf(CB)}, 60), add({bf(CH)}, 1))", "buyer_volatility_per_holder")

# ══════════════════════════════════════════════════════════════
# 8. FLOW ACCELERATION & REGIME CHANGE
# ══════════════════════════════════════════════════════════════

# Net flow acceleration (second derivative)
net_shares = f"subtract({bf(B)}, {bf(S)})"
add(f"ts_delta(ts_delta({net_shares}, 20), 63)", "net_share_accel_20_63")

# Acceleration × ownership (ownership provides context for acceleration meaning)
add(f"multiply(({S_OWN}), ts_delta(ts_delta({net_shares}, 20), 63))", "own_x_net_accel")

# Flow regime change: recent flow / long-term average flow
add(f"divide(divide(subtract({bf(VB, 20)}, {bf(VS, 20)}), {bf(EV, 20)}), add(divide(subtract({bf(VB, 63)}, {bf(VS, 63)}), {bf(EV, 63)}), 0.0001))", "flow_regime_ratio")

# ══════════════════════════════════════════════════════════════
# 9. BUY VS HOLD DYNAMICS
# ══════════════════════════════════════════════════════════════

# Buyer count as fraction of total active participants (buyers + sellers)
add(f"divide({bf(CB)}, add(add({bf(CB)}, {bf(CS)}), 1))", "buyer_fraction_active")

# Change in buyer fraction (directional shift in participation)
add(f"ts_delta(divide({bf(CB)}, add(add({bf(CB)}, {bf(CS)}), 1)), 20)", "buyer_fraction_delta")

# Net holders change (institutions entering vs leaving)
add(f"ts_delta({bf(CH)}, 63)", "holder_change_63d")

# ══════════════════════════════════════════════════════════════
# 10. DECAY SWEEP on BEST COMBO
# ══════════════════════════════════════════════════════════════

for decay in [2, 6, 8, 10, 20]:
    s = copy.deepcopy(BASE_SETTINGS)
    s["decay"] = decay
    expr = f"multiply(({S_OWN}), ({S_NET}))"
    expressions.append({"type": "REGULAR", "settings": s, "regular": expr})
    print(f"  + [own_x_netmom_decay{decay}] {expr[:100]}...")

# ── Write & Run ────────────────────────────────────────────────────
input_dir = OUTPUT_DIR / "input"
input_dir.mkdir(parents=True, exist_ok=True)
(OUTPUT_DIR / "config").mkdir(parents=True, exist_ok=True)
CONFIG_STUB.write_text("{}")

alpha_json = input_dir / "institutions6_boost.json"
with open(alpha_json, "w") as f:
    json.dump(expressions, f, indent=2)

print(f"\nTotal: {len(expressions)} expressions")
print(f"Alpha list: {alpha_json}")

output_csv = (OUTPUT_DIR / "simulation_status.csv").resolve()
cmd = [
    sys.executable, str(SIMULATOR.resolve()),
    "--config", str(CONFIG_STUB.resolve()),
    "--alpha-json", str(alpha_json.resolve()),
    "--output-csv", str(output_csv),
    "--batch-size", str(BATCH_SIZE),
    "--concurrency", str(CONCURRENCY),
]
print(f"Running batch simulator...")
result = subprocess.run(cmd, cwd=str(SKILL_ROOT / "scripts"))

if output_csv.exists():
    with open(output_csv) as f:
        reader = list(csv.DictReader(f))
    seen = set()
    completed = []
    for row in reader:
        expr = row.get("regular_expression", "")
        if row.get("status") == "COMPLETE" and expr not in seen:
            seen.add(expr)
            completed.append(row)
    completed.sort(key=lambda r: abs(float(r.get("sharpe", 0) or 0)), reverse=True)
    print(f"\n=== Results: {len(completed)} unique COMPLETE ===\n")
    for i, row in enumerate(completed[:20]):
        sharpe = float(row.get("sharpe", 0) or 0)
        fitness = float(row.get("fitness", 0) or 0)
        turnover = float(row.get("turnover", 0) or 0)
        expr = row.get("regular_expression", "")
        print(f"{i+1}. Sharpe={sharpe:+.2f}  Fitness={fitness:+.2f}  Turnover={turnover:.4f}")
        print(f"   {expr[:130]}")
        print()
else:
    print(f"CSV not found: {output_csv}")
