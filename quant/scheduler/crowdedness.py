"""G2: 因子拥挤度检测 — 截面相关性 + 趋势追踪.

检测两类拥挤信号:
  1. 截面拥挤: 任意两因子在横截面上 Spearman ρ > 0.7 → 同一方向押注, alpha 被套利
  2. 趋势拥挤: 60 日平均配对相关性单调上升 → 因子动物园正在拥挤

来源: 幻方 AI 因子拥挤度模型; Novy-Marx (2015) "Backtesting Strategies"; 
      De Prado (2018) AFML Ch.13.
"""
import numpy as np
import pandas as pd
from datetime import datetime
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger

_log = get_logger("quant.scheduler.crowdedness")

CROWD_CORR_THRESHOLD = 0.70       # 截面相关性阈值
CROWD_PAIR_MIN_STOCKS = 30        # 因子对有效股票数下限
CROWD_TREND_DAYS = 60             # 趋势拥挤回溯天数
CROWD_TREND_ALERT_UP = 0.05       # 60日平均相关性上升 5% 告警


def check_factor_crowdedness(
    today: str,
    symbols: list[str] = None,
    store=None,
) -> dict:
    """每日 15:30 执行: 因子拥挤度检测.

    Returns: {
        n_factors, n_high_corr_pairs, crowd_index, crowd_index_prev,
        trend_up, alert, high_corr_pairs, details
    }
    """
    if symbols is None:
        from quant.data.repos import UniverseRepo
        symbols = UniverseRepo().get_symbols(exclude_market='BJ')[:300]

    _close_store = False
    if store is None:
        from quant.data.store import DataStore
        store = DataStore()
        _close_store = True

    try:
        from quant.factor.compute._registry import get_factor_names
        from quant.factor.compute import compute_all_factors
        from quant.factor.windows import max_factor_calendar_days

        active_names = get_factor_names(status_filter="using")
        if len(active_names) < 2:
            _log.info(f"[{today}] crowdedness: only {len(active_names)} factor(s), skip")
            return _empty_result(len(active_names))

        eff_days = max(60, max_factor_calendar_days(active_names))
        start = (pd.Timestamp(today) - pd.Timedelta(days=eff_days)).strftime("%Y-%m-%d")
        data = store.get_daily(symbols, start=start, end=today)

        if data.empty:
            _log.warning(f"[{today}] crowdedness: no daily data, skip")
            return _empty_result(len(active_names))

        fundamentals = store.get_fundamentals(symbols, date=today)
        factor_values = compute_all_factors(
            data, today, fundamentals=fundamentals, status_filter="using",
        )
        factor_values = {k: v for k, v in factor_values.items() if isinstance(v, pd.Series)}

        if len(factor_values) < 2:
            _log.info(f"[{today}] crowdedness: only {len(factor_values)} factor(s) with values, skip")
            return _empty_result(len(active_names))

        # ── 1. 截面拥挤: pairwise Spearman ρ ──
        fnames = list(factor_values.keys())
        high_corr_pairs = []
        pair_corrs = []

        for i in range(len(fnames)):
            for j in range(i + 1, len(fnames)):
                s_i = factor_values[fnames[i]].dropna()
                s_j = factor_values[fnames[j]].dropna()
                common = s_i.index.intersection(s_j.index)
                if len(common) < CROWD_PAIR_MIN_STOCKS:
                    continue
                from scipy.stats import spearmanr as _spearmanr
                rho, _ = _spearmanr(s_i[common], s_j[common])
                if np.isnan(rho):
                    continue
                pair_corrs.append(rho)
                if abs(rho) > CROWD_CORR_THRESHOLD:
                    high_corr_pairs.append({
                        "factor_a": fnames[i],
                        "factor_b": fnames[j],
                        "correlation": round(float(rho), 4),
                    })

        crowd_index = round(float(np.mean(np.abs(pair_corrs))), 4) if pair_corrs else 0.0

        # ── 2. 趋势拥挤: 与前 60 日均值的比较 ──
        crowd_index_prev = _get_prev_crowd_index(store, today)
        trend_up = crowd_index_prev is not None and (
            crowd_index > crowd_index_prev * (1 + CROWD_TREND_ALERT_UP)
        )

        alert = len(high_corr_pairs) > 0 or (trend_up and crowd_index > 0.3)

        # ── 3. 落盘 + 持久化 ──
        _store_crowd_snapshot(store, today, crowd_index, len(high_corr_pairs),
                              len(factor_values), alert)

        if high_corr_pairs:
            _log.warning(
                f"[{today}] G2 crowdedness: {len(high_corr_pairs)} high-correlation pairs "
                f"(>{CROWD_CORR_THRESHOLD:.0%}), crowd_index={crowd_index:.3f}"
            )
            for p in high_corr_pairs[:5]:
                _log.warning(f"  {p['factor_a']} ↔ {p['factor_b']}: ρ={p['correlation']:+.3f}")
        elif trend_up:
            _log.info(
                f"[{today}] G2 crowdedness: trend alert — crowd_index {crowd_index_prev:.3f}→{crowd_index:.3f} "
                f"(+{CROWD_TREND_ALERT_UP:.0%} threshold)"
            )
        else:
            _log.info(
                f"[{today}] G2 crowdedness: {len(factor_values)} factors, "
                f"crowd_index={crowd_index:.3f}, no alerts"
            )

        return {
            "n_factors": len(factor_values),
            "n_high_corr_pairs": len(high_corr_pairs),
            "crowd_index": crowd_index,
            "crowd_index_prev": crowd_index_prev or 0.0,
            "trend_up": trend_up,
            "alert": alert,
            "high_corr_pairs": high_corr_pairs[:10],
            "details": {
                "total_pairs": len(pair_corrs),
                "mean_abs_corr": crowd_index,
                "max_corr": round(float(max(pair_corrs)), 4) if pair_corrs else 0.0,
            },
        }

    except Exception as e:
        _log.warning(f"[{today}] G2 crowdedness failed (non-fatal): {type(e).__name__}: {e}")
        return _empty_result(0)
    finally:
        if _close_store:
            store.close()


def _empty_result(n_factors: int) -> dict:
    return {
        "n_factors": n_factors,
        "n_high_corr_pairs": 0,
        "crowd_index": 0.0,
        "crowd_index_prev": 0.0,
        "trend_up": False,
        "alert": False,
        "high_corr_pairs": [],
        "details": {"total_pairs": 0, "mean_abs_corr": 0.0, "max_corr": 0.0},
    }


def _get_prev_crowd_index(store, today: str) -> float | None:
    """取最近 60 日的平均 crowd_index (通过 market.db)."""
    try:
        import sqlite3
        db_path = store.db_path
        prev = (pd.Timestamp(today) - pd.Timedelta(days=CROWD_TREND_DAYS)).strftime("%Y-%m-%d")
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT AVG(crowd_index) FROM factor_crowd_snapshot "
                "WHERE date >= ? AND date < ?",
                (prev, today),
            ).fetchone()
        return round(float(rows[0]), 4) if rows and rows[0] is not None else None
    except Exception:
        return None


def _store_crowd_snapshot(store, date: str, crowd_index: float,
                          n_high_pairs: int, n_factors: int, alert: bool):
    """持久化拥挤度快照到 market.db."""
    try:
        import sqlite3
        db_path = store.db_path
        with sqlite3.connect(db_path) as conn:
            conn.execute(
            """CREATE TABLE IF NOT EXISTS factor_crowd_snapshot (
                date TEXT PRIMARY KEY,
                crowd_index REAL NOT NULL DEFAULT 0.0,
                n_high_pairs INTEGER NOT NULL DEFAULT 0,
                n_factors INTEGER NOT NULL DEFAULT 0,
                alert INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            )"""
        )
            conn.execute(
                "INSERT OR REPLACE INTO factor_crowd_snapshot "
                "(date, crowd_index, n_high_pairs, n_factors, alert) "
                "VALUES (?, ?, ?, ?, ?)",
                (date, crowd_index, n_high_pairs, n_factors, 1 if alert else 0),
            )
    except Exception as e:
        _log.warning(f"[{date}] failed to store crowd snapshot: {e}")
