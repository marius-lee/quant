#!/usr/bin/env bash
# test-v397: 回测策略全链路审计修复后冒烟测试
# 用法: bash scripts/smoke_v397.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHONPATH=. .venv/bin/python3 -c "
from quant.backtest.loop import run_backtest

r = run_backtest(
    '2026-07-01', '2026-08-03',
    capital=5000,
    mode='smoke',
    # test-v397: OOS 验证期 (8月隔离)
    oos_start_date='2026-08-01',
)

m = r.get('metrics', {})
print()
print('=== SMOKE TEST (test-v397) ===')
print(f'  Period:  2026-07-01 → 2026-08-03')
print(f'  Sharpe:  {m.get(\"sharpe\", \"N/A\")}')
print(f'  CAGR:    {m.get(\"cagr_pct\", \"N/A\")}%')
print(f'  MDD:     {m.get(\"max_drawdown_pct\", \"N/A\")}%')
print(f'  Sortino: {m.get(\"sortino\", \"N/A\")}')
print(f'  Calmar:  {m.get(\"calmar\", \"N/A\")}')
print(f'  DSR:     {m.get(\"dsr\", \"N/A\")}')
print(f'  Equity:  Y{m.get(\"final_equity\", \"N/A\")}')
print(f'  Signals: {r.get(\"avg_signals_per_day\", \"N/A\")}/day')
print(f'  Errors:  {r.get(\"errors\", \"N/A\")}')
print(f'  Elapsed: {r.get(\"elapsed_sec\", \"N/A\")}s')
if m.get('is'):
    print(f'  [IS] Sharpe: {m[\"is\"].get(\"sharpe\")}  CAGR: {m[\"is\"].get(\"cagr_pct\")}%')
if m.get('oos'):
    print(f'  [OOS] Sharpe: {m[\"oos\"].get(\"sharpe\")}  CAGR: {m[\"oos\"].get(\"cagr_pct\")}%')
print(f'  Equity points: {len(r.get(\"equity_curve\", []))}')
print()
"
