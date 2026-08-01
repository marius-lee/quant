#!/bin/bash
# 全量回测验证 — 2026-06-01→2026-07-31
set -e
cd "$(dirname "$0")/.."
PYTHONPATH=. .venv/bin/python -c "
from quant.backtest.loop import run_backtest
r = run_backtest(
    start_date='2026-06-01',
    end_date='2026-07-31',
    capital=5000,
    universe_size=300,
    factor_status_filter='backtesting',
    retrain_freq=0
)
print(f'CAGR={r.get(\"cagr\",0):.1%} Sharpe={r.get(\"sharpe\",0):.3f} MDD={r.get(\"mdd\",0):.1%} errors={r.get(\"errors\",0)}')
"
