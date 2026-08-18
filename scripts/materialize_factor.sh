#!/usr/bin/env bash
# 单因子强制物化指定日期区间 (force=True: 删除旧数据重算, 无视 blocked/指纹缺失判定)
# v529: blocked 因子数据补齐后无法自动恢复 + 节假日日期反复空算 — force 模式豁免
#        blocked 剔除 (store.py v529 fix), 本脚本用于定向修复单因子数据缺口
# 用法: bash scripts/materialize_factor.sh ocfp 2020-01-02 2023-12-29
# 幂等: force=True 会重写目标区间全量值, 可重复执行 (结果确定)
set -euo pipefail
cd "$(dirname "$0")/.."

FACTOR=${1:?usage: materialize_factor.sh <factor> <start> <end>}
START=${2:?usage: materialize_factor.sh <factor> <start> <end>}
END=${3:?usage: materialize_factor.sh <factor> <start> <end>}

PYTHONPATH=. .venv/bin/python3 - <<EOF
from quant.factor.store import FactorStore
from quant.data.repos import UniverseRepo
from quant.config.paths import FACTOR_CACHE_DB
import pandas as pd

fs = FactorStore(db_path=FACTOR_CACHE_DB)
dates = [d.strftime('%Y-%m-%d') for d in pd.date_range('$START', '$END', freq='B')]
factor_names = ['$FACTOR']
symbols = UniverseRepo().get_symbols(exclude_market='BJ')

r = fs.materialize(dates, factor_names, symbols, force=True,
                   workers=2, max_slice_days=25)
print(f"materialize done: rows={r['n_rows']} dates={r['n_dates']} in {r['elapsed_sec']:.1f}s")
EOF