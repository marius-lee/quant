"""策略基类 — 所有独立策略的公共接口.
消除 etf/smallcap/timing 的 record_trade/get_state/_get_realtime 重复 (60行).
"""
import sqlite3, os
from abc import ABC, abstractmethod
from datetime import date

TRADE_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trades.db")
MARKET_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")
INITIAL_CAPITAL = 5000.0


class Strategy(ABC):
    """独立策略基类. 子类实现 get_signal() 即可."""
    STRATEGY: str = ""

    def __init__(self):
        pass

    @abstractmethod
    def get_signal(self) -> dict:
        """返回买入信号."""
        ...

    def _capital(self) -> float:
        """从 sim_trades 读当前可用资金."""
        c = sqlite3.connect(TRADE_DB)
        row = c.execute(
            "SELECT capital_after FROM sim_trades WHERE strategy=? AND capital_after IS NOT NULL ORDER BY id DESC LIMIT 1",
            (self.STRATEGY,)).fetchone()
        c.close()
        return round(row[0], 2) if row else self._initial_capital()

    def _initial_capital(self) -> float:
        """从 strategy_config 读本金."""
        from config.loader import get as cfg
        try:
            c = sqlite3.connect(TRADE_DB)
            row = c.execute("SELECT initial_capital FROM strategy_config WHERE strategy=?", (self.STRATEGY,)).fetchone()
            c.close()
            return round(row[0], 2) if row else float(cfg("backtest.initial_capital", 5000))
        except Exception:
            return float(cfg("backtest.initial_capital", 5000))

    def record_trade(self, symbol: str, price: float, shares: int, side: str = "buy"):
        """统一交易记录."""
        c = sqlite3.connect(TRADE_DB)
        pnl = None
        if side == "sell":
            buy = c.execute(
                "SELECT price, shares FROM sim_trades WHERE symbol=? AND side='buy' AND strategy=? ORDER BY id DESC LIMIT 1",
                (symbol, self.STRATEGY)).fetchone()
            if buy:
                fee = max(price * shares * 0.0003, 5) + price * shares * 0.001
                buy_fee = max(buy[0] * buy[1] * 0.0003, 5)
                pnl = round((price - buy[0]) * shares - fee - buy_fee, 2)
        curr_cap = self._capital() if side == "buy" else self._capital()
        capital_after = None
        if side == "buy":
            capital_after = round(curr_cap - price * shares - max(price * shares * 0.0003, 5), 2)
        elif side == "sell" and pnl is not None:
            capital_after = round(curr_cap + price * shares - max(price * shares * 0.0003, 5) - price * shares * 0.001, 2)

        c.execute("INSERT INTO sim_trades (date,symbol,side,price,shares,strategy,pnl,capital_after) VALUES (?,?,?,?,?,?,?,?)",
                  (date.today().isoformat(), symbol, side, price, shares, self.STRATEGY, pnl, capital_after))
        c.commit(); c.close()
        return pnl

    def _get_positions(self) -> list:
        c = sqlite3.connect(TRADE_DB)
        rows = c.execute(
            "SELECT symbol, price, shares FROM sim_trades WHERE side='buy' AND strategy=? AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?)",
            (self.STRATEGY, self.STRATEGY)).fetchall()
        c.close()
        return rows

    def _get_realtime(self, symbols: list) -> dict:
        try:
            from execution.quote import fetch_quotes
            return fetch_quotes(symbols)
        except Exception:
            return {}

    def get_state(self) -> dict:
        """通用 get_state — 子类可覆写."""
        pos = self._get_positions()
        pnl = self._get_pnl()
        symbols = [r[0] for r in pos]
        quotes = self._get_realtime(symbols) if symbols else {}
        positions = []
        for r in pos:
            sym, cost, shares = r[0], r[1], r[2]
            q = quotes.get(sym, {})
            cur = q.get("price", cost) if q else cost
            positions.append({
                "symbol": sym, "name": q.get("name", ""), "shares": shares,
                "price": cost, "current": round(cur, 2),
                "pnl_pct": round((cur / cost - 1) * 100, 2),
                "value": round(shares * cur, 2),
            })
        capital = self._capital()
        pos_value = sum(p["value"] for p in positions)
        return {
            "positions": positions, "realized_pnl": round(pnl, 2),
            "signal": self.get_signal(),
            "capital": round(capital, 2),
            "total_asset": round(capital + pos_value, 2),
        }

    def _get_pnl(self) -> float:
        c = sqlite3.connect(TRADE_DB)
        row = c.execute("SELECT COALESCE(SUM(pnl),0) FROM sim_trades WHERE side='sell' AND strategy=?", (self.STRATEGY,)).fetchone()
        c.close()
        return row[0]

    def get_recent_sells(self, limit: int = 5) -> list:
        c = sqlite3.connect(TRADE_DB)
        rows = c.execute("SELECT pnl FROM sim_trades WHERE side='sell' AND strategy=? AND pnl IS NOT NULL ORDER BY id DESC LIMIT ?", (self.STRATEGY, limit)).fetchall()
        c.close()
        return [r[0] for r in rows]

    def affordable_lots(self, capital: float, price: float) -> int:
        return int(capital / (price * 100 + max(price * 100 * 0.0003, 5)))
