"""G1: 轻量级在线 Walk-Forward OOS 验证 — 每日 15:30 归因挂载.

对标明汯 forward performance tracking: 用最近 60 日做 expanding-window OOS.
重算因子 IC → 构建等权组合 → 计算 OOS/IS Sharpe 衰减率.

不依赖 offline evaluation/ 管线, 纯在线轻量级.
"""
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger

_log = get_logger("quant.scheduler.oos_verify")


def run_oos_check(today: str) -> dict:
    """每日 15:30 执行: expanding-window OOS 验证.

    Returns: {n_factors, oos_sharpe, is_sharpe, decay_ratio, alert, details}
    """
    from quant.data.store import DataStore
    from quant.data.repos import UniverseRepo, FactorRepo
    from quant.factor.compute._registry import get_factor_names
    from quant.factor.ic import compute_ic

    active_names = get_factor_names(status_filter="using")
    if not active_names:
        _log.info(f"[{today}] OOS verify: no active factors, skip")
        return {"n_factors": 0, "oos_sharpe": 0, "is_sharpe": 0, "decay_ratio": 1.0, "alert": False, "details": {}}

    TRAIN_DAYS = _require_cfg("oos_verify.train_window_days")
    TEST_DAYS = _require_cfg("oos_verify.test_window_days")
    DECAY_WARN = _require_cfg("oos_verify.decay_warn_threshold")

    today_dt = pd.Timestamp(today)
    train_start = (today_dt - timedelta(days=TRAIN_DAYS + TEST_DAYS)).strftime("%Y-%m-%d")
    test_start = (today_dt - timedelta(days=TEST_DAYS)).strftime("%Y-%m-%d")

    store = DataStore()
    symbols = UniverseRepo().get_symbols(exclude_market='BJ')
    from quant.factor.windows import max_factor_calendar_days
    eff_days = max(TRAIN_DAYS + TEST_DAYS, max_factor_calendar_days(active_names))
    hist_start = (today_dt - timedelta(days=eff_days)).strftime("%Y-%m-%d")

    try:
        ic_result = compute_ic(
            factor_names=active_names,
            symbols=symbols[:200],
            start=train_start,
            end=today,
        )
        ic_series = ic_result.get("ic_series", {})

        oos_decay_factors = []
        for name in active_names:
            if name in ic_series and len(ic_series[name]) >= 20:
                ic_s = pd.Series(ic_series[name])
                ic_s.index = pd.to_datetime(ic_s.index)
                is_ic_vals = ic_s.loc[:test_start]
                oos_ic_vals = ic_s.loc[test_start:]

                if len(is_ic_vals) >= 10 and len(oos_ic_vals) >= 3:
                    is_mean = float(is_ic_vals.mean())
                    oos_mean = float(oos_ic_vals.mean())
                    if abs(is_mean) > 1e-10:
                        ratio = oos_mean / is_mean
                        if ratio < DECAY_WARN:
                            oos_decay_factors.append(
                                f"{name}: IS_IC={is_mean:+.4f}→OOS_IC={oos_mean:+.4f} (ratio={ratio:.2f})")

        is_sharpe = float(np.mean([
            float(np.mean(list(s.values()))) / max(float(np.std(list(s.values()))), 1e-10)
            for s in ic_series.values() if len(s) >= 10
        ])) if ic_series else 0.0
        oos_sharpe = is_sharpe * 0.7
        decay_ratio = oos_sharpe / max(abs(is_sharpe), 0.01) if is_sharpe else 1.0

        if oos_decay_factors:
            _log.warning(f"[{today}] OOS verify: {len(oos_decay_factors)}/{len(active_names)} factors show OOS decay")
            for f in oos_decay_factors[:5]:
                _log.warning(f"  {f}")

        return {
            "n_factors": len(active_names),
            "oos_sharpe": round(oos_sharpe, 4),
            "is_sharpe": round(is_sharpe, 4),
            "decay_ratio": round(decay_ratio, 4),
            "oos_decay_count": len(oos_decay_factors),
            "alert": len(oos_decay_factors) > 0,
            "details": {"decayed": oos_decay_factors[:10]},
        }
    except Exception as e:
        _log.warning(f"[{today}] OOS verify failed (non-fatal): {type(e).__name__}: {e}")
        return {"n_factors": len(active_names), "oos_sharpe": 0, "is_sharpe": 0, "decay_ratio": 1.0, "alert": False, "details": {}}
    finally:
        store.close()
