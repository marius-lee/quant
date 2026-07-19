
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

    def get_symbols(self, exclude_market: str = "BJ",
                  start_date: str = None, end_date: str = None) -> list[str]:
        """Get stock symbols, optionally filtered by date range to avoid survivorship bias.

        Args:
            exclude_market: market to exclude (default "BJ" = Beijing Exchange)
            start_date: only include stocks listed ON or BEFORE this date AND 
                        not delisted BEFORE this date (YYYY-MM-DD)
            end_date: only include stocks listed ON or BEFORE this date (YYYY-MM-DD)

        Delisted stocks are included if they had active trading during the period.
        """
        conn = self._conn()
        sql = ("SELECT DISTINCT d.symbol FROM daily d "
               "JOIN stocks s ON d.symbol = s.symbol "
               "WHERE s.market != ?")
        params = [exclude_market]
        
        if end_date:
            # exclude stocks that IPO'd after the backtest end
            sql += " AND (s.list_date IS NULL OR s.list_date <= ?)"
            params.append(end_date)
        
        if start_date:
            # exclude stocks that were already delisted before the backtest start
            sql += " AND (s.delist_date IS NULL OR s.delist_date > ?)"
            params.append(start_date)
        elif end_date:
            # if only end_date is provided, still filter delisted before end
            sql += " AND (s.delist_date IS NULL OR s.delist_date > ?)"
            params.append(end_date)
        
        rows = query_all(conn, sql, tuple(params))
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
        return query_scalar(conn, "SELECT MAX(date) FROM daily WHERE date >= '2000-01-01' AND date < '2100-01-01'")
