#!/usr/bin/env python3
"""Generate institutions6 Matrix expressions and run batch simulation directly.
Bypasses the variant/enhance pipeline to avoid metadata leak bug.
"""
from __future__ import annotations

import copy, csv, json, os, sys, subprocess
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────
BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "5"))
CONCURRENCY = int(os.environ.get("CONCURRENCY", "3"))
OUTPUT_DIR = Path(".brain_runtime/runs/institutions6_batch_direct/artifacts/03_simulate")
SKILL_ROOT = Path(".agents/skills/brain-simAlphasinBatch-and-track")
SIMULATOR = SKILL_ROOT / "scripts" / "batch_simulator.py"
CONFIG_STUB = OUTPUT_DIR / "config" / "empty_config.json"

SETTINGS = {
    "instrumentType": "EQUITY",
    "region": "USA",
    "universe": "TOP3000",
    "delay": 1,
    "decay": 4,
    "neutralization": "INDUSTRY",
    "truncation": 0.08,
    "testPeriod": "P0Y0M0D",
    "unitHandling": "VERIFY",
    "nanHandling": "OFF",
    "language": "FASTEXPR",
    "pasteurization": "ON",
    "maxTrade": "OFF",
    "visualization": False,
}

# ── institutions6 MATRIX field IDs ──────────────────────────────────────
H  = "inst6_total_shares_held_by_institutions"
O  = "inst6_total_share_held_by_owners"
B  = "inst6_num_of_institutional_shares_bought"
S  = "inst6_num_of_institutional_shares_sold"
VB = "inst6_value_of_institutional_shares_bought"
VS = "inst6_value_of_institutional_shares_sold"
VH = "inst6_value_held_by_institutions"
VO = "inst6_value_held_by_owners"
NB = "inst6_num_of_institutional_buyers"
NS = "inst6_num_of_institutional_sellers"
NH = "inst6_num_of_institutional_holders"
CB = "count_institutional_buyers_security"
CH = "count_institutional_holders_security"
CS = "count_institutional_sellers_security"
EV = "aggregate_equity_value_all_owners"
SI = "aggregate_share_count_institutions"
SA = "aggregate_share_count_all_owners"
MVA = "market_value_institutional_shares_acquired"
MVD = "market_value_institutional_shares_disposed"
QSA = "quantity_institutional_shares_acquired"
QSD = "quantity_institutional_shares_disposed"

bf = lambda f, w=20: f"ts_backfill({f}, {w})"

# ── Build expressions ───────────────────────────────────────────────────
expressions = []

def add(expr: str, note: str = "") -> None:
    entry = {"type": "REGULAR", "settings": copy.deepcopy(SETTINGS), "regular": expr}
    expressions.append(entry)
    tag = f" [{note}]" if note else ""
    print(f"  +{tag} {expr[:110]}...")

# ══════════════════════════════════════════════════
# CATEGORY 1: Net Flow / Momentum (时序 — 最可靠)
# ══════════════════════════════════════════════════

# Net buyer momentum (top performer from previous run: Sharpe 1.10)
add(f"ts_delta(subtract({bf(CB)}, {bf(CS)}), 20)", "net_buyer_momentum_20d")

# Net buyer momentum longer window
add(f"ts_delta(subtract({bf(CB)}, {bf(CS)}), 60)", "net_buyer_momentum_60d")

# Net buyer momentum short window
add(f"ts_delta(subtract({bf(CB)}, {bf(CS)}), 10)", "net_buyer_momentum_10d")

# Net share flow momentum
add(f"ts_delta(subtract({bf(B)}, {bf(S)}), 20)", "net_share_flow_momentum_20d")

# Net value flow momentum
add(f"ts_delta(subtract({bf(VB)}, {bf(VS)}), 20)", "net_value_flow_momentum_20d")

# ══════════════════════════════════════════════════
# CATEGORY 2: Flow Intensity / Normalized Flow
# ══════════════════════════════════════════════════

# Net buyer change relative to total holders
add(f"divide(ts_delta(subtract({bf(CB)}, {bf(CS)}), 20), {bf(CH)})", "net_buyer_delta_per_holder")

# Net flow / total institutional value
add(f"divide(subtract({bf(VB)}, {bf(VS)}), {bf(VH)})", "net_flow_over_inst_value")

# Net flow / total shares held
add(f"divide(subtract({bf(B)}, {bf(S)}), {bf(H)})", "net_flow_over_shares_held")

# Net buyer / holders (breadth intensity)
add(f"divide(subtract({bf(CB)}, {bf(CS)}), {bf(CH)})", "net_buyer_breadth")

# ══════════════════════════════════════════════════
# CATEGORY 3: Buyer/Seller Asymmetry
# ══════════════════════════════════════════════════

# Buyer count / Seller count ratio
add(f"divide({bf(CB)}, add({bf(CS)}, 1))", "buyer_seller_ratio")

# Value bought / Value sold ratio
add(f"divide({bf(VB)}, add({bf(VS)}, 1))", "value_bought_sold_ratio")

# Buyer conviction: avg value per buyer / avg value per seller
add(f"divide(divide({bf(VB)}, add({bf(NB)}, 1)), divide({bf(VS)}, add({bf(NS)}, 1)))", "buyer_seller_conviction")

# Shares bought / Shares sold ratio
add(f"divide({bf(B)}, add({bf(S)}, 1))", "shares_bought_sold_ratio")

# ══════════════════════════════════════════════════
# CATEGORY 4: Ownership Structure
# ══════════════════════════════════════════════════

# Institutional ownership concentration
add(f"divide({bf(H)}, {bf(O)})", "inst_ownership_ratio")

# Ownership ratio change
add(f"ts_delta(divide({bf(H)}, {bf(O)}), 20)", "ownership_ratio_delta_20d")

# Ownership ratio acceleration
add(f"ts_delta(ts_delta(divide({bf(H)}, {bf(O)}), 20), 20)", "ownership_ratio_acceleration")

# ══════════════════════════════════════════════════
# CATEGORY 5: Flow Reversal / Mean Reversion
# ══════════════════════════════════════════════════

# Previous period net flow (negative = contrarian)
add(f"subtract(0, ts_delta(subtract({bf(CB, 60)}, {bf(CS, 60)}), 60))", "flow_reversal_60d")

# Flow horizon divergence: short-term vs long-term
add(f"subtract(divide(subtract({bf(VB, 20)}, {bf(VS, 20)}), {bf(EV, 20)}), divide(subtract({bf(VB, 60)}, {bf(VS, 60)}), {bf(EV, 60)}))", "flow_horizon_divergence")

# ══════════════════════════════════════════════════
# CATEGORY 6: Acceleration / Second Derivative
# ══════════════════════════════════════════════════

# Net share flow acceleration
add(f"ts_delta(ts_delta(subtract({bf(B)}, {bf(S)}), 20), 20)", "net_share_flow_acceleration")

# Net value flow acceleration
add(f"ts_delta(ts_delta(subtract({bf(VB)}, {bf(VS)}), 20), 20)", "net_value_flow_acceleration")

# ══════════════════════════════════════════════════
# CATEGORY 7: Churn / Turnover
# ══════════════════════════════════════════════════

# Gross institutional trading / total shares held
add(f"divide(add({bf(B)}, {bf(S)}), {bf(H)})", "share_turnover_inst")

# Net flow / Gross flow (directional conviction)
add(f"divide(subtract({bf(VB)}, {bf(VS)}), add({bf(VB)}, {bf(VS)}))", "net_over_gross_value_flow")

# ══════════════════════════════════════════════════
# CATEGORY 8: Implied Price / Cost Basis
# ══════════════════════════════════════════════════

# Implied avg price per share held by institutions
add(f"divide({bf(VH)}, {bf(H)})", "implied_cost_basis")

# Buy price / Hold price ratio
add(f"divide(divide({bf(VB)}, add({bf(B)}, 1)), divide({bf(VH)}, add({bf(H)}, 1)))", "buy_vs_hold_price")

# ══════════════════════════════════════════════════
# CATEGORY 9: Interaction Terms
# ══════════════════════════════════════════════════

ownership_ratio = f"divide({bf(H)}, {bf(O)})"
net_flow_value = f"divide(subtract({bf(VB)}, {bf(VS)}), {bf(EV)})"

# Ownership × Net flow
add(f"multiply(({ownership_ratio}), ({net_flow_value}))", "ownership_x_netflow")

# Net flow × Buyer breadth
buyer_breadth = f"divide({bf(CB)}, {bf(CH)})"
add(f"multiply(({net_flow_value}), ({buyer_breadth}))", "netflow_x_buyer_breadth")

# ══════════════════════════════════════════════════
# CATEGORY 10: Holder Dynamics
# ══════════════════════════════════════════════════

# Holder count growth
add(f"ts_delta({bf(CH)}, 20)", "holder_count_growth")

# Buyer count growth
add(f"ts_delta({bf(CB)}, 20)", "buyer_count_growth")

# Seller count growth (negative = good)
add(f"subtract(0, ts_delta({bf(CS)}, 20))", "seller_count_decline")

# ══════════════════════════════════════════════════
# CATEGORY 11: Value vs Quantity Divergence
# ══════════════════════════════════════════════════

# Avg price bought vs avg price sold
add(f"subtract(divide({bf(VB)}, add({bf(B)}, 1)), divide({bf(VS)}, add({bf(S)}, 1)))", "avg_price_bought_vs_sold")

# Dollar net vs Share net divergence
dollar_net = f"subtract({bf(VB)}, {bf(VS)})"
share_net = f"subtract({bf(B)}, {bf(S)})"
add(f"subtract(divide({dollar_net}, {bf(EV)}), divide({share_net}, {bf(O)}))", "dollar_vs_share_netflow")

# ══════════════════════════════════════════════════
# CATEGORY 12: Market Value Acquired vs Disposed
# ══════════════════════════════════════════════════

# Net market value flow (alternative to inst6 value fields)
add(f"ts_delta(subtract({bf(MVA)}, {bf(MVD)}), 20)", "mkt_value_net_momentum")

# Market value acquired / disposed ratio
add(f"divide({bf(MVA)}, add({bf(MVD)}, 1))", "mkt_value_acquired_disposed_ratio")

# ══════════════════════════════════════════════════
# CATEGORY 13: Aggregate vs Institution Comparison
# ══════════════════════════════════════════════════

# Aggregate share count inst vs inst6 total shares held
add(f"subtract({bf(SI, 20)}, {bf(H, 20)})", "aggregate_vs_inst6_share_diff")

# Aggregate equity value inst vs inst6 value held
add(f"subtract(aggregate_equity_value_institutions, {bf(VH, 20)})", "aggregate_vs_inst6_value_diff")

# ── Write alpha list ────────────────────────────────────────────────────
input_dir = OUTPUT_DIR / "input"
input_dir.mkdir(parents=True, exist_ok=True)
(OUTPUT_DIR / "config").mkdir(parents=True, exist_ok=True)
CONFIG_STUB.write_text("{}")

alpha_json = input_dir / "institutions6_combined.json"
with open(alpha_json, "w") as f:
    json.dump(expressions, f, indent=2)

print(f"\nTotal expressions: {len(expressions)}")
print(f"Alpha list: {alpha_json}")

# ── Run batch simulator ─────────────────────────────────────────────────
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
print(f"  {' '.join(cmd)}")
print()

result = subprocess.run(cmd, cwd=str(SKILL_ROOT / "scripts"))

# ── Print results ───────────────────────────────────────────────────────
if output_csv.exists():
    print(f"\n=== Simulation Results ===")
    with open(output_csv) as f:
        reader = list(csv.DictReader(f))

    seen = set()
    completed = []
    failed = []
    for row in reader:
        expr = row.get("regular_expression", "")
        status = row.get("status", "")
        if status == "COMPLETE":
            if expr in seen:
                continue
            seen.add(expr)
            completed.append(row)
        elif status == "SUBMISSION_FAILED":
            failed.append(row)

    completed.sort(key=lambda r: abs(float(r.get("sharpe", 0) or 0)), reverse=True)

    print(f"Completed: {len(completed)}, Failed: {len(failed)}")
    print()
    for i, row in enumerate(completed):
        sharpe = float(row.get("sharpe", 0) or 0)
        fitness = float(row.get("fitness", 0) or 0)
        turnover = float(row.get("turnover", 0) or 0)
        expr = row.get("regular_expression", "")
        print(f"{i+1}. Sharpe={sharpe:+.2f}  Fitness={fitness:+.2f}  Turnover={turnover:.4f}")
        print(f"   {expr[:120]}")
        print()

    if failed:
        print(f"Failed ({len(failed)}):")
        for row in failed[:3]:
            err = str(row.get("error_details", ""))[:150]
            print(f"   {row.get('regular_expression', '')[:80]}...")
            print(f"   Error: {err}")
else:
    print(f"Output CSV not found: {output_csv}")
    print(f"Exit code: {result.returncode}")
