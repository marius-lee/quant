"""换手率/流动性因子 — 幻方 Tier S 因子接入 (2026-07-20).

来源:
    - 幻方量化公开方法论 (2023-2024): 量价非线性、换手率二阶特征
    - 东吴证券金工 (2024): CTR 换手率切割刀, IC=-7.6%, IR=2.63
    - 国盛证券金工 (2023): 高低位放量因子, IC=-6.6%, IR=3.00
    - 华安证券金工 (2024): 加速换手因子, IC=-10.5%, IR=4.29
"""
import numpy as np
import pandas as pd
from typing import Optional

from quant.factor.registry import _cs_zscore
from quant.utils.logger import get_logger as _get_logger

_log = _get_logger("factor.compute")


def compute_ctr(data: "pd.DataFrame", date: str, window: int = 20) -> "pd.Series":
    """CTR 换手率切割刀 (Conditional Turnover Reversal).

    算法:
        1. 对过去 window 日, 每日计算隔夜收益: overnight = open/prev_close - 1
        2. 按 overnight > 0 (up) 和 overnight < 0 (down) 分组
        3. 每组内计算换手率变化: turnover_chg = turnover_t / turnover_{t-1} - 1
        4. 取两组均值之差: CTR = mean(turnover_chg_up) - mean(turnover_chg_down)
        5. 截面 zscore 后取负号 (高CTR→散户追涨→未来收益低)

    来源: 东吴证券金工 (2024), IC=-7.6%, IR=2.63.
          CTR 度量隔夜方向不对称的换手行为: 上涨日放量 vs 下跌日放量的结构差异.
    """
    opn = data["open"]
    close = data["close"]
    to = data["turnover"]

    if date not in to.index:
        return pd.Series(np.nan, index=to.columns, name="ctr_20d")

    idx = to.index.get_loc(date)
    start = max(0, idx - window)
    if idx - start < 5:
        return pd.Series(np.nan, index=to.columns, name="ctr_20d")

    symbols = to.columns
    ctr_values = {}
    prev_close = close.shift(1)

    for sym in symbols:
        try:
            o_sym = opn[sym].iloc[start:idx + 1]
            pc_sym = prev_close[sym].iloc[start:idx + 1]
            to_sym = to[sym].iloc[start:idx + 1]
        except (KeyError, IndexError):
            continue

        # 隔夜收益: open / prev_close - 1
        valid = pc_sym.notna() & o_sym.notna() & (pc_sym != 0)
        overnight = np.where(valid, o_sym.values / pc_sym.values - 1, np.nan)

        # 换手率变化: turnover_t / turnover_{t-1} - 1
        to_chg = to_sym.pct_change().values

        up_mask = overnight > 0
        down_mask = overnight < 0

        up_chg = to_chg[up_mask]
        down_chg = to_chg[down_mask]

        # 至少各 2 个观测
        if len(up_chg) < 2 or len(down_chg) < 2:
            continue

        up_mean = np.nanmean(up_chg)
        down_mean = np.nanmean(down_chg)
        ctr_values[sym] = up_mean - down_mean

    result = pd.Series(ctr_values)
    if result.empty:
        return pd.Series(np.nan, index=symbols, name="ctr_20d")
    # 高CTR = 上涨日换手异常放大 → 散户追涨 → 未来跑输 → 取负号
    return _cs_zscore(-result).rename("ctr_20d")


def compute_hl_volume(data: "pd.DataFrame", date: str, window: int = 20) -> "pd.Series":
    """高低位放量因子 (High-Low Turnover Divergence).

    算法:
        1. 过去 window 日 turnover 序列
        2. P80 = 80 分位数 (高位换手), P20 = 20 分位数 (低位换手)
        3. 因子值 = (P80 - P20) / mean(turnover)
        4. 截面 zscore 后取负号 (放量分化大 → 筹码分散 → 未来跑输)

    来源: 国盛证券金工 (2023), IC=-6.6%, IR=3.00.
          高位放量(主力出货)与低位缩量(无人接盘)的分化反映筹码结构恶化.
    """
    to = data["turnover"]

    if date not in to.index:
        return pd.Series(np.nan, index=to.columns, name="hl_volume_20d")

    idx = to.index.get_loc(date)
    start = max(0, idx - window + 1)

    symbols = to.columns
    values = {}
    for sym in symbols:
        try:
            to_sym = to[sym].iloc[start:idx + 1].dropna()
        except (KeyError, IndexError):
            continue
        if len(to_sym) < 10:
            continue
        p80 = np.percentile(to_sym.values, 80)
        p20 = np.percentile(to_sym.values, 20)
        mean_to = np.mean(to_sym.values)
        if mean_to == 0:
            continue
        values[sym] = (p80 - p20) / mean_to

    result = pd.Series(values)
    if result.empty:
        return pd.Series(np.nan, index=symbols, name="hl_volume_20d")
    # 分化大 → 负面信号 → 取负号
    return _cs_zscore(-result).rename("hl_volume_20d")


def compute_turnover_accel(data: "pd.DataFrame", date: str,
                           short: int = 5, long: int = 10) -> "pd.Series":
    """加速换手因子 (Turnover Acceleration).

    算法:
        1. 一阶变化: d5 = turnover_t / turnover_{t-5} - 1
        2. 二阶变化: d10 = turnover_{t-5} / turnover_{t-10} - 1
        3. accel = d5 / d10 (二阶/一阶 比率)
        4. 分母接近 0 时取符号方向 (sign(d5) * sign(d10) * large_value)
        5. 截面 zscore 后取负号 (加速放量 → 投机过热 → 未来跑输)

    来源: 华安证券金工 (2024), IC=-10.5%, IR=4.29.
          幻方方法论"加速/减速特征": 不仅看换手率变化方向, 更看变化速率(二阶).
    """
    to = data["turnover"]

    if date not in to.index:
        return pd.Series(np.nan, index=to.columns, name="turnover_accel")

    idx = to.index.get_loc(date)
    if idx < long:
        return pd.Series(np.nan, index=to.columns, name="turnover_accel")

    symbols = to.columns
    values = {}
    max_ratio = 10.0  # 业界标准: 换手加速度裁剪上限, 来源: 华安2024 — 3σ 裁剪

    for sym in symbols:
        try:
            to_sym = to[sym]
        except (KeyError, IndexError):
            continue

        t0 = to_sym.iloc[idx]
        t5 = to_sym.iloc[idx - short] if idx >= short else np.nan
        t10 = to_sym.iloc[idx - long] if idx >= long else np.nan

        if pd.isna(t0) or pd.isna(t5) or pd.isna(t10) or t5 == 0 or t10 == 0:
            continue

        d5 = t0 / t5 - 1.0
        d10 = t5 / t10 - 1.0

        if abs(d10) < 1e-8:
            # 一阶变化接近 0 → d5 的符号决定方向, 给最大裁剪值
            values[sym] = max_ratio if d5 > 0 else -max_ratio
        else:
            accel = d5 / d10
            values[sym] = np.clip(accel, -max_ratio, max_ratio)

    result = pd.Series(values)
    if result.empty:
        return pd.Series(np.nan, index=symbols, name="turnover_accel")
    # 加速放量 → 负面信号 → 取负号
    return _cs_zscore(-result).rename("turnover_accel")
