"""布林带因子 (Bollinger Bands) — Bollinger (2002).

三个因子:
  bb_pct_b:   %B = (close - lower) / (upper - lower) — 价格在带内的位置 (0~1)
  bb_width:   bandwidth = (upper - lower) / SMA — 相对波动率
  bb_squeeze: width 的 Z-score — 压缩/扩张状态

来源: Bollinger (2002) "Bollinger on Bollinger Bands"
A股截面实证: %B < 0.2 后续20日超额收益为正 (超卖反转)
              bandwidth 低→后续突破概率高于均值
              squeeze < -1.5 后突破方向与 %B 符号一致
"""

import numpy as np
import pandas as pd

from quant.config.constants import _require_cfg
from quant.factor.registry import _cs_zscore

# ── 参数从 config.yaml 读取 ──
BB_WINDOW = _require_cfg("factor.bb.window")              # N: 主窗口, 默认 20
BB_WIDTH_MULTIPLIER = _require_cfg("factor.bb.width_multiplier")  # K: 带宽系数, 默认 2.0
BB_SQUEEZE_LOOKBACK = _require_cfg("factor.bb.squeeze_lookback")   # M: squeeze 历史窗口, 默认 60


def _compute_bb_bands(close: pd.DataFrame, window: int, k: float):
    """Compute SMA, upper, lower bands from close price DataFrame.

    Args:
        close: DataFrame with date index and symbol columns
        window: rolling window (default 20)
        k: standard deviation multiplier (default 2.0)

    Returns: (sma, upper, lower, width) — each a DataFrame
    """
    sma = close.rolling(window).mean()
    std = close.rolling(window).std()
    upper = sma + k * std
    lower = sma - k * std
    width = (upper - lower) / sma.replace(0, np.nan)
    return sma, upper, lower, width


def compute_bb_pct_b(data: pd.DataFrame, date: str, window: int = None, k: float = None) -> pd.Series:
    """%B 因子: (close - lower) / (upper - lower).

    低 %B (接近0) = 价格被压缩在下轨附近 = 超卖 → 预期反弹 → 取负号使得低%B得高分。
    IC 预期方向: 正 (低%B → 高分 → 正收益).

    Args:
        data: MultiIndex DataFrame with close column
        date: target date
        window: rolling window (default from config: factor.bb.window)
        k: width multiplier (default from config: factor.bb.width_multiplier)
    """
    w = window or BB_WINDOW
    mult = k or BB_WIDTH_MULTIPLIER
    close = data["close"]
    if date not in close.index:
        return pd.Series(np.nan, index=close.columns, name="bb_pct_b")

    _, upper, lower, _ = _compute_bb_bands(close, w, mult)
    upper_t = upper.loc[date]
    lower_t = lower.loc[date]
    close_t = close.loc[date]

    pct_b = (close_t - lower_t) / (upper_t - lower_t + 1e-9)
    # 超卖(低%B) → 高分 → 取负号
    return _cs_zscore(-pct_b).rename("bb_pct_b")


def compute_bb_width(data: pd.DataFrame, date: str, window: int = None, k: float = None) -> pd.Series:
    """布林带宽因子: (upper - lower) / SMA.

    低 bandwidth = 价格波动率低 = 压缩状态 → 后续突破概率高 → 取负号使得低width得高分。
    与 range_20d (基于 (high-low)/close) 正交：width 用标准差，range 用日极值。

    IC 预期方向: 正 (低width → 高分 → 正收益).

    Args:
        data: MultiIndex DataFrame with close column
        date: target date
        window: rolling window (default from config: factor.bb.window)
        k: width multiplier (default from config: factor.bb.width_multiplier)
    """
    w = window or BB_WINDOW
    mult = k or BB_WIDTH_MULTIPLIER
    close = data["close"]
    if date not in close.index:
        return pd.Series(np.nan, index=close.columns, name="bb_width")

    _, _, _, width = _compute_bb_bands(close, w, mult)
    width_t = width.loc[date]
    # 低width(压缩) → 高分 → 取负号
    return _cs_zscore(-width_t).rename("bb_width")


def compute_bb_squeeze(data: pd.DataFrame, date: str,
                       window: int = None, k: float = None,
                       squeeze_lookback: int = None) -> pd.Series:
    """布林带挤压因子: bandwidth 相对历史均值的 Z-score.

    squeeze = (width_t - mean(width, M)) / std(width, M), M = squeeze_lookback (默认 60).
    负 squeeze = 处于压缩状态 (width 低于历史均值) = 突破前兆.

    机构在压缩期建仓 → 负 squeeze 结合趋势方向有预测力。
    截面单独使用: 负squeeze → 高分 → 取负号 → 预期正IC.

    Args:
        data: MultiIndex DataFrame with close column
        date: target date
        window: rolling window for BB (default: factor.bb.window)
        k: width multiplier (default: factor.bb.width_multiplier)
        squeeze_lookback: historical window for Z-score (default: factor.bb.squeeze_lookback)
    """
    w = window or BB_WINDOW
    mult = k or BB_WIDTH_MULTIPLIER
    m = squeeze_lookback or BB_SQUEEZE_LOOKBACK
    close = data["close"]
    if date not in close.index:
        return pd.Series(np.nan, index=close.columns, name="bb_squeeze")

    _, _, _, width = _compute_bb_bands(close, w, mult)
    idx = width.index.get_loc(date)
    start = max(0, idx - m + 1)
    width_hist = width.iloc[start:idx + 1]

    if len(width_hist) < 10:
        return pd.Series(np.nan, index=close.columns, name="bb_squeeze")

    width_mean = width_hist.mean()
    width_std = width_hist.std()
    width_t = width_hist.iloc[-1]

    squeeze = (width_t - width_mean) / (width_std + 1e-9)
    # 负squeeze(压缩) → 高分 → 取负号
    return _cs_zscore(-squeeze).rename("bb_squeeze")
