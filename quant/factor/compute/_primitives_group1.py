from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg
"""Factor compute primitives group 1."""

def _prims_cache_dir(key: str) -> str:
    """返回 primitive 缓存目录路径（每个 key 一个目录，内含多个 parquet 文件）"""
    return os.path.join(_PRIMITIVE_CACHE_DIR, f"prims_{key}")

# 内存缓存 (LRU bounded — M1 8GB 硬约束)
# 来源: M1 Max 实测 — WAL checkpoint + Python GIL 下 4 线程最优, 缓存上限 8 条目
#       每条目 ~20-50MB (5000 symbols × 2000 days × 滚动统计量), 8 条目 ≈ 160-400MB 峰值
_PRIMITIVE_CACHE: dict[str, dict] = {}
_MAX_MEMORY_CACHE = _require_cfg("factor.compute.cache_max_memory_entries")
_MAX_DISK_CACHE = _require_cfg("factor.compute.cache_max_disk_entries")

_PRIM_CACHE_VERSION = "v2"  # v477: 窗口集/依存代码变化时递增 — 旧缓存缺 rsi_14 的教训



def _cache_key(data_hash: str, factor_names: list[str] | None) -> str:
    """生成缓存键 — v472: 仅数据哈希, 不含因子批次。

    v472 前 key 含 factor_names: 相同数据的两个批次 (如 ma_alignment_20d 与
    momentum_63d) 各自生成独立目录重复计算, 且窗口集随批次变化互相不可用。
    窗口集恒定后内容与批次无关, key 退化为 data_hash。
    旧格式目录 (prims_{hash}_{names}) 由磁盘 LRU 自然淘汰。

    v477: key 追加 _PRIM_CACHE_VERSION — 仅 data_hash 时, 修改窗口集/primary
    依赖代码后旧磁盘缓存依旧 HIT 缺新 prim (实证: 冒烟 31 日期全 FAIL,
    rsi_14 无 parquet 文件)。版本段使代码变更自动失效旧缓存重算 (prim
    一次全量 ~5s, 成本可忽略), 旧目录由磁盘 LRU 淘汰。
    """
    return f"{data_hash}.{_PRIM_CACHE_VERSION}"


def _data_hash(data: pd.DataFrame) -> str:
    """基于数据形状、索引范围、列生成哈希"""
    idx = data.index
    hash_str = f"{len(data)}_{idx[0] if len(idx) else 'none'}_{idx[-1] if len(idx) else 'none'}_{list(data.columns.levels[0]) if isinstance(data.columns, pd.MultiIndex) else list(data.columns)}"
    return hashlib.sha256(hash_str.encode()).hexdigest()[:16]


def _cache_path(key: str) -> str:
    return _prims_cache_dir(key)


def _evict_lru_if_needed() -> None:
    """LRU 淘汰: 超过 _MAX_MEMORY_CACHE 时弹出最旧的条目。

    来源: Python 3.7+ dict 插入有序, next(iter(d)) 返回最旧键.
    M1 8GB 硬约束 — 每条目 ~20-50MB, 上限 8 条目 = 160-400MB 峰值.
    """
    while len(_PRIMITIVE_CACHE) > _MAX_MEMORY_CACHE:
        oldest_key = next(iter(_PRIMITIVE_CACHE.keys()))
        del _PRIMITIVE_CACHE[oldest_key]
        _log.debug("  primitives cache EVICTED (LRU): %s", oldest_key)


def _evict_disk_lru_if_needed() -> None:
    """磁盘缓存 LRU 淘汰: 超过 _MAX_DISK_CACHE 目录数时删除 mtime 最旧的目录。

    来源: v470 实测 — 物化每 chunk 落盘 ~1GB prs_*.pkl, key 含数据哈希 (窗口随 chunk 滚动),
          跨 chunk 命中率趋近 0, 无上限曾膨胀至 10GB; 按 mtime 淘汰最旧.
    """
    if _MAX_DISK_CACHE <= 0:
        return
    import glob as _glob
    dirs = sorted(
        (os.path.getmtime(p), p) for p in _glob.glob(os.path.join(_PRIMITIVE_CACHE_DIR, "prims_*"))
        if os.path.isdir(p)
    )
    while len(dirs) > _MAX_DISK_CACHE:
        mtime, oldest = dirs.pop(0)
        try:
            import shutil
            shutil.rmtree(oldest)
            _log.info(f"  primitives cache EVICTED (disk LRU): {os.path.basename(oldest)}")
        except OSError as e:
            _log.warning(f"  cache evict failed: {oldest}: {e}")

_log = get_logger("factor.primitives")




def _required_windows(factor_names: list[str] | None) -> set[int]:
    """恒定标准窗口集 (v472) — 不再按因子列表推导。

    2026-08-12 事故 (batch2 23 因子 × 1846 日期全败, 67min/0 行):
    ma_alignment_20d 声明窗口仅 20, 但 shortcut 硬编码依赖 ma_5/ma_10/ma_60;
    按声明窗口推导出 {20} → prims 缺 ma_10 → KeyError → 整日判失败,
    checkpoint 永久重试。按因子列表手工维护 extra-windows 映射同样会漏
    (shortcut_extra_windows 曾只覆盖 5 个因子, 维护即失效)。

    修复: 窗口集与因子批次无关, 恒定全集 = 全部已注册窗口。
    代价: prims 计算量恒定 (一次全量), 换来自动覆盖所有硬编码依赖。

    factor_names 参数保留仅为向后兼容, 不再参与推导。

    v477 冒烟补漏: 94 因子全量物化暴露 rsi_rev_14d → rsi_14 缺失 (14 不在集合内),
    晚间链无此因子故未触发。14 为已注册窗口, 补入恒定集。
    """
    return {5, 10, 14, 20, 60, 63, 120, 126, 250, 252}



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
                          factor_names: list[str] | None = None,
                          save_disk_cache: bool = True) -> dict:
    """预计算所有价格因子共享的滚动统计量。

    Args:
        data: MultiIndex DataFrame (field, symbol), 含 close/open/high/low/volume/amount
        factor_names: 本次需要物化的因子名列表。None 时按原行为计算全部窗口。
        save_disk_cache: False 时跳过磁盘 parquet 保存 (v497: 物化每 chunk 的
            data hash 不同 → 落盘 ~0.7GB 但跨 chunk 命中率 ≈ 0, 纯耗时 60s+;
            物化场景传 False 省时, IC/单次评估保持默认 True)。

    Returns:
        {primitive_name: DataFrame(date × symbol)}
        键如: "log_ret", "cum_log_5", "vol_20", "roll_max_250", "turnover"
              以及预计算好的 shortcut zscore panel: "zscore:{factor_name}"
    """
    # ── 缓存检查 ──
    data_hash = _data_hash(data)
    cache_key = _cache_key(data_hash, factor_names)
    cache_dir = _cache_path(cache_key)
    
    # 内存缓存命中
    if cache_key in _PRIMITIVE_CACHE:
        _log.info(f"  primitives cache HIT (memory): {cache_key}")
        return _PRIMITIVE_CACHE[cache_key]
    
    # 磁盘缓存命中 (目录下读取所有 parquet 文件重建 dict)
    if os.path.isdir(cache_dir):
        try:
            import glob as _glob
            prims = {}
            for p in _glob.glob(os.path.join(cache_dir, "*.parquet")):
                name = os.path.basename(p).replace(".parquet", "")
                df = pd.read_parquet(p)
                prims[name] = df
            if prims:
                _PRIMITIVE_CACHE[cache_key] = prims
                _evict_lru_if_needed()
                try:
                    os.utime(cache_dir, None)
                except OSError:
                    pass
                _log.info(f"  primitives cache HIT (disk): {cache_key}")
                return prims
        except Exception as e:
            _log.warning(f"Cache load failed: {e}, recomputing...")
    
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

    if "volume" in data.columns.levels[0]:
        vol = data["volume"].astype(float)
        for w in sorted(all_windows):
            if w <= 1:
                continue
            prims[f"volume_ma_{w}"] = vol.rolling(w, min_periods=max(w // 2, 1)).mean()
        prims["raw_volume"] = vol
        _log.info("  primitives: volume_ma (multi-window)")

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
    
    # ── 保存到缓存 (float32 parquet 目录) ──
    if not save_disk_cache:
        _PRIMITIVE_CACHE[cache_key] = prims
        _evict_lru_if_needed()
        _log.info(f"  primitives cache SKIP disk save (save_disk_cache=False): {cache_key}")
        return prims
    try:
        os.makedirs(cache_dir, exist_ok=True)
        for name, df in prims.items():
            # 转 float32 节省空间
            if df.dtypes.apply(lambda x: x.kind == 'f').any():
                df = df.astype({c: 'float32' for c in df.columns if df[c].dtype.kind == 'f'})
            df.to_parquet(os.path.join(cache_dir, f"{name}.parquet"), 
                          compression='zstd', compression_level=3, index=True)
        _PRIMITIVE_CACHE[cache_key] = prims
        _evict_lru_if_needed()
        _evict_disk_lru_if_needed()
        _log.info(f"  primitives cache SAVED: {cache_key}")
    except Exception as e:
        _log.warning(f"Cache save failed: {e}")
    
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



