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
from quant.config.constants import _require_cfg

_log = get_logger("factor.primitives")



def _required_windows(factor_names: list[str] | None) -> set[int]:
    """根据因子名列表推导需要的滚动窗口。

    factor_names=None 时返回默认全集 (向后兼容)。
    对 turnover_anomaly 等双窗口因子, 额外提取函数签名中的 long 参数窗口。
    """
    if factor_names is None:
        return {5, 10, 20, 60, 63, 120, 126, 250, 252}
    import inspect as _ins
    from quant.factor.compute.price import _PRICE_FN_MAP
    from quant.factor.compute.fundamental import _FUNDAMENTAL_FN_MAP
    windows = set()
    for name in factor_names:
        entry = _PRICE_FN_MAP.get(name)
        if entry:
            fn, win = entry
            if isinstance(win, int) and win > 1:
                windows.add(win)
            # 双窗口因子: 提取函数签名中的额外窗口参数
            # turnover_anomaly: short=5 (from map), long=60 (from fn default)
            #   来源: Lee & Swaminathan (2000) — 60d 长期换手率基线
            if hasattr(fn, '__name__') and fn.__name__ == 'compute_turnover_anomaly':
                try:
                    long_win = _ins.signature(fn).parameters['long'].default
                    if isinstance(long_win, int) and long_win > 1:
                        windows.add(long_win)
                except Exception:
                    pass
        if name in _FUNDAMENTAL_FN_MAP:
            pass
    if not windows:
        windows = {5, 10, 20, 60, 63, 120, 126, 250, 252}
    return windows


def _ts_rank_vectorized(df: pd.DataFrame, window: int) -> pd.DataFrame:
    """滚动窗口内最后元素的百分位排名 (向量化实现)。

    替代 pandas rolling.apply(lambda...), 速度提升 50-100x。
    语义: out[t] = (窗口内 <= x[t] 的元素数) / window。
    与 ts_rank 标准定义等价 (允许 ties 用 ≤ 计数)。

    Args:
        df: 行=日期, 列=symbol 的 DataFrame
        window: 滚动窗口长度

    Returns:
        同形状 DataFrame, 每行是 x[t] 在 [t-window+1, t] 窗口内的排名分位
    """
    arr = df.apply(pd.to_numeric, errors='coerce').values.astype(float)
    T, N = arr.shape
    out = np.full_like(arr, np.nan)
    for t in range(window - 1, T):
        win = arr[t - window + 1:t + 1]  # (window, N)
        last = win[-1]
        out[t] = np.nansum(win <= last, axis=0) / window
    return pd.DataFrame(out, index=df.index, columns=df.columns)


def precompute_primitives(data: pd.DataFrame,
                          factor_names: list[str] | None = None) -> dict:
    """预计算所有价格因子共享的滚动统计量。

    Args:
        data: MultiIndex DataFrame (field, symbol), 含 close/open/high/low/volume/amount
        factor_names: 本次需要物化的因子名列表。None 时按原行为计算全部窗口。

    Returns:
        {primitive_name: DataFrame(date × symbol)}
        键如: "log_ret", "cum_log_5", "vol_20", "roll_max_250", "turnover"
              以及预计算好的 shortcut zscore panel: "zscore:{factor_name}"
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
    prims["log_ret"] = np.log(close.astype(float)).diff()

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
    if high is not None:
        prims["high"] = high
    if low is not None:
        prims["low"] = low

    # ── 滚动统计量 (基于 log_ret) ──
    log_ret = prims["log_ret"]
    # 从 factor_names 推导所需窗口 (A4: 按需原语)
    all_windows = _required_windows(factor_names)
    _log.info("  primitives: required windows %s (from %d factors)",
              sorted(all_windows), len(factor_names) if factor_names else 0)

    for w in sorted(all_windows):
        if w <= 1:
            continue
        # 滚动累积收益 (动量用)
        _log.info(f"  primitives: cum_log_{w} (window={w})")
        prims[f"cum_log_{w}"] = log_ret.rolling(w, min_periods=max(w//2, 1)).sum()
        # 滚动波动率
        prims[f"vol_{w}"] = log_ret.rolling(w, min_periods=max(w//2, 1)).std() * np.sqrt(_require_cfg("market.annual_trading_days"))
        # 滚动均值收益 — 仅 uret_20d 用 w=20 (v366: cum_log 已覆盖动量/反转, mean_log 线性相关)
        if w == 20:
            prims[f"mean_log_{w}"] = log_ret.rolling(w, min_periods=max(w//2, 1)).mean()

    # ── 滚动统计量 (基于 pct_ret) — 仅 max_pct 被 _max_return shortcut 消费 ──
    pct_ret = prims["pct_ret"]
    for w in sorted(all_windows):
        if w <= 1:
            continue
        prims[f"max_pct_{w}"] = pct_ret.rolling(w, min_periods=max(w//2, 1)).max()
    # roll_high/roll_low/min_pct/vol_ma/amt_ma — v366 killed: 5 统计族所有窗口无消费者验证



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
    # v366: 复用 pct_ret 避免 close.pct_change() 重复计算
    if volume is not None:
        close_ret = prims["pct_ret"]
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

    # ── ADR-043 layer2: Amihud 非流动性原始值 (amihud/amihud_20d/turnover_adj_amihud 共用) ──
    if amount is not None and "pct_ret" in prims:
        _log.info("  primitives: amihud_raw + rolling means")
        dollar_vol = amount * 1000
        prims["amihud_raw"] = prims["pct_ret"].abs() / dollar_vol.replace(0, np.nan)
        for w in sorted(all_windows | {20, 250}):
            if w <= 1:
                continue
            prims[f"amihud_ma_{w}"] = (prims["amihud_raw"]
                .rolling(w, min_periods=max(w // 2, 1)).mean() * 1e6)

    # ── ADR-043 layer2: 日内/隔夜收益 (day_night 共用) ──
    if opn is not None:
        _log.info("  primitives: intra_ret + night_jump")
        prims["intra_ret"] = np.log(close / opn)
        prims["night_jump_raw"] = np.log(opn / close.shift(1)).abs()
        # 预计算滚动和 (day_night 硬编码窗口 10/20)
        prims["intra_rev_20"] = prims["intra_ret"].rolling(20, min_periods=10).sum()
        prims["night_jump_10"] = prims["night_jump_raw"].rolling(10, min_periods=5).sum()

    # ── ADR-043 layer2: 理想振幅原始值 (ideal_amplitude 共用) ──
    if high is not None and low is not None:
        prims["ideal_amp_raw"] = (high - low) / low.replace(0, np.nan)

    # ── ADR-043 layer2: 隔夜缺口 5d 均值 (overnight_gap_5d 共用) ──
    if "overnight_gap" in prims:
        prims["overnight_gap_ma_5"] = prims["overnight_gap"].rolling(
            5, min_periods=3).mean()

    # ── ADR-043 layer2: 量价同步原始值 (vol_price_sync_20d 共用) ──
    # v366: 复用 pct_ret 避免 close.pct_change() 重复计算
    if volume is not None:
        close_ret = prims["pct_ret"]
        up_mask = close_ret > 0
        down_mask = close_ret < 0
        up_sync = (close_ret * volume).where(up_mask, 0)
        down_sync = (close_ret.abs() * volume).where(down_mask, 0)
        prims["vol_price_sync_raw"] = (up_sync.rolling(20, min_periods=10).mean()
            / down_sync.rolling(20, min_periods=10).mean().replace(0, np.nan) - 1)

    # ── RSI ──
    pct = prims["pct_ret"]
    for w in sorted(all_windows):
        if w <= 1:
            continue
        gain = pct.where(pct > 0, 0).rolling(w, min_periods=max(w // 2, 1)).mean()
        loss = (-pct.where(pct < 0, 0)).rolling(w, min_periods=max(w // 2, 1)).mean()
        rs = gain / loss.replace(0, np.nan)
        prims[f"rsi_{w}"] = 100 - (100 / (1 + rs))

    # ── v374: 4 新因子原始面板 (shortcut-ize range/seasonality/tail_risk/market_beta) ──
    # range_20d: -(high-low)/close 的 20d 均值
    if high is not None and low is not None:
        prims["range_raw"] = (high - low) / close
        prims["range_ma_20"] = prims["range_raw"].rolling(20, min_periods=10).mean()

    # seasonality_12m_1m: 12月前同月收益 (跳过最近1月)
    # close[t-21] / close[t-252] - 1, 来源: Heston & Sadka (2008)
    prims["seasonality_raw"] = close.shift(21) / close.shift(252) - 1

    # tail_risk: 0.6×(-skew_252) + 0.4×(ret<5%分位比率)
    # skew 已在上方 for 循环中计算, 直接引用 skew_252
    if "skew_252" in prims:
        pct_ret_252 = prims["pct_ret"].rolling(252, min_periods=60)
        var_5pct = pct_ret_252.quantile(0.05)
        tail_hits = (prims["pct_ret"] < var_5pct).rolling(252, min_periods=60).sum() / 252
        prims["tail_risk_raw"] = 0.6 * (-prims["skew_252"]) + 0.4 * tail_hits

    # ── shortcut 因子整块 zscore panel 预计算 (A2) ──
    # 物化场景下逐日调 _cs_zscore 开销大; 这里一次算完整块, shortcut 直接取行.
    _precompute_shortcut_zscore_panels(prims, factor_names)

    elapsed = (pd.Timestamp.now() - t0).total_seconds()
    _log.info(f"  primitives done: {len(prims)} tables in {elapsed:.1f}s")
    return prims


def _precompute_shortcut_zscore_panels(prims: dict,
                                       factor_names: list[str] | None) -> None:
    """对常见 shortcut 因子预计算整块 zscore panel。

    结果存入 prims[f"zscore:{factor_name}"], shortcut 函数优先命中。
    仅覆盖一元变换类因子; 复杂因子保持原 per-date 路径。
    """
    from quant.factor.registry import _cs_zscore_frame
    from quant.factor.compute.price import _PRICE_FN_MAP
    from quant.config.constants import _VOL_RATIO_LONG

    if factor_names is None:
        factor_names = list(_PRICE_FN_MAP.keys())

    for name in factor_names:
        entry = _PRICE_FN_MAP.get(name)
        if not entry:
            continue
        fn, win = entry
        fn_name = fn.__name__
        zkey = f"zscore:{name}"
        if zkey in prims:
            continue

        raw = None
        try:
            if fn_name == "compute_momentum" and f"cum_log_{win}" in prims:
                raw = prims[f"cum_log_{win}"]
            elif fn_name == "compute_volatility" and f"vol_{win}" in prims:
                raw = -prims[f"vol_{win}"]
            elif fn_name == "compute_max_return" and f"max_pct_{win}" in prims:
                raw = -prims[f"max_pct_{win}"]
            elif fn_name == "compute_skewness" and f"skew_{win}" in prims:
                raw = -prims[f"skew_{win}"]
            elif fn_name == "compute_rsi_reversal" and f"rsi_{win}" in prims:
                raw = -prims[f"rsi_{win}"]
            elif fn_name == "compute_reversal" and f"cum_log_{win}" in prims:
                # cum_log = 窗口内对数收益之和, 与原始 compute_reversal 的 -sum(log_ret) 一致
                raw = -prims[f"cum_log_{win}"]
            elif fn_name == "compute_residual_momentum" and "benchmark_ret" in prims:
                resid = prims["log_ret"].sub(prims["benchmark_ret"], axis=0)
                raw = resid.rolling(win, min_periods=max(win // 2, 1)).sum()
            elif fn_name == "compute_idiosyncratic_vol" and "benchmark_ret" in prims:
                # 向量化 OLS β 回归: β_i = Cov(r_i, r_bm) / Var(r_bm)
                # = ρ(r_i, r_bm) × σ(r_i) / σ(r_bm)
                # 来源: Ang et al. (2006, JF) — 特质波动率异象, 需对基准做 β 回归取残差
                log_ret = prims["log_ret"]
                bm_ret = prims["benchmark_ret"]
                half = max(win // 2, 1)
                rho = log_ret.rolling(win, min_periods=half).corr(bm_ret)
                sig_i = log_ret.rolling(win, min_periods=half).std()
                sig_bm = bm_ret.rolling(win, min_periods=half).std()
                with np.errstate(divide='ignore', invalid='ignore'):
                    beta = rho.multiply(sig_i).div(sig_bm, axis=0)
                resid = log_ret - beta.mul(bm_ret, axis=0)
                raw = -resid.rolling(win, min_periods=half).std() * np.sqrt(
                    _require_cfg("market.annual_trading_days"))
            elif fn_name == "compute_overnight_gap" and "overnight_gap" in prims:
                raw = prims["overnight_gap"].rolling(win, min_periods=max(win // 2, 1)).mean()
            elif fn_name == "compute_money_flow" and f"money_flow_{win}" in prims:
                raw = prims[f"money_flow_{win}"]
            elif fn_name == "compute_volume_price_corr" and f"vol_price_corr_{win}" in prims:
                raw = prims[f"vol_price_corr_{win}"]
            elif fn_name == "compute_alpha035" and win is None:
                raw = _alpha035_raw_panel(prims)
            elif fn_name == "compute_turnover_anomaly":
                # turnover_anomaly: (MA_5 - MA_60) / std_60
                # short=5 from _PRICE_FN_MAP, long=60 from fn signature default
                import inspect as _insp
                try:
                    long_w = _insp.signature(fn).parameters['long'].default
                except Exception:
                    long_w = 60
                s_key, l_key, std_key = f"turnover_ma_{win}", f"turnover_ma_{long_w}", f"turnover_std_{long_w}"
                if s_key in prims and l_key in prims and std_key in prims:
                    raw = (prims[s_key] - prims[l_key]) / prims[std_key].replace(0, np.nan)
            # ── v374: 4 新 shortcut 面板 ──
            elif fn_name == "compute_intraday_range" and "range_ma_20" in prims:
                raw = -prims["range_ma_20"]
            elif fn_name == "compute_seasonality_12m_1m" and "seasonality_raw" in prims:
                raw = prims["seasonality_raw"]
            elif fn_name == "compute_tail_risk" and "tail_risk_raw" in prims:
                raw = prims["tail_risk_raw"]
            elif fn_name == "compute_market_beta_60d" and "benchmark_ret" in prims:
                # 向量化 beta: cov(r_i, r_bm) / var(r_bm) rolling 60d, 取负(低beta溢价)
                log_ret = prims["log_ret"]
                bm_ret = prims["benchmark_ret"]
                win = 60
                half = 30
                rho = log_ret.rolling(win, min_periods=half).corr(bm_ret)
                sig_i = log_ret.rolling(win, min_periods=half).std()
                sig_bm = bm_ret.rolling(win, min_periods=half).std()
                with np.errstate(divide='ignore', invalid='ignore'):
                    beta = rho.multiply(sig_i).div(sig_bm, axis=0)
                raw = -beta  # 低beta溢价: 低beta→高分

            if raw is not None:
                prims[zkey] = _cs_zscore_frame(raw)
        except Exception as e:
            _log.warning("  shortcut panel %s failed: %s", name, e)


def _alpha035_raw_panel(prims: dict) -> pd.DataFrame | None:
    """alpha035_range_mom 的原始 composite panel (未 zscore)。"""
    if "raw_volume" not in prims or "log_ret" not in prims:
        return None
    volume = prims["raw_volume"]
    if "high" not in prims or "low" not in prims:
        return None
    rng = prims["high"] - prims["low"]
    ret = prims["pct_ret"]
    vr = _ts_rank_vectorized(volume, 32)
    rr = _ts_rank_vectorized(rng, 16)
    mr = _ts_rank_vectorized(ret, 32)
    return vr * (1 - rr) * (1 - mr)


# ═══════════════════════════════════════════════════════════
# 因子快捷计算映射 — 用预计算算子直接推导因子值
# ═══════════════════════════════════════════════════════════

def _momentum(prims: dict, date: str, window: int):
    """动量 = cum_log_N.loc[date] → zscore"""
    name = f"momentum_{window}d"
    zkey = f"zscore:{name}"
    if zkey in prims:
        return prims[zkey].loc[date].rename(name)
    from quant.factor.registry import _cs_zscore
    key = f"cum_log_{window}"
    s = prims[key].loc[date].dropna()
    return _cs_zscore(s).rename(name)

def _volatility(prims: dict, date: str, window: int):
    """波动率 = -vol_N.loc[date] (低波异象) → zscore"""
    name = f"volatility_{window}d"
    zkey = f"zscore:{name}"
    if zkey in prims:
        return prims[zkey].loc[date].rename(name)
    from quant.factor.registry import _cs_zscore
    key = f"vol_{window}"
    s = prims[key].loc[date].dropna()
    return _cs_zscore(-s).rename(name)

def _max_return(prims: dict, date: str, window: int):
    """最大收益 = -max_pct_N.loc[date] → zscore"""
    name = f"max_ret_{window}d"
    zkey = f"zscore:{name}"
    if zkey in prims:
        return prims[zkey].loc[date].rename(name)
    from quant.factor.registry import _cs_zscore
    key = f"max_pct_{window}"
    s = prims[key].loc[date].dropna()
    return _cs_zscore(-s).rename(name)

def _skewness(prims: dict, date: str, window: int):
    """偏度 = -skew_N.loc[date] (负偏度异象) → zscore"""
    name = f"skewness_{window}d"
    zkey = f"zscore:{name}"
    if zkey in prims:
        return prims[zkey].loc[date].rename(name)
    from quant.factor.registry import _cs_zscore
    key = f"skew_{window}"
    s = prims[key].loc[date].dropna()
    return _cs_zscore(-s).rename(name)

def _rsi_reversal(prims: dict, date: str, window: int):
    """RSI 反转 = -rsi_N.loc[date] → zscore"""
    name = f"rsi_rev_{window}d"
    zkey = f"zscore:{name}"
    if zkey in prims:
        return prims[zkey].loc[date].rename(name)
    from quant.factor.registry import _cs_zscore
    key = f"rsi_{window}"
    s = prims[key].loc[date].dropna()
    return _cs_zscore(-s).rename(name)

def _volume_ratio(prims: dict, date: str, window: int):
    """量比 = vol_ma_N / vol_ma_L"""
    name = f"vol_ratio_{window}d"
    zkey = f"zscore:{name}"
    if zkey in prims:
        return prims[zkey].loc[date].rename(name)
    from quant.factor.registry import _cs_zscore
    from quant.config.constants import _VOL_RATIO_LONG
    s_key = f"vol_ma_{window}"
    l_key = f"vol_ma_{_VOL_RATIO_LONG}"
    short_avg = prims[s_key].loc[date]
    long_avg = prims[l_key].loc[date]
    ratio = short_avg / long_avg.replace(0, np.nan)
    return _cs_zscore(ratio).rename(name)

def _overnight_gap(prims: dict, date: str, window: int):
    """隔夜缺口: 从预计算 gap 取 rolling mean"""
    name = f"gap_{window}d"
    zkey = f"zscore:{name}"
    if zkey in prims:
        return prims[zkey].loc[date].rename(name)
    from quant.factor.registry import _cs_zscore
    gap_ma = prims["overnight_gap"].rolling(window, min_periods=max(window // 2, 1)).mean()
    s = gap_ma.loc[date].dropna()
    return _cs_zscore(s).rename(name)

# _intraday_range removed from FACTOR_SHORTCUT — 走 fn(data) 路径

def _turnover_reversal(prims: dict, date: str, short: int, long: int = 20):
    """换手率反转: 用预计算 turnover_ma 替代每日期 to.rolling(mean)。
    v366: 消除 O(T×N) 每日期滚动, 直接用 prims 中的 turnover_ma_5/20."""
    from quant.factor.registry import _cs_zscore
    s_key = f"turnover_ma_{short}"
    l_key = f"turnover_ma_{long}"
    if s_key not in prims or l_key not in prims:
        to = prims.get("turnover")
        if to is None:
            return None
        s_avg = to.rolling(short, min_periods=max(short // 2, 1)).mean().loc[date]
        l_avg = to.rolling(long, min_periods=max(long // 2, 1)).mean().loc[date]
    else:
        s_avg = prims[s_key].loc[date]
        l_avg = prims[l_key].loc[date]
    ratio = s_avg / l_avg.replace(0, np.nan)
    return _cs_zscore(-(ratio - 1)).rename(f"turnover_rev_{short}d")

def _money_flow(prims: dict, date: str, window: int):
    """资金流 = money_flow_N.loc[date] (Chaikin CMF) → zscore"""
    name = f"money_flow_{window}d"
    zkey = f"zscore:{name}"
    if zkey in prims:
        return prims[zkey].loc[date].rename(name)
    from quant.factor.registry import _cs_zscore
    key = f"money_flow_{window}"
    s = prims[key].loc[date].dropna()
    return _cs_zscore(s).rename(name)

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
    name = f"vol_price_corr_{window}d"
    zkey = f"zscore:{name}"
    if zkey in prims:
        return prims[zkey].loc[date].rename(name)
    from quant.factor.registry import _cs_zscore
    key = f"vol_price_corr_{window}"
    s = prims[key].loc[date].dropna()
    return _cs_zscore(s).rename(name)


# ═══════════════════════════════════════════════════════════
# 映射表: 因子函数名 → 快捷计算函数
# 不在映射表中的因子走原始函数 (fallback)
# ═══════════════════════════════════════════════════════════

def _reversal(prims: dict, date: str, window: int):
    """短周期反转 = -cum_log_{window}.loc[date] → zscore.
    算法: 窗口内对数收益之和取负 (test-v337 真反转).
    与原始 compute_reversal (sum(log_ret)) 等价, 经 _cs_zscore 后归一化.
    来源: Jegadeesh (1990) — 短期反转效应; Lehmann (1990)."""
    name = f"reversal_{window}d"
    zkey = f"zscore:{name}"
    if zkey in prims:
        return prims[zkey].loc[date].rename(name)
    from quant.factor.registry import _cs_zscore
    key = f"cum_log_{window}"
    s = prims[key].loc[date].dropna()
    return _cs_zscore(-s).rename(name)

def _residual_momentum(prims: dict, date: str, window: int):
    """残差动量 = 总收益 - 基准收益 (beta≈1近似) → cum → zscore.
    算法: cumsum(log_ret - benchmark_ret) 最后 window 日求和, 截面 zscore.
    来源: Blitz, Huij & Martens (2011) — 残差动量; AQR (2014) — 纯 Alpha 剥离."""
    name = f"residual_momentum_{window}d"
    zkey = f"zscore:{name}"
    if zkey in prims:
        return prims[zkey].loc[date].rename(name)
    from quant.factor.registry import _cs_zscore
    import numpy as np
    if "benchmark_ret" not in prims:
        return pd.Series(np.nan, index=prims["log_ret"].columns, name=name)
    residual_ret = prims["log_ret"].sub(prims["benchmark_ret"], axis=0)
    cum_resid = residual_ret.rolling(window, min_periods=max(window // 2, 1)).sum()
    s = cum_resid.loc[date].dropna()
    return _cs_zscore(s).rename(name)

def _idio_vol(prims: dict, date: str, window: int):
    """特质波动率 = std(resid) 对沪深300做向量化OLS β回归取残差, 取负 zscore.
    来源: Ang et al. (2006, JF) — 特质波动率异象: 高特质波动→低收益."""
    name = f"idio_vol_{window}d"
    zkey = f"zscore:{name}"
    if zkey in prims:
        return prims[zkey].loc[date].rename(name)
    from quant.factor.registry import _cs_zscore
    import numpy as np
    if "benchmark_ret" not in prims:
        return pd.Series(np.nan, index=prims["log_ret"].columns, name=name)
    log_ret = prims["log_ret"]
    bm_ret = prims["benchmark_ret"]
    half = max(window // 2, 1)
    # β_i = Cov(r_i, r_bm) / Var(r_bm) = ρ × σ_i / σ_bm
    rho = log_ret.rolling(window, min_periods=half).corr(bm_ret)
    sig_i = log_ret.rolling(window, min_periods=half).std()
    sig_bm = bm_ret.rolling(window, min_periods=half).std()
    with np.errstate(divide='ignore', invalid='ignore'):
        beta = rho.multiply(sig_i).div(sig_bm, axis=0)
    resid = log_ret - beta.mul(bm_ret, axis=0)
    vol = resid.rolling(window, min_periods=half).std() * np.sqrt(_require_cfg("market.annual_trading_days"))
    s = vol.loc[date].dropna()
    return _cs_zscore(-s).rename(name)

def _turnover_anomaly(prims: dict, date: str, short: int = 5, long: int = 60):
    """换手率异常 = (短期均值 - 长期均值) / 长期标准差 → zscore.
    算法与原始 compute_turnover_anomaly 完全一致:
      (MA_short - MA_long) / std_long → 截面 zscore
    来源: Lee & Swaminathan (2000) — turnover anomaly; A股实证 IC≈0.03.
    short=5 来自 _PRICE_FN_MAP window 参数, long=60 来自原始函数默认参数."""
    from quant.factor.registry import _cs_zscore
    import numpy as np
    s_key = f"turnover_ma_{short}"
    l_key = f"turnover_ma_{long}"
    std_key = f"turnover_std_{long}"
    if s_key not in prims or l_key not in prims or std_key not in prims:
        return pd.Series(np.nan, index=prims["log_ret"].columns, name="turnover_anomaly")
    s_avg = prims[s_key].loc[date]
    l_avg = prims[l_key].loc[date]
    l_std = prims[std_key].loc[date]
    anomaly = (s_avg - l_avg) / l_std.replace(0, np.nan)
    anomaly = anomaly.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(anomaly).rename("turnover_anomaly")

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
    """STR 量稳换手率 shortcut = -std(turnover, N日), 取负 zscore.
    注: 省略了原始 compute_str 中的市值中性化 (sklearn OLS),
    对 zscore 排名影响 <2% rank shift. 如需完整中性化, 走原始函数.
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


def _alpha035(prims: dict, date: str, window: int = None):
    """Alpha#35 向量化 shortcut: 直接取预计算 zscore panel。"""
    zkey = "zscore:alpha035_range_mom"
    if zkey in prims:
        return prims[zkey].loc[date].rename("alpha035_range_mom")
    # 未命中预计算面板时返回 NaN (正常不应发生)
    return pd.Series(np.nan, index=prims.get("log_ret", pd.DataFrame()).columns,
                     name="alpha035_range_mom")


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
    # compute_volume_ratio — removed (v367: vol_ma dead, no _PRICE_FN_MAP entry)
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
    # "compute_str": removed — shortcut 省略市值中性化 OLS, 与原始 compute_str 不等价 (test-v365)
    # "compute_abn_turnover": removed — conflicts with full OLS in _alternative.py (2026-07-21 audit M2)
    # Alpha101 #35 向量化 shortcut (ADR-??? 待编号)
    "compute_alpha035": _alpha035,
}


# ═══════════════════════════════════════════════════════════
# ADR-043 layer2: 8 新 shortcut (amihud/day_night/ideal_amp/gap_5d/vol_price_sync/hl_volume)
# ═══════════════════════════════════════════════════════════

def _amihud(prims: dict, date: str, window: int):
    """Amihud 非流动性 = amihud_ma_{w}.loc[date] → zscore."""
    from quant.factor.registry import _cs_zscore
    key = f"amihud_ma_{window}"
    if key not in prims:
        return pd.Series(np.nan, index=prims["log_ret"].columns, name=f"amihud_{window}d")
    s = prims[key].loc[date].dropna()
    return _cs_zscore(s).rename(f"amihud_{window}d")

def _amihud_20d(prims: dict, date: str, window: int = 20):
    """短期 Amihud = amihud_ma_20.loc[date] → zscore."""
    from quant.factor.registry import _cs_zscore
    key = "amihud_ma_20"
    if key not in prims:
        return pd.Series(np.nan, index=prims["log_ret"].columns, name="amihud_20d")
    s = prims[key].loc[date].dropna()
    return _cs_zscore(s).rename("amihud_20d")

def _turnover_adj_amihud(prims: dict, date: str, window: int = 20):
    """换手率调整 Amihud = amihud_ma_20 / sqrt(turnover_ma_20) → zscore."""
    from quant.factor.registry import _cs_zscore
    import numpy as np
    am_key = "amihud_ma_20"
    to_key = f"turnover_ma_{window}"
    if am_key not in prims or to_key not in prims:
        return pd.Series(np.nan, index=prims["log_ret"].columns, name="turnover_adj_amihud_20d")
    am = prims[am_key].loc[date]
    avg_to = prims[to_key].loc[date]
    adj = am / np.sqrt(avg_to.replace(0, np.nan).astype(float))
    return _cs_zscore(adj).rename("turnover_adj_amihud_20d")

def _day_night(prims: dict, date: str, window: int = None):
    """昼夜合成 = 0.6×intra_rev_20 + 0.4×night_jump_10 → 取负 zscore."""
    from quant.factor.registry import _cs_zscore
    if "intra_rev_20" not in prims or "night_jump_10" not in prims:
        return pd.Series(np.nan, index=prims["log_ret"].columns, name="day_night")
    intra = prims["intra_rev_20"].loc[date]
    night = prims["night_jump_10"].loc[date]
    raw = 0.6 * intra + 0.4 * night
    return _cs_zscore(-raw).rename("day_night")

def _ideal_amplitude(prims: dict, date: str, window: int = 20):
    """理想振幅 = -(top 25% amp - bottom 25% amp), numpy 向量化."""
    from quant.factor.registry import _cs_zscore
    import numpy as np
    if "ideal_amp_raw" not in prims:
        return pd.Series(np.nan, index=prims["log_ret"].columns, name="ideal_amplitude")
    raw = prims["ideal_amp_raw"]
    recent = raw.loc[:date].tail(window)
    if recent.shape[0] < 3:
        return pd.Series(np.nan, index=prims["log_ret"].columns, name="ideal_amplitude")
    arr = recent.to_numpy(dtype=float)
    k = max(int(window * 0.25), 1)
    if k > arr.shape[0]:
        k = arr.shape[0] - 1
    valid = ~np.isnan(arr)
    cnt = valid.sum(axis=0)
    a = np.where(valid, arr, -np.inf)
    top = np.partition(a, arr.shape[0] - k, axis=0)[-k:]
    top_mean = np.where(top > -np.inf, top, 0).sum(axis=0) / np.maximum(
        (top > -np.inf).sum(axis=0), 1)
    b = np.where(valid, arr, np.inf)
    bot = np.partition(b, k - 1, axis=0)[:k]
    bot_mean = np.where(bot < np.inf, bot, 0).sum(axis=0) / np.maximum(
        (bot < np.inf).sum(axis=0), 1)
    vals = -(top_mean - bot_mean)
    ok = (cnt >= window) & np.isfinite(vals)
    result = pd.Series(np.nan, index=prims["log_ret"].columns)
    result.iloc[ok] = vals[ok]
    return _cs_zscore(result).rename("ideal_amplitude")

def _overnight_gap_5d(prims: dict, date: str, window: int = None):
    """隔夜动量 5d = overnight_gap_ma_5.loc[date] → zscore."""
    from quant.factor.registry import _cs_zscore
    if "overnight_gap_ma_5" not in prims:
        return pd.Series(np.nan, index=prims["log_ret"].columns, name="overnight_gap_5d")
    s = prims["overnight_gap_ma_5"].loc[date].dropna()
    return _cs_zscore(s).rename("overnight_gap_5d")

def _vol_price_sync_20d(prims: dict, date: str, window: int = None):
    """量价同步 20d = vol_price_sync_raw.loc[date] → 取负 zscore."""
    from quant.factor.registry import _cs_zscore
    if "vol_price_sync_raw" not in prims:
        return pd.Series(np.nan, index=prims["log_ret"].columns, name="vol_price_sync_20d")
    s = prims["vol_price_sync_raw"].loc[date].dropna()
    return _cs_zscore(-s).rename("vol_price_sync_20d")

def _hl_volume(prims: dict, date: str, window: int = 20):
    """高低位放量 = (P80 - P20) / mean(turnover), numpy 向量化取负 zscore."""
    from quant.factor.registry import _cs_zscore
    import numpy as np
    if "turnover" not in prims:
        return pd.Series(np.nan, index=prims["log_ret"].columns, name="hl_volume_20d")
    to = prims["turnover"]
    recent = to.loc[:date].tail(window)
    if recent.shape[0] < 10:
        return pd.Series(np.nan, index=prims["log_ret"].columns, name="hl_volume_20d")
    arr = recent.to_numpy(dtype=float)
    p80 = np.nanpercentile(arr, 80, axis=0)
    p20 = np.nanpercentile(arr, 20, axis=0)
    mean_to = np.nanmean(arr, axis=0)
    with np.errstate(divide='ignore', invalid='ignore'):
        vals = np.where(mean_to > 0, (p80 - p20) / mean_to, np.nan)
    result = pd.Series(vals, index=prims["log_ret"].columns)
    return _cs_zscore(-result).rename("hl_volume_20d")


FACTOR_SHORTCUT["compute_amihud"] = _amihud
FACTOR_SHORTCUT["compute_amihud_20d"] = _amihud_20d
FACTOR_SHORTCUT["compute_turnover_adj_amihud"] = _turnover_adj_amihud
FACTOR_SHORTCUT["compute_day_night"] = _day_night
FACTOR_SHORTCUT["compute_ideal_amplitude"] = _ideal_amplitude
FACTOR_SHORTCUT["compute_overnight_gap_5d"] = _overnight_gap_5d
FACTOR_SHORTCUT["compute_vol_price_sync_20d"] = _vol_price_sync_20d
FACTOR_SHORTCUT["compute_hl_volume"] = _hl_volume


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

# ── v374: 4 新 shortcut (range_20d / seasonality_12m_1m / tail_risk / market_beta_60d) ──

def _intraday_range(prims: dict, date: str, window: int):
    """日内振幅 = -range_ma_20.loc[date] → zscore."""
    zkey = "zscore:range_20d"
    if zkey in prims:
        return prims[zkey].loc[date].rename("range_20d")
    from quant.factor.registry import _cs_zscore
    s = prims["range_ma_20"].loc[date].dropna()
    return _cs_zscore(-s).rename("range_20d")

def _seasonality(prims: dict, date: str, window: int = None):
    """季节效应 = seasonality_raw.loc[date] → zscore. 来源: Heston & Sadka (2008)."""
    zkey = "zscore:seasonality_12m_1m"
    if zkey in prims:
        return prims[zkey].loc[date].rename("seasonality_12m_1m")
    from quant.factor.registry import _cs_zscore
    s = prims["seasonality_raw"].loc[date].dropna()
    return _cs_zscore(s).rename("seasonality_12m_1m")

def _tail_risk_shortcut(prims: dict, date: str, window: int = None):
    """尾部风险 = tail_risk_raw.loc[date] → zscore. 来源: Kelly & Jiang (2014)."""
    zkey = "zscore:tail_risk"
    if zkey in prims:
        return prims[zkey].loc[date].rename("tail_risk")
    from quant.factor.registry import _cs_zscore
    s = prims["tail_risk_raw"].loc[date].dropna()
    return _cs_zscore(s).rename("tail_risk")

def _market_beta(prims: dict, date: str, window: int = None):
    """市场Beta = -beta_60d.loc[date] → zscore. 低beta溢价, 来源: Frazzini & Pedersen (2014)."""
    zkey = "zscore:market_beta_60d"
    if zkey in prims:
        return prims[zkey].loc[date].rename("market_beta_60d")
    return pd.Series(np.nan, index=prims["log_ret"].columns, name="market_beta_60d")

FACTOR_SHORTCUT["compute_intraday_range"] = _intraday_range
FACTOR_SHORTCUT["compute_seasonality_12m_1m"] = _seasonality
FACTOR_SHORTCUT["compute_tail_risk"] = _tail_risk_shortcut
FACTOR_SHORTCUT["compute_market_beta_60d"] = _market_beta
