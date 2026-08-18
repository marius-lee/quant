#!/usr/bin/env bash
# 因子缓存补齐指定日期区间 (自增量: 只物化缺失日期, 已存在跳过)
# v500: 回测 IC 窗口阻断时用于补缺
# 用法: bash scripts/materialize_range.sh 2024-01-01 2025-01-02
# 幂等: 已物化日期自动跳过, 可重复执行
set -euo pipefail
cd "$(dirname "$0")/.."

START=${1:?usage: materialize_range.sh <start> <end>}
END=${2:?usage: materialize_range.sh <start> <end>}

PYTHONPATH=. .venv/bin/python3 - <<EOF
from quant.factor.store import FactorStore
from quant.data.store import DataStore
from quant.data.repos import UniverseRepo
from quant.config.paths import FACTOR_CACHE_DB
from quant.factor.compute import get_factor_names
import pandas as pd

store = DataStore()
fs = FactorStore(db_path=FACTOR_CACHE_DB)
dates = pd.date_range('$START', '$END', freq='B')
date_strs = [d.strftime('%Y-%m-%d') for d in dates]
factor_names = get_factor_names(status_filter='backtesting')
symbols = UniverseRepo().get_symbols(exclude_market='BJ')

print(f'{len(date_strs)} dates x {len(factor_names)} factors x {len(symbols)} symbols')
# v525/v527: 不注入 store (subprocess 段并行); workers=2 防 8GB OOM (09:30-15:00 monitor 时段实测 3 并发被 jetsam 杀)
r = fs.materialize(date_strs, factor_names, symbols, force=False,
                   workers=2, max_slice_days=25)
print(f"materialize done: {r['n_rows']} rows in {r['elapsed_sec']:.1f}s")
EOF
