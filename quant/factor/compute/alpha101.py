"""Alpha 101 因子 — WorldQuant 标准公式 (test-v326).

接入策略: 原生 Python 实现而非表达式编译 (避免编译器扩展).
优先实现 7 个最高优先级 alphas, 与已有缺口互补.
所有因子需要 VWAP=amount/volume 和 adv_N=rolling(amount, N).
"""

import numpy as np
import pandas as pd
from quant.factor.registry import _cs_zscore
from quant.utils.logger import get_logger

_log = get_logger(__name__)


def _vwap(data):
    """日均价 = amount / volume."""
    if "amount" in data and "volume" in data:
        amt = data["amount"].astype(float)
        vol = data["volume"].astype(float)
        return amt / vol.replace(0, np.nan)
    return None


def _adv(data, window):
    """window 日均成交额."""
    if "amount" in data:
        return data["amount"].astype(float).rolling(window, min_periods=max(window // 2, 1)).mean()
    return None


def _row_at(s, date, offset=0):
    """取 date (偏移 offset 个交易日) 当日的横截面行 — B2 (2026-08-18):
    物化传入整 chunk 数据, 原 .iloc[-1] 恒取 chunk 末行 → 前视 + 同值.
    offset=0: 当日; offset=-1: 前一交易日 (用于 delta 类因子).
    date 不存在 (停牌/非交易日) → None, 调用方返回 None."""
    _d = pd.Timestamp(date)
    if _d not in s.index:
        return None
    _i = s.index.get_loc(_d) + offset
    if _i < 0 or _i >= len(s):
        return None
    return s.iloc[_i]


def _rolling_corr(a, b, window):
    """滚动相关系数."""
    ma = a.rolling(window, min_periods=max(window // 2, 1)).mean()
    mb = b.rolling(window, min_periods=max(window // 2, 1)).mean()
    cov = ((a - ma) * (b - mb)).rolling(window, min_periods=max(window // 2, 1)).mean()
    sa = a.rolling(window, min_periods=max(window // 2, 1)).std()
    sb = b.rolling(window, min_periods=max(window // 2, 1)).std()
    return cov / (sa * sb).replace(0, np.nan)


# ═══════════════════════════════════════════════
# Alpha #33: 开盘缺口 — rank(-(1-open/close))
# ═══════════════════════════════════════════════
def compute_alpha033(data, date, window=None):
    """Alpha#33: -1 + open/close. 高开→正, 低开→负."""
    close = data["close"].astype(float) if isinstance(data.columns, pd.MultiIndex) else data.astype(float)
    opn = data["open"].astype(float) if "open" in data.columns.get_level_values(0) else None
    if close is None or opn is None or close.empty:
        return None
    # B2 (2026-08-18): 原 .iloc[-1] 恒取 chunk 末行 → 前视. 按 date 取当日.
    c_today = _row_at(close, date)
    o_today = _row_at(opn, date)
    if c_today is None or o_today is None:
        return None
    gap = o_today / c_today - 1
    return _cs_zscore(gap, sparse=True).rename("alpha033_gap")


# ═══════════════════════════════════════════════
# Alpha #42: VWAP 收盘偏离 — rank(vwap-close)/rank(vwap+close)
# ═══════════════════════════════════════════════
def compute_alpha042(data, date, window=None):
    """Alpha#42: VWAP vs close 偏离度."""
    v = _vwap(data)
    close = data["close"].astype(float) if isinstance(data.columns, pd.MultiIndex) else data.astype(float)
    if v is None or close is None or close.empty:
        return None
    # B2: 按 date 取当日, 原 .iloc[-1] 恒取 chunk 末行 → 前视.
    v_last = _row_at(v, date)
    c_last = _row_at(close, date)
    if v_last is None or c_last is None:
        return None
    num = v_last - c_last
    den = v_last + c_last
    ratio = num / den.replace(0, np.nan)
    return _cs_zscore(ratio, sparse=True).rename("alpha042_vwap_div")


# ═══════════════════════════════════════════════
# Alpha #41: 几何中间价偏离 VWAP — sqrt(high*low) - vwap
# ═══════════════════════════════════════════════
def compute_alpha041(data, date, window=None):
    """Alpha#41: 几何均值 vs VWAP."""
    v = _vwap(data)
    high = data["high"].astype(float) if "high" in data.columns.get_level_values(0) else None
    low = data["low"].astype(float) if "low" in data.columns.get_level_values(0) else None
    if v is None or high is None or low is None:
        return None
    # B2: 按 date 取当日, 原 .iloc[-1] 恒取 chunk 末行 → 前视.
    h_today = _row_at(high, date)
    l_today = _row_at(low, date)
    v_today = _row_at(v, date)
    if h_today is None or l_today is None or v_today is None:
        return None
    geo = np.sqrt(np.asarray(h_today.values, dtype=np.float64) * np.asarray(l_today.values, dtype=np.float64))
    diff = geo - v_today
    return _cs_zscore(diff, sparse=True).rename("alpha041_geo_vwap")


# ═══════════════════════════════════════════════
# Alpha #12: 量价方向 — sign(delta(volume)) * -delta(close)
# ═══════════════════════════════════════════════
def compute_alpha012(data, date, window=None):
    """Alpha#12: 成交量变化方向 × 价格反向变动."""
    close = data["close"].astype(float) if isinstance(data.columns, pd.MultiIndex) else data.astype(float)
    volume = data["volume"].astype(float) if "volume" in data.columns.get_level_values(0) else None
    if close is None or volume is None or close.empty:
        return None
    # B2: 按 date 取当日 + 前一交易日, 原 .iloc[-1]/.iloc[-2] 恒取 chunk 末行 → 前视.
    v_today = _row_at(volume, date)
    v_prev = _row_at(volume, date, -1)
    c_today = _row_at(close, date)
    c_prev = _row_at(close, date, -1)
    if v_today is None or v_prev is None or c_today is None or c_prev is None:
        return None
    dv = np.sign(v_today - v_prev)
    dp = -(c_today - c_prev)
    return _cs_zscore(dv * dp, sparse=True).rename("alpha012_vol_price_dir")


# ═══════════════════════════════════════════════
# Alpha #2: 量价背离 — -correlation(rank(delta(log(volume))), rank(return), 6)
# ═══════════════════════════════════════════════
def compute_alpha002(data, date, window=None):
    """Alpha#2: 量价背离. v372: 仅取 tail 行做 rolling corr (O(T×N)→O(W×N))."""
    close = data["close"].astype(float) if isinstance(data.columns, pd.MultiIndex) else data.astype(float)
    volume = data["volume"].astype(float) if "volume" in data.columns.get_level_values(0) else None
    if close is None or volume is None or close.empty or len(close) < 10:
        return None
    window = 6
    dlog_vol = np.log(volume.replace(0, np.nan)).diff()
    ret = close.pct_change()
    # B2: 原 .iloc[-tail:] 恒取 chunk 尾部 (含 date 之后的未来行) → 前视.
    # 改为截到 date 为止的 tail 段.
    _d = pd.Timestamp(date)
    if _d not in close.index:
        return None
    _idx = close.index.get_loc(_d)
    _sl = max(0, _idx - (window + 2) + 1)
    corr = _rolling_corr(
        dlog_vol.rank(pct=True).iloc[_sl:_idx + 1],
        ret.rank(pct=True).iloc[_sl:_idx + 1],
        window).iloc[-1]
    return _cs_zscore(-corr, sparse=True).rename("alpha002_vol_price_div")


# ═══════════════════════════════════════════════
# Alpha #35: 量+区间+动量 — ts_rank(vol,32) * (1-ts_rank(range,16)) * (1-ts_rank(ret,32))
# ═══════════════════════════════════════════════
def compute_alpha035(data, date, window=None):
    """Alpha#35: 成交量×价格区间×动量 复合."""
    close = data["close"].astype(float) if isinstance(data.columns, pd.MultiIndex) else data.astype(float)
    high = data["high"].astype(float) if "high" in data.columns.get_level_values(0) else None
    low = data["low"].astype(float) if "low" in data.columns.get_level_values(0) else None
    volume = data["volume"].astype(float) if "volume" in data.columns.get_level_values(0) else None
    if close is None or volume is None or high is None or low is None or len(close) < 35:
        return None

    # ts_rank: percentile of latest value in rolling window
    def _ts_rank(x, w):
        return x.rolling(w, min_periods=max(w // 2, 1)).apply(
            lambda s: (s.iloc[-1] >= s).sum() / len(s), raw=False
        )

    rng = high - low
    ret = close.pct_change()
    vr = _ts_rank(volume, 32)
    rr = _ts_rank(rng, 16)
    mr = _ts_rank(ret, 32)
    # B2: 原 .iloc[-1] 恒取 chunk 末行 → 前视. rolling 序列按 date 定位取值.
    _d = pd.Timestamp(date)
    if _d not in close.index:
        return None
    _idx = close.index.get_loc(_d)
    composite = vr.iloc[_idx] * (1 - rr.iloc[_idx]) * (1 - mr.iloc[_idx])
    return _cs_zscore(composite, sparse=True).rename("alpha035_range_mom")


# ═══════════════════════════════════════════════
# Alpha #55: 筹码位置-量相关
# ═══════════════════════════════════════════════
def compute_alpha055(data, date, window=None):
    """Alpha#55: 价格位置 vs 成交量 相关性. v372: rolling max/min/rank 全历史, corr 仅 tail."""
    close = data["close"].astype(float) if isinstance(data.columns, pd.MultiIndex) else data.astype(float)
    high = data["high"].astype(float) if "high" in data.columns.get_level_values(0) else None
    low = data["low"].astype(float) if "low" in data.columns.get_level_values(0) else None
    volume = data["volume"].astype(float) if "volume" in data.columns.get_level_values(0) else None
    if close is None or volume is None or high is None or low is None or len(close) < 15:
        return None

    w = 12
    # B2: 原 .iloc[-tail:] 恒取 chunk 尾部 (含未来行) → 前视. 改为截到 date 为止.
    _d = pd.Timestamp(date)
    if _d not in close.index:
        return None
    _idx = close.index.get_loc(_d)
    tail = w + 2
    _sl = max(0, _idx - tail + 1)
    hh = high.iloc[_sl:_idx + 1].rolling(w, min_periods=max(w // 2, 1)).max()
    ll = low.iloc[_sl:_idx + 1].rolling(w, min_periods=max(w // 2, 1)).min()
    pos_tail = (close.iloc[_sl:_idx + 1] - ll) / (hh - ll).replace(0, np.nan)
    # pos 百分位 rank 也仅需 tail 内 (pos 值域 [0,1], 跨时间可比)
    pos_ranked = pos_tail.rank(pct=True)
    vol_ranked = volume.rank(pct=True)
    corr = _rolling_corr(pos_ranked.iloc[-8:], vol_ranked.iloc[_idx - 7:_idx + 1], 6).iloc[-1]
    return _cs_zscore(-corr, sparse=True).rename("alpha055_pos_vol")
