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
        if "turnover" in data.columns.levels[0]:
            prims["approx_turnover"] = data["turnover"]

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



   # ── 沪深300基准收益 (residual_momentum / idio_vol 共用) ──
   # 来源: AQR (2014) — 残差动量需要基准收益做回归; Ang et al. (2006) — 特质波动需要CAPM基准
   # benchmark_ret 从 benchmark_daily 表加载, 在 materialize() 中通过 store.get_benchmark() 添加
   # 指数数据不在 daily 表中, 此处不做 if "000300" in close.columns 检查

    # ── turnover 滚动统计 (trcf/str/abn_turnover/turnover_anomaly 共用) ──
    if "turnover" in data.columns.levels[0]:
        to = data["turnover"].astype(float)
        for w in sorted(all_windows):
            if w <= 1:
                continue
            prims[f"turnover_ma_{w}"] = to.rolling(w, min_periods=max(w // 2, 1)).mean()
            prims[f"turnover_std_{w}"] = to.rolling(w, min_periods=max(w // 2, 1)).std()
        prims["turnover"] = to
        _log.info("  primitives: turnover_ma/roll/std (multi-window)")

    # ── 资金流向 (Chaikin Money Flow) ──
    if high is not None and low is not None and amount is not None:
        hl_range = high - low
        hl_range = hl_range.where(hl_range > 0)
        mfm = ((close - low) - (high - close)) / hl_range
        mfv = mfm * amount
        for w in sorted(all_windows):
            if w <= 1:
                continue
            prims[f"money_flow_{w}"] = (
                mfv.rolling(w, min_periods=max(w // 2, 1)).sum()
                / amount.rolling(w, min_periods=max(w // 2, 1)).sum()
            )

    # ── 移动均线 ──
    for w in sorted(all_windows):
        if w <= 1:
            continue
        prims[f"ma_{w}"] = close.rolling(w, min_periods=max(w // 2, 1)).mean()

    # ── 量价相关性 (Pearson) ──
    if volume is not None:
        close_ret = close.pct_change()
        vol_chg = volume.pct_change()
        for w in sorted(all_windows):
            if w <= 1:
                continue
            prims[f"vol_price_corr_{w}"] = close_ret.rolling(
                w, min_periods=max(w // 2, 1)).corr(vol_chg)

    # ── 偏度 ──
    for w in sorted(all_windows):
        if w <= 1:
            continue
        prims[f"skew_{w}"] = log_ret.rolling(w, min_periods=max(w // 2, 1)).skew()

    # ── RSI ──
    pct = prims["pct_ret"]
    for w in sorted(all_windows):
        if w <= 1:
            continue
        gain = pct.where(pct > 0, 0).rolling(w, min_periods=max(w // 2, 1)).mean()
        loss = (-pct.where(pct < 0, 0)).rolling(w, min_periods=max(w // 2, 1)).mean()
        rs = gain / loss.replace(0, np.nan)
        prims[f"rsi_{w}"] = 100 - (100 / (1 + rs))

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
    s = prims[key].loc[date].dropna()
    return _cs_zscore(s).rename(f"momentum_{window}d")

def _volatility(prims: dict, date: str, window: int):
    """波动率 = -vol_N.loc[date] (低波异象) → zscore"""
    from quant.factor.registry import _cs_zscore
    key = f"vol_{window}"
    s = prims[key].loc[date].dropna()
    return _cs_zscore(-s).rename(f"volatility_{window}d")

def _max_return(prims: dict, date: str, window: int):
    """最大收益 = -max_pct_N.loc[date] → zscore"""
    from quant.factor.registry import _cs_zscore
    key = f"max_pct_{window}"
    s = prims[key].loc[date].dropna()
    return _cs_zscore(-s).rename(f"max_ret_{window}d")

def _skewness(prims: dict, date: str, window: int):
    """偏度 = -skew_N.loc[date] (负偏度异象) → zscore"""
    from quant.factor.registry import _cs_zscore
    key = f"skew_{window}"
    s = prims[key].loc[date].dropna()
    return _cs_zscore(-s).rename(f"skewness_{window}d")

def _rsi_reversal(prims: dict, date: str, window: int):
    """RSI 反转 = -rsi_N.loc[date] → zscore"""
    from quant.factor.registry import _cs_zscore
    key = f"rsi_{window}"
    s = prims[key].loc[date].dropna()
    return _cs_zscore(-s).rename(f"rsi_rev_{window}d")

def _volume_ratio(prims: dict, date: str, window: int):
    """量比 = vol_ma_N / vol_ma_L"""
    from quant.factor.registry import _cs_zscore
    from quant.config.constants import _VOL_RATIO_LONG
    s_key = f"vol_ma_{window}"
    l_key = f"vol_ma_{_VOL_RATIO_LONG}"
    short_avg = prims[s_key].loc[date]
    long_avg = prims[l_key].loc[date]
    ratio = short_avg / long_avg.replace(0, np.nan)
    return _cs_zscore(ratio).rename(f"vol_ratio_{window}d")

def _overnight_gap(prims: dict, date: str, window: int):
    """隔夜缺口: 从预计算 gap 取 rolling mean"""
    from quant.factor.registry import _cs_zscore
    gap_ma = prims["overnight_gap"].rolling(window, min_periods=max(window // 2, 1)).mean()
    s = gap_ma.loc[date].dropna()
    return _cs_zscore(s).rename(f"gap_{window}d")

# _intraday_range removed from FACTOR_SHORTCUT — 走 fn(data) 路径

def _turnover_reversal(prims: dict, date: str, short: int, long: int = 20):
    """换手率反转: 需要换手率数据, 用 approx_turnover 近似。"""
    from quant.factor.registry import _cs_zscore
    to = prims["approx_turnover"]
    s_avg = to.rolling(short, min_periods=max(short // 2, 1)).mean().loc[date]
    l_avg = to.rolling(long, min_periods=max(long // 2, 1)).mean().loc[date]
    ratio = s_avg / l_avg.replace(0, np.nan)
    return _cs_zscore(-(ratio - 1)).rename(f"turnover_rev_{short}d")

def _money_flow(prims: dict, date: str, window: int):
    """资金流 = money_flow_N.loc[date] (Chaikin CMF) → zscore"""
    from quant.factor.registry import _cs_zscore
    key = f"money_flow_{window}"
    s = prims[key].loc[date].dropna()
    return _cs_zscore(s).rename(f"money_flow_{window}d")

def _ma_alignment(prims: dict, date: str, window: int):
    """均线排列 = sum(MA_short/MA_long - 1) → zscore"""
    from quant.factor.registry import _cs_zscore
    import numpy as np
    ma5 = prims["ma_5"].loc[date]
    ma10 = prims["ma_10"].loc[date]
    ma20 = prims["ma_20"].loc[date]
    ma60 = prims["ma_60"].loc[date]
    with np.errstate(divide='ignore', invalid='ignore'):
        score = ((ma5 / ma10.replace(0, np.nan) - 1).fillna(0)
               + (ma10 / ma20.replace(0, np.nan) - 1).fillna(0)
               + (ma20 / ma60.replace(0, np.nan) - 1).fillna(0))
    return _cs_zscore(score).rename("ma_alignment")

def _volume_price_corr(prims: dict, date: str, window: int):
    """量价相关 = vol_price_corr_N.loc[date] (Pearson) → zscore"""
    from quant.factor.registry import _cs_zscore
    key = f"vol_price_corr_{window}"
    s = prims[key].loc[date].dropna()
    return _cs_zscore(s).rename(f"vol_price_corr_{window}d")


# ═══════════════════════════════════════════════════════════
# 映射表: 因子函数名 → 快捷计算函数
# 不在映射表中的因子走原始函数 (fallback)
# ═══════════════════════════════════════════════════════════

def _reversal(prims: dict, date: str, window: int):
    """短周期反转 = -pct_ret_ma_N.loc[date] → zscore.
    算法: 短期收益率均值的负值 (反转效应: 近期涨→未来跌).
    来源: Jegadeesh (1990) — 短期反转效应; Lehmann (1990)."""
    from quant.factor.registry import _cs_zscore
    key = f"mean_log_{window}"
    s = prims[key].loc[date].dropna()
    return _cs_zscore(-s).rename(f"reversal_{window}d")

def _residual_momentum(prims: dict, date: str, window: int):
    """残差动量 = 总收益 - 基准收益 (beta≈1近似) → cum → zscore.
    算法: cumsum(log_ret - benchmark_ret) 最后 window 日求和, 截面 zscore.
    来源: Blitz, Huij & Martens (2011) — 残差动量; AQR (2014) — 纯 Alpha 剥离."""
    from quant.factor.registry import _cs_zscore
    import numpy as np
    if "benchmark_ret" not in prims:
        return pd.Series(np.nan, index=prims["log_ret"].columns, name=f"residual_momentum_{window}d")
    residual_ret = prims["log_ret"].sub(prims["benchmark_ret"], axis=0)
    cum_resid = residual_ret.rolling(window, min_periods=max(window // 2, 1)).sum()
    s = cum_resid.loc[date].dropna()
    return _cs_zscore(s).rename(f"residual_momentum_{window}d")

def _idio_vol(prims: dict, date: str, window: int):
    """特质波动率 = std(log_ret - benchmark_ret) 滚动 window 日 → 取负 zscore.
    来源: Ang et al. (2006, JF) — 特质波动率异象: 高特质波动→低收益."""
    from quant.factor.registry import _cs_zscore
    import numpy as np
    if "benchmark_ret" not in prims:
        return pd.Series(np.nan, index=prims["log_ret"].columns, name=f"idio_vol_{window}d")
    residual_ret = prims["log_ret"].sub(prims["benchmark_ret"], axis=0)
    vol = residual_ret.rolling(window, min_periods=max(window // 2, 1)).std() * np.sqrt(252)
    s = vol.loc[date].dropna()
    return _cs_zscore(-s).rename(f"idio_vol_{window}d")

def _turnover_anomaly(prims: dict, date: str, short: int = 5, long: int = 60):
    """换手率异常 = turnover 短期均值 / 长期均值 - 1 → 取负 zscore.
    来源: Lee & Swaminathan (2000) — turnover anomaly; A股实证 IC≈0.03."""
    from quant.factor.registry import _cs_zscore
    import numpy as np
    s_key = f"turnover_ma_{short}"
    l_key = f"turnover_ma_{long}"
    if s_key not in prims or l_key not in prims:
        return pd.Series(np.nan, index=prims["log_ret"].columns, name=f"turnover_anomaly")
    s_avg = prims[s_key].loc[date]
    l_avg = prims[l_key].loc[date]
    ratio = s_avg / l_avg.replace(0, np.nan)
    return _cs_zscore(-(ratio - 1)).rename("turnover_anomaly")

def _trcf(prims: dict, date: str, window: int = 120):
    """TRCF 换手率收敛 = -log(1 + std(MA5/10/20/60/120 turnover)).
    来源: 数据源适配报告 — ICIR=4.19, turnover 类最强."""
    from quant.factor.registry import _cs_zscore
    import numpy as np
    to_keys = [f"turnover_ma_{w}" for w in [5, 10, 20, 60, 120]]
    if not all(k in prims for k in to_keys):
        return pd.Series(np.nan, index=prims["log_ret"].columns, name="trcf")
    mas = [prims[k].loc[date] for k in to_keys]
    std_ma = pd.Series(np.std(mas, axis=0), index=mas[0].index)
    result = -np.log(1 + std_ma)
    return _cs_zscore(result.fillna(0)).rename("trcf")

def _str(prims: dict, date: str, window: int = 20):
    """STR 量稳换手率 = -std(turnover, N日), 取负 zscore.
    来源: 东吴证券(2021) — 换手率波动小→未来收益高. IC=-7.9%, IR=2.96."""
    from quant.factor.registry import _cs_zscore
    key = f"turnover_std_{window}"
    if key not in prims:
        return pd.Series(np.nan, index=prims["log_ret"].columns, name="str")
    s = prims[key].loc[date].dropna()
    return _cs_zscore(-s).rename("str")

def _abn_turnover(prims: dict, date: str, window: int = 20):
    """ABN_TURN 异常换手率 = -|turnover / avg(turnover) - 1| → zscore.
    来源: 换手率偏离历史均值越大→投机信号越强→未来收益越低."""
    from quant.factor.registry import _cs_zscore
    to_key = "turnover"
    ma_key = f"turnover_ma_{window}"
    if to_key not in prims or ma_key not in prims:
        return pd.Series(np.nan, index=prims["log_ret"].columns, name="abn_turnover")
    current = prims[to_key].loc[date]
    avg = prims[ma_key].loc[date]
    dev = abs(current / avg.replace(0, np.nan) - 1).fillna(0)
    return _cs_zscore(-dev).rename("abn_turnover")

FACTOR_SHORTCUT = {
    # compute_momentum — 直接取 cum_log_N
    "compute_momentum":            _momentum,
    # compute_volatility — 直接取 vol_N
    "compute_volatility":           _volatility,
    # compute_max_return — 直接取 max_pct_N
    "compute_max_return":           _max_return,
    # compute_skewness — 从预计算 skew_N 取
    "compute_skewness":             _skewness,
    # compute_rsi_reversal — 从预计算 rsi_N 取
    "compute_rsi_reversal":         _rsi_reversal,
    # compute_volume_ratio — 从预计算取
    "compute_volume_ratio":         _volume_ratio,
    # compute_overnight_gap — 从预计算取
    "compute_overnight_gap":        _overnight_gap,
    # compute_intraday_range — 已移除: 需要 high/low 原始数据, 不在 primitives 中
    # 换手率 — 从预计算取
    "compute_turnover_reversal":    _turnover_reversal,
    # "compute_turnover_change": removed — mapped to wrong fn; correct impl TBD (2026-07-21 audit M1)
    # 资金流 — 从预计算 money_flow_N 取
    "compute_money_flow":           _money_flow,
    # 均线排列 — 从预计算 ma_N 取
    "compute_ma_alignment":         _ma_alignment,
    # 量价相关 — 从预计算 vol_price_corr_N 取
    "compute_volume_price_corr":    _volume_price_corr,
    # 新增 (test-v152): reversal/residual/idio/turnover/trcf/str/abn 走 primitives
    "compute_reversal":             _reversal,
    "compute_residual_momentum":    _residual_momentum,
    "compute_idiosyncratic_vol":    _idio_vol,
    "compute_turnover_anomaly":     _turnover_anomaly,
    "compute_trcf":                 _trcf,
    "compute_str":                  _str,
    # "compute_abn_turnover": removed — conflicts with full OLS in _alternative.py (2026-07-21 audit M2)
}

# ═══════════════════════════════════════════════════════════
# 幻方 Tier S 新因子 shortcut (2026-07-20)
# ═══════════════════════════════════════════════════════════

def _turnover_accel(prims: dict, date: str, short: int = 5, long: int = 10):
    """加速换手因子 shortcut: (turnover_t / turnover_{t-5} - 1) / (turnover_{t-5} / turnover_{t-10} - 1).
    来源: 华安证券金工 (2024), IC=-10.5%, IR=4.29.
          幻方方法论"加速/减速特征": 换手率二阶导数(变化速率).
    """
    from quant.factor.registry import _cs_zscore
    import numpy as np
    if "turnover" not in prims:
        return pd.Series(np.nan, index=prims["log_ret"].columns, name="turnover_accel")
    to = prims["turnover"]
    t = to.loc[date]
    t5 = to.shift(short).loc[date]
    t10 = to.shift(long).loc[date]
    max_ratio = 10.0  # 3σ 裁剪上限, 来源: 华安2024
    d5 = t / t5.replace(0, np.nan) - 1.0
    d10 = t5 / t10.replace(0, np.nan) - 1.0
    with np.errstate(divide='ignore', invalid='ignore'):
        accel = np.where(np.abs(d10.values) < 1e-8, np.sign(d5.values) * max_ratio,
                     np.clip(d5.values / d10.values, -max_ratio, max_ratio))
    result = pd.Series(accel, index=t.index).dropna()
    return _cs_zscore(-result).rename("turnover_accel")


def _uret(prims: dict, date: str, window: int = 20):
    """URet 信息分布不均 shortcut: -1 * vol_20 / |mean_log_20|.
    来源: 东吴证券金工 (2023), IC=-5.4%, IR=2.21.
          幻方"信息分布不均"方法论.
    """
    from quant.factor.registry import _cs_zscore
    import numpy as np
    vol_key = f"vol_{window}"
    mean_key = f"mean_log_{window}"
    if vol_key not in prims or mean_key not in prims:
        return pd.Series(np.nan, index=prims["log_ret"].columns, name="uret_20d")
    vol = prims[vol_key].loc[date]
    mean_ret = prims[mean_key].loc[date]
    denom = mean_ret.abs().replace(0, np.nan)
    uret = (vol / denom).replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(-uret).rename("uret_20d")

FACTOR_SHORTCUT["compute_turnover_accel"] = _turnover_accel
FACTOR_SHORTCUT["compute_uret"] = _uret
