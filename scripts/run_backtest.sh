#!/bin/bash
# 全量回测验证 — 完整6.5年周期 (对齐业界标准 2020-2026)
set -e
cd "$(dirname "$0")/.."
PYTHONPATH=. .venv/bin/python -c "
from quant.backtest.loop import run_backtest
r = run_backtest(
    start_date='2020-01-01',
    end_date='2026-07-31',
    capital=5000,
    universe_size=3000,
    factor_status_filter='backtesting',
    retrain_freq=0
)
print(f'CAGR={r.get(\"cagr\",0):.1%} Sharpe={r.get(\"sharpe\",0):.3f} MDD={r.get(\"mdd\",0):.1%} errors={r.get(\"errors\",0)}')
print(f'Annual return={r.get(\"annual_return\",\"N/A\")} Max drawdown={r.get(\"max_drawdown\",\"N/A\")}')
"
