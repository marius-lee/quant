#!/usr/bin/env bash
# 全量回测
# 用法: bash scripts/full_backtest.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHONPATH=. .venv/bin/python3 -c "
from quant.backtest.loop import run_backtest
r = run_backtest('2026-01-05', _end, capital=50000)
m = r['metrics']
print(f'Sharpe={m[\"sharpe\"]}  CAGR={m[\"cagr_pct\"]}%  MDD={m[\"max_drawdown_pct\"]}%')
print(f'Sortino={m[\"sortino\"]}  Calmar={m[\"calmar\"]}  WinRate={m[\"win_rate\"]}')
print(f'equity=¥{m[\"final_equity\"]:,.0f}  return={m[\"total_return_pct\"]}%')
print(f'alpha={m.get(\"alpha\",\"?\")}  IR={m.get(\"info_ratio\",\"?\")}  beta={m.get(\"beta\",\"?\")}')
print(f'days={m[\"n_days\"]}  errors={r[\"errors\"]}  elapsed={r[\"elapsed_sec\"]}s')
"
