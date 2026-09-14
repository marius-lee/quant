"""Gap 1: Event-driven backtesting loop — walk-forward simulation.

Runs the full pipeline day-by-day over a historical period, simulating
T+1 execution, commissions, lot-size constraints, and stop-losses.

Usage:
    from backtest import run_backtest
    result = run_backtest("2022-01-01", "2024-12-31", capital=5000)
    print(result["metrics"])
"""

from quant.core.phase_tracker import PhaseTracker, PhaseResult
import os, sys, time, uuid as _uuid
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
import traceback
from quant.utils.logger import get_logger, set_trace_id, offline_mode
from quant.backtest.analyze import FactorTracker, diagnose, apply_diagnosis
from quant.backtest.broker import SimulatedBroker
from quant.config.constants import _require_cfg
from quant.config import loader as cfgl
from quant.factor.stats_cache import compute_backtest_ic
from quant.alpha.model import AlphaModel
from quant.backtest.data_cache import get_or_load_backtest_data, _compute_cache_key

_log = get_logger("backtest.loop")

# Ensure project root on path
_root = os.path.dirname(os.path.dirname(__file__))
if _root not in sys.path:
    sys.path.insert(0, _root)
def _get_prices(symbols, date_str, store, field="open", data_full=None):
    """Get prices — fast path from preloaded data_full, fallback to DataStore DB.

    test-v398 (perf): 回测中 data_full 已预加载全量日线，直接从内存切片，
    消除每日期 4+ 次 SQLite round-trip。非回测路径回退 DB 查询。
    """
    syms = list(symbols)
    if not syms:
        return {}
    # Fast path: slice from preloaded multi-field DataFrame (field × symbol MultiIndex)
    if data_full is not None:
        try:
            if date_str in data_full.index and field in data_full.columns.get_level_values(0):
                series = data_full.loc[date_str, field]
                if hasattr(series, "dropna"):
                    series = series.dropna()
                return {s: float(v) for s, v in series.items()
                        if s in syms and v and v > 0}
        except (KeyError, TypeError, IndexError):
            pass  # fall through to DB path
    # Slow path: DB query (live / non-backtest / data_full miss)
    df = store.get_daily(syms, start=date_str, end=date_str, columns=[field])
    if df.empty or date_str not in df.index:
        return {}
    series = df.loc[date_str, field].dropna()
    return {s: float(v) for s, v in series.items() if v and v > 0}

BACKTEST_DB = os.path.join(_root, "data", "backtest_trades.db")


# 是不可达死代码 (from quant.backtest.loop import BacktestEngine 会 ImportError).
class BacktestEngine:
    """Convenience wrapper for parameterized backtesting."""

    def __init__(self, start="2022-01-01", end="2024-12-31", capital=5000):
        self.start = start
        self.end = end
        self.capital = capital

    def run(self):
        return run_backtest(self.start, self.end, self.capital)

    @property
    def default_params(self):
        return {"start": self.start, "end": self.end, "capital": self.capital}
