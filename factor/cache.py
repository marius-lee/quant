"""因子缓存管理 — 增量更新 factors_cache 表。

auto_run.py 和 web app 共用此模块。
"""
import pandas as pd
from data.store import DataStore
from data.repository import StockRepo
from utils.logger import get_logger

logger = get_logger("factor.cache")


def update_cache(store: DataStore):
    """增量更新因子缓存。只计算 daily 表中新增日期的因子，INSERT OR REPLACE 写入。

    仅在以下情况全量重建:
      1. factors_cache 表不存在
      2. factors_cache 为空（首次运行）
    """
    from factor.technical import TechnicalFactors
    from factor.game_theory import GameTheoryFactors
    from factor.fundamental import FundamentalCrossSection
    from factor.real_fundamental import compute as compute_real_factors
    from config.loader import get as cfg

    conn = store._connect()

    import sqlite3
    try:
        fc_max = conn.execute("SELECT MAX(date) FROM factors_cache").fetchone()[0]
    except sqlite3.OperationalError:
        fc_max = None  # factors_cache 表不存在，首次运行
    dl_max = conn.execute("SELECT MAX(date) FROM daily").fetchone()[0]
    conn.close()

    if fc_max is not None:
        fc_max = fc_max.replace("-", "")  # YYYY-MM-DD → YYYYMMDD for comparison
    if dl_max is None:
        dl_max = "00000000"

    if fc_max and fc_max >= dl_max:
        logger.info("factor cache up to date, skipping")
        return
    is_full_rebuild = not bool(fc_max)
    if is_full_rebuild:
        logger.info("factor cache empty, full rebuild required")
    stocks_repo = StockRepo(store)
    stocks = stocks_repo.get_qualified()

    start_date = (pd.to_datetime(fc_max) + pd.Timedelta(days=1)).strftime("%Y-%m-%d") if fc_max else cfg("data.start_date", "2020-01-01")
    logger.info(f"factor cache: {len(stocks)} stocks, new dates from {start_date}")

    batch_size = 200
    for i in range(0, len(stocks), batch_size):
        chunk = stocks[i:i+batch_size]
        raw = store.get_daily(chunk, start=start_date.replace("-", ""))
        close_df = raw["close"].sort_index().dropna(how="all")
        if close_df.empty:
            continue

        high_df = raw["high"].sort_index() if "high" in raw else close_df * 1.01
        low_df  = raw["low"].sort_index()  if "low"  in raw else close_df * 0.99
        vol_df  = raw["volume"].sort_index() if "volume" in raw else close_df * 1e7
        amt_df  = raw["amount"].sort_index() if "amount" in raw else close_df * 1e8
        open_df = raw["open"].sort_index() if "open" in raw else close_df.shift(1)
        use_technical = cfg("factor.use_technical", True)
        use_fundamental = cfg("factor.use_fundamental", True)

        if use_technical:
            try:
                tech = TechnicalFactors()
                tf = tech.compute({"close": close_df})
            except Exception:
                logger.exception("TechnicalFactors failed, using empty")
                tf = pd.DataFrame()
        else:
            tf = pd.DataFrame()

        try:
            gt = GameTheoryFactors()
            gf = gt.compute({
                "close": close_df, "high": high_df, "low": low_df,
                "volume": vol_df, "amount": amt_df, "open": open_df,
            })
        except Exception:
            logger.exception("GameTheoryFactors failed, skipping")
            gf = pd.DataFrame()

        if use_fundamental:
            try:
                fund = FundamentalCrossSection()
                ff = fund.compute({
                    "close": close_df, "volume": vol_df, "high": high_df, "low": low_df
                })
            except Exception:
                logger.exception("FundamentalCrossSection failed, skipping")
                ff = pd.DataFrame()
        else:
            ff = pd.DataFrame()
        all_wide = pd.concat([tf, gf, ff], axis=1)

        real_fund = compute_real_factors(close_df, chunk, store, full_rebuild=is_full_rebuild)
        if not real_fund.empty and use_fundamental:
            all_wide = pd.concat([all_wide, real_fund.unstack(level=1)], axis=1)

        # 自动生成因子 (WorldQuant 风格)
        try:
            from factor.alpha_factory import generate as alpha_generate
            alpha = alpha_generate(close_df, vol_df, high_df, low_df, n_factors=100, n_keep=20)
            if not alpha.empty:
                alpha_wide = alpha.unstack(level=1)
                all_wide = pd.concat([all_wide, alpha_wide], axis=1)
        except Exception:
            logger.warning("alpha factory generation failed, skipping")

        stacked = all_wide.stack(level=1, future_stack=True).round(6)
        stacked = stacked.reset_index()
        stacked.columns = ["date", "stock"] + [c for c in stacked.columns[2:]]

        conn = store._connect()
        mode = "replace" if (i == 0 and is_full_rebuild) else "append"
        if mode == "append":
            # 先删除已有日期的行，避免因中断重跑导致 IntegrityError
            dates = stacked["date"].unique()
            for d in dates:
                conn.execute("DELETE FROM factors_cache WHERE date = ?", (d,))
            conn.commit()
        stacked.to_sql("factors_cache", conn, if_exists="append", index=False, chunksize=30000)
        conn.commit()
        conn.close()

        if i % 500 == 0:
            logger.info(f"factor cache: {min(i+batch_size, len(stocks))}/{len(stocks)}")

    conn = store._connect()
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_fc_uniq ON factors_cache(stock,date)")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # 表不存在（无数据可写）
    conn.close()
    logger.info("factor cache update done")
