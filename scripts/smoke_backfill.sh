#!/bin/bash
# 冒烟测试 — 验证 backfill_range 拉取
set -e
cd "$(dirname "$0")/.."

echo "=== Before ===" 
sqlite3 quant/data/market.db "SELECT COUNT(DISTINCT symbol) FROM daily WHERE date BETWEEN '2019-01-01' AND '2019-01-10'"

echo "=== Pull ==="
PYTHONPATH=. .venv/bin/python -c "
from quant.utils.excepthook import setup; setup()
from quant.data.store import DataStore
store = DataStore()
n = store.backfill_range('2019-01-01', '2019-01-10')
store.close()
print(f'New rows: {n}')
"

echo "=== After ==="
sqlite3 quant/data/market.db "SELECT COUNT(DISTINCT symbol) FROM daily WHERE date BETWEEN '2019-01-01' AND '2019-01-10'"
