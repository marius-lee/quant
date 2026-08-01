"""幻方公开因子 (Tier A/B) — 券商研报交叉验证。

因子列表:
  换手率波动率   — std(turnover)/mean(turnover) × (-1)
  MIF          — |隔夜收益| × 换手率自相关
  特质换手率波动  — 截面去均值后 turnover std
  换手加速度     — (Δ5 - Δ20) / σ(turnover)
  量价背离度     — -corr(close, volume) 变化率

来源: 幻方公开方法论文 × 14 家券商研报 (2026-07-20 筛选)
"""

import numpy as np
import pandas as pd

from quant.config.constants import _require_cfg
from quant.factor.registry import _cs_zscore

# ── 参数 ──
HF_TURNOVER_VOL_WINDOW = _require_cfg("factor.huanfang.turnover_vol_window")    # 20
HF_MIF_WINDOW = _require_cfg("factor.huanfang.mif_window")                       # 20
HF_IDIO_VOL_WINDOW = _require_cfg("factor.huanfang.idio_vol_window")             # 20
HF_TACCEL_SHORT = _require_cfg("factor.huanfang.taccel_short")                   # 5
HF_TACCEL_LONG = _require_cfg("factor.huanfang.taccel_long")                     # 20
HF_VP_DIV_WINDOW = _require_cfg("factor.huanfang.vp_div_window")                # 20


# ═══════════════════════════════════════════════════════════
# 1. 换手率波动率 — std(turnover)/mean(turnover), 取负号
#    来源: 兴业 2025, |IC|>10%. 高换手波动=不稳定=负收益.
# ═══════════════════════════════════════════════════════════

def compute_turnover_vol(data: pd.DataFrame, date: str,
                         window: int = None) -> pd.Series:
    """换手率波动率: std(turnover, window) / mean(turnover, window), negated."""
    w = window or HF_TURNOVER_VOL_WINDOW
    t = data["turnover"]
    if date not in t.index:
        return pd.Series(np.nan, index=t.columns, name="turnover_vol")
    idx = t.index.get_loc(date)
    start = max(0, idx - w + 1)
    t_hist = t.iloc[start:idx + 1]
    mean_t = t_hist.mean()
    std_t = t_hist.std()
    cv = std_t / mean_t.replace(0, np.nan)  # coefficient of variation
    # 高 CV = 不稳定 = 负收益 → 取负号
    return _cs_zscore(-cv).rename("turnover_vol")


# ═══════════════════════════════════════════════════════════
# 2. MIF 市场非有效性 — |隔夜收益| × turnover 自相关
#    来源: 国盛 2022, IR=2.49. 高 MIF = 定价无效 = 有望修正.
# ═══════════════════════════════════════════════════════════

def compute_mif(data: pd.DataFrame, date: str,
                window: int = None) -> pd.Series:
    """MIF: |overnight_return| × corr(turnover, mean_turnover, window)."""
    w = window or HF_MIF_WINDOW
    close, open_, t = data["close"], data["open"], data["turnover"]
    if date not in close.index:
        return pd.Series(np.nan, index=close.columns, name="mif")
    idx = close.index.get_loc(date)
    start = max(0, idx - w + 1)

    # Overnight return: (open_t - close_{t-1}) / close_{t-1}
    prev_close = close.shift(1)
    overnight_ret = abs((open_ - prev_close) / prev_close.replace(0, np.nan))
    overnight_t = overnight_ret.iloc[idx]

    # Turnover autocorrelation: corr(turnover_t, mean(turnover))
    t_hist = t.iloc[start:idx + 1]
    t_mean = t_hist.mean()
    # rolling corr between each day's turnover and the mean across window
    if len(t_hist) < 5:
        return _cs_zscore(pd.Series(0.0, index=t.columns)).rename("mif")

    # Simplified: last-day turnover deviation from mean, normalized by std
    t_last = t_hist.iloc[-1]
    t_std = t_hist.std()
    t_dev = (t_last - t_mean) / t_std.replace(0, np.nan)

    mif_raw = overnight_t * abs(t_dev)
    # 高 MIF = 定价错位 → 预期修正 → 取正号
    return _cs_zscore(mif_raw).rename("mif")


# ═══════════════════════════════════════════════════════════
# 3. 特质换手率波动 — 截面去均值后 std(turnover, window)
#    来源: 券商综合, |IC|>10%. 剥离市场换手趋势后的异动.
# ═══════════════════════════════════════════════════════════

def compute_idio_turnover_vol(data: pd.DataFrame, date: str,
                              window: int = None) -> pd.Series:
    """特质换手率波动: 截面去均值 turnover 的时序 std."""
    w = window or HF_IDIO_VOL_WINDOW
    t = data["turnover"]
    if date not in t.index:
        return pd.Series(np.nan, index=t.columns, name="idio_turnover_vol")
    idx = t.index.get_loc(date)
    start = max(0, idx - w + 1)
    t_hist = t.iloc[start:idx + 1]

    # 每日截面去均值 (去除市场整体换手水平)
    cs_mean = t_hist.mean(axis=1)
    t_residual = t_hist.sub(cs_mean, axis=0)

    # 残余时序 std
    idio_vol = t_residual.std()
    # 高异动 = 信息事件 → 取正号 (事件驱动型超额)
    return _cs_zscore(idio_vol).rename("idio_turnover_vol")


# ═══════════════════════════════════════════════════════════
# 4. 换手加速度 — (Δ5 - Δ20) / σ(turnover)
#    来源: 幻方"加速特征"方法论. 换手变化率的加速度.
#    与现有 turnover_accel 互补 — 那个是比率, 这个是差分归一化.
# ═══════════════════════════════════════════════════════════

def compute_turnover_accel(data: pd.DataFrame, date: str,
                           short: int = None, long: int = None) -> pd.Series:
    """换手加速度: (Δturnover_short - Δturnover_long) / σ(turnover, long)."""
    s = short or HF_TACCEL_SHORT
    l = long or HF_TACCEL_LONG
    t = data["turnover"]
    if date not in t.index:
        return pd.Series(np.nan, index=t.columns, name="turnover_accel_5_20")
    idx = t.index.get_loc(date)
    if idx < l:
        return pd.Series(np.nan, index=t.columns, name="turnover_accel_5_20")

    # Δshort = turnover_t - turnover_{t-s}
    delta_s = t.iloc[idx] - t.iloc[max(0, idx - s)]
    # Δlong = turnover_t - turnover_{t-l}
    delta_l = t.iloc[idx] - t.iloc[max(0, idx - l)]

    t_hist = t.iloc[max(0, idx - l + 1):idx + 1]
    sigma = t_hist.std()

    # 加速度 = (Δshort - Δlong) / σ
    accel = (delta_s - delta_l) / sigma.replace(0, np.nan)
    # 正加速度 = 换手加速 → 动量信号 → 取正号
    return _cs_zscore(accel).rename("turnover_accel_5_20")


# ═══════════════════════════════════════════════════════════
# 5. 量价背离度 — 负 price-volume 相关性变化
#    来源: 幻方"量价非线性"框架. 量价同向=趋势健康, 背离=转折信号.
# ═══════════════════════════════════════════════════════════

def compute_vp_divergence(data: pd.DataFrame, date: str,
                          window: int = None) -> pd.Series:
    """量价背离度: -corr(close.pct_change(), volume.pct_change(), window) 的变化率."""
    w = window or HF_VP_DIV_WINDOW
    close, volume = data["close"], data["volume"]
    if date not in close.index:
        return pd.Series(np.nan, index=close.columns, name="vp_divergence")
    idx = close.index.get_loc(date)
    if idx < max(w, 2):
        return pd.Series(np.nan, index=close.columns, name="vp_divergence")

    # 当前窗口的量价相关性
    c_curr = close.iloc[max(0, idx - w + 1):idx + 1]
    v_curr = volume.iloc[max(0, idx - w + 1):idx + 1]
    corr_curr = c_curr.corrwith(v_curr)

    # 前一窗口
    c_prev = close.iloc[max(0, idx - 2 * w + 1):max(0, idx - w + 1)]
    v_prev = volume.iloc[max(0, idx - 2 * w + 1):max(0, idx - w + 1)]
    corr_prev = c_prev.corrwith(v_prev)

    # 负相关性变化 = 背离加剧 → 转折信号
    divergence = (corr_prev - corr_curr)
    return _cs_zscore(divergence).rename("vp_divergence")
