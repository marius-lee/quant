
"""TradeRepo — daily_signals, sim_trades, strategy_config CRUD."""

from __future__ import annotations

import json
import logging
from typing import Optional

from quant.data.repos._base import DatabaseManager, query_all, query_row, query_scalar

logger = logging.getLogger(__name__)


class TradeRepo:
    """Operations for trade-related tables (trades.db or backtest_trades.db)."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None,
                 db_path: str = "data/trades.db"):
        self.db = db_manager or DatabaseManager.get_instance()
        self.db_path = db_path

    def _conn(self):
        return self.db.get_connection(self.db_path)

    # ── daily_signals ──

    def save_daily_signals(self, date: str, signals_json: str,
                           strategy: str = "quant", capital: float = 0.0):
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO daily_signals (date, strategy, signals_json, capital, generated_at) "
            "VALUES (?, ?, ?, ?, datetime('now'))",
            (date, strategy, signals_json, capital))
        conn.commit()

    def get_daily_signals(self, date: str,
                          strategy: str = "quant") -> dict | None:
        conn = self._conn()
        row = query_row(conn,
            "SELECT signals_json, capital, generated_at FROM daily_signals "
            "WHERE date=? AND strategy=? ORDER BY generated_at DESC LIMIT 1",
            (date, strategy))
        if not row:
            return None
        return {
            "signals_json": row["signals_json"],
            "capital": row["capital"],
            "generated_at": row["generated_at"],
        }

    # ── strategy_config ──

    def get_strategy_config(self, strategy: str) -> dict:
        conn = self._conn()
        rows = query_all(conn,
            "SELECT key, value FROM strategy_config WHERE strategy=?",
            (strategy,))
        return {r["key"]: r["value"] for r in rows}

    def set_strategy_config(self, strategy: str, key: str, value):
        conn = self._conn()
        conn.execute(
            "INSERT OR REPLACE INTO strategy_config (strategy, key, value) VALUES (?, ?, ?)",
            (strategy, key, str(value)))
        conn.commit()

    # ── sim_trades ──

    def record_trade(self, strategy: str, date: str, symbol: str,
                     side: str, shares: int, price: float, capital: float):
        conn = self._conn()
        conn.execute(
            "INSERT INTO sim_trades (strategy, date, symbol, side, shares, price, capital) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (strategy, date, symbol, side, shares, price, capital))
        conn.commit()

    def get_trades(self, strategy: str,
                   start_date: str | None = None,
                   end_date: str | None = None) -> list[dict]:
        conn = self._conn()
        sql = "SELECT * FROM sim_trades WHERE strategy=?"
        params: list = [strategy]
        if start_date:
            sql += " AND date >= ?"
            params.append(start_date)
        if end_date:
            sql += " AND date <= ?"
            params.append(end_date)
        sql += " ORDER BY date"
        rows = query_all(conn, sql, tuple(params))
        return [dict(r) for r in rows]
