"""低波动 / 换手因子: 20日历史波动率, 20日平均换手率。v629: 改写为新接口 (data, date, window)。"""

import pandas as pd
import numpy as np
from quant.factor.registry import _cs_zscore

from quant.utils.logger import get_logger

logger = get_logger("factor.compute.classic.low_vol")



def compute_alpha_volatility_20d(data: "pd.DataFrame", date: str, window: int) -> "pd.Series":
    """20日历史波动率 (Ang-Hob-Xing-Zhang 2006). v629: 新接口改写 (data, date, window)。"""
    close = data["close"]
    if date not in close.index:
        return pd.Series(dtype=float)
    ret = close.pct_change()
    vol = ret.rolling(window).std().iloc[-1]
    return _cs_zscore(-vol).rename(f"alpha_volatility_{window}d")


def compute_alpha_turnover_20d(data: "pd.DataFrame", date: str, window: int) -> "pd.Series":
    """20日平均换手率 (Datar-Naik-Radcliffe 1998). v629: 新接口改写 (data, date, window)。

    data["turnover"] 是 daily.turnover (换手率 %), v525 dispatch 预加载。
    """
    turnover = data["turnover"]
    if date not in turnover.index:
        return pd.Series(dtype=float)
    avg_turnover = turnover.rolling(window).mean().iloc[-1]
    return _cs_zscore(-avg_turnover).rename(f"alpha_turnover_{window}d")
