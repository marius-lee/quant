"""技术因子: 14日RSI。v629: 改写为新接口 (data, date, window)。"""

import pandas as pd
import numpy as np
from quant.factor.registry import _cs_zscore

from quant.utils.logger import get_logger

logger = get_logger("factor.compute.classic.technical")



def compute_alpha_rsi_14d(data: "pd.DataFrame", date: str, window: int) -> "pd.Series":
    """14日RSI (Wilder 1978). v629: 新接口改写 (data, date, window)。"""
    close = data["close"]
    if date not in close.index:
        return pd.Series(dtype=float)
    delta = close.diff()
    gain = delta.where(delta > 0, 0).rolling(window).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window).mean()
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return _cs_zscore(rsi.iloc[-1]).rename(f"alpha_rsi_{window}d")
