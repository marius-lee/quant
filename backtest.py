"""多日回测 — 在历史数据上批量运行 pipeline，追踪 PnL 曲线。

 用法:
   PYTHONPATH=. python3 backtest.py                                    # 默认 2026-01-01 → 2026-06-30
   PYTHONPATH=. python3 backtest.py 2025-01-01 2026-06-30 5000        # 指定区间+本金
"""

import sys, os, time, json, sqlite3
from datetime import datetime, timedelta
from collections import defaultdict

import pandas as pd
import numpy as np

from config.loader import get as cfg
from data.store import DataStore
from execution.engine import ExecutionEngine
from data.benchmark import sync_benchmark
from utils.logger import get_logger

logger = get_logger("backtest")
TRADE_DB = os.path.join(os.path.dirname(__file__), "data", "trades.db")
LOT_SIZE = cfg("backtest.lot_size", 100)

# 回测区间最低交易日数 — Grinold & Kahn (1999): 60月≈250日, 量化策略评估最低线
# Lo (2002): SE(Sharpe) = √[(1+½S²)/T], T<1年时无统计价值
# min_backtest_days read from config below


def run_backtest(start_date=None, end_date=None, capital=None):
    """在 [start_date, end_date] 区间内逐日运行 pipeline。
    默认值来源: config.yaml backtest.default_* (单一真相源).

    返回: DataFrame(index=date, columns=[cash, positions_value, total_wealth, return])
    """
    if start_date is None:
        start_date = cfg("backtest.default_start", "2026-01-01")
    if end_date is None:
        end_date = cfg("backtest.default_end", "2026-06-30")
    if capital is None:
        capital = cfg("backtest.default_capital", 5000)

    import pipeline
    from execution.engine import Order  # P0-2: unified Order dataclass

    # 清理旧交易记录
    if os.path.exists(TRADE_DB):
        os.remove(TRADE_DB)

    store = DataStore()
    # 同步基准指数数据
    sync_benchmark(cfg("backtest.benchmark", "000300"))
    engine = ExecutionEngine()
    engine.set_initial_capital("quant", capital)

    # 获取区间内所有交易日
    all_dates = [r[0] for r in store._connect().execute(
        "SELECT DISTINCT date FROM daily WHERE date >= ? AND date <= ? ORDER BY date",
        (start_date, end_date)
    ).fetchall()]

    # 回测周期需覆盖至少 250 个交易日 (≈1年, 50次调仓) 以保证 Sharpe/IR 估计的统计意义
    # Grinold & Kahn (1999): 60月≈250日; Lo (2002): SE(Sharpe) = √[(1+½S²)/T]
    rebalance_interval = cfg("backtest.rebalance_interval_days", 5)
    rebalance_dates = all_dates[::rebalance_interval]  # 默认每5个交易日一次
    logger.info(f"Backtest: {len(all_dates)} trading days, {len(rebalance_dates)} rebalance dates")
    if len(all_dates) < cfg("backtest.min_backtest_days", 250):
        logger.warning(
            f"回测区间仅 {len(all_dates)} 个交易日 ({len(rebalance_dates)} 次调仓), "
            f"少于业界最低标准 {cfg("backtest.min_backtest_days", 250)} 天 (1年), Sharpe/IR 估计不可靠"
        )
        # 仍继续执行 — 短期回测仍有相对比较价值

