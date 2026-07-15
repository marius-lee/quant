"""共享中间计算图 — 预计算所有因子共用的滚动统计量。

核心思路 (ARCH-IMPROVEMENT-2026-07-13 第三轮):
  所有滚动统计量 (pct_change, rolling_sum/std/max/min 等) 一次算完,
  因子只做截面操作 (z-score/rank) — 不再重复 O(lookback × symbols)。

用法:
  prims = precompute_primitives(data_full)
  result = FACTOR_SHORTCUT["momentum_20d"](prims, date, 20)
"""
import numpy as np
import pandas as pd
from quant.utils.logger import get_logger

_log = get_logger("factor.primitives")


def precompute_primitives(data: pd.DataFrame) -> dict:
    """预计算所有价格因子共享的滚动统计量。

    Args:
        data: MultiIndex DataFrame (field, symbol), 含 close/open/high/low/volume/amount

    Returns:
        {primitive_name: DataFrame(date × symbol)}
        键如: "log_ret", "cum_log_5", "vol_20", "roll_max_250", "turnover"
    """
    t0 = pd.Timestamp.now()
    close = data["close"].astype(float)
    volume = data["volume"].astype(float) if "volume" in data.columns.levels[0] else None
    amount = data["amount"].astype(float) if "amount" in data.columns.levels[0] else None
    high = data["high"].astype(float) if "high" in data.columns.levels[0] else None
    low = data["low"].astype(float) if "low" in data.columns.levels[0] else None
    opn = data["open"].astype(float) if "open" in data.columns.levels[0] else None

    prims = {}
    
    # ── 对数收益 (几乎所有的时序列因子共用) ──
    _log.info("  primitives: log_ret")
    prims["log_ret"] = np.log(close).diff()
    
    # ── 简单收益 ──
    _log.info("  primitives: pct_ret")
    prims["pct_ret"] = close.pct_change()

    # ── 隔夜缺口 ──
    if opn is not None:
        prims["overnight_gap"] = (opn - close.shift(1)) / close.shift(1)

    # ── 换手率 ──
    if volume is not None:
        # total_shares 不在 data 中，换手率 ≈ volume / amount（用成交额反推）
        # 或者直接用 volume 代替，在因子函数内处理
        prims["raw_volume"] = volume
        if amount is not None and (amount > 0).all(None):
            prims["approx_turnover"] = (volume * close) / amount.replace(0, np.nan)

    if amount is not None:
        prims["raw_amount"] = amount

    # ── 滚动统计量 (基于 log_ret) ──
    log_ret = prims["log_ret"]
    # 从 _PRICE_FN_MAP 提取所有用到的窗口大小
    from quant.factor.compute.price import _PRICE_FN_MAP
    all_windows = set()
    for _, win in _PRICE_FN_MAP.values():
        if isinstance(win, int) and win > 1:
            all_windows.add(win)
        elif isinstance(win, int) and win <= 1:
            pass  # 0/1窗口无意义
    # 补充常见窗口
    all_windows |= {5, 10, 20, 60, 63, 120, 126, 250, 252}

    for w in sorted(all_windows):
        if w <= 1:
            continue
        # 滚动累积收益 (动量用)
        _log.info(f"  primitives: cum_log_{w} (window={w})")
        prims[f"cum_log_{w}"] = log_ret.rolling(w, min_periods=max(w//2, 1)).sum()
        # 滚动波动率
        prims[f"vol_{w}"] = log_ret.rolling(w, min_periods=max(w//2, 1)).std() * np.sqrt(252)
        # 滚动均值收益
        prims[f"mean_log_{w}"] = log_ret.rolling(w, min_periods=max(w//2, 1)).mean()
    
    # ── 滚动统计量 (基于 close) ──
    for w in sorted(all_windows):
        if w <= 1:
            continue
        prims[f"roll_high_{w}"] = close.rolling(w, min_periods=max(w//2, 1)).max()
        prims[f"roll_low_{w}"] = close.rolling(w, min_periods=max(w//2, 1)).min()
    
    # ── 滚动统计量 (基于 pct_ret) ──
    pct_ret = prims["pct_ret"]
    for w in sorted(all_windows):
        if w <= 1:
            continue
        prims[f"max_pct_{w}"] = pct_ret.rolling(w, min_periods=max(w//2, 1)).max()
        prims[f"min_pct_{w}"] = pct_ret.rolling(w, min_periods=max(w//2, 1)).min()

    # ── 滚动成交量均值 ──
    if volume is not None:
        for w in sorted(all_windows):
            if w <= 1:
                continue
            prims[f"vol_ma_{w}"] = volume.rolling(w, min_periods=max(w//2, 1)).mean()

    if amount is not None:
        for w in sorted(all_windows):
            if w <= 1:
                continue
            prims[f"amt_ma_{w}"] = amount.rolling(w, min_periods=max(w//2, 1)).mean()

    elapsed = (pd.Timestamp.now() - t0).total_seconds()
    _log.info(f"  primitives done: {len(prims)} tables in {elapsed:.1f}s")
    return prims


# ═══════════════════════════════════════════════════════════
# 因子快捷计算映射 — 用预计算算子直接推导因子值
# ═══════════════════════════════════════════════════════════

def _momentum(prims: dict, date: str, window: int):
    """动量 = cum_log_N.loc[date] → zscore"""
    from quant.factor.registry import _cs_zscore
    key = f"cum_log_{window}"
    if key in prims:
        s = prims[key].loc[date].dropna()
        return _cs_zscore(s).rename(f"momentum_{window}d")
    return None

def _volatility(prims: dict, date: str, window: int):
    """波动率 = -vol_N.loc[date] (低波异象) → zscore"""
    from quant.factor.registry import _cs_zscore
    key = f"vol_{window}"
    if key in prims:
        s = prims[key].loc[date].dropna()
        return _cs_zscore(-s).rename(f"volatility_{window}d")
    return None

def _max_return(prims: dict, date: str, window: int):
    """最大收益 = -max_pct_N.loc[date] → zscore"""
    from quant.factor.registry import _cs_zscore
    key = f"max_pct_{window}"
    if key in prims:
        s = prims[key].loc[date].dropna()
        return _cs_zscore(-s).rename(f"max_ret_{window}d")
    return None

def _skewness(prims: dict, date: str, window: int):
    """偏度: 需要原始 log_ret 数组计算 skew, 无法从预计算直接得出。返回 None 走 fallback。"""
    return None  # 需要自定义窗口 skew，走原始函数

def _rsi_reversal(prims: dict, date: str, window: int):
    """RSI: 需要逐日涨幅/跌幅分拆, 无法从预计算直接得出。返回 None 走 fallback。"""
    return None

def _volume_ratio(prims: dict, date: str, window: int):
    """量比 = vol_ma_N / vol_ma_L"""
    from quant.factor.registry import _cs_zscore
    from quant.config.constants import _VOL_RATIO_LONG
    s_key = f"vol_ma_{window}"
    l_key = f"vol_ma_{_VOL_RATIO_LONG}"
    if s_key in prims and l_key in prims:
        short_avg = prims[s_key].loc[date]
        long_avg = prims[l_key].loc[date]
        ratio = short_avg / long_avg.replace(0, np.nan)
        return _cs_zscore(ratio).rename(f"vol_ratio_{window}d")
    return None

def _overnight_gap(prims: dict, date: str, window: int):
    """隔夜缺口: 从预计算 gap 取 rolling mean"""
    from quant.factor.registry import _cs_zscore
    if "overnight_gap" in prims:
        gap_ma = prims["overnight_gap"].rolling(window, min_periods=max(window//2,1)).mean()
        s = gap_ma.loc[date].dropna()
        return _cs_zscore(s).rename(f"gap_{window}d")
    return None

def _intraday_range(prims: dict, date: str, window: int):
    """日内振幅: 需要 (high-low)/close 再 rolling mean, 无法从预计算直接得出。"""
    return None  # 走原始函数

def _turnover_reversal(prims: dict, date: str, short: int, long: int = 20):
    """换手率反转: 需要换手率数据, 用 approx_turnover 近似。"""
    from quant.factor.registry import _cs_zscore
    key = "approx_turnover"
    if key in prims:
        to = prims[key]
        s_avg = to.rolling(short, min_periods=max(short//2,1)).mean().loc[date]
        l_avg = to.rolling(long, min_periods=max(long//2,1)).mean().loc[date]
        ratio = s_avg / l_avg.replace(0, np.nan)
        return _cs_zscore(-(ratio - 1)).rename(f"turnover_rev_{short}d")
    return None

def _money_flow(prims: dict, date: str, window: int):
    """资金流: 需要 (high+low+close)/3 * volume, 无法直接从预计算得出。"""
    return None  # 走原始函数

def _ma_alignment(prims: dict, date: str, window: int):
    """均线排列: 需要多均线比较, 无法从预计算直接得出。"""
    return None

def _volume_price_corr(prims: dict, date: str, window: int):
    """量价相关: 需要 pairwise correlation, 无法从预计算直接得出。"""
    return None


# ═══════════════════════════════════════════════════════════
# 映射表: 因子函数名 → 快捷计算函数
# 不在映射表中的因子走原始函数 (fallback)
# ═══════════════════════════════════════════════════════════

FACTOR_SHORTCUT = {
    # compute_momentum 因子 — 直接取 cum_log_N
    "compute_momentum":            _momentum,
    # compute_volatility — 直接取 vol_N
    "compute_volatility":           _volatility,
    # compute_max_return — 直接取 max_pct_N
    "compute_max_return":           _max_return,
    # compute_skewness — 需要自定义计算，走 fallback
    "compute_skewness":             _skewness,
    # compute_rsi_reversal — 需要自定义计算，走 fallback
    "compute_rsi_reversal":         _rsi_reversal,
    # compute_volume_ratio — 从预计算取
    "compute_volume_ratio":         _volume_ratio,
    # compute_overnight_gap — 从预计算取
    "compute_overnight_gap":        _overnight_gap,
    # compute_intraday_range — 需要原始 high/low，走 fallback
    "compute_intraday_range":       _intraday_range,
    # 换手率 — 从预计算取
    "compute_turnover_reversal":    _turnover_reversal,
    "compute_turnover_change":      _turnover_reversal,
    # 复杂因子 — 走 fallback
    "compute_money_flow":           _money_flow,
    "compute_ma_alignment":         _ma_alignment,
    "compute_volume_price_corr":    _volume_price_corr,
}
