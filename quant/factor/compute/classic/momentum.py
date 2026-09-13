"""经典 Alpha 动量因子: 20日/60日动量, 5日反转。v629: 改写为新接口 (data, date, window)。"""

import pandas as pd
import numpy as np
from quant.factor.registry import _cs_zscore

from quant.utils.logger import get_logger

logger = get_logger("factor.compute.classic.momentum")



def compute_alpha_momentum_20d(data: "pd.DataFrame", date: str, window: int) -> "pd.Series":
    """20日价格动量 (Jegadeesh-Titman 1993).

    Interface: v629 — data[MultiIndex(field,symbol)] preloaded by FactorStore.materialize,
    取代旧版 (date_str, conn) 直接 DB 查询 (v525 dispatch 不兼容旧接口 → IC 永远 0).
    """
    close = data["close"]
    if date not in close.index:
        return pd.Series(dtype=float)
    mom = close.pct_change(window).iloc[-1]
    return _cs_zscore(mom).rename(f"alpha_momentum_{window}d")


def compute_alpha_momentum_60d(data: "pd.DataFrame", date: str, window: int) -> "pd.Series":
    """60日价格动量 (Jegadeesh-Titman 1993)."""
    close = data["close"]
    if date not in close.index:
        return pd.Series(dtype=float)
    mom = close.pct_change(window).iloc[-1]
    return _cs_zscore(mom).rename(f"alpha_momentum_{window}d")


def compute_alpha_reversal_5d(data: "pd.DataFrame", date: str, window: int) -> "pd.Series":
    """5日短期反转 (Lehmann 1990)."""
    close = data["close"]
    if date not in close.index:
        return pd.Series(dtype=float)
    rev = -close.pct_change(window).iloc[-1]
    return _cs_zscore(rev).rename(f"alpha_reversal_{window}d")
