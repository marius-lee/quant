"""Shared intermediate computations for factor vectorization.

Precomputes rolling statistics (returns, volatility, moving averages, ATR)
once for all 78 price factors — eliminates repeated identical calculations.
"""

import pandas as pd
import numpy as np


def compute_shared(data: pd.DataFrame) -> dict:
    """Precompute all shared rolling statistics from OHLCV data.

    Input: data with MultiIndex columns (field, symbol)
    Returns: dict of DataFrames keyed by stat name
    """
    close = data["close"]
    volume = data["volume"]
    high = data["high"]
    low = data["low"]
    amount = data["amount"]

    s = {}

    # Returns
    s["ret_1d"] = close.pct_change(1)
    s["ret_5d"] = close.pct_change(5)
    s["ret_20d"] = close.pct_change(20)
    s["ret_60d"] = close.pct_change(60)
    s["log_ret"] = np.log(close).diff()

    # Volatility
    s["vol_5d"] = s["ret_1d"].rolling(5).std()
    s["vol_20d"] = s["ret_1d"].rolling(20).std()
    s["vol_60d"] = s["ret_1d"].rolling(60).std()

    # Downside vol
    _neg = s["ret_1d"].copy()
    _neg[_neg > 0] = 0
    s["downside_vol_20d"] = _neg.rolling(20).std()

    # Moving averages
    s["ma_5d"] = close.rolling(5).mean()
    s["ma_20d"] = close.rolling(20).mean()
    s["ma_60d"] = close.rolling(60).mean()

    # Volume / turnover
    s["volume_ma_5d"] = volume.rolling(5).mean()
    s["volume_ma_20d"] = volume.rolling(20).mean()
    s["amount_ma_5d"] = amount.rolling(5).mean()
    s["amount_ma_20d"] = amount.rolling(20).mean()

    # Price range
    s["high_20d"] = high.rolling(20).max()
    s["low_20d"] = low.rolling(20).min()
    s["range_20d"] = (high - low).rolling(20).mean()

    # ATR
    pc = close.shift(1)
    tr = pd.concat([high - low, (high - pc).abs(), (low - pc).abs()], axis=1).max(axis=1)
    s["atr_14"] = tr.rolling(14).mean()

    # Higher moments
    s["skew_20d"] = s["ret_1d"].rolling(20).skew()
    s["kurt_20d"] = s["ret_1d"].rolling(20).kurt()

    return s
