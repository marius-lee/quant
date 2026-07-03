#!/bin/bash
# 完整干净回测: 清空交易记录 + 用修复后的数据跑
cd "$(dirname "$0")/.."
rm -f data/trades.db
PYTHONPATH=. .venv/bin/python3 backtest.py 2026-01-01 2026-06-30 5000
