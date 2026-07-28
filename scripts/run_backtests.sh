#!/usr/bin/env bash
# 量化回测 — 三层验证
# 用法: bash scripts/run_backtests.sh
set -euo pipefail
cd "$(dirname "$0")/.."

PY=".venv/bin/python3"
export PYTHONPATH=.

echo "========================================"
echo "1/3 Smoke 测试 (40天, 10股票, ~2min)"
echo "========================================"
$PY -c "
from quant.backtest.loop import run_backtest
r = run_backtest('2026-06-01', '2026-07-27', capital=5000, mode='smoke')
m = r['metrics']
print(f'Sharpe={m[\"sharpe\"]}  CAGR={m[\"cagr_pct\"]}%  MDD={m[\"max_drawdown_pct\"]}%')
print(f'equity=¥{m[\"final_equity\"]:,.0f}  return={m[\"total_return_pct\"]}%  win_rate={m[\"win_rate\"]}')
print(f'days={m[\"n_days\"]}  signals/day={r[\"avg_signals_per_day\"]}  errors={r[\"errors\"]}  elapsed={r[\"elapsed_sec\"]}s')
"

echo ""
echo "========================================"
echo "2/3 全量回测 (2025-01 → 2026-07, ~10min)"
echo "========================================"
$PY -c "
from quant.backtest.loop import run_backtest
r = run_backtest('2025-01-01', '2026-07-27', capital=5000)
m = r['metrics']
print(f'Sharpe={m[\"sharpe\"]}  CAGR={m[\"cagr_pct\"]}%  MDD={m[\"max_drawdown_pct\"]}%')
print(f'equity=¥{m[\"final_equity\"]:,.0f}  return={m[\"total_return_pct\"]}%  win_rate={m[\"win_rate\"]}')
print(f'Solino={m.get(\"sortino\",\"?\")}  Calmar={m.get(\"calmar\",\"?\")}')
print(f'alpha={m.get(\"alpha\",\"?\")}  IR={m.get(\"info_ratio\",\"?\")}  beta={m.get(\"beta\",\"?\")}')
print(f'days={m[\"n_days\"]}  signals/day={r[\"avg_signals_per_day\"]}  errors={r[\"errors\"]}  elapsed={r[\"elapsed_sec\"]}s')
diag = r.get('diagnosis', {})
print(f'diagnosis: {diag.get(\"summary\", \"?\")}')
"

echo ""
echo "========================================"
echo "3/3 查看历史回测记录"
echo "========================================"
$PY -c "
import sqlite3, json
db = 'quant/data/backtest_trades.db'
conn = sqlite3.connect(db)
conn.execute('''CREATE TABLE IF NOT EXISTS backtest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy TEXT, started_at TEXT, start_date TEXT, end_date TEXT,
    initial_capital REAL, sharpe REAL, cagr_pct REAL, max_dd_pct REAL,
    sortino REAL, calmar REAL, win_rate REAL,
    final_equity REAL, total_return_pct REAL, n_days INTEGER,
    errors INTEGER, elapsed_sec REAL
)''')
rows = conn.execute('SELECT strategy, start_date, end_date, sharpe, cagr_pct, max_dd_pct, final_equity, elapsed_sec, started_at FROM backtest_runs ORDER BY id DESC LIMIT 5').fetchall()
print(f'{\"Strategy\":<15} {\"Period\":<24} {\"Sharpe\":>7} {\"CAGR\":>7} {\"MDD\":>7} {\"Equity\":>10} {\"Time\":>6}')
print('-' * 85)
for r in rows:
    period = f'{r[1]} → {r[2]}'
    print(f'{r[0]:<15} {period:<24} {r[3] or 0:>7.3f} {r[4] or 0:>6.1f}% {r[5] or 0:>6.1f}% ¥{r[6] or 0:>9,.0f} {r[7] or 0:>5.0f}s')
conn.close()
"
