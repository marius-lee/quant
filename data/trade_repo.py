"""TradeRepository — sim_trades 统一数据访问层.
消除跨8个文件的10+重复SQL查询.
"""

from utils.logger import get_logger
logger = get_logger("data.trade_repo")
import sqlite3, os

TRADE_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trades.db")


class TradeRepo:
    def __init__(self, db_path: str = TRADE_DB):
        self._db = db_path
        self._ensure_tables()

    def _ensure_tables(self):
        """统一 schema 管理: sim_trades + strategy_config + migrations + indexes.
        所有模块通过此方法确保表存在，不再各自持有 DDL。"""
        c = self._conn()
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript("""
            CREATE TABLE IF NOT EXISTS sim_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL, symbol TEXT NOT NULL,
                side TEXT NOT NULL CHECK(side IN ('buy','sell')),
                price REAL NOT NULL, shares INTEGER NOT NULL,
                pnl REAL DEFAULT 0, pnl_pct REAL DEFAULT 0,
                capital_after REAL DEFAULT 0,
                strategy TEXT DEFAULT 'quant',
                board_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS strategy_config (
                strategy TEXT PRIMARY KEY,
                -- 策略参数默认值来源: config/config.yaml (单一真相源)
                --   initial_capital → backtest.default_capital=100000
                --   cash_balance    → 由 set_initial_capital() 同步写入 = initial_capital
                --   max_positions   → risk.max_positions=20
                --   stop_loss_pct   → risk.stop_loss_pct=0.15
                -- SQL DEFAULT 已移除; 所有写入均通过 TradeRepo API 显式传值.
                initial_capital REAL NOT NULL,
                cash_balance REAL NOT NULL,
                max_positions INTEGER,
                stop_loss_pct REAL,
                combine_mode TEXT DEFAULT 'sleeve',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
        """)
        # ── 迁移: 兼容旧 schema ──
        for col, typ in [('cash_balance', 'REAL'), ('initialized', 'INTEGER DEFAULT 0'), ('updated_at', 'TEXT')]:
            try:
                c.execute(f"ALTER TABLE strategy_config ADD COLUMN {col} {typ}")
            except Exception:
                pass
        c.execute("UPDATE strategy_config SET cash_balance = initial_capital WHERE cash_balance IS NULL")
        c.execute("UPDATE strategy_config SET initialized = 1 WHERE initialized IS NULL")
        # ── 索引: 消除全表扫描 ──
        c.executescript("""
            CREATE INDEX IF NOT EXISTS idx_st_strategy_id
                ON sim_trades(strategy, id);
            CREATE INDEX IF NOT EXISTS idx_st_positions
                ON sim_trades(strategy, side, symbol);
            CREATE INDEX IF NOT EXISTS idx_st_t1_check
                ON sim_trades(symbol, side, date, strategy);
        """)
        c.commit()
        c.close()

    def _conn(self): return sqlite3.connect(self._db)

    # ── 资金 ──
    def get_cash(self, strategy: str) -> float:
        """返回当前现金余额 — strategy_config.cash_balance (资金唯一真相源).

        首次启动时 cash_balance = initial_capital (¥5000).
        每次交易后自动更新.
        """
        c = self._conn()
        row = c.execute(
            "SELECT cash_balance, COALESCE(initialized,0) FROM strategy_config WHERE strategy=?",
            (strategy,)).fetchone()
        c.close()
        if row and row[0] is not None:
            return round(float(row[0]), 2)
        return 0.0

    def is_initialized(self, strategy: str = "quant") -> bool:
        """策略是否已初始化 (initial_capital 已写入且不应被覆盖)。"""
        c = self._conn()
        row = c.execute(
            "SELECT COALESCE(initialized,0) FROM strategy_config WHERE strategy=?",
            (strategy,)).fetchone()
        c.close()
        return bool(row and row[0])

    def get_initial_capital(self, strategy: str = "quant") -> float:
        """读取种子本金 (strategy_config 表, 启动时写入一次)。"""
        c = self._conn()
        row = c.execute(
            "SELECT initial_capital FROM strategy_config WHERE strategy=?",
            (strategy,)).fetchone()
        c.close()
        return float(row[0]) if row else 0.0
    def set_initial_capital(self, strategy: str, capital: float):
        """设置种子本金 (同时初始化现金余额)。"""
        c = self._conn()
        c.execute(
            "INSERT OR REPLACE INTO strategy_config (strategy, initial_capital, cash_balance, initialized, updated_at) VALUES (?, ?, ?, 1, datetime('now'))",
            (strategy, capital, capital))
        c.commit(); c.close()
        logger.info(f"[capital] {strategy} initial_capital=cash_balance={capital}")

    # ── 持仓 ──
    def get_positions(self, strategy: str) -> list[dict]:
        c = self._conn()
        buys = c.execute(
            "SELECT symbol, SUM(shares), SUM(price*shares)/SUM(shares), MAX(board_count), MIN(created_at) FROM sim_trades WHERE side='buy' AND strategy=? GROUP BY symbol",
            (strategy,)).fetchall()
        sells = c.execute(
            "SELECT symbol, SUM(shares) FROM sim_trades WHERE side='sell' AND strategy=? GROUP BY symbol",
            (strategy,)).fetchall()
        sell_map = {r[0]: r[1] for r in sells}
        c.close()
        return [{"symbol": r[0], "price": round(r[2],4) if r[2] else 0, "shares": max(0, r[1] - sell_map.get(r[0], 0)), "board_count": r[3] or 0, "buy_time": r[4]} for r in buys if r[1] > sell_map.get(r[0], 0)]


    # ── 交易记录 ──
    def record_trade(self, trade: dict, strategy: str = "chen"):
        """记录一笔交易。trade 字典包含: date, symbol, side, price, shares, [board_count, pnl, pnl_pct]"""
        date_str = trade.get("date", "")
        symbol = trade.get("symbol", "")
        side = trade.get("side", "")
        price = float(trade.get("price", 0))
        shares = int(trade.get("shares", 0))
        board_count = int(trade.get("board_count", 0))
        pnl = trade.get("pnl")
        pnl_pct = trade.get("pnl_pct")
        logger.info(f"[trade] {date_str} {side} {symbol} {shares}@{price}")
        c = self._conn()
        c.execute("INSERT INTO sim_trades (date,symbol,side,price,shares,board_count,pnl,pnl_pct,strategy) VALUES (?,?,?,?,?,?,?,?,?)",
                  (date_str, symbol, side, price, shares, board_count, pnl, pnl_pct, strategy))
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


    # ── 交易辅助查询 ──
    def check_t1(self, strategy: str, symbol: str, date: str) -> bool:
        """T+1 检查: 当日是否已有该 symbol 的买入。"""
        c = self._conn()
        cnt = c.execute(
            "SELECT COUNT(*) FROM sim_trades WHERE symbol=? AND side='buy' AND date=? AND strategy=?",
            (symbol, date, strategy)
        ).fetchone()[0]
        c.close()
        return cnt > 0

    def get_last_buy_price(self, strategy: str, symbol: str) -> tuple | None:
        """返回最近一次买入的 (price, shares)，用于 PnL 计算。"""
        c = self._conn()
        row = c.execute(
            "SELECT price, shares FROM sim_trades WHERE symbol=? AND side='buy' AND strategy=? ORDER BY id DESC LIMIT 1",
            (symbol, strategy)
        ).fetchone()
        c.close()
        return (float(row[0]), int(row[1])) if row else None

    def record_trade(self, strategy: str, date: str, symbol: str,
                     side: str, price: float, shares: int,
                     pnl: float = 0.0, pnl_pct: float = 0.0,
                     board_count: int = 0, cost: float = 0.0) -> None:
        """写入一笔交易并原子更新现金余额 (单连接事务)。"""
        c = self._conn()
        # 读取当前现金
        cash_row = c.execute(
            "SELECT cash_balance FROM strategy_config WHERE strategy=?",
            (strategy,)
        ).fetchone()
        cash = float(cash_row[0]) if cash_row and cash_row[0] is not None else 0.0

        if side == 'buy':
            cash -= price * shares + cost
        else:
            cash += price * shares - cost

        c.execute(
            "INSERT INTO sim_trades(date, symbol, side, price, shares, pnl, pnl_pct, strategy, board_count) VALUES(?,?,?,?,?,?,?,?,?)",
            (date, symbol, side, price, shares, pnl, pnl_pct, strategy, board_count)
        )
        c.execute(
            "UPDATE strategy_config SET cash_balance = ?, updated_at = datetime('now') WHERE strategy = ?",
            (round(cash, 2), strategy)
        )
        c.commit()
        c.close()


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
