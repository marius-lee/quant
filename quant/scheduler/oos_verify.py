"""G1: 在线 Walk-Forward OOS 验证 — 每日 15:30 归因挂载.

对标明汯 forward performance tracking:
  - expanding-window: IS 窗口到 test_start, OOS 窗口 test_start→today
  - per-factor IC Information Ratio (IC_IR = mean/std)
  - 自己拉数据+算因子+算 IC，不依赖 compute_ic (后者 skip warmup 会砍掉 OOS 窗口)
  - 聚合用 median
  - IS 期采样 (每 SAMPLE_INTERVAL 天算一次), OOS 期每天算 — 提速 ~8x

来源: ADR 029 四层回测, Grinold & Kahn (1999) Ch6.
"""
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from scipy import stats as _stats
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger

_log = get_logger(__name__)

_MIN_IC_OBS = 20           # 单日 IC 计算最少股票数
_MIN_TOTAL_POINTS = 10     # 因子最少 IC 天数
_MIN_IS_POINTS = 6         # IS 窗口最少天数
_MIN_OOS_POINTS = 2        # OOS 窗口最少天数
_IS_SAMPLE_INTERVAL = 5    # IS 期采样间隔 (每 N 个交易日算一次 IC, 提速)


def run_oos_check(today: str) -> dict:
    """每日 15:30 执行: expanding-window OOS 验证 (IS 采样提速)."""
    from quant.data.store import DataStore
    from quant.data.repos import UniverseRepo
    from quant.factor.compute._registry import get_factor_names
    from quant.factor.compute import compute_all_factors
    from quant.factor.windows import max_factor_calendar_days

    active_names = get_factor_names(status_filter="using")
    if not active_names:
        _log.info(f"[{today}] OOS verify: no active factors, skip")
        return _empty(0)

    TRAIN_DAYS = _require_cfg("oos_verify.train_window_days")
    TEST_DAYS  = _require_cfg("oos_verify.test_window_days")
    DECAY_WARN = _require_cfg("oos_verify.decay_warn_threshold")

    today_dt = pd.Timestamp(today)
    from quant.execution.calendar import is_trading_day as _is_td
    _bd = [d for d in pd.date_range(end=today_dt, periods=TEST_DAYS + 2, freq="B") if _is_td(d.date())][:TEST_DAYS + 1]
    test_start = _bd[0].strftime("%Y-%m-%d")

    _raw_cal = max_factor_calendar_days(active_names)
    _cal_cap = min(_raw_cal, 504) if _raw_cal < 1e9 else 504
    fac_cal = _cal_cap
    # 回看窗口 = 训练天数 + 测试天数 + 因子最大日历跨度, 不再强制 252 下限
    total_lookback = TRAIN_DAYS + TEST_DAYS + fac_cal
    data_start = (today_dt - timedelta(days=total_lookback)).strftime("%Y-%m-%d")

    store = DataStore()
    symbols = UniverseRepo().get_symbols(exclude_market='BJ')[:300]
    data = store.get_daily(symbols, start=data_start, end=today)

    if data.empty:
        store.close()
        _log.warning(f"[{today}] OOS verify: no daily data loaded")
        return _empty(len(active_names))

    # ── 生成 IC 序列 (IS 采样, OOS 全量) ──
    all_dates = pd.date_range(start=data_start, end=today_dt, freq="B")
    all_dates = [d for d in all_dates if _is_td(d.date())]
    all_trading_days = [d.strftime("%Y-%m-%d") for d in all_dates
                        if d.strftime("%Y-%m-%d") <= today]

    # IS 期采样: 每 _IS_SAMPLE_INTERVAL 个交易日算一次
    trading_days = []
    for i, ds in enumerate(all_trading_days):
        if ds >= test_start:
            trading_days.append(ds)       # OOS 期每天
        elif i % _IS_SAMPLE_INTERVAL == 0:
            trading_days.append(ds)       # IS 期采样

    n_full = len(all_trading_days)
    _log.info(f"[{today}] OOS verify: {len(trading_days)}/{n_full} trading days sampled "
              f"(IS×1/{_IS_SAMPLE_INTERVAL}) | {len(active_names)} active factors | "
              f"lookback={total_lookback}cd")

    ic_series_per_factor = {name: {} for name in active_names}
    daily_close = data["close"]

    for ds in trading_days:
        if ds not in daily_close.index:
            continue
        close_slice = daily_close.loc[ds]
        if not isinstance(close_slice, pd.Series) or len(close_slice) < 2:
            continue

        # forward 1-day return
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
        fundamentals = store.get_fundamentals(symbols, ds)

        try:
            fv = compute_all_factors(
                data, ds, fundamentals=fundamentals,
                factor_names=active_names,
            )
        except Exception:
            continue

        if not fv:
            continue

        for fname in active_names:
            f_series = fv.get(fname)
            if f_series is None or not isinstance(f_series, pd.Series):
                continue
            common = f_series.dropna().index.intersection(fwd.dropna().index)
            if len(common) < _MIN_IC_OBS:
                continue
            rho, _ = _stats.spearmanr(f_series[common], fwd[common])
            if not np.isnan(rho):
                ic_series_per_factor[fname][ds] = float(rho)

    # ── IS/OOS 拆分 + IR 计算 ──
    factor_irs = {}
    decayed_factors = []

    for name in active_names:
        daily_ic = ic_series_per_factor.get(name, {})
        if len(daily_ic) < _MIN_TOTAL_POINTS:
            continue

        ic_s = pd.Series(daily_ic)
        ic_s.index = pd.to_datetime(ic_s.index)
        ic_s = ic_s.sort_index()
        is_vals = ic_s[ic_s.index < test_start]
        oos_vals = ic_s[ic_s.index >= test_start]
        n_is = len(is_vals)
        n_oos = len(oos_vals)

        if n_is < _MIN_IS_POINTS or n_oos < _MIN_OOS_POINTS:
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
            if ratio < DECAY_WARN:
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

    _log.info(
        f"[{today}] OOS verify: {n_qualified}/{len(active_names)} factors qualified | "
        f"IS_IR={is_ir_agg:+.4f} OOS_IR={oos_ir_agg:+.4f} decay={decay_ratio:.2%} | "
        f"test_start={test_start} ({TEST_DAYS}td)"
    )
    if decayed_factors:
        _log.warning(f"[{today}] OOS decay alert: {len(decayed_factors)}/{n_qualified} "
                     f"below {DECAY_WARN:.0%}")
        for f in decayed_factors[:5]:
            _log.warning(f"  {f}")

    store.close()
    return {
        "n_factors": len(active_names),
        "n_qualified": n_qualified,
        "oos_ir": round(oos_ir_agg, 4),
        "is_ir": round(is_ir_agg, 4),
        "decay_ratio": round(decay_ratio, 4),
        "oos_decay_count": len(decayed_factors),
        "alert": len(decayed_factors) > 0,
        "details": {
            "decayed": decayed_factors[:10],
            "per_factor": factor_irs,
        },
    }


def _empty(n: int) -> dict:
    return {
        "n_factors": n, "n_qualified": 0,
        "oos_ir": 0.0, "is_ir": 0.0, "decay_ratio": 1.0,
        "oos_decay_count": 0, "alert": False,
        "details": {"decayed": [], "per_factor": {}},
    }
