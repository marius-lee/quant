"""Backtest strategy naming — auto-increment convention.

Convention:
  backtest_1, backtest_2, ...  — full backtests
  smoke_1, smoke_2, ...        — quick smoke tests

Queries strategy_config table to find the next available number.
"""

import os, sqlite3

_TRADES_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trades.db")


def next_name(prefix: str) -> str:
    """Return the next available name for the given prefix.

    Queries strategy_config for names matching {prefix}_% and returns
    {prefix}_{max_N + 1}. Returns {prefix}_1 if no matches exist.

    Example:
        next_name("backtest")  → "backtest_3"  (if backtest_1, backtest_2 exist)
        next_name("smoke")     → "smoke_1"     (if no smoke_* exists)
    """
    conn = sqlite3.connect(_TRADES_DB)
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
    """Next backtest strategy name."""
    return next_name("backtest")


def next_smoke_name() -> str:
    """Next smoke test strategy name."""
    return next_name("smoke")
