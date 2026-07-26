#!/bin/bash
# v306 夜间链状态速查 — 随时执行: bash scripts/v306_status.sh
cd "$(dirname "$0")/.."

echo "=== $(date '+%m-%d %H:%M:%S') ==="
if lsof -t logs/v306_overnight.log >/dev/null 2>&1; then
    echo "chain: ALIVE"
else
    echo "chain: EXITED"
fi

echo "--- overnight (主日志 tail) ---"
tail -8 logs/v306_overnight.log 2>/dev/null

echo "--- fund_flow 进度 ---"
grep -E '^\s+\[[0-9]+/' logs/v306_fund_flow.log 2>/dev/null | tail -2
grep -E "ABORTED|Done:" logs/v306_fund_flow.log 2>/dev/null | tail -2

echo "--- jq_valuation 结果 ---"
grep -vE "rate limited" logs/v306_jq_valuation.log 2>/dev/null | tail -3

echo "--- factor_cache ---"
tail -3 logs/v306_factor_cache.log 2>/dev/null

echo "--- 库内数据 max(date) ---"
.venv/bin/python -c "
import sqlite3
db = sqlite3.connect('quant/data/market.db')
for t in ['daily_valuation', 'fund_flow', 'margin_detail']:
    mx, n = db.execute(f'SELECT MAX(date), COUNT(*) FROM {t}').fetchone()
    print(f'{t:16s} max={mx} rows={n}')
db.close()
c = sqlite3.connect('quant/data/factor_cache.db')
print('factor_values   max =', c.execute('SELECT MAX(date) FROM factor_values').fetchone()[0])
c.close()
"
