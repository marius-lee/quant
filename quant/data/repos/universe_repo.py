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
                  start_date: str = None, end_date: str = None,
                  exclude_st: bool = False,
                  exclude_new_stock_days: int = 0,
                  min_price: float = 0.0,
                  exclude_zero_turnover_days: int = 0,
                  min_daily_amount: float = 0.0) -> list[str]:
        """Get stock symbols with optional universe pre-filtering.

        Args:
            exclude_market: market to exclude (default "BJ" = Beijing Exchange)
            start_date: only include stocks listed ON or BEFORE this date AND
                        not delisted BEFORE this date (YYYY-MM-DD)
            end_date: only include stocks listed ON or BEFORE this date (YYYY-MM-DD)
            exclude_st: exclude *ST/ST stocks (实盘信号专用; 来源: 聚宽/米筐/BigQuant 默认)
            exclude_new_stock_days: exclude stocks listed < N days ago (实盘; 来源: 聚宽60天)
            min_price: minimum latest close price (实盘; 来源: 面值退市¥1+安全边际)
            exclude_zero_turnover_days: exclude if turnover=0 for last N days (实盘; 停牌/僵尸股)
            min_daily_amount: minimum latest daily amount in yuan (实盘; 来源: BigQuant 500万)

        Delisted stocks are included if they had active trading during the period.
        """
        conn = self._conn()
        sql = ("SELECT DISTINCT d.symbol FROM daily d "
               "JOIN stocks s ON d.symbol = s.symbol "
               "WHERE s.market != ?")
        params = [exclude_market]

        # ── 实盘 universe 预过滤 (SQL 层, 对齐业界标准) ──

        # ST/*ST 排除 — stocks.name 查询当前名称
        if exclude_st:
            sql += " AND (s.name IS NULL OR s.name NOT LIKE '%ST%')"

        # ── Compute reliable reference dates: MAX(date) may return dates with zero
        # turnover/close when data pull fails (e.g. akshare connection lost).
        # Use latest date where the relevant column has real values.
        _ref_close = query_scalar(conn,
            "SELECT COALESCE((SELECT MAX(date) FROM daily WHERE close > 0),"
            " (SELECT MAX(date) FROM daily))") or "2020-01-01"
        _ref_amount = query_scalar(conn,
            "SELECT COALESCE((SELECT MAX(date) FROM daily WHERE amount > 0),"
            " (SELECT MAX(date) FROM daily))") or "2020-01-01"
        _ref_turnover = query_scalar(conn,
            "SELECT COALESCE((SELECT MAX(date) FROM daily WHERE turnover > 0),"
            " (SELECT MAX(date) FROM daily))") or "2020-01-01"

        # 新股排除 — stocks.list_date
        if exclude_new_stock_days > 0:
            from datetime import datetime, timedelta
            cutoff = (datetime.now() - timedelta(days=exclude_new_stock_days)).strftime('%Y%m%d')
            sql += " AND (s.list_date IS NULL OR s.list_date <= ?)"
            params.append(cutoff)

        # 最低股价 — 用最新交易日 close (ref_close 避免数据拉取失败日 close=0 误判)
        if min_price > 0.0:
            sql += (" AND d.symbol IN (SELECT symbol FROM daily "
                    "WHERE date = ? AND close >= ?)")
            params.extend([_ref_close, min_price])

        # 连续换手率=0 排除 — 最近 N 天 turnover 之和>0 (ref_turnover 避免数据拉取失败日误判)
        if exclude_zero_turnover_days > 0:
            sql += (" AND d.symbol IN (SELECT symbol FROM daily "
                    "WHERE date >= date(?, ? || ' days') "
                    "GROUP BY symbol HAVING SUM(turnover) > 0)")
            params.extend([_ref_turnover, f"-{exclude_zero_turnover_days}"])

        # 最低日成交额 (ref_amount 避免数据拉取失败日 amount=0 误判)
        if min_daily_amount > 0.0:
            sql += (" AND d.symbol IN (SELECT symbol FROM daily "
                    "WHERE date = ? AND amount >= ?)")
            params.extend([_ref_amount, min_daily_amount])

        # ── 回测日期范围过滤 ──
        if end_date:
            sql += " AND (s.list_date IS NULL OR s.list_date <= ?)"
            params.append(end_date)

        if start_date:
            sql += " AND (s.delist_date IS NULL OR s.delist_date > ?)"
            params.append(start_date)
        elif end_date:
            sql += " AND (s.delist_date IS NULL OR s.delist_date > ?)"
            params.append(end_date)

        rows = query_all(conn, sql, tuple(params))
        return [r[0] for r in rows]

    def get_stock_markets(self) -> dict[str, str]:
        """Return {symbol: market} mapping."""
        conn = self._conn()
        rows = query_all(conn, "SELECT symbol, market FROM stocks")
        return {r["symbol"]: r["market"] for r in rows}

    def get_stock_info(self, symbols: list[str]) -> dict[str, dict]:
        """Return {symbol: {name, market, list_date, delist_date, ...}} for given symbols."""
        if not symbols:
            return {}
        ph = ",".join("?" * len(symbols))
        conn = self._conn()
        rows = query_all(conn,
            f"SELECT symbol, name, market, list_date, delist_date FROM stocks WHERE symbol IN ({ph})",
            tuple(symbols))
        return {r["symbol"]: dict(r) for r in rows}

    def get_all_stocks(self) -> list[dict]:
        """Return all stocks with metadata."""
        conn = self._conn()
        return [dict(r) for r in query_all(conn, "SELECT symbol, name, market, list_date, delist_date FROM stocks")]

    def rank_by_turnover(self, symbols: list[str], date_str: str,
                         lookback_days: int = 7, top_n: int = 800) -> list[str]:
        """Rank symbols by average turnover over lookback_days, return top_n."""
        if not symbols:
            return []
        ph = ",".join("?" * len(symbols))
        conn = self._conn()
        rows = conn.execute(
            f"SELECT symbol, AVG(turnover) as avg_to FROM daily "
            f"WHERE symbol IN ({ph}) AND date >= date(?, ? || ' days') AND date <= ? "
            f"GROUP BY symbol ORDER BY avg_to DESC LIMIT ?",
            tuple(symbols) + (date_str, f"-{lookback_days}", date_str, top_n)
        ).fetchall()
        return [r[0] for r in rows]
