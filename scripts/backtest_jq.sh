#!/bin/bash
# 回测 — 参数来自 config.yaml backtest.default_*
# 用法: bash scripts/backtest_jq.sh
set -e
cd "$(dirname "$0")/.."
PYTHONPATH=. .venv/bin/python3 << 'PYEOF'
from backtest import run_backtest
from config.loader import get as cfg
result = run_backtest(
    start_date=cfg("backtest.default_start", "2023-01-01"),
    end_date=cfg("backtest.default_end", "2026-06-30"),
    capital=cfg("backtest.default_capital", 100000),
)
print()
print('=== KEY METRICS ===')
print(f'Final wealth: {result["total_wealth"].iloc[-1]:.2f}')
print(f'Cumulative return: {(result["total_wealth"].iloc[-1]/cfg("backtest.default_capital",100000)-1)*100:+.1f}%')
daily_ret = result['total_wealth'].pct_change().dropna()
if len(daily_ret) > 0:
    sharpe = daily_ret.mean() / daily_ret.std() * (252**0.5)
    print(f'Sharpe (est): {sharpe:.3f}')
PYEOF
