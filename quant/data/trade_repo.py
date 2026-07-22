"""TradeRepository — sim_trades 统一数据访问层.
消除跨8个文件的10+重复SQL查询.
"""

from quant.utils.logger import get_logger
logger = get_logger("data.trade_repo")
import sqlite3
from quant.data.repos._base import DatabaseManager
import os as _os_root

_PROJECT_ROOT = _os_root.path.dirname(_os_root.path.dirname(_os_root.path.dirname(_os_root.path.abspath(__file__))))


TRADE_DB = _os_root.path.join(_PROJECT_ROOT, "quant", "data", "trades.db")
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
                mode TEXT DEFAULT 'live',
                board_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS pending_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT NOT NULL DEFAULT 'quant',
                symbol TEXT NOT NULL,
                side TEXT NOT NULL DEFAULT 'buy',
                target_shares INTEGER NOT NULL,
                limit_price REAL NOT NULL,
                reference_price REAL,
                status TEXT NOT NULL DEFAULT 'pending',
                placed_at TEXT NOT NULL,
                filled_at TEXT,
                filled_shares INTEGER DEFAULT 0,
                filled_price REAL,
                chase_count INTEGER DEFAULT 0,
                day TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS strategy_config (
                strategy TEXT PRIMARY KEY,
                -- 策略参数默认值来源: config/config.yaml (单一真相源)
                --   initial_capital → backtest.default_capital=100000
                --   max_positions   → risk.max_positions=20
                --   stop_loss_pct   → risk.stop_loss_pct=0.15
                -- SQL DEFAULT 已移除; 所有写入均通过 TradeRepo API 显式传值.
                initial_capital REAL NOT NULL,
                max_positions INTEGER,
                stop_loss_pct REAL,
                combine_mode TEXT DEFAULT 'sleeve',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
        """)
        # ── 迁移: 兼容旧 schema (无 mode 列) ──
        try:
            c.execute("ALTER TABLE sim_trades ADD COLUMN mode TEXT DEFAULT 'live'")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            c.execute("ALTER TABLE sim_trades ADD COLUMN cost REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # ── 迁移: daily_signals 兼容旧 schema (无 mode 列) ──
        try:
            c.execute("ALTER TABLE daily_signals ADD COLUMN mode TEXT DEFAULT 'live'")
        except sqlite3.OperationalError:
            pass  # column already exists
        try:
            c.execute("ALTER TABLE pending_orders ADD COLUMN cancel_reason TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        try:
            c.execute("ALTER TABLE daily_signals ADD COLUMN exec_notes TEXT DEFAULT '{}'")
        except sqlite3.OperationalError:
            pass
        sc_cols = {r[1] for r in c.execute("PRAGMA table_info(strategy_config)").fetchall()}
        if 'mode' not in sc_cols:
            c.executescript('''
                CREATE TABLE strategy_config_new (
                    strategy TEXT NOT NULL, mode TEXT NOT NULL DEFAULT \'live\',
                    initial_capital REAL NOT NULL, cash_balance REAL,
                    initialized INTEGER DEFAULT 0, updated_at TEXT,
                    PRIMARY KEY (strategy, mode)
                );
                INSERT OR IGNORE INTO strategy_config_new (strategy, initial_capital, cash_balance, initialized, updated_at)
                    SELECT strategy, initial_capital, cash_balance, COALESCE(initialized,0), updated_at FROM strategy_config;
                DROP TABLE strategy_config;
                ALTER TABLE strategy_config_new RENAME TO strategy_config;
            ''')
        # ── 迁移: 删除 cash_balance 列 (不再使用, 由 get_cash() 实时计算) ──
        if 'cash_balance' in sc_cols:
            c.executescript('''
                CREATE TABLE strategy_config_new2 (
                    strategy TEXT NOT NULL, mode TEXT NOT NULL DEFAULT \'live\',
                    initial_capital REAL NOT NULL,
                    initialized INTEGER DEFAULT 0, updated_at TEXT,
                    PRIMARY KEY (strategy, mode)
                );
                INSERT INTO strategy_config_new2 (strategy, mode, initial_capital, initialized, updated_at)
                    SELECT strategy, mode, initial_capital, COALESCE(initialized,0), updated_at FROM strategy_config;
                DROP TABLE strategy_config;
                ALTER TABLE strategy_config_new2 RENAME TO strategy_config;
            ''')
        # ── 每日权益快照 — 持久化回撤追踪 (2026-07-21 audit H3 scheme B) ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_equity (
                date TEXT PRIMARY KEY,
                cash REAL,
                position_value REAL,
                total_equity REAL,
                drawdown_pct REAL,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        c.execute("UPDATE strategy_config SET initialized = 1 WHERE initialized IS NULL OR initialized = 0")
        # ── 每日信号持久化 ──
        c.execute("""
            CREATE TABLE IF NOT EXISTS daily_signals (
                date        TEXT PRIMARY KEY,
                strategy    TEXT DEFAULT 'quant',
                signals_json TEXT NOT NULL,
                capital     REAL,
                generated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # ── 索引: 消除全表扫描 ──
        c.executescript("""
            CREATE INDEX IF NOT EXISTS idx_st_strategy_id
                ON sim_trades(strategy, id);
            CREATE INDEX IF NOT EXISTS idx_st_positions
                ON sim_trades(strategy, side, symbol);
            CREATE INDEX IF NOT EXISTS idx_st_t1_check
                ON sim_trades(symbol, side, date, strategy);
            CREATE INDEX IF NOT EXISTS idx_po_status
                ON pending_orders(status, day);
        """)
        c.commit()
        c.close()

    def _conn(self): return sqlite3.connect(self._db)

    # ── 资金 ──
    def get_cash(self, strategy: str, mode: str = 'live') -> float:
        """返回当前现金余额 — 始终从 sim_trades 计算 (单一真相源).

        cash = initial_capital + SUM(sell: price*shares - cost) - SUM(buy: price*shares + cost)
        """
        c = self._conn()
        row = c.execute(
            "SELECT COALESCE(initial_capital,0) FROM strategy_config WHERE strategy=?",
            (strategy,)).fetchone()
        initial = float(row[0]) if row else 0.0
        buys = c.execute(
            "SELECT COALESCE(SUM(price*shares + COALESCE(cost,0)), 0) FROM sim_trades "
            "WHERE side='buy' AND strategy=? AND mode=?",
            (strategy, mode)).fetchone()[0]
        sells = c.execute(
            "SELECT COALESCE(SUM(price*shares - COALESCE(cost,0)), 0) FROM sim_trades "
            "WHERE side='sell' AND strategy=? AND mode=?",
            (strategy, mode)).fetchone()[0]
        c.close()
        return round(initial + float(sells) - float(buys), 2)

    def is_initialized(self, strategy: str = "quant", mode: str = "live") -> bool:
        """策略是否已初始化 (initial_capital 已写入且不应被覆盖)。"""
        c = self._conn()
        row = c.execute(
            "SELECT COALESCE(initialized,0) FROM strategy_config WHERE strategy=?",
            (strategy,)).fetchone()
        c.close()
        return bool(row and row[0])

    def get_initial_capital(self, strategy: str = "quant", mode: str = "live") -> float:
        """读取种子本金 (strategy_config 表, 启动时写入一次)。"""
        c = self._conn()
        row = c.execute(
            "SELECT initial_capital FROM strategy_config WHERE strategy=?",
            (strategy,)).fetchone()
        c.close()
        return float(row[0]) if row else 0.0
    def set_initial_capital(self, strategy: str, capital: float, mode: str = "live"):
        """设置种子本金 (同时初始化现金余额)。"""
        c = self._conn()
        c.execute(
            "INSERT OR REPLACE INTO strategy_config (strategy, mode, initial_capital, initialized, updated_at) VALUES (?, ?, ?, 1, datetime('now'))",
            (strategy, mode, capital))
        c.commit(); c.close()
        logger.info(f"[capital] {strategy}/{mode} initial_capital={capital}")

    # ── 持仓 ──
    def get_positions(self, strategy: str, mode: str = "live") -> list[dict]:
        c = self._conn()
        buys = c.execute(
            "SELECT symbol, SUM(shares), SUM(price*shares)/SUM(shares), MAX(board_count), MIN(datetime(created_at, 'localtime')) FROM sim_trades WHERE side='buy' AND strategy=? AND mode=? GROUP BY symbol",
            (strategy, mode)).fetchall()
        sells = c.execute(
            "SELECT symbol, SUM(shares) FROM sim_trades WHERE side='sell' AND strategy=? AND mode=? GROUP BY symbol",
            (strategy, mode)).fetchall()
        sell_map = {r[0]: r[1] for r in sells}
        c.close()
        return [{"symbol": r[0], "price": round(r[2],4) if r[2] else 0, "shares": max(0, r[1] - sell_map.get(r[0], 0)), "board_count": r[3] or 0, "buy_time": r[4]} for r in buys if r[1] > sell_map.get(r[0], 0)]


    # ── 交易查询 ──
    def get_trades(self, strategy: str = "", mode: str = "live", limit: int = 20) -> list[dict]:
        c = self._conn()
        if strategy:
            rows = c.execute("SELECT date,symbol,side,price,shares,pnl,pnl_pct,created_at FROM sim_trades WHERE strategy=? AND mode=? ORDER BY id DESC LIMIT ?", (strategy, mode, limit)).fetchall()
        else:
            rows = c.execute("SELECT date,symbol,side,price,shares,pnl,pnl_pct,created_at FROM sim_trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        c.close()
        return [{"date": r[0], "symbol": r[1], "side": r[2], "price": r[3], "shares": r[4], "pnl": r[5], "pnl_pct": r[6], "created_at": r[7]} for r in rows]

    def get_sells(self, strategy: str, mode: str = "live") -> list:
        c = self._conn()
        rows = c.execute("SELECT pnl FROM sim_trades WHERE side='sell' AND strategy=? AND mode=? AND pnl IS NOT NULL", (strategy, mode)).fetchall()
        c.close()
        return [r[0] for r in rows]

    def get_pnl(self, strategy: str, mode: str = "live") -> float:
        c = self._conn()
        row = c.execute("SELECT COALESCE(SUM(pnl),0) FROM sim_trades WHERE side='sell' AND strategy=? AND mode=? AND pnl IS NOT NULL", (strategy, mode)).fetchone()
        c.close()
        return row[0]


    # ── 交易辅助查询 ──
    def check_t1(self, strategy: str, symbol: str, date: str, mode: str = "live") -> bool:
        """T+1 检查: 当日是否已有该 symbol 的买入。"""
        c = self._conn()
        cnt = c.execute(
            "SELECT COUNT(*) FROM sim_trades WHERE symbol=? AND side='buy' AND date=? AND strategy=? AND mode=?",
            (symbol, date, strategy, mode)
        ).fetchone()[0]
        c.close()
        return cnt > 0

    def record_daily_equity(self, date: str, cash: float, position_value: float, strategy: str = "quant"):
        """写入每日权益快照, 含 peak-to-trough 回撤计算 (2026-07-21 audit H3).

        Args:
            date: YYYY-MM-DD
            cash: 现金余额
            position_value: 持仓市值
            strategy: 策略名
        """
        c = self._conn()
        total = cash + position_value
        # 查历史最高权益计算回撤
        peak_row = c.execute(
            "SELECT MAX(total_equity) FROM daily_equity"
        ).fetchone()
        peak = max(peak_row[0] or total, total)
        dd_pct = round((peak - total) / peak * 100, 2) if peak > 0 else 0.0
        c.execute(
            "INSERT OR REPLACE INTO daily_equity (date, cash, position_value, total_equity, drawdown_pct) "
            "VALUES (?, ?, ?, ?, ?)",
            (date, cash, position_value, total, dd_pct)
        )
        c.commit()
        c.close()

    def get_max_drawdown(self, lookback_days: int = 60) -> float:
        """最近 N 天最大回撤 (peak-to-trough %), 从 daily_equity 读取."""
        c = self._conn()
        row = c.execute(
            "SELECT MAX(drawdown_pct) FROM daily_equity "
            "WHERE date >= date('now', ? || ' days')",
            (f"-{lookback_days}",)
        ).fetchone()
        c.close()
        return float(row[0]) if row and row[0] else 0.0

    def get_open_position_cost(self, strategy: str, mode: str = "live") -> float:
        """未平仓持仓总成本 = SUM(price*shares) for open buy positions.

        等价于 index() 中手动 SQL, 但走 TradeRepo 统一入口 (2026-07-21 audit M3).
        """
        c = self._conn()
        row = c.execute(
            "SELECT COALESCE(SUM(price*shares),0) FROM sim_trades "
            "WHERE side='buy' AND strategy=? AND mode=? "
            "AND symbol NOT IN (SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=? AND mode=?)",
            (strategy, mode, strategy, mode)
        ).fetchone()
        c.close()
        return float(row[0]) if row else 0.0

    def get_average_cost(self, strategy: str, symbol: str, mode: str = "live") -> float:
        """加权平均买入成本: 买入总额/买入总股数 (FIFO近似).

        替代 get_last_buy_price(LIFO), 多次买入时 PnL 更准确.
        来源: 2026-07-21 audit H7.
        """
        c = self._conn()
        row = c.execute(
            "SELECT SUM(price * shares) / NULLIF(SUM(shares), 0) FROM sim_trades "
            "WHERE symbol=? AND side='buy' AND strategy=? AND mode=?",
            (symbol, strategy, mode)
        ).fetchone()
        c.close()
        return float(row[0]) if row and row[0] else 0.0

    def get_last_buy_price(self, strategy: str, symbol: str, mode: str = "live") -> tuple | None:
        """返回最近一次买入的 (price, shares)，用于 PnL 计算。"""
        c = self._conn()
        row = c.execute(
            "SELECT price, shares FROM sim_trades WHERE symbol=? AND side='buy' AND strategy=? AND mode=? ORDER BY id DESC LIMIT 1",
            (symbol, strategy, mode)
        ).fetchone()
        c.close()
        return (float(row[0]), int(row[1])) if row else None

    def record_trade(self, strategy: str, date: str, symbol: str,
                     side: str, price: float, shares: int,
                     pnl: float = 0.0, pnl_pct: float = 0.0,
                     board_count: int = 0, cost: float = 0.0,
                     mode: str = "live",
                     conn: "sqlite3.Connection" = None) -> None:
        """写入交易到 sim_trades (单一真相源).

        现金余额由 get_cash() 实时计算, 不再维护 cash_balance 列。
        若 conn 提供，使用外部连接（调用者管理事务），内部不 commit/close。
        """
        own_conn = conn is None
        c = conn if conn is not None else self._conn()

        c.execute(
            "INSERT INTO sim_trades(date, symbol, side, price, shares, pnl, pnl_pct, strategy, board_count, mode, cost) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (date, symbol, side, price, shares, pnl, pnl_pct, strategy, board_count, mode, cost)
        )
        if own_conn:
            c.commit()
            c.close()


    # ── 统计 ──
    def get_counts(self, strategy: str, mode: str = "live") -> tuple:
        c = self._conn()
        buys = c.execute("SELECT COUNT(*) FROM sim_trades WHERE side='buy' AND strategy=? AND mode=?", (strategy, mode)).fetchone()[0]
        sells = c.execute("SELECT COUNT(*) FROM sim_trades WHERE side='sell' AND strategy=? AND mode=?", (strategy, mode)).fetchone()[0]
        win_trades = c.execute("SELECT COUNT(*) FROM sim_trades WHERE side='sell' AND strategy=? AND mode=? AND pnl>0", (strategy, mode)).fetchone()[0]
        c.close()
        return buys, sells, win_trades

    def get_date_range(self, strategy: str, mode: str = "live") -> tuple:
        c = self._conn()
        row = c.execute("SELECT MIN(date), MAX(date) FROM sim_trades WHERE strategy=? AND mode=?", (strategy, mode)).fetchone()
        c.close()
        return (row[0], row[1]) if row else (None, None)

    # ── daily_signals 持久化 ──
    def save_signals(self, date_str: str, targets: list, capital: float, strategy: str = "quant", mode: str = "live"):
        """持久化每日信号 (JSON), execute 阶段从此表读取."""
        import json as _json
        c = self._conn()
        c.execute(
            "INSERT OR REPLACE INTO daily_signals (date, strategy, signals_json, capital) VALUES (?, ?, ?, ?)",
            (date_str, strategy, _json.dumps(targets), capital)
        )
        c.commit()
        c.close()
        logger.info(f"[signals] saved {len(targets)} targets for {date_str} to daily_signals")

    def get_latest_signals(self, strategy: str = "quant", mode: str = "live") -> dict | None:
        """读取最近一天的信号 (用于手动补跑 execute)."""
        import json as _json
        c = self._conn()
        row = c.execute(
            "SELECT date, signals_json, capital FROM daily_signals WHERE strategy=? AND mode=? ORDER BY date DESC LIMIT 1",
            (strategy, mode)
        ).fetchone()
        c.close()
        if row:
            return {"date": row[0], "targets": _json.loads(row[1]), "capital": row[2]}
        return None

    def get_signal_exec_notes(self, date_str: str) -> dict:
        import json as _json
        c = self._conn()
        row = c.execute(
            "SELECT exec_notes FROM daily_signals WHERE date=? ORDER BY generated_at DESC LIMIT 1",
            (date_str,)
        ).fetchone()
        c.close()
        if row and row[0]:
            try:
                return _json.loads(row[0])
            except Exception:
                return {}
        return {}

    def update_signal_exec_note(self, date_str: str, symbol: str, note: str):
        import json as _json
        c = self._conn()
        row = c.execute(
            "SELECT exec_notes FROM daily_signals WHERE date=? ORDER BY generated_at DESC LIMIT 1",
            (date_str,)
        ).fetchone()
        if not row:
            c.close()
            return
        try:
            notes = _json.loads(row[0]) if row[0] else {}
        except Exception:
            notes = {}
        notes[symbol] = note
        c.execute(
            "UPDATE daily_signals SET exec_notes=? WHERE date=?",
            (_json.dumps(notes, ensure_ascii=False), date_str)
        )
        c.commit()
        c.close()

    def has_trades_today(self, strategy: str, date_str: str, mode: str = "live") -> bool:
        c = self._conn()
        cnt = c.execute("SELECT COUNT(*) FROM sim_trades WHERE date=? AND strategy=? AND mode=?", (date_str, strategy, mode)).fetchone()[0]
        c.close()
        return cnt > 0
