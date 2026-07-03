#!/bin/bash
# 一键同步北向资金 + 龙虎榜 + 涨停池
# 用法: bash scripts/sync_new_sources.sh [max_stocks]

set -e
cd "$(dirname "$0")/.."
PYTHONPATH=. .venv/bin/python3 -c "
from data.northbound import sync_all
from data.lhb import sync_lhb
from data.limit_up import sync_range
import sys

n_stocks = int(sys.argv[1]) if len(sys.argv) > 1 else 100

print('=== 1/3: 北向资金 (前 {} 只市值最大股票, 每只间隔 0.3s) ==='.format(n_stocks))
sync_all(max_stocks=n_stocks)
print()

print('=== 2/3: 龙虎榜 (2025-01-01 至今) ===')
sync_lhb(start_date='2025-01-01')
print()

print('=== 3/3: 涨停池 (2026-01-01 至今) ===')
sync_range(start_date='2026-01-01')
print()
print('Done! All 3 sources synced.')
" "$@"
