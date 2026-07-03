#!/bin/bash
set -e
cd "$(dirname "$0")/.."

echo "=== Step 0: Validate ==="
PYTHONPATH=. .venv/bin/python3 scripts/validate.py

echo ""
echo "=== Step 1: Clean trades ==="
rm -f data/trades.db

echo ""
echo "=== Step 2: Backtest ==="
PYTHONPATH=. .venv/bin/python3 backtest.py 2026-01-01 2026-06-30 5000
