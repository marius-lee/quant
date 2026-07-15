#!/bin/bash
set -e
cd "$(dirname "$0")/.."

echo "=== Step 0: Validate ==="
PYTHONPATH=. .venv/bin/python3 scripts/validate.py

echo ""
echo "=== Step 1: Clean trades ==="
rm -f quant/data/trades.db

echo ""
echo "=== Step 2: Backtest ==="
PYTHONPATH=. .venv/bin/python3 << 'PYEOF'
from config.loader import get as cfg
from backtest import run_backtest
result = run_backtest(
    cfg("backtest.default_start", "2023-01-01"),
    cfg("backtest.default_end", "2026-06-30"),
    cfg("backtest.default_capital", 100000),
)
print("Final wealth:", result["total_wealth"].iloc[-1])
PYEOF
