"""Benchmark tracker — daily strategy-vs-benchmark comparison.

Stores daily tracking in trades.db, computes rolling alpha/IR/beta/up-down capture.
"""

import os
import sqlite3
from quant.data.repos._base import DatabaseManager
import numpy as np
import pandas as pd
from quant.utils.logger import get_logger

_log = get_logger("benchmark.tracker")

_TRADES_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trades.db")
_MARKET_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")


def _ensure_table():
    conn = sqlite3.connect(_TRADES_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS benchmark_tracking (
            date TEXT PRIMARY KEY,
            strategy_equity REAL NOT NULL,
            strategy_return REAL,
            bench_return REAL,
            alpha REAL,
            rolling_alpha_60d REAL,
            rolling_ir_60d REAL,
            rolling_beta_60d REAL,
            up_capture_60d REAL,
            down_capture_60d REAL
        )
    """)
    conn.commit()
    conn.close()


def get_benchmark_return(date_str: str, index_code: str = "000300"):
    """Get benchmark daily return for a given date. Returns None if unavailable."""
    conn = sqlite3.connect(_MARKET_DB)
    rows = conn.execute(
        "SELECT date, close FROM benchmark_daily WHERE index_code=? AND date <= ? ORDER BY date DESC LIMIT 2",
        (index_code, date_str)
    ).fetchall()
    conn.close()
    if len(rows) < 2:
        return None
    today_close = float(rows[0][1])
    yesterday_close = float(rows[1][1])
    if yesterday_close <= 0:
        return None
    return today_close / yesterday_close - 1


def record_daily(date_str: str, strategy_equity: float,
                 yesterday_equity=None):
    """Record one day of benchmark tracking.

    Args:
        date_str: YYYY-MM-DD
        strategy_equity: current total wealth
        yesterday_equity: previous day's total wealth (for computing strategy return)
    """
    _ensure_table()

    strat_ret = None
    if yesterday_equity is not None and yesterday_equity > 0:
        strat_ret = strategy_equity / yesterday_equity - 1

    bench_ret = get_benchmark_return(date_str)
    alpha_val = (strat_ret - bench_ret) if (strat_ret is not None and bench_ret is not None) else None

    conn = sqlite3.connect(_TRADES_DB)
    conn.execute("""
        INSERT OR REPLACE INTO benchmark_tracking
        (date, strategy_equity, strategy_return, bench_return, alpha)
        VALUES (?, ?, ?, ?, ?)
    """, (date_str, strategy_equity, strat_ret, bench_ret, alpha_val))
    conn.commit()
    conn.close()
    _log.info(f"benchmark_tracking {date_str}: strat={strat_ret} bench={bench_ret} alpha={alpha_val}")


def compute_rolling_metrics(window: int = 60):
    """Compute rolling alpha, IR, beta, up/down capture from stored tracking data.

    Updates the benchmark_tracking table with rolling metrics for the most recent dates.
    """
    conn = sqlite3.connect(_TRADES_DB)
    df = pd.read_sql_query(
        "SELECT date, strategy_return, bench_return FROM benchmark_tracking "
        "WHERE strategy_return IS NOT NULL AND bench_return IS NOT NULL "
        "ORDER BY date",
        conn
    )
    conn.close()
    if df.empty or len(df) < window:
        return

    df = df.set_index("date")
    strat = df["strategy_return"]
    bench = df["bench_return"]

    for i in range(window - 1, len(df)):
        date_str = df.index[i]
        w_strat = strat.iloc[i - window + 1:i + 1]
        w_bench = bench.iloc[i - window + 1:i + 1]

        alpha_series = w_strat - w_bench
        rolling_alpha = alpha_series.mean() * 252

        alpha_std = alpha_series.std()
        rolling_ir = rolling_alpha / (alpha_std * np.sqrt(252)) if alpha_std > 0 else 0.0

        bench_var = w_bench.var()
        rolling_beta = w_strat.cov(w_bench) / bench_var if bench_var > 0 else 1.0

        up_mask = w_bench > 0
        up_cap = None
        if up_mask.any() and w_bench[up_mask].mean() != 0:
            up_cap = w_strat[up_mask].mean() / w_bench[up_mask].mean()

        down_mask = w_bench < 0
        down_cap = None
        if down_mask.any() and w_bench[down_mask].mean() != 0:
            down_cap = w_strat[down_mask].mean() / w_bench[down_mask].mean()

        conn2 = sqlite3.connect(_TRADES_DB)
        conn2.execute("""
            UPDATE benchmark_tracking
            SET rolling_alpha_60d=?, rolling_ir_60d=?, rolling_beta_60d=?,
                up_capture_60d=?, down_capture_60d=?
            WHERE date=?
        """, (rolling_alpha, rolling_ir, rolling_beta,
              up_cap, down_cap, date_str))
        conn2.commit()
        conn2.close()

    _log.info(f"compute_rolling_metrics: updated {len(df) - window + 1} rows")


def get_tracking_summary():
    """Return the most recent benchmark tracking data for the web API."""
    _ensure_table()
    conn = sqlite3.connect(_TRADES_DB)
    rows = conn.execute(
        "SELECT date, strategy_return, bench_return, alpha FROM benchmark_tracking "
        "WHERE strategy_return IS NOT NULL AND bench_return IS NOT NULL ORDER BY date"
    ).fetchall()
    conn.close()

    if not rows:
        return {"available": False, "message": "No benchmark tracking data yet"}

    strat_cum = 1.0
    bench_cum = 1.0
    curves = []
    s_eq = 1.0
    b_eq = 1.0
    for date_str, sr, br, _ in rows:
        if sr is not None:
            s_eq *= (1 + sr)
        if br is not None:
            b_eq *= (1 + br)
        curves.append({
            "date": date_str,
            "strategy_equity": round(s_eq, 6),
            "benchmark_equity": round(b_eq, 6),
        })

    strat_cum_pct = round((strat_cum - 1) * 100, 2)
    bench_cum_pct = round((bench_cum - 1) * 100, 2)

    # Latest rolling metrics
    latest = conn = sqlite3.connect(_TRADES_DB)
    lr = latest.execute(
        "SELECT rolling_alpha_60d, rolling_ir_60d, rolling_beta_60d, "
        "up_capture_60d, down_capture_60d FROM benchmark_tracking "
        "WHERE rolling_alpha_60d IS NOT NULL ORDER BY date DESC LIMIT 1"
    ).fetchone()
    latest.close()

    result = {
        "available": True,
        "cumulative": {
            "strategy_pct": strat_cum_pct,
            "benchmark_pct": bench_cum_pct,
            "alpha_pct": round(strat_cum_pct - bench_cum_pct, 2),
        },
        "benchmark_code": "000300",
        "benchmark_name": "\u6caa\u6df1300",
        "curves": curves[-120:],
    }

    if lr:
        result["latest_rolling"] = {
            "alpha_60d": lr[0],
            "ir_60d": lr[1],
            "beta_60d": lr[2],
            "up_capture_60d": lr[3],
            "down_capture_60d": lr[4],
        }

    return result


class BenchmarkTracker:
    """Singleton-style tracker for daily benchmark comparison."""

    def __init__(self):
        self._yesterday_equity = None
        _ensure_table()

    def record(self, date_str: str, total_wealth: float):
        record_daily(date_str, total_wealth, self._yesterday_equity)
        self._yesterday_equity = total_wealth
