"""Point-in-time (PIT) disclosure filtering for financial statements.

A financial report with `stat_date` (会计期间) S is only *known* as of a trade
date D if its disclosure date <= D. The disclosure date is:

  * `pub_date`  — when the data source carries a real announcement date
    (tushare). This is the honest PIT boundary.
  * `stat_date` + statutory disclosure lag — for sources without an
    announcement date (`pub_date` NULL or == `stat_date`, e.g. sina), we
    approximate disclosure using CSRC rules:
        annual (12月)  -> 120d
        semi   (06月)  -> 62d
        quarterly       -> 45d
    (config: data.financials.disclosure_lag_days)

This mirrors the canonical logic in `quant/data/store.py:get_financials` so all
financial PIT handling stays consistent. Using `stat_date` directly as the
"known-as-of" boundary (the prior behaviour) is look-ahead bias: it lets a
report published 1-4 months after its period end leak into signals computed on
the period-end date.
"""

import numpy as np
import pandas as pd
from quant.config.constants import _require_cfg

from quant.utils.logger import get_logger

logger = get_logger("factor.compute._pit")



def _disclosure_lags() -> tuple[int, int, int]:
    _lag = _require_cfg("data.financials.disclosure_lag_days")
    return int(_lag["annual"]), int(_lag["semi_annual"]), int(_lag["quarterly"])


def pit_visible_mask(df: "pd.DataFrame", date) -> "pd.Series":
    """Boolean mask: rows of a financial DataFrame disclosed as of `date` (PIT-safe).

    `df` must contain a `stat_date` column and (optionally) a `pub_date` column.
    If `pub_date` is absent, every row falls back to the statutory-lag rule.
    """
    if df is None or (hasattr(df, "empty") and df.empty):
        return pd.Series([], dtype=bool)
    if "stat_date" not in df.columns:
        return pd.Series([True] * len(df), index=df.index)
    lag_a, lag_s, lag_q = _disclosure_lags()
    ts = pd.Timestamp(date)
    sd = pd.to_datetime(df["stat_date"])
    if "pub_date" in df.columns:
        pub = pd.to_datetime(df["pub_date"], errors="coerce")
        has_pub = pub.notna() & (pub != sd)
    else:
        pub = pd.Series(pd.NaT, index=df.index)
        has_pub = pd.Series(False, index=df.index)
    disc = sd.copy()
    m = sd.dt.month
    lag = np.where(m == 12, lag_a, np.where(m == 6, lag_s, lag_q))
    disc = disc.where(has_pub, disc + pd.to_timedelta(lag, unit="D"))
    disc = pub.where(has_pub, disc)
    return disc <= ts


def pit_where_sql() -> str:
    """SQL fragment implementing the same PIT disclosure rule as `pit_visible_mask`.

    Contains 5 placeholders in order:
        ? (pub_date <= date),
        ? (stat_date <= date(...)),
        ? (annual lag), ? (semi lag), ? (quarterly lag)
    Pair with `pit_where_params(date)`.
    """
    return (
        "(pub_date IS NOT NULL AND pub_date != stat_date AND pub_date <= ?) "
        "OR ((pub_date IS NULL OR pub_date = stat_date) "
        "AND stat_date <= date(?, '-' || CASE strftime('%m', stat_date) "
        "WHEN '12' THEN ? WHEN '06' THEN ? ELSE ? END || ' days'))"
    )


def pit_where_params(date) -> tuple:
    """Params for `pit_where_sql`: (date, date, annual_lag, semi_lag, quarterly_lag)."""
    lag_a, lag_s, lag_q = _disclosure_lags()
    date_str = date.strftime("%Y-%m-%d") if isinstance(date, pd.Timestamp) else str(date)
    return (date_str, date_str, str(lag_a), str(lag_s), str(lag_q))
