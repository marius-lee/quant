#!/bin/bash
# 冒烟测试 — backfill_range 小范围验证
set -e
cd "$(dirname "$0")/.."

echo "=== Before ===" 
sqlite3 quant/data/market.db "SELECT COUNT(DISTINCT symbol) FROM daily WHERE date BETWEEN '2019-01-01' AND '2019-01-03'"

echo "=== Pull 10 stocks ==="
PYTHONPATH=. .venv/bin/python -c "
from quant.utils.excepthook import setup; setup()
from quant.data.store import DataStore
store = DataStore()
n = store._backfill_via_baostock(['000001','000002','000004','000006','000007','600000','600001','600002','600003','600004'], '2019-01-01', '2019-01-03')
store.close()
print(f'New rows: {n}')
"

echo "=== After ==="
sqlite3 quant/data/market.db "SELECT COUNT(DISTINCT symbol) FROM daily WHERE date BETWEEN '2019-01-01' AND '2019-01-03'"
echo "PASS"
