"""因子数据管理 — 增量写入 factors 表（永久派生数据，地位等同 daily 表）。

全链路 Polars long：不再构建 pandas wide MultiIndex，消除 concat+stack 瓶颈。
"""
import pandas as pd
import polars as pl
from data.store import DataStore
from data.repository import StockRepo
from utils.logger import get_logger

logger = get_logger("factor.compute")


def _polars_from_long(df: pd.DataFrame, col: str) -> pl.LazyFrame:
    """pandas (date,stock) → Polars long lazy"""
    s = df.stack(future_stack=True).reset_index()
    s.columns = ["date", "stock", col]
    return pl.from_pandas(s).lazy().with_columns(pl.col("date").cast(pl.Date))


def compute_factors(store: DataStore):
    """计算全量因子并写入 factors 表。"""
    from factor.fast_compute import compute_all as _polars_compute
    from factor.alpha_factory import generate as alpha_generate
    from config.loader import get as cfg

    conn = store._connect()

    import sqlite3
    try:
        fc_max = conn.execute("SELECT MAX(date) FROM factors").fetchone()[0]
    except sqlite3.OperationalError:
        fc_max = None
    dl_max = conn.execute("SELECT MAX(date) FROM daily").fetchone()[0]

    if fc_max is not None:
        fc_max = fc_max.replace("-", "")
    if dl_max is None:
        dl_max = "00000000"

    if fc_max and fc_max >= dl_max:
        logger.info("factors up to date, skipping")
        return
    is_full_rebuild = not bool(fc_max)
    if is_full_rebuild:
        logger.info("factors empty, full rebuild required")
    stocks_repo = StockRepo(store)
    stocks = stocks_repo.get_qualified()

    start_date = (pd.to_datetime(fc_max) + pd.Timedelta(days=1)).strftime("%Y-%m-%d") if fc_max else cfg("data.start_date", "2020-01-01")
    logger.info(f"factors: {len(stocks)} stocks, new dates from {start_date}")

    batch_size = 200
    table_written = False
    alpha_formulas = None

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

        # ---- 1. 手工因子 (Polars long, lazy, not collected yet) ----
        try:
            lf = _polars_compute(close_df, high_df, low_df, vol_df, amt_df, open_df)
        except Exception:
            logger.exception("Polars compute failed")
            raise

        # ---- 2. 真实基本面 join ----
        from factor.real_fundamental import compute as compute_real
        rf = compute_real(close_df, chunk, store, full_rebuild=is_full_rebuild)
        if not rf.empty and cfg("factor.use_fundamental", True):
            # rf is (date,stock) with factor columns → pandas → Polars long → join
            rf_long = rf.stack(level=1, future_stack=True).reset_index()
            rf_long.columns = ["date", "stock", "factor", "value"]
            rf_wide = rf_long.pivot(index=["date", "stock"], columns="factor", values="value").reset_index()
            rf_pl = pl.from_pandas(rf_wide).lazy().with_columns(pl.col("date").cast(pl.Date))
            lf = lf.join(rf_pl, on=["date", "stock"], how="left")

        # ---- 3. 龙虎榜因子 join (stock-level snapshot → broadcast to all dates) ----
        try:
            from factor.dragon_tiger import compute as compute_lhb
            latest_date = close_df.index[-1].strftime("%Y-%m-%d")
            lhb_df = compute_lhb(store, chunk, latest_date)
            if not lhb_df.empty and (lhb_df.values != 0).any():
                lhb_df = lhb_df.reset_index()
                lhb_df.columns = ["stock"] + list(lhb_df.columns[1:])
                # broadcast: stock-wise LHB → cross join with dates in this chunk
                dates_df = pd.DataFrame({"date": close_df.index})
                lhb_broad = dates_df.merge(lhb_df, how="cross")
                lhb_pl = pl.from_pandas(lhb_broad).lazy().with_columns(pl.col("date").cast(pl.Date))
                lf = lf.join(lhb_pl, on=["date", "stock"], how="left")
        except Exception:
            logger.warning("dragon tiger factor failed, skipping")

        # ---- 4. Alpha 因子 join ----
        try:
            if alpha_formulas is None:
                alpha_df, alpha_formulas = alpha_generate(close_df, vol_df, high_df, low_df, n_factors=100, n_keep=20)
                if alpha_formulas is not None and not alpha_formulas:
                    alpha_formulas = False  # sentinel: 0 候选，后续批次跳过，不再重试
            elif alpha_formulas:
                alpha_df, _ = alpha_generate(close_df, vol_df, high_df, low_df, formulas=alpha_formulas)
            else:
                alpha_df = pd.DataFrame()  # alpha_formulas is False, skip
            if not alpha_df.empty:
                alpha_long = alpha_df.reset_index()
                alpha_long.columns = ["date", "stock"] + list(alpha_long.columns[2:])
                alpha_pl = pl.from_pandas(alpha_long).lazy().with_columns(pl.col("date").cast(pl.Date))
                lf = lf.join(alpha_pl, on=["date", "stock"], how="left")
        except Exception:
            logger.warning("alpha factory generation failed, skipping")

        # ---- 5. Collect → select only needed columns → executemany ----
        factor_cols = [c for c in lf.collect_schema().names() if c not in ("date", "stock", "close", "ret", "hl", "ov_ret", "dollar_vol", "sqrt_vol", "dollar_volume")]
        # Force compute only needed columns, fill NaN, round
        pdf = lf.select([pl.col("date").cast(pl.Utf8).str.slice(0, 10).alias("date"),
                         pl.col("stock")] +
                        [pl.col(c).round(6).alias(c) for c in factor_cols]).collect().to_pandas()
        rows = list(pdf.itertuples(index=False, name=None))

        # Build insert
        all_cols = ["date", "stock"] + factor_cols
        ph = ",".join("?" for _ in all_cols)
        insert_sql = f"INSERT INTO factors ({','.join(all_cols)}) VALUES ({ph})"

        conn = store._connect()
        if is_full_rebuild and not table_written:
            conn.execute("DROP TABLE IF EXISTS factors")
            # Use to_sql to create the table with correct types (replace mode = CREATE)
            placeholder = pd.DataFrame(columns=all_cols)
            placeholder.to_sql("factors", conn, if_exists="fail", index=False)
            table_written = True
        else:
            stocks_in = set(r[1] for r in rows)
            dates_in = set(r[0] for r in rows)
            stock_ph = ",".join("?" for _ in stocks_in)
            for d in dates_in:
                conn.execute(f"DELETE FROM factors WHERE date=? AND stock IN ({stock_ph})", [d] + list(stocks_in))

        conn.executemany(insert_sql, rows)
        conn.commit()

        if (i // batch_size) % 5 == 0:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

        if i % 500 == 0:
            logger.info(f"factors: {min(i+batch_size, len(stocks))}/{len(stocks)}")

    conn = store._connect()
    try:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_factors_uniq ON factors(stock,date)")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    logger.info("factors update done")
