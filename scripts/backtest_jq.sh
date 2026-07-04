#!/bin/bash
# 6因子回测（含 JQData 每日估值）
# 用法: bash scripts/backtest_jq.sh
set -e
cd "$(dirname "$0")/.."
PYTHONPATH=. .venv/bin/python3 -c "
from backtest import run_backtest
result = run_backtest(start_date='2026-01-01', end_date='2026-06-30', capital=5000)
print()
print('=== KEY METRICS ===')
print(f'Final wealth: {result[\"total_wealth\"].iloc[-1]:.2f}')
print(f'Cumulative return: {(result[\"total_wealth\"].iloc[-1]/5000-1)*100:+.1f}%')
daily_ret = result['total_wealth'].pct_change().dropna()
if len(daily_ret) > 0:
    sharpe = daily_ret.mean() / daily_ret.std() * (252**0.5)
    print(f'Sharpe (est): {sharpe:.3f}')
"
