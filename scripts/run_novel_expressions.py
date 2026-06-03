#!/usr/bin/env python3
"""Generate novel expressions for institutions6 — fresh concepts beyond ownership ratio."""
from __future__ import annotations

import copy
import csv
import json
import os

OUTPUT = ".claude/skills/brain-simAlphasinBatch-and-track/data/novel_expressions.json"
ALPHA_LIST_PATH = ".claude/skills/brain-simAlphasinBatch-and-track/data/alpha_list.json"
CSV_R1 = ".claude/skills/brain-simAlphasinBatch-and-track/outputs/simulation_status.csv"
CSV_R2 = ".claude/skills/brain-simAlphasinBatch-and-track/outputs/variant_simulation_status_round2.csv"

SETTINGS = {
    "instrument_type": "EQUITY",
    "region": "USA",
    "universe": "TOP3000",
    "delay": 1,
    "decay": 4,
    "neutralization": "INDUSTRY",
    "truncation": 0.08,
    "test_period": "P0Y0M",
    "unit_handling": "VERIFY",
    "nan_handling": "OFF",
    "language": "FASTEXPR",
    "pasteurization": "ON",
    "max_trade": "OFF",
    "visualization": False,
}

_already = set()

def load_existing():
    for path in [ALPHA_LIST_PATH, CSV_R1, CSV_R2]:
        if not os.path.exists(path):
            continue
        if path.endswith('.json'):
            with open(path) as f:
                for e in json.load(f):
                    _already.add(e.get("regular", "").strip())
        else:
            with open(path) as f:
                for r in csv.DictReader(f):
                    _already.add(r.get("regular_expression", "").strip())

load_existing()
print(f"Existing expressions in history: {len(_already)}")

expressions = []

def add(expr, note=""):
    if expr.strip() in _already:
        print(f"  SKIP (exists): {note} -> {expr[:70]}...")
        return
    _already.add(expr.strip())
    entry = {"type": "REGULAR", "settings": copy.deepcopy(SETTINGS), "regular": expr}
    expressions.append(entry)
    print(f"  ADD [{note}]: {expr[:90]}...")

# ── Shorthand field names ──
H = "inst6_total_shares_held_by_institutions"     # shares held by inst
O = "inst6_total_share_held_by_owners"             # shares held by all owners
B = "inst6_num_of_institutional_shares_bought"     # shares bought
S = "inst6_num_of_institutional_shares_sold"       # shares sold
VB = "inst6_value_of_institutional_shares_bought"  # value bought
VS = "inst6_value_of_institutional_shares_sold"    # value sold
VH = "inst6_value_held_by_institutions"            # value held by inst
VO = "inst6_value_held_by_owners"                  # value held by all owners
NB = "inst6_num_of_institutional_buyers"           # num buyers
NS = "inst6_num_of_institutional_sellers"          # num sellers
NH = "inst6_num_of_institutional_holders"          # num holders
CB = "count_institutional_buyers_security"         # count buyers
CH = "count_institutional_holders_security"        # count holders
CS = "count_institutional_sellers_security"        # count sellers
EV = "aggregate_equity_value_all_owners"           # total equity value
SI = "aggregate_share_count_institutions"          # share count inst
SA = "aggregate_share_count_all_owners"            # share count all

bf = lambda f, w=20: f"ts_backfill({f}, {w})"

# ═══════════════════════════════════════════════════════════
# CATEGORY 1: Buy/Sell Asymmetry (ratio not net)
# ═══════════════════════════════════════════════════════════

# Buy value / Sell value — pure asymmetry
add(f"divide({bf(VB)}, {bf(VS)})", "buy_sell_value_ratio")
add(f"ts_delta(divide({bf(VB)}, {bf(VS)}), 20)", "buy_sell_value_ratio_delta")

# Buyer count / Seller count
add(f"divide({bf(NB)}, {bf(NS)})", "buyer_seller_count_ratio")
add(f"ts_av_diff(divide({bf(NB)}, {bf(NS)}), 60)", "buyer_seller_count_ratio_avdiff")

# Buyer conviction / Seller conviction (value per buyer / value per seller)
add(f"divide(divide({bf(VB)}, {bf(NB)}), divide({bf(VS)}, {bf(NS)}))", "conviction_ratio")

# ═══════════════════════════════════════════════════════════
# CATEGORY 2: Institutional Churn / Turnover
# ═══════════════════════════════════════════════════════════

# Gross institutional trading / total market value — churn rate
add(f"divide(add({bf(VB)}, {bf(VS)}), {bf(EV)})", "inst_churn_gross")
add(f"-divide(add({bf(VB)}, {bf(VS)}), {bf(EV)})", "inst_churn_gross_inv")

# Net flow / Gross flow — directional conviction ratio
add(f"divide(subtract({bf(VB)}, {bf(VS)}), add({bf(VB)}, {bf(VS)}))", "net_over_gross_flow")

# Share turnover: shares bought+shares sold / total shares held by inst
add(f"divide(add({bf(B)}, {bf(S)}), {bf(H)})", "share_turnover_inst")
add(f"-divide(add({bf(B)}, {bf(S)}), {bf(H)})", "share_turnover_inst_inv")

# ═══════════════════════════════════════════════════════════
# CATEGORY 3: Implied Price / Cost Basis
# ═══════════════════════════════════════════════════════════

# Implied avg price per share held by institutions
add(f"divide({bf(VH)}, {bf(H)})", "implied_cost_basis")

# Implied price per share bought (at transaction)
add(f"divide({bf(VB)}, {bf(B)})", "buy_price_per_share")

# Buy price / Hold price — are they buying above or below avg cost?
add(f"divide(divide({bf(VB)}, {bf(B)}), divide({bf(VH)}, {bf(H)}))", "buy_vs_hold_price")

# ═══════════════════════════════════════════════════════════
# CATEGORY 4: Interaction Terms
# ═══════════════════════════════════════════════════════════

# Ownership ratio × Net flow — double confirmation
ownership_ratio = f"divide({bf(H)}, {bf(O)})"
net_flow_value = f"divide(subtract({bf(VB)}, {bf(VS)}), {bf(EV)})"
add(f"multiply(({ownership_ratio}), ({net_flow_value}))", "ownership_x_netflow")

# Ownership ratio × Buying conviction
buy_conviction = f"divide({bf(VB)}, {bf(NB)})"
add(f"multiply(({ownership_ratio}), ({buy_conviction}))", "ownership_x_conviction")

# Net flow × Buyer breadth
buyer_breadth = f"divide({bf(NB)}, {bf(NH)})"
add(f"multiply(({net_flow_value}), ({buyer_breadth}))", "netflow_x_buyer_breadth")

# ═══════════════════════════════════════════════════════════
# CATEGORY 5: Value vs Share Divergence
# ═══════════════════════════════════════════════════════════

# Dollar-value net flow vs Share-count net flow divergence
# If dollar net flow is positive but share net flow is negative → institutions buying expensive shares
dollar_net = f"subtract({bf(VB)}, {bf(VS)})"
share_net = f"subtract({bf(B)}, {bf(S)})"
add(f"subtract(divide({dollar_net}, {bf(EV)}), divide({share_net}, {bf(O)}))", "dollar_vs_share_netflow_divergence")

# ═══════════════════════════════════════════════════════════
# CATEGORY 6: Acceleration / Second Derivative
# ═══════════════════════════════════════════════════════════

# Acceleration of net flow
net_flow = f"subtract({bf(B)}, {bf(S)})"
add(f"ts_delta(ts_delta({net_flow}, 20), 20)", "net_flow_acceleration")

# Acceleration of ownership ratio
add(f"ts_delta(ts_delta(({ownership_ratio}), 20), 20)", "ownership_acceleration")

# ═══════════════════════════════════════════════════════════
# CATEGORY 7: Dispersion / Heterogeneity
# ═══════════════════════════════════════════════════════════

# Std of buyer count — variation in institutional attention
add(f"ts_std_dev({bf(CB, 20)}, 60)", "buyer_count_volatility")

# Coefficient of variation of holders
add(f"divide(ts_std_dev({bf(CH, 20)}, 60), {bf(CH, 20)})", "holder_cv")

# ═══════════════════════════════════════════════════════════
# CATEGORY 8: Flow Reversal / Mean Reversion
# ═══════════════════════════════════════════════════════════

# Previous period net flow (negative sign = contrarian)
add(f"-ts_delta({net_flow}, 60)", "flow_reversal_60d")

# Long-term vs short-term flow divergence
add(f"subtract(divide({bf(VB, 20)}, {bf(EV, 20)}), divide({bf(VB, 60)}, {bf(EV, 60)}))", "flow_horizon_divergence")

# ═══════════════════════════════════════════════════════════
# CATEGORY 9: Value Held vs Value Traded
# ═══════════════════════════════════════════════════════════

# Net flow as fraction of total institutional value (not total market value)
add(f"divide({dollar_net}, {bf(VH)})", "netflow_over_inst_value")

# Buying only / Institutional value — pure accumulation intensity
add(f"divide({bf(VB, 20)}, {bf(VH, 20)})", "buy_over_inst_value")

# ═══════════════════════════════════════════════════════════
# CATEGORY 10: Refined Ownership + Dynamic
# ═══════════════════════════════════════════════════════════

# Ownership ratio scaled by holder count growth
holder_growth = f"ts_delta({bf(NH, 20)}, 20)"
add(f"multiply(({ownership_ratio}), ({holder_growth}))", "ownership_x_holder_growth")

# Net flow × (1 - ownership) — flow matters more when ownership is low (room to grow)
add(f"multiply(({net_flow_value}), subtract(1, ({ownership_ratio})))", "netflow_x_room_to_grow")

# ═══════════════════════════════════════════════════════════
# CATEGORY 11: Aggregate vs Inst6 field divergence
# ═══════════════════════════════════════════════════════════

# Difference between aggregate share count and inst6 total shares held
# This could capture data revisions or reporting discrepancies
add(f"subtract({bf(SI, 20)}, {bf(H, 20)})", "aggregate_vs_inst6_share_diff")

# ═══════════════════════════════════════════════════════════
# Write output
# ═══════════════════════════════════════════════════════════
os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
with open(OUTPUT, "w") as f:
    json.dump(expressions, f, indent=2)

print(f"\nGenerated {len(expressions)} novel expressions")
print(f"Output: {OUTPUT}")
