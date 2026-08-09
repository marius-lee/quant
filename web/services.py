"""Web 服务层 (P2-5) — 将路由中的 SQL 封装到 Service 类。

Router 只做: request → service → response.
Service 层封装: SQL 查询 + 参数校验.

Services:
  PositionService     — 持仓 + 市值估值
  BacktestService     — 回测历史记录
  StockService       — 股票名称查找 (批量)
  SignalService      — 日度信号

所有 Service 方法接受 strategy 参数, 防止多策略数据交叉 (P1-20).
"""

import sqlite3
from typing import Optional

from quant.utils.logger import get_logger
from quant.config.paths import MARKET_DB, TRADE_DB, BACKTEST_DB

logger = get_logger("web.services")


class PositionService:
    """持仓服务 — 查询实盘持仓 + 市值估值。"""

    @staticmethod
    def get_live_positions(strategy: str = "quant") -> list[dict]:
        """查询指定策略的实盘持仓 (净多仓).

        Returns: [{symbol, shares, avg_cost}, ...]
        """
        conn = sqlite3.connect(TRADE_DB)
        try:
            rows = conn.execute(
                "SELECT symbol, SUM(CASE WHEN side='buy' THEN shares ELSE -shares END) AS net_shares"
                " FROM sim_trades WHERE strategy=? AND mode='live'"
                " GROUP BY symbol HAVING SUM(CASE WHEN side='buy' THEN shares ELSE -shares END) > 0",
                (strategy,)
            ).fetchall()
            return [{"symbol": r[0], "shares": r[1]} for r in rows]
        finally:
            conn.close()

    @staticmethod
    def estimate_position_value(strategy: str = "quant") -> float:
        """用最新收盘价估值持仓市值。"""
        positions = PositionService.get_live_positions(strategy)
        if not positions:
            return 0.0
        mc = sqlite3.connect(MARKET_DB)
        try:
            mc.execute("PRAGMA busy_timeout=3000")
            total = 0.0
            for pos in positions:
                cr = mc.execute(
                    "SELECT close FROM daily WHERE symbol=? ORDER BY date DESC LIMIT 1",
                    (pos["symbol"],)
                ).fetchone()
                if cr and cr[0] and cr[0] > 0:
                    total += cr[0] * pos["shares"]
            return round(total, 2)
        finally:
            mc.close()

    @staticmethod
    def get_portfolio_summary(strategy: str = "quant") -> dict:
        """完整投资组合摘要: 现金 + 持仓市值 + 总资产 + PnL."""
        from quant.data.repos import TradeRepo
        repo = TradeRepo()
        base = repo.get_initial_capital(strategy)
        capital = repo.get_cash(strategy) or base
        position_value = PositionService.estimate_position_value(strategy)
        total_asset = round(capital + position_value, 2)
        total_pnl = round(total_asset - base, 2)
        return {
            "total_pnl": total_pnl,
            "total_asset": total_asset,
            "initial_capital": base,
            "cash": round(capital, 2),
            "position_value": position_value,
        }


class BacktestService:
    """回测历史服务 — 查询 backtest_runs 表."""

    @staticmethod
    def get_history(limit: int = 20) -> list[dict]:
        """获取最近 N 条回测记录。"""
        conn = sqlite3.connect(BACKTEST_DB)
        try:
            rows = conn.execute(
                "SELECT strategy, start_date, end_date, initial_capital, "
                "sharpe, cagr_pct, max_dd_pct, sortino, calmar, win_rate, "
                "dsr, alpha, info_ratio, beta, "
                "final_equity, total_return_pct, n_days, errors, elapsed_sec, started_at "
                "FROM backtest_runs ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
            result = []
            for r in rows:
                result.append({
                    "strategy": r[0], "start": r[1], "end": r[2], "capital": r[3],
                    "sharpe": r[4], "cagr": r[5], "mdd": r[6],
                    "sortino": r[7], "calmar": r[8], "win_rate": r[9],
                    "dsr": r[10], "alpha": r[11], "ir": r[12], "beta": r[13],
                    "equity": r[14], "return_pct": r[15],
                    "days": r[16], "errors": r[17], "elapsed": r[18], "at": r[19],
                })
            return result
        finally:
            conn.close()


class StockService:
    """股票服务 — 批量名称查找。"""

    @staticmethod
    def get_names(symbols: list[str], conn: Optional[sqlite3.Connection] = None) -> dict[str, str]:
        """批量查询股票名称, 返回 {symbol: name}."""
        if not symbols:
            return {}
        own_conn = conn is None
        if own_conn:
            conn = sqlite3.connect(MARKET_DB)
        try:
            conn.execute("PRAGMA busy_timeout=3000")
            ph = ",".join("?" for _ in symbols)
            rows = conn.execute(
                f"SELECT symbol, name FROM stocks WHERE symbol IN ({ph})",
                symbols
            ).fetchall()
            return {r[0]: r[1] for r in rows}
        finally:
            if own_conn:
                conn.close()


class SignalService:
    """信号服务 — 查询 daily_signals 表."""

    @staticmethod
    def get_recent_signals(limit: int = 20) -> list[dict]:
        """获取最近 N 条信号记录."""
        from quant.data.repos import TradeRepo
        conn = TradeRepo()._conn()
        try:
            rows = conn.execute(
                "SELECT date, signals_json FROM daily_signals ORDER BY date DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [{"date": r[0], "signals": r[1]} for r in rows]
        finally:
            conn.close()
