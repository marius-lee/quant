#!/usr/bin/env bash
# 冒烟回测 — 快速验证因子+alpha+执行管线完整性
# 用法: bash scripts/smoke_test.sh
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHONPATH=. .venv/bin/python3 -c "
from quant.backtest.loop import run_backtest
r = run_backtest('2026-06-01', '2026-07-27', capital=5000, mode='smoke')
m = r['metrics']
print(f'Sharpe={m[\"sharpe\"]}  CAGR={m[\"cagr_pct\"]}%  MDD={m[\"max_drawdown_pct\"]}%')
print(f'equity=¥{m[\"final_equity\"]:,.0f}  return={m[\"total_return_pct\"]}%  win_rate={m[\"win_rate\"]}')
print(f'days={m[\"n_days\"]}  signals/day={r[\"avg_signals_per_day\"]}  errors={r[\"errors\"]}  elapsed={r[\"elapsed_sec\"]}s')
"
