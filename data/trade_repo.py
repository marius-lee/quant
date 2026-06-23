"""TradeRepository — sim_trades 统一数据访问层.
消除跨8个文件的10+重复SQL查询.
"""
import sqlite3, os

TRADE_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trades.db")


class TradeRepo:
    def __init__(self, db_path: str = TRADE_DB):
        self._db = db_path

    def _conn(self): return sqlite3.connect(self._db)

    # ── 资金 ──
    def get_capital(self, strategy: str, fallback: float = 5000.0) -> float:
        c = self._conn()
        row = c.execute(
            "SELECT capital_after FROM sim_trades WHERE strategy=? AND capital_after IS NOT NULL ORDER BY id DESC LIMIT 1",
            (strategy,)).fetchone()
        c.close()
        return round(row[0], 2) if row else fallback

    # ── 持仓 ──
    def get_positions(self, strategy: str) -> list[dict]:
        c = self._conn()
        rows = c.execute(
            "SELECT symbol, price, shares, board_count, date FROM sim_trades WHERE side='buy' AND strategy=? AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?)",
            (strategy, strategy)).fetchall()
        c.close()
        return [{"symbol": r[0], "price": r[1], "shares": r[2], "board_count": r[3] or 0, "date": r[4]} for r in rows]

    def get_position_cost(self, strategy: str) -> float:
        c = self._conn()
        row = c.execute(
            "SELECT COALESCE(SUM(price*shares),0) FROM sim_trades WHERE side='buy' AND strategy=? AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?)",
            (strategy, strategy)).fetchone()
        c.close()
        return row[0]

    # ── 交易记录 ──
    def record_trade(self, date_str: str, symbol: str, side: str, price: float, shares: int, strategy: str = "chen", board_count: int = 0, capital_after: float = None, pnl: float = None, pnl_pct: float = None):
        c = self._conn()
        c.execute("INSERT INTO sim_trades (date,symbol,side,price,shares,board_count,pnl,pnl_pct,capital_after,strategy) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (date_str, symbol, side, price, shares, board_count, pnl, pnl_pct, capital_after, strategy))
        c.commit(); c.close()

    def get_trades(self, strategy: str = "", limit: int = 20) -> list[dict]:
        c = self._conn()
        if strategy:
            rows = c.execute("SELECT date,symbol,side,price,shares,pnl,pnl_pct FROM sim_trades WHERE strategy=? ORDER BY id DESC LIMIT ?", (strategy, limit)).fetchall()
        else:
            rows = c.execute("SELECT date,symbol,side,price,shares,pnl,pnl_pct FROM sim_trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        c.close()
        return [{"date": r[0], "symbol": r[1], "side": r[2], "price": r[3], "shares": r[4], "pnl": r[5], "pnl_pct": r[6]} for r in rows]

    def get_sells(self, strategy: str) -> list:
        c = self._conn()
        rows = c.execute("SELECT pnl FROM sim_trades WHERE side='sell' AND strategy=? AND pnl IS NOT NULL", (strategy,)).fetchall()
        c.close()
        return [r[0] for r in rows]

    def get_pnl(self, strategy: str) -> float:
        c = self._conn()
        row = c.execute("SELECT COALESCE(SUM(pnl),0) FROM sim_trades WHERE side='sell' AND strategy=? AND pnl IS NOT NULL", (strategy,)).fetchone()
        c.close()
        return row[0]

    # ── 统计 ──
    def get_counts(self, strategy: str) -> tuple:
        c = self._conn()
        buys = c.execute("SELECT COUNT(*) FROM sim_trades WHERE side='buy' AND strategy=?", (strategy,)).fetchone()[0]
        sells = c.execute("SELECT COUNT(*) FROM sim_trades WHERE side='sell' AND strategy=?", (strategy,)).fetchone()[0]
        win_trades = c.execute("SELECT COUNT(*) FROM sim_trades WHERE side='sell' AND strategy=? AND pnl>0", (strategy,)).fetchone()[0]
        c.close()
        return buys, sells, win_trades

    def get_date_range(self, strategy: str) -> tuple:
        c = self._conn()
        row = c.execute("SELECT MIN(date), MAX(date) FROM sim_trades WHERE strategy=?", (strategy,)).fetchone()
        c.close()
        return (row[0], row[1]) if row else (None, None)

    def has_trades_today(self, strategy: str, date_str: str) -> bool:
        c = self._conn()
        cnt = c.execute("SELECT COUNT(*) FROM sim_trades WHERE date=? AND strategy=?", (date_str, strategy)).fetchone()[0]
        c.close()
        return cnt > 0
