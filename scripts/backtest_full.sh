#!/usr/bin/env bash
# 全量回测: 2020-01-01 → 数据最新日 (动态; v540 起 loop.py 默认区间读 config)
# OOS 起点 2025-06-01 为业务评估窗口 (改时同步 config, 勿随手改)
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHONPATH=. .venv/bin/python3 -c "
from quant.backtest.loop import run_backtest
import time, sys, sqlite3
from quant.config.paths import MARKET_DB

_c = sqlite3.connect(MARKET_DB)
_end = _c.execute('SELECT MAX(date) FROM daily').fetchone()[0]
_c.close()
t0 = time.time()
print(f'Starting full backtest 2020-01-01 → {_end}...', file=sys.stderr)

r = run_backtest(
    '2020-01-01', _end,
    capital=5000,
    oos_start_date='2025-06-01',
)

elapsed = time.time() - t0
m = r.get('metrics', {})
is_ = m.get('is', {})
oos = m.get('oos', {})

print()
print('=== FULL BACKTEST RESULTS ===')
print(f'  Total elapsed:  {elapsed:.0f}s ({elapsed/60:.1f}min)')
print(f'  Errors:         {r.get(\"errors\", \"N/A\")}')
print(f'  Signals/day:    {r.get(\"avg_signals_per_day\", \"N/A\")}')
print()
print(f'  [FULL] Sharpe:  {m.get(\"sharpe\", \"N/A\")}')
print(f'  [FULL] CAGR:    {m.get(\"cagr_pct\", \"N/A\")}%')
print(f'  [FULL] MDD:     {m.get(\"max_drawdown_pct\", \"N/A\")}%')
print(f'  [FULL] Calmar:  {m.get(\"calmar\", \"N/A\")}')
print(f'  [FULL] Sortino: {m.get(\"sortino\", \"N/A\")}')
print(f'  [FULL] DSR:     {m.get(\"dsr\", \"N/A\")}')
print(f'  [FULL] Equity:  Y{m.get(\"final_equity\", \"N/A\")}')
print()
if is_:
    print(f'  [IS]   Sharpe: {is_.get(\"sharpe\", \"N/A\")}')
    print(f'  [IS]   CAGR:   {is_.get(\"cagr_pct\", \"N/A\")}%')
    print(f'  [IS]   MDD:    {is_.get(\"max_drawdown_pct\", \"N/A\")}%')
if oos:
    print(f'  [OOS]  Sharpe: {oos.get(\"sharpe\", \"N/A\")}')
    print(f'  [OOS]  CAGR:   {oos.get(\"cagr_pct\", \"N/A\")}%')
    print(f'  [OOS]  MDD:    {oos.get(\"max_drawdown_pct\", \"N/A\")}%')
print()
if r.get('error'):
    print(f'  ERROR: {r[\"error\"]}')
print('=== DONE ===')
"
