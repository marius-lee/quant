"""Backtest strategy naming — auto-increment convention.

Convention:
  backtest_1, backtest_2, ...  — full backtests
  smoke_1, smoke_2, ...        — quick smoke tests

Queries strategy_config table to find the next available number.
"""

import os, sqlite3

from quant.config.paths import TRADE_DB as _TRADES_DB
from quant.config.paths import BACKTEST_DB as _BACKTEST_DB


def next_name(prefix: str, db_path: str = None) -> str:
    """Return the next available name for the given prefix.

    Queries strategy_config for names matching {prefix}_% and returns
    {prefix}_{max_N + 1}. Returns {prefix}_1 if no matches exist.

    Args:
        prefix: strategy name prefix (e.g. "backtest", "smoke")
        db_path: database to query (TRADE_DB or BACKTEST_DB). Required.
    """
    if db_path is None:
        raise ValueError("db_path is required — use BACKTEST_DB for backtests, TRADE_DB for live")
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT strategy FROM strategy_config WHERE strategy LIKE ?",
            (f"{prefix}_%",)
        ).fetchall()
        max_n = 0
        for (name,) in rows:
            try:
                n = int(name[len(prefix) + 1:])
                max_n = max(max_n, n)
            except ValueError:
                continue
        return f"{prefix}_{max_n + 1}"
    finally:
        conn.close()


def next_backtest_name() -> str:
    """Next backtest strategy name. Queries BACKTEST_DB to avoid clobbering live strategy names."""
    return next_name("backtest", _BACKTEST_DB)


def next_smoke_name() -> str:
    """Next smoke test strategy name."""
    return next_name("smoke", _BACKTEST_DB)
