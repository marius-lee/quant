#!/usr/bin/env python3
"""v629: Backfill factor_ic_daily — 直接从因子缓存加载因子值, 批量计算 IC。

用途: alpha_* 因子通过 Phase 2+3+4 但 DSR=None (factor_ic_daily 无历史),
      无法从 probation 晋升到 active。此脚本直接从 parquet 缓存加载因子值
      + 每日收盘价计算前向收益, 批量 Spearman IC, 回填到 factor_ic_daily
      (scope='backtest')。

优势: compute_ic() 每次重新计算因子 (~2.5min/日), 此脚本直接读缓存
      bulk_load (parquet 列式), IC 纯向量化 — 速度 ~100x。

用法:
    PYTHONPATH=. .venv/bin/python scripts/backfill_ic_daily.py [start] [end] [n_symbols]
      默认: start=2022-01-01, end=2026-09-11, n_symbols=800

幂等性: INSERT OR REPLACE。
版本: v1.1 (2026-09-11)
"""
import sys
import time
import pandas as pd
import numpy as np
import sqlite3
from scipy.stats import spearmanr

from quant.utils.logger import get_logger
from quant.data.repos import FactorRepo, UniverseRepo
from quant.config.paths import MARKET_DB
from quant.factor.store.core import FactorStore
from quant.config.paths import FACTOR_CACHE_DB

logger = get_logger("scripts.backfill_ic_daily")

# 5 factors that survived Phase 3 CPCV + Phase 4 cost check
SURVIVING_FACTORS = [
    "alpha_momentum_20d", "alpha_rsi_14d", "alpha_momentum_60d",
    "alpha012_vol_dir", "alpha002_vol_div",
]
# Monitoring factors also backfilled for completeness
MONITORING_FACTORS = [
    "alpha_bp", "alpha_sp", "alpha_volatility_20d", "rsi_rev_14d",
]
ALL_FACTORS = sorted(set(SURVIVING_FACTORS + MONITORING_FACTORS))


def main():
    start_date = sys.argv[1] if len(sys.argv) > 1 else "2022-01-01"
    end_date = sys.argv[2] if len(sys.argv) > 2 else "2026-09-11"
    n_symbols = int(sys.argv[3]) if len(sys.argv) > 3 else 800

    t0 = time.monotonic()
    fs = FactorStore(db_path=FACTOR_CACHE_DB)
    repo = FactorRepo()
    repo.ensure_ic_daily_table()

    # Get trading dates
    conn = sqlite3.connect(MARKET_DB)
    rows = conn.execute(
        "SELECT DISTINCT date FROM daily WHERE date >= ? AND date <= ? ORDER BY date",
        (start_date, end_date)
    ).fetchall()
    conn.close()
    dates = [r[0] for r in rows]
    logger.info(f"backfill_ic_daily: {len(dates)} trading dates, "
                f"{len(ALL_FACTORS)} factors, {n_symbols} symbols")

    # Get universe
    symbols = UniverseRepo().get_symbols(exclude_market="BJ")[:n_symbols]

    # Load forward 1-day returns and close prices for all dates
    conn = sqlite3.connect(MARKET_DB)
    # Build date index
    date_idx = {d: i for i, d in enumerate(dates)}
    n = len(dates)
    # Load close prices: {date: {symbol: close}}
    close_df = pd.read_sql_query(
        f"""SELECT symbol, date, close FROM daily
             WHERE symbol IN ({",".join("?"*len(symbols))})
               AND date >= ? AND date <= ?""",
        conn, params=symbols + [start_date, end_date]
    )
    conn.close()
    if close_df.empty:
        logger.error("No close data loaded, aborting")
        return

    close_piv = close_df.pivot(index="date", columns="symbol", values="close").ffill()
    fwd_ret = close_piv.pct_change().shift(-1)  # tomorrow's return

    # Bulk load factor values from cache
    factor_values = fs.bulk_load(dates, symbols=symbols, factor_names=ALL_FACTORS)
    # factor_values: {date_str: {factor_name: Series(symbol→value)}}

    # Compute IC for each date
    total_written = 0
    for i, ds in enumerate(dates):
        if ds not in factor_values or ds not in close_piv.index:
            continue

        fwd = fwd_ret.loc[ds].dropna()
        if len(fwd) < 30:
            continue

        for fname in ALL_FACTORS:
            if fname not in factor_values[ds]:
                continue
            fv = factor_values[ds][fname]
            if fv is None or fv.empty:
                continue
            # Align
            common = fv.index.intersection(fwd.index)
            if len(common) < 30:
                continue
            vals = fv.loc[common].astype(float)
            rets = fwd.loc[common].astype(float)
            valid = vals.notna() & rets.notna()
            if valid.sum() < 30:
                continue
            vals_v = vals[valid].values
            rets_v = rets[valid].values
            if np.std(vals_v) == 0 or np.std(rets_v) == 0:
                continue
            try:
                rho, _ = spearmanr(vals_v, rets_v)
                if not np.isnan(rho):
                    repo.insert_ic_daily(ds, fname, float(rho),
                                         n_stocks=len(common),
                                         scope="backtest")
                    total_written += 1
            except Exception:
                pass

        if (i + 1) % 50 == 0:
            elapsed = time.monotonic() - t0
            logger.info(f"backfill_ic_daily: {i+1}/{len(dates)} dates "
                        f"({total_written} IC values, {elapsed:.0f}s)")

    elapsed = time.monotonic() - t0
    logger.info(f"backfill_ic_daily: DONE — {total_written} IC values in {elapsed:.1f}s")

    # Summary per factor
    for fname in ALL_FACTORS:
        c = conn_execute = sqlite3.connect(MARKET_DB)
        count = c.execute(
            "SELECT COUNT(*) FROM factor_ic_daily WHERE factor_name=? AND scope='backtest'",
            (fname,)
        ).fetchone()[0]
        c.close()
        logger.info(f"  {fname}: {count} IC values in factor_ic_daily")


if __name__ == "__main__":
    main()
