#!/usr/bin/env python3
"""v629: 物化 alpha_* 因子缓存 — 重新运行 8 阶段因子评估必需.

用途: 将 11 个 alpha_101 因子 (momentum/reversal/volatility/RSI PE/PB/PS/CFP/turnover)
      + ep_ratio 物化到因子缓存 (parquet), 使 Phase 2 评估能计算它们的真实 IC。
      这些因子在 v629 复活到 evaluating 状态, 但 parquet_f 中缺少缓存数据 → IC=0。

用法:
    PYTHONPATH=. .venv/bin/python scripts/materialize_alpha_factors.py [--force-all]

幂等性: force=True 整段重算 (因这些因子从未物化过, 全部为新数据);
        已物化的日期自动跳过 (fit skip), 多次运行安全。

版本: v1.0 (2026-09-11)
"""
import sys
import time
import pandas as pd
import sqlite3

from quant.factor.store import FactorStore
from quant.data.repos import UniverseRepo
from quant.config.paths import FACTOR_CACHE_DB, MARKET_DB
from quant.factor.compute import get_factor_names
from quant.utils.logger import get_logger

logger = get_logger("scripts.materialize_alpha_factors")

# 11 alpha_* 因子 + ep_ratio — 都是 daily price / fundamental, 数据源完整
ALPHA_FACTORS = [
    "ep_ratio",
    "alpha_momentum_20d", "alpha_momentum_60d",
    "alpha_reversal_5d",
    "alpha_volatility_20d",
    "alpha_turnover_20d",
    "alpha_rsi_14d",
    "alpha_ep", "alpha_bp", "alpha_sp", "alpha_cfp",
]


def main():
    t0 = time.monotonic()
    force_all = "--force-all" in sys.argv

    fs = FactorStore(db_path=FACTOR_CACHE_DB)
    conn = sqlite3.connect(MARKET_DB)
    end = conn.execute("SELECT MAX(date) FROM daily").fetchone()[0]
    conn.close()

    dates = pd.date_range("2020-01-01", end, freq="B")
    date_strs = [d.strftime("%Y-%m-%d") for d in dates]
    symbols = UniverseRepo().get_symbols(exclude_market="BJ")

    logger.info(
        f"materialize_alpha_factors: {len(date_strs)} dates x {len(ALPHA_FACTORS)} factors "
        f"x {len(symbols)} symbols"
    )

    fs.materialize(
        date_strs,
        ALPHA_FACTORS,
        symbols,
        force=force_all,
        max_slice_days=25,
    )

    elapsed = time.monotonic() - t0
    logger.info(f"materialize_alpha_factors: DONE in {elapsed:.1f}s")


if __name__ == "__main__":
    main()
