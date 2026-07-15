"""ATR (Average True Range) — volatility-based stop-loss and position sizing.

ATR(14) = SMA(TR, 14) where TR = max(high-low, |high-prev_close|, |low-prev_close|)
Source: J. Welles Wilder (1978) — New Concepts in Technical Trading Systems
"""

import pandas as pd
import numpy as np


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Compute ATR for a single symbol.

    Args:
        high, low, close: price series (same index)
        period: ATR lookback, default 14

    Returns: Series of ATR values (first period-1 rows are NaN)
    """
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = (high - prev_close).abs()
    tr3 = (low - prev_close).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def atr_stop_loss(entry_price: float, atr_value: float, multiplier: float = 2.0) -> float:
    """ATR-based stop-loss price.

    stop = entry_price - multiplier * ATR
    Default multiplier=2.0 (captures ~95% of noise under normal distribution).

    Returns: stop-loss price. ATR 不可用时从 config 读取默认止损比例 (无 fallback)。
    """
    if atr_value <= 0 or np.isnan(atr_value):
        from quant.config.constants import _require_cfg
        default_pct = _require_cfg("risk.default_stop_loss_pct")
        return entry_price * (1 - default_pct)
    return entry_price - multiplier * atr_value


def atr_position_size(capital: float, atr_value: float, multiplier: float = 2.0,
                      max_risk_pct: float = 0.02) -> int:
    """ATR-based position sizing.

    shares = capital * max_risk_pct / (multiplier * ATR)
    Limits position to max_risk_pct of capital per trade.

    Returns: max shares (rounded down to lot size)
    """
    if atr_value <= 0 or np.isnan(atr_value):
        return 0
    risk_per_share = multiplier * atr_value
    if risk_per_share <= 0:
        return 0
    shares = int(capital * max_risk_pct / risk_per_share)
    return shares
