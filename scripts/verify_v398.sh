#!/usr/bin/env bash
# test-v398: 重建因子缓存 + 冒烟验证
set -euo pipefail
cd "$(dirname "$0")/.."

echo "=== Step 1: 重建因子缓存 (ThreadPoolExecutor 并行) ==="
PYTHONPATH=. .venv/bin/python3 -c "
from quant.factor.store import FactorStore; from quant.data.store import DataStore
from quant.data.repos import UniverseRepo; from quant.config.paths import FACTOR_CACHE_DB
from quant.factor.compute import get_factor_names; import pandas as pd
store = DataStore(); fs = FactorStore(db_path=FACTOR_CACHE_DB)
dates = pd.date_range('2025-07-06', '2026-08-03', freq='B')
fs.materialize([d.strftime('%Y-%m-%d') for d in dates],
    get_factor_names(status_filter='backtesting'),
    UniverseRepo().get_symbols(exclude_market='BJ'), store=store, force=True)
print('DONE')
"

echo "=== Step 2: pytest ==="
.venv/bin/python3 -m pytest test/ tests/ -q

echo "=== Step 3: 冒烟测试 (跑两次验证一致性) ==="
for i in 1 2; do
  PYTHONPATH=. .venv/bin/python3 -c "
from quant.backtest.loop import run_backtest
r = run_backtest('2026-07-01', '2026-08-03', capital=5000, mode='smoke', oos_start_date='2026-08-01')
m = r['metrics']
print(f'RUN$i: Sharpe={m.get(\"sharpe\")} CAGR={m.get(\"cagr_pct\")}% MDD={m.get(\"max_drawdown_pct\")}% Errors={r.get(\"errors\")} loop={r.get(\"elapsed_sec\")}s')
"
done

echo "=== ALL DONE ==="
