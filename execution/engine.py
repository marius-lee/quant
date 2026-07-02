"""执行引擎 — 模拟订单执行 + 交易记录持久化。

状态: trades.db (交易唯一真相源)
依赖: execution/cost.py 成本模型
"""

import os
import sqlite3
from datetime import date as date_type
from typing import Optional
from dataclasses import dataclass
from execution.cost import CostModel


TRADE_DB_DEFAULT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trades.db")


@dataclass
class Order:
    """模拟订单。"""
    symbol: str
    side: str     # buy | sell
    shares: int
    price: float
    cost: float = 0.0


class ExecutionEngine:
    """模拟执行引擎: 订单执行 → trades.db, 更新 capital_after。"""

    def __init__(self, db_path: str = None, cost_model: CostModel = None):
        self.db_path = db_path or TRADE_DB_DEFAULT
        self.cost_model = cost_model or CostModel()
        self._ensure_schema()

    def _ensure_schema(self):
        conn = sqlite3.connect(self.db_path)
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS sim_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                shares INTEGER NOT NULL,
                pnl REAL DEFAULT 0,
                pnl_pct REAL DEFAULT 0,
                capital_after REAL DEFAULT 0,
                strategy TEXT DEFAULT 'quant',
                board_count INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS strategy_config (
                strategy TEXT PRIMARY KEY,
                initial_capital REAL NOT NULL
            );
        """)
        conn.commit()
        conn.close()

    def get_capital(self, strategy: str = "quant") -> float:
        """获取当前策略总资产 (现金 + 持仓市值)。"""
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT capital_after FROM sim_trades WHERE strategy=? ORDER BY id DESC LIMIT 1",
                (strategy,)
            ).fetchone()
            if row and row[0] is not None:
                cash = row[0]
                # 加上持仓市值
                positions = conn.execute("""
                    SELECT symbol, price, shares FROM sim_trades
                    WHERE side='buy' AND strategy=? AND symbol NOT IN (
                        SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?
                    )
                """, (strategy, strategy)).fetchall()
                pos_value = sum(p[1] * p[2] for p in positions)
                return cash + pos_value
            # 无交易记录: 回退到 strategy_config
            row2 = conn.execute(
                "SELECT initial_capital FROM strategy_config WHERE strategy=?",
                (strategy,)
            ).fetchone()
            if row2 and row2[0] is not None:
                return row2[0]
        finally:
            conn.close()
        from config.loader import get as cfg
        return cfg("backtest.initial_capital", 5000)

    def get_cash(self, strategy: str = "quant") -> float:
        """获取当前现金余额 (不含持仓市值)。用于执行引擎成本计算。"""
        conn = sqlite3.connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT capital_after FROM sim_trades WHERE strategy=? ORDER BY id DESC LIMIT 1",
                (strategy,)
            ).fetchone()
            if row and row[0] is not None:
                return row[0]
            # 无交易记录: 回退到 strategy_config
            row2 = conn.execute(
                "SELECT initial_capital FROM strategy_config WHERE strategy=?",
                (strategy,)
            ).fetchone()
            if row2 and row2[0] is not None:
                return row2[0]
        finally:
            conn.close()
        from config.loader import get as cfg
        return cfg("backtest.initial_capital", 5000)

    def set_initial_capital(self, strategy: str, capital: float):
        """设置策略初始资金。"""
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR REPLACE INTO strategy_config(strategy, initial_capital) VALUES(?,?)",
            (strategy, capital),
        )
        conn.commit()
        conn.close()

    def execute(
        self,
        orders: list,
        date: str,
        strategy: str = "quant",
    ) -> int:
        """执行模拟交易。

        orders: [Order, ...] 或 [(symbol, side, shares, price), ...]
        date: 交易日期 (YYYY-MM-DD)
        strategy: 策略标识

        返回: 执行的订单数
        """
        capital = self.get_cash(strategy)  # 现金余额, 用于交易成本计算
        conn = sqlite3.connect(self.db_path)

        executed = 0
        for o in orders:
            # 兼容 tuple 和 Order 两种格式
            if isinstance(o, (list, tuple)):
                symbol, side, shares, price = o[0], o[1], o[2], o[3]
            else:
                symbol, side, shares, price = o.symbol, o.side, o.shares, o.price

            if side == "buy":
                cost = self.cost_model.buy_cost(price, shares)
                capital -= cost
            else:
                # T+1 检查: 当日买入的股票不可卖出 (A股交易规则)
                same_day = conn.execute(
                    "SELECT COUNT(*) FROM sim_trades WHERE symbol=? AND side='buy' AND date=? AND strategy=?",
                    (symbol, date, strategy)
                ).fetchone()[0]
                if same_day > 0:
                    from utils.logger import get_logger
                    get_logger("execution.engine").warning(
                        f"T+1 blocked: {symbol} bought today, cannot sell until next trading day"
                    )
                    continue
                # 卖出: 先算卖出收入
                proceeds = self.cost_model.sell_proceeds(price, shares)
                # 查找原始买入价格计算 PnL
                orig = conn.execute(
                    "SELECT price, shares FROM sim_trades WHERE symbol=? AND side='buy' AND strategy=? ORDER BY id DESC LIMIT 1",
                    (symbol, strategy),
                ).fetchone()
                pnl = 0.0
                pnl_pct = 0.0
                if orig:
                    pnl = (price - orig[0]) * shares
                    pnl_pct = (price / orig[0] - 1) if orig[0] > 0 else 0.0
                capital += proceeds

            conn.execute(
                "INSERT INTO sim_trades(date, symbol, side, price, shares, pnl, pnl_pct, capital_after, strategy) VALUES(?,?,?,?,?,?,?,?,?)",
                (date, symbol, side, price, shares,
                 0.0 if side == "buy" else pnl,
                 0.0 if side == "buy" else pnl_pct,
                 round(capital, 2),
                 strategy),
            )
            executed += 1

        conn.commit()
        conn.close()

        from utils.logger import get_logger
        get_logger("execution.engine").info(
            f"executed {executed} orders, capital_after=¥{capital:,.2f}"
        )
        return executed

    def get_positions(self, strategy: str = "quant") -> list[dict]:
        """获取当前持仓列表。"""
        conn = sqlite3.connect(self.db_path)
        buys = conn.execute("""
            SELECT symbol, price, shares, date FROM sim_trades
            WHERE side='buy' AND strategy=? AND symbol NOT IN (
                SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=?
            )
            ORDER BY date
        """, (strategy, strategy)).fetchall()
        conn.close()
        return [
            {"symbol": r[0], "price": r[1], "shares": r[2], "date": r[3]}
            for r in buys
        ]

    def get_trades(self, strategy: str = "quant", limit: int = 50) -> list[dict]:
        """获取最近交易记录。"""
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(
            "SELECT date, symbol, side, price, shares, pnl, capital_after FROM sim_trades WHERE strategy=? ORDER BY id DESC LIMIT ?",
            (strategy, limit),
        ).fetchall()
        conn.close()
        return [
            {"date": r[0], "symbol": r[1], "side": r[2], "price": r[3],
             "shares": r[4], "pnl": r[5], "capital_after": r[6]}
            for r in rows
        ]
