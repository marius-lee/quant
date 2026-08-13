"""G1: 在线 Walk-Forward OOS 验证 — 对标 Alphalens / DolphinDB 三模块分离架构.

模块:
  compute_ic_series() — 纯数学: 逐日 Spearman IC, 对标 Alphalens performance.factor_information_coefficient()
  analyze_is_oos()    — 纯统计: IS/OOS 拆分 → IR → 衰减检测, 对标 DolphinDB singleFactorAnalysis()
  run_oos_check()     — 编排层: 加载数据 → 计算 → 分析 → 返回, 对标 Alphalens tears.create_full_tear_sheet()

调用方:
  attribution._run()       — status_filter="using", n_symbols=attribution_n_symbols
  compute_backtest_ic()    — status_filter="backtesting", n_symbols=backtest_n_symbols

设计约束:
  - 纯计算函数不创建 DataStore / FactorStore, 不读 config, 不 import quant.* 模块
  - 编排函数 run_oos_check() 只创建连接、组装数据、串联调用, 不嵌入算法逻辑
  - 算法常量从 config.yaml 模块级读取一次, 不在函数体内读
"""
import numpy as np
import pandas as pd
from datetime import timedelta
from scipy import stats as _stats
from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

_log = get_logger(__name__)

# ── 算法常量 (config.yaml 单一真相源, 模块级读一次) ──
_MIN_IC_OBS          = _require_cfg("oos_verify.min_ic_obs")
_MIN_TOTAL_POINTS     = _require_cfg("oos_verify.min_total_points")
_MIN_IS_POINTS        = _require_cfg("oos_verify.min_is_points")
_MIN_OOS_POINTS       = _require_cfg("oos_verify.min_oos_points")
_IS_SAMPLE_INTERVAL   = _require_cfg("oos_verify.sample_interval")


# ════════════════════════════════════════════════════════════════════
# Phase 1: 纯 IC 计算 — 对标 Alphalens factor_information_coefficient()
# ════════════════════════════════════════════════════════════════════

def compute_ic_series(
    daily_close: "pd.DataFrame",
    factor_values_by_date: dict,
    trading_days: list,
    *,
    min_obs: int = _MIN_IC_OBS,
) -> dict:
    """纯计算: 逐日 Spearman Rank IC.

    对标: Alphalens performance.factor_information_coefficient()
          DolphinDB singleFactorAnalysis() 中的 IC 计算段

    Args:
        daily_close: 宽表 DataFrame, index=date, columns=symbol, values=收盘价
        factor_values_by_date: {date: {factor_name: Series(symbol→value)}}
            调用方负责: FactorStore.load() 批量加载后组装
        trading_days: 交易日列表 (YYYY-MM-DD), 升序
        min_obs: 单日 IC 最少股票数 (来源: Kendall & Stuart 1979)

    Returns:
        {factor_name: {date: ic_value}}

    设计约束: 不创建 DataStore / FactorStore, 不读 config, 纯 numpy/scipy 运算.
    """
    # 收集所有因子名
    factor_names = set()
    for fv in factor_values_by_date.values():
        factor_names.update(fv.keys())
    factor_names = sorted(factor_names)
    ic_series = {name: {} for name in factor_names}

    # 构建 all_trading_days 索引 (前向收益需要找 next_day)
    all_trading_days = sorted(daily_close.index.astype(str).tolist())

    for ds in trading_days:
        if ds not in daily_close.index:
            continue
        close_slice = daily_close.loc[ds]
        if not isinstance(close_slice, pd.Series) or len(close_slice) < 2:
            continue

        # 前向 1 日收益: return(t→t+1)
        try:
            ds_idx = all_trading_days.index(ds)
        except ValueError:
            continue
        if ds_idx + 1 >= len(all_trading_days):
            continue
        next_ds = all_trading_days[ds_idx + 1]
        if next_ds not in daily_close.index:
            continue
        next_close = daily_close.loc[next_ds]
        if not isinstance(next_close, pd.Series):
            continue
        fwd = (next_close / close_slice) - 1

        fv = factor_values_by_date.get(ds, {})
        if not fv:
            continue

        for fname in factor_names:
            f_series = fv.get(fname)
            if f_series is None or not isinstance(f_series, pd.Series):
                continue
            common = f_series.dropna().index.intersection(fwd.dropna().index)
            if len(common) < min_obs:
                continue
            rho, _ = _stats.spearmanr(f_series[common], fwd[common])
            if not np.isnan(rho):
                ic_series[fname][ds] = float(rho)

    return ic_series


# ════════════════════════════════════════════════════════════════════
# Phase 2: 纯 IS/OOS 统计分析 — 对标 DolphinDB singleFactorAnalysis()
# ════════════════════════════════════════════════════════════════════

def analyze_is_oos(
    ic_series: dict,
    test_start: str,
    *,
    min_total_points: int = _MIN_TOTAL_POINTS,
    min_is_points: int = _MIN_IS_POINTS,
    min_oos_points: int = _MIN_OOS_POINTS,
    decay_warn_threshold: float,
) -> dict:
    """纯统计: IS/OOS 拆分 → IR → 衰减检测.

    对标: Alphalens tears.create_information_tear_sheet() 中的统计段
          DolphinDB singleFactorAnalysis() 输出字典

    Args:
        ic_series: {factor_name: {date: ic_value}} — compute_ic_series() 的输出
        test_start: OOS 起始日期 (YYYY-MM-DD), 由调用方负责计算
        min_total_points/min_is_points/min_oos_points: 统计算法常量 (config.yaml)
        decay_warn_threshold: OOS_IR/IS_IR 低于此值告警 (config.yaml, 调用方传入)

    Returns:
        per_factor: {name: {is_ir, oos_ir, is_mean, oos_mean, n_is, n_oos}}
        decayed: [str], n_qualified: int, is_ir_agg, oos_ir_agg, decay_ratio
    """
    factor_names = list(ic_series.keys())
    factor_irs = {}
    decayed_factors = []

    for name in factor_names:
        daily_ic = ic_series.get(name, {})
        if len(daily_ic) < min_total_points:
            continue

        ic_s = pd.Series(daily_ic)
        ic_s.index = pd.to_datetime(ic_s.index)
        ic_s = ic_s.sort_index()
        is_vals = ic_s[ic_s.index < test_start]
        oos_vals = ic_s[ic_s.index >= test_start]
        n_is = len(is_vals)
        n_oos = len(oos_vals)

        if n_is < min_is_points or n_oos < min_oos_points:
            continue

        is_mean = float(is_vals.mean())
        is_std  = float(is_vals.std())
        oos_mean = float(oos_vals.mean())
        oos_std  = float(oos_vals.std())
        is_ir = is_mean / max(is_std, 1e-10)
        oos_ir = oos_mean / max(oos_std, 1e-10)

        factor_irs[name] = {
            "is_ir": round(is_ir, 4), "oos_ir": round(oos_ir, 4),
            "n_is": n_is, "n_oos": n_oos,
            "is_mean": round(is_mean, 4), "oos_mean": round(oos_mean, 4),
        }

        if abs(is_ir) > 0.01:
            ratio = oos_ir / is_ir if is_ir > 0 else 1.0
            if ratio < decay_warn_threshold:
                decayed_factors.append(
                    f"{name}: IS_IR={is_ir:+.4f} → OOS_IR={oos_ir:+.4f} (ratio={ratio:.2f})"
                )

    n_qualified = len(factor_irs)
    if n_qualified == 0:
        is_ir_agg, oos_ir_agg, decay_ratio = 0.0, 0.0, 1.0
    else:
        is_irs = [v["is_ir"] for v in factor_irs.values()]
        oos_irs = [v["oos_ir"] for v in factor_irs.values()]
        is_ir_agg = float(np.median(is_irs))
        oos_ir_agg = float(np.median(oos_irs))
        decay_ratio = oos_ir_agg / max(abs(is_ir_agg), 0.01) if abs(is_ir_agg) > 0.01 else 1.0

    return {
        "per_factor": factor_irs,
        "decayed": decayed_factors,
        "n_qualified": n_qualified,
        "is_ir_agg": round(is_ir_agg, 4),
        "oos_ir_agg": round(oos_ir_agg, 4),
        "decay_ratio": round(decay_ratio, 4),
    }


# ════════════════════════════════════════════════════════════════════
# Phase 3: 编排层 — 对标 Alphalens tears.create_full_tear_sheet()
# ════════════════════════════════════════════════════════════════════

def run_oos_check(
    today: str,
    *,
    status_filter: str,
    train_days: int,
    test_days: int,
    decay_warn_threshold: float,
    n_symbols: int,
    symbols: list = None,  # test-v466 (BT-4): 显式符号集 — 回测 IC 与主循环同口径
    factor_cache: dict = None,  # test-v397 (P0): {date: {factor: Series}}, 跳过 gzip I/O
) -> dict:
    """编排层: 加载数据 → compute_ic_series → analyze_is_oos → 返回结果.

    6 个关键字参数全部必传。内部不读 config — config 由调用方负责。

    Args:
        today: 目标日期 YYYY-MM-DD
        status_filter: "using" (实盘) 或 "backtesting" (回测)
        train_days: 训练窗口日历天数
        test_days: OOS 测试窗口日历天数
        decay_warn_threshold: 衰减预警阈值
        n_symbols: IC 计算用股票数 (0=全量)

    Returns:
        {n_factors, ic_daily, n_qualified, oos_ir, is_ir, decay_ratio,
         oos_decay_count, alert, details: {decayed, per_factor}}
    """
    from quant.data.store import DataStore
    from quant.data.repos import UniverseRepo
    from quant.factor.compute._registry import get_factor_names
    from quant.factor.store import FactorStore
    from quant.config.paths import FACTOR_CACHE_DB

    # ── 1. 因子池 ──
    factor_names = get_factor_names(status_filter=status_filter)
    if not factor_names:
        _log.info(f"[{today}] OOS verify: no factors (filter={status_filter}), skip")
        return _empty(len(factor_names))

    # ── 2. 日期窗口 ──
    today_dt = pd.Timestamp(today)
    from quant.execution.calendar import is_trading_day as _is_td
    _bd = [d for d in pd.date_range(end=today_dt, periods=test_days + 2, freq="B")
           if _is_td(d.date())][:test_days + 1]
    test_start = _bd[0].strftime("%Y-%m-%d")
    total_lookback = train_days + test_days + 5
    data_start = (today_dt - timedelta(days=total_lookback)).strftime("%Y-%m-%d")

    # ── 3. 数据加载 (编排层职责) ──
    store = DataStore()
    if symbols is not None:
        # 调用方已提供符号集 (回测 IC 与主循环同口径), 不另行排名
        symbols = list(symbols)
        if n_symbols and len(symbols) > n_symbols:
            symbols = symbols[:n_symbols]
    else:
        all_symbols = UniverseRepo().get_symbols(exclude_market='BJ')
        # test-v466 (BT-4): 流动性排名后取 top-N — 原 all_symbols[:n_symbols] 按
        # universe 表顺序切片 (非流动性), IC 样本偏差 + 与回测口径不一致。
        if n_symbols == 0 or len(all_symbols) <= n_symbols:
            symbols = all_symbols
        else:
            symbols = store.rank_by_turnover(
                all_symbols, test_start,
                lookback_days=_require_cfg("backtest.universe_turnover_days"),
                top_n=n_symbols,
            )
            _log.info(f"[{today}] OOS verify: top-{len(symbols)} by turnover (of {len(all_symbols)})")
    data = store.get_daily(symbols, start=data_start, end=today)

    if data.empty:
        store.close()
        _log.warning(f"[{today}] OOS verify: no daily data loaded (filter={status_filter})")
        return _empty(len(factor_names))

    # ── 4. 交易日列表 (IS 采样, OOS 全量) ──
    all_dates = pd.date_range(start=data_start, end=today_dt, freq="B")
    all_dates = [d for d in all_dates if _is_td(d.date())]
    all_trading_days = [d.strftime("%Y-%m-%d") for d in all_dates
                        if d.strftime("%Y-%m-%d") <= today]

    trading_days = []
    for i, ds in enumerate(all_trading_days):
        if ds >= test_start:
            trading_days.append(ds)       # OOS 期每天
        elif i % _IS_SAMPLE_INTERVAL == 0:
            trading_days.append(ds)       # IS 期采样

    _log.info(f"[{today}] OOS verify: {len(trading_days)}/{len(all_trading_days)} trading days sampled "
              f"(IS×1/{_IS_SAMPLE_INTERVAL}) | {len(factor_names)} factors (filter={status_filter}) | "
              f"lookback={total_lookback}cd, {len(symbols)} symbols")

    # ── 5. 批量加载因子值 — factor_cache (内存) 优先, 回退 FactorStore (gzip I/O) ──
    # test-v397 (P0): 回测启动时已预加载全量因子值, 跳过 ~180 次 gzip 文件打开
    factor_values_by_date = {}
    if factor_cache is not None:
        for ds in trading_days:
            fv = factor_cache.get(ds)
            if fv:
                factor_values_by_date[ds] = fv
            else:
                # test-v466 (BT-5): 单日缺失降级跳过 — 原 RuntimeError 中断整个
                # OOS 验证链; 缺失是增量物化的常态 (尾部若干交易日未物化)。
                _log.warning(
                    f"[{today}] factor_cache miss for {ds} ({len(symbols)} symbols, "
                    f"{len(factor_names)} factors) — skipped for this day"
                )
    else:
        from quant.factor.store import FactorStore
        from quant.config.paths import FACTOR_CACHE_DB
        fs = FactorStore(db_path=FACTOR_CACHE_DB)
        for ds in trading_days:
            fv = fs.load(ds, symbols=symbols, factor_names=factor_names)
            if fv:
                factor_values_by_date[ds] = fv
            else:
                _log.warning(
                    f"[{today}] factor_cache miss for {ds} ({len(symbols)} symbols, "
                    f"{len(factor_names)} factors) — skipped for this day"
                )
        fs.close()

    # ── 6. 纯计算 (无 DB/Config 依赖) ──
    ic_series = compute_ic_series(
        data["close"], factor_values_by_date, trading_days,
        min_obs=_MIN_IC_OBS,
    )

    # ── 7. 纯统计 (无 DB/Config 依赖) ──
    result = analyze_is_oos(
        ic_series, test_start,
        min_total_points=_MIN_TOTAL_POINTS,
        min_is_points=_MIN_IS_POINTS,
        min_oos_points=_MIN_OOS_POINTS,
        decay_warn_threshold=decay_warn_threshold,
    )

    store.close()

    n_qualified = result["n_qualified"]
    decayed_factors = result["decayed"]

    _log.info(
        f"[{today}] OOS verify: {n_qualified}/{len(factor_names)} factors qualified | "
        f"IS_IR={result['is_ir_agg']:+.4f} OOS_IR={result['oos_ir_agg']:+.4f} "
        f"decay={result['decay_ratio']:.2%} | "
        f"filter={status_filter} test_start={test_start} ({test_days}td)"
    )
    if decayed_factors:
        _log.warning(f"[{today}] OOS decay alert: {len(decayed_factors)}/{n_qualified} "
                     f"below {decay_warn_threshold:.0%}")
        for f in decayed_factors[:5]:
            _log.warning(f"  {f}")

    return {
        "n_factors": len(factor_names),
        "n_symbols": len(symbols),
        "ic_daily": ic_series,
        "n_qualified": n_qualified,
        "oos_ir": result["oos_ir_agg"],
        "is_ir": result["is_ir_agg"],
        "decay_ratio": result["decay_ratio"],
        "oos_decay_count": len(decayed_factors),
        "alert": len(decayed_factors) > 0,
        "details": {
            "decayed": decayed_factors[:10],
            "per_factor": result["per_factor"],
        },
    }


def _empty(n: int) -> dict:
    return {
        "n_factors": n, "n_qualified": 0,
        "oos_ir": 0.0, "is_ir": 0.0, "decay_ratio": 1.0,
        "oos_decay_count": 0, "alert": False,
        "details": {"decayed": [], "per_factor": {}},
    }
