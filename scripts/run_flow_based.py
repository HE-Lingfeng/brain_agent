#!/usr/bin/env python3
"""Flow-based expressions — higher frequency, lower self-correlation than holdings."""
from __future__ import annotations

import copy, csv, json, os

OUTPUT = ".claude/skills/brain-simAlphasinBatch-and-track/data/flow_expressions.json"
ALPHA_LIST_PATH = ".claude/skills/brain-simAlphasinBatch-and-track/data/alpha_list.json"

SETTINGS = {
    "instrument_type": "EQUITY", "region": "USA", "universe": "TOP3000",
    "delay": 1, "decay": 4, "neutralization": "INDUSTRY", "truncation": 0.08,
    "test_period": "P0Y0M", "unit_handling": "VERIFY", "nan_handling": "OFF",
    "language": "FASTEXPR", "pasteurization": "ON", "max_trade": "OFF",
    "visualization": False,
}

bf = lambda f, w=20: f"ts_backfill({f}, {w})"

# Fields
B = "inst6_num_of_institutional_shares_bought"
S = "inst6_num_of_institutional_shares_sold"
VB = "inst6_value_of_institutional_shares_bought"
VS = "inst6_value_of_institutional_shares_sold"
H = "inst6_total_shares_held_by_institutions"
O = "inst6_total_share_held_by_owners"
NB = "inst6_num_of_institutional_buyers"
NS = "inst6_num_of_institutional_sellers"
NH = "inst6_num_of_institutional_holders"
EV = "aggregate_equity_value_all_owners"
CH = "count_institutional_holders_security"

_already = set()
for p in [ALPHA_LIST_PATH]:
    if os.path.exists(p):
        with open(p) as f:
            for e in json.load(f):
                _already.add(e.get("regular","").strip())
for c in ["simulation_status.csv", "variant_simulation_status_round2.csv", "novel_simulation_status.csv", "gate_fix_status.csv"]:
    p = os.path.join(os.path.dirname(OUTPUT), "..", "outputs", c)
    if os.path.exists(p):
        with open(p) as f:
            for r in csv.DictReader(f):
                _already.add(r.get("regular_expression","").strip())

exprs = []
def add(expr, note, s=None):
    if not s:
        s = copy.deepcopy(SETTINGS)
    if expr.strip() in _already:
        print(f"  SKIP: {note}")
        return
    _already.add(expr.strip())
    exprs.append({"type": "REGULAR", "settings": s, "regular": expr})
    print(f"  ADD [{note}]: {expr[:100]}...")

# ═══ Flow-based expressions (higher turnover, lower self-corr) ═══

# Net flow / shares held (flow turnover) — more variable than ownership ratio
net_flow = f"subtract({bf(B)}, {bf(S)})"
add(f"ts_delta(divide({net_flow}, {bf(H)}), 10)", "flow_turnover_delta10")

# Short-window net flow / market cap
add(f"divide(subtract({bf(B, 10)}, {bf(S, 10)}), {bf(O, 10)})", "netflow_10d")

# Buyer-minus-seller count normalized (breadth signal)
add(f"zscore(divide(subtract({bf(NB)}, {bf(NS)}), {bf(NH)}))", "buyer_seller_breadth_z")

# Flow acceleration (net flow delta / abs net flow)
add(f"divide(ts_delta({net_flow}, 10), add(ts_backfill(abs({net_flow}), 10), 1))", "flow_accel_normalized")

# Value flow / Share flow divergence (smart money indicator)
add(f"divide(divide({bf(VB)}, {bf(B)}), divide({bf(VS)}, {bf(S)}))", "value_per_share_buy_vs_sell")

# Net buyer count change (breadth momentum)
buyer_seller_net = f"subtract({bf(NB)}, {bf(NS)})"
add(f"ts_delta({buyer_seller_net}, 20)", "buyer_seller_net_delta20")
add(f"ts_av_diff({buyer_seller_net}, 60)", "buyer_seller_net_avdiff60")

# Flow imbalance normalized by holder count
add(f"divide(divide({net_flow}, {bf(H)}), {bf(CH)})", "flow_per_holder")

# Explicit group_neutralize wrapped flow signals
flow_ratio = f"divide(subtract({bf(VB)}, {bf(VS)}), {bf(EV)})"
for group in ["industry", "subindustry"]:
    add(f"group_neutralize({flow_ratio}, {group})", f"group_neut_flow_{group}")
    add(f"group_neutralize(ts_delta({flow_ratio}, 20), {group})", f"group_neut_flow_delta_{group}")

# ts_decay_linear on flow signals
add(f"ts_decay_linear({flow_ratio}, 8)", "decay_linear_flow")

# Ownership ratio × ts_decay_linear (structure + reduced corr)
ownership = f"divide({bf(H)}, {bf(O)})"
add(f"multiply(({ownership}), ts_decay_linear(({ownership}), 12))", "ownership_x_decaylinear")

# Cross-sectional: industry-relative ownership
add(f"group_neutralize(({ownership}), industry)", "group_neut_ownership_raw")

# ═══ Higher Sharpe potential: combine ownership + flow ═══
# Ownership gives direction, flow gives timing
add(f"multiply(({ownership}), group_neutralize({flow_ratio}, industry))", "ownership_x_flow_neut")

# Zscore of ownership within industry (cross-sectional = lower corr)
add(f"group_neutralize(zscore(({ownership})), industry)", "zscore_group_neut_ownership")

os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
with open(OUTPUT, "w") as f:
    json.dump(exprs, f, indent=2)
print(f"\nGenerated {len(exprs)} flow-based variants -> {OUTPUT}")
