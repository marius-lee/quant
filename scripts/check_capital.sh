#!/bin/bash
cd "$(dirname "$0")/.."
STRATEGY="quant"

echo "=== 资金概览 ==="
PYTHONPATH=. .venv/bin/python3 << PYEOF
from quant.data.trade_repo import TradeRepo
tr = TradeRepo()
strat = "$STRATEGY"
cash = tr.get_cash(strat)
init_cap = tr.get_initial_capital(strat)
positions = tr.get_positions(strat)
total_cost = sum(p["shares"] * p["price"] for p in positions)
print(f"  策略:        {strat}")
print(f"  初始资金:    {init_cap:.2f}")
print(f"  可用资金:    {cash:.2f}")
print(f"  持仓成本:    {total_cost:.2f}")
print(f"  总资产:      {cash + total_cost:.2f}")
print(f"  总收益:      {cash + total_cost - init_cap:+.2f}")
PYEOF

echo ""
echo "=== 持仓明细 ==="
PYTHONPATH=. .venv/bin/python3 << PYEOF
from quant.data.trade_repo import TradeRepo
tr = TradeRepo()
strat = "$STRATEGY"
positions = tr.get_positions(strat)
if positions:
    for p in positions:
        sym = p["symbol"]
        shares = p["shares"]
        price = p["price"]
        pnl = 0  # get_pnl is for closed trades only
        print(f"  {sym:8s} {shares}股  成本均价{price:.2f}")
else:
    print("  (无持仓)")
PYEOF

echo ""
echo "=== 是否重跑信号? ==="
echo "当前信号基于 07-13 数据，现已拉取 07-14/15/16"
echo "重跑: bash scripts/run_task.sh signals 2026-07-16"
