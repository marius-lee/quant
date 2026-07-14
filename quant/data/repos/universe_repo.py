
"""UniverseRepo — stocks, daily data, fundamentals queries."""

from __future__ import annotations

import logging
from typing import Optional

from quant.data.repos._base import DatabaseManager, query_all, query_scalar

logger = logging.getLogger(__name__)


class UniverseRepo:
    """Queries for stock universe, daily price data, and fundamentals."""

    def __init__(self, db_manager: Optional[DatabaseManager] = None,
                 db_path: str = "quant/data/market.db"):
        self.db = db_manager or DatabaseManager.get_instance()
        self.db_path = db_path

    def _conn(self):
        return self.db.get_connection(self.db_path)

    def get_symbols(self, exclude_market: str = "BJ") -> list[str]:
        """Get all stock symbols, optionally excluding a market."""
        conn = self._conn()
        rows = query_all(conn,
            "SELECT DISTINCT d.symbol FROM daily d "
            "JOIN stocks s ON d.symbol = s.symbol "
            "WHERE s.market != ?", (exclude_market,))
        return [r[0] for r in rows]

    def get_stock_markets(self) -> dict[str, str]:
        """Return {symbol: market} mapping."""
        conn = self._conn()
        rows = query_all(conn, "SELECT symbol, market FROM stocks")
        return {r["symbol"]: r["market"] for r in rows}

    def get_trading_dates(self, start: str, end: str) -> list[str]:
        """Get trading days in range."""
        conn = self._conn()
        rows = query_all(conn,
            "SELECT DISTINCT date FROM daily WHERE date >= ? AND date <= ? ORDER BY date",
            (start, end))
        return [r[0] for r in rows]

    def get_turnover_snapshot(self, symbols: list[str], date: str,
                              lookback_days: int = 20) -> dict[str, float]:
        """Return {symbol: avg_turnover} for ranking."""
        if not symbols:
            return {}
        conn = self._conn()
        ph = ",".join("?" * len(symbols))
        rows = query_all(conn,
            f"SELECT symbol, AVG(turnover) as avg_turn FROM daily "
            f"WHERE symbol IN ({ph}) AND date <= ? "
            f"GROUP BY symbol ORDER BY date DESC LIMIT ?",
            tuple(symbols) + (date, lookback_days * len(symbols)))
        return {r[0]: r[1] for r in rows if r[1]}

    def get_latest_trade_date(self) -> str | None:
        conn = self._conn()
        return query_scalar(conn, "SELECT MAX(date) FROM daily")
