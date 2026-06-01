"""Polars 加速手工因子计算 — 全程 long 格式，不进 pandas wide。

策略：pandas wide → Polars long → 加列计算全部因子 → 返回 Polars long。
消除 concat(18K列)+stack(level=1)+wide 中间态。
"""
import pandas as pd
import polars as pl

WINDOWS = (5, 10, 20, 60)


def _wide_to_long(close: pd.DataFrame, extra: dict = None) -> pl.LazyFrame:
    """pandas wide (date×stock) → Polars long (date, stock, close, ...)"""
    s = close.stack(future_stack=True).reset_index()
    s.columns = ["date", "stock", "close"]
    lf = pl.from_pandas(s).lazy().with_columns(pl.col("date").cast(pl.Date))

    if extra:
        for name, df in extra.items():
            s2 = df.stack(future_stack=True).reset_index()
            s2.columns = ["date", "stock", name]
            lf = lf.join(pl.from_pandas(s2).lazy().with_columns(pl.col("date").cast(pl.Date)),
                        on=["date", "stock"], how="left")
    return lf


def compute_all(close_df: pd.DataFrame, high_df=None, low_df=None,
                vol_df=None, amt_df=None, open_df=None) -> pl.LazyFrame:
    """返回 Polars LazyFrame (date, stock, 因子1, 因子2, ...) — 尚未 collect。"""

    extras = {}
    for key, df in [("high", high_df), ("low", low_df), ("volume", vol_df),
                    ("amount", amt_df), ("open", open_df)]:
        if df is not None:
            extras[key] = df

    base = _wide_to_long(close_df, extras)
    wl = list(WINDOWS)

    # ---- 基础中间列 ----
    base = base.with_columns([
        pl.col("close").pct_change().over("stock").alias("ret"),
        (pl.col("close") * pl.col("volume") + 1).log().alias("dollar_volume"),
    ])
    if high_df is not None and low_df is not None:
        base = base.with_columns([((pl.col("high") - pl.col("low")) / pl.col("close")).alias("hl")])
    if open_df is not None:
        base = base.with_columns([(pl.col("open") / pl.col("close").shift(1).over("stock") - 1).alias("ov_ret")])
    if amt_df is not None:
        base = base.with_columns([(pl.col("amount") / 1e8).alias("dollar_vol")])
    if vol_df is not None:
        base = base.with_columns([pl.col("volume").sqrt().alias("sqrt_vol")])

    # ---- 一次性构建所有因子表达式 ----
    exprs = []
    for w in wl:
        c, r = pl.col("close"), pl.col("ret")
        ma, rmax = c.rolling_mean(w, min_periods=5).over("stock"), c.rolling_max(w, min_periods=5).over("stock")

        exprs += [
            c.pct_change(w).over("stock").alias(f"momentum_{w}d"),
            (-r.rolling_mean(w, min_periods=5).over("stock")).alias(f"reversal_{w}d"),
            r.rolling_std(w, min_periods=5).over("stock").alias(f"volatility_{w}d"),
            ((c - ma) / ma).alias(f"ma_deviation_{w}d"),
            ((c - rmax) / rmax).alias(f"drawdown_{w}d"),
        ]
        if vol_df is not None:
            vm = pl.col("volume").rolling_mean(w, min_periods=5).over("stock")
            exprs.append((pl.col("volume") / vm).alias(f"volume_ratio_{w}d"))
        # Game Theory
        if amt_df is not None:
            exprs.append((r.abs() / pl.col("dollar_vol")).rolling_mean(w, min_periods=5).over("stock").alias(f"amihud_il_{w}d"))
        if open_df is not None:
            ovs = pl.col("ov_ret").rolling_std(w, min_periods=5).over("stock")
            tvs = r.rolling_std(w, min_periods=5).over("stock").replace(0, None)
            exprs.append((ovs / tvs).fill_nan(0).alias(f"pin_proxy_{w}d"))
        if vol_df is not None:
            exprs.append((r.abs() / pl.col("sqrt_vol")).rolling_mean(w, min_periods=5).over("stock").alias(f"kyle_lambda_{w}d"))
            exprs.append((pl.col("volume") / pl.col("volume").rolling_mean(w * 5, min_periods=5).over("stock")).clip(0, 10).alias(f"info_arrival_{w}d"))
        mkt_ret = r.mean().over("date")
        exprs.append((r - mkt_ret).abs().rolling_mean(w, min_periods=5).over("stock").alias(f"herding_csmad_{w}d"))
        if high_df is not None and low_df is not None:
            exprs.append(pl.col("hl").rolling_mean(w, min_periods=5).over("stock").alias(f"hl_spread_{w}d"))
        exprs.append(r.rolling_skew(w).over("stock").abs().alias(f"nash_distortion_{w}d"))

    # ---- 基本面代理 ----
    exprs += [
        pl.col("dollar_volume").alias("dollar_volume"),
        (c / c.rolling_mean(20, min_periods=5).over("stock")).alias("value_proxy"),
        (-r.rolling_std(20, min_periods=5).over("stock")).alias("quality_proxy"),
        c.pct_change(5).over("stock").alias("growth_proxy"),
        (r.rolling_std(5, min_periods=5).over("stock") / r.rolling_std(20, min_periods=5).over("stock").replace(0, None)).fill_nan(1.0).alias("vol_convergence"),
        ((c - c.rolling_mean(20, min_periods=5).over("stock")) / c.rolling_mean(20, min_periods=5).over("stock")).alias("ma20_deviation"),
    ]
    if vol_df is not None:
        a5 = (c * pl.col("volume")).rolling_mean(5, min_periods=5).over("stock")
        a20 = (c * pl.col("volume")).rolling_mean(20, min_periods=5).over("stock").replace(0, None)
        exprs.append((a5 / a20).fill_nan(1.0).alias("amount_proxy"))

    return base.with_columns(exprs)
