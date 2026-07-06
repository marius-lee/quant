#!/bin/bash
# Smoke test: 15s quick backtest, catch regressions before push
# Run: bash scripts/smoke_test.sh

set -e
cd "$(dirname "$0")/.."

echo "=== Smoke: backtest 2026-06-01..2026-06-30 ==="
.venv/bin/python3 -c "
import sys; sys.path.insert(0, '.')
from backtest import run_backtest
result = run_backtest('2026-06-01', '2026-06-30', 100000)
n = len(result)
w = float(result.total_wealth.iloc[-1])
print(f'Rows: {n}, Final wealth: {w:,.2f}')
assert n > 0, 'empty backtest result'
assert w > 0, f'negative wealth: {w}'
assert w < 1000000, f'absurd wealth: {w}'
print('SMOKE PASSED')
"
echo ""
echo "=== Smoke: pipeline generates signals ==="
.venv/bin/python3 -c "
import sys; sys.path.insert(0, '.')
from pipeline import generate_signals
s = generate_signals('2026-06-05')
n = len(s.get('target_positions', []))
print(f'Target positions: {n}')
assert n > 0, 'no signals generated'
print('SMOKE PASSED')
"
