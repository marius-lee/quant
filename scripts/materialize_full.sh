#!/usr/bin/env bash
# 全量重建因子缓存: 2020-01-01 → 最新
# 物化起点约定 (v473 复核, 勿改回): 物化从 2020-01-01 起, 数据备齐到 2019-01-01。
# 2018 年 daily 仅 ~354 只股票子集 (2019 起才全量 ~3,500+ 只), 2019 起点会
# 把 378 天 lookback 拉到 2018 残缺数据 → 早期全 NaN + 短窗因子误标半脏缓存。
# 增量幂等: force=True 整段重算; 已物化日期自动跳过 (fit skip)
# v525: 不注入 store — 主进程调度 + subprocess 段并行 (默认 3 并发),
#       store 注入会退化为主进程同步直算 (无并行, 大内存, 8GB 机 OOM 风险)
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHONPATH=. .venv/bin/python3 -c "
from quant.factor.store import FactorStore
from quant.data.repos import UniverseRepo
from quant.config.paths import FACTOR_CACHE_DB
from quant.factor.compute import get_factor_names
import pandas as pd

fs = FactorStore(db_path=FACTOR_CACHE_DB)
dates = pd.date_range('2020-01-01', '2026-12-31', freq='B')
date_strs = [d.strftime('%Y-%m-%d') for d in dates]
factor_names = get_factor_names(status_filter='backtesting')
symbols = UniverseRepo().get_symbols(exclude_market='BJ')

print(f'{len(date_strs)} dates x {len(factor_names)} factors x {len(symbols)} symbols')
fs.materialize(date_strs, factor_names, symbols, force=True)
print('DONE')
"