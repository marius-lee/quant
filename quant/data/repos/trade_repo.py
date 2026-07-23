"""TradeRepo — sim_trades / pending_orders / strategy_config / daily_signals 统一数据访问层.

本项目唯一 TradeRepo。通过 DatabaseManager 单例获取连接，与其他 repos 类对齐。
所有模块通过 `from quant.data.repos import TradeRepo` 访问。
"""

from __future__ import annotations

import json as _json
import sqlite3

from quant.utils.logger import get_logger
from quant.data.repos._base import DatabaseManager
from quant.config.paths import TRADE_DB

logger = get_logger("repos.trade_repo")


class TradeRepo:
    """Trade data access — single source of truth for trades.db.

    Auto-creates schema on first init. Uses DatabaseManager for connection pooling.
    """

    def __init__(self, db_manager=None, db_path: str = None):
        self._db = db_manager or DatabaseManager.get_instance()
        self._path = db_path or TRADE_DB
        self._ensure_schema()

    def _conn(self):
        """Return DatabaseManager-managed connection (no per-call open/close)."""
        return self._db.get_connection(self._path)

    # ── Schema ──────────────────────────────────────────────

    def _ensure_schema(self):
        """Idempotent DDL + migrations. Called once per __init__."""
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
                cost REAL DEFAULT 0,
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
                cancel_reason TEXT DEFAULT '',
                day TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS strategy_config (
                strategy TEXT PRIMARY KEY,
                initial_capital REAL NOT NULL,
                max_positions INTEGER,
                stop_loss_pct REAL,
                combine_mode TEXT DEFAULT 'sleeve',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS daily_signals (
                date        TEXT PRIMARY KEY,
                strategy    TEXT DEFAULT 'quant',
                signals_json TEXT NOT NULL,
                capital     REAL,
                generated_at TEXT DEFAULT (datetime('now')),
                mode TEXT DEFAULT 'live',
                exec_notes TEXT DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS daily_equity (
                date TEXT PRIMARY KEY,
                cash REAL,
                position_value REAL,
                total_equity REAL,
                drawdown_pct REAL,
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS position_meta (
                symbol TEXT NOT NULL,
                day TEXT NOT NULL,
                tp1_hit INTEGER DEFAULT 0,
                peak_price REAL DEFAULT 0,
                PRIMARY KEY (symbol, day)
            );
        """)

        # ── Column migrations (idempotent via try/except) ──
        for col_sql in [
            "ALTER TABLE sim_trades ADD COLUMN mode TEXT DEFAULT 'live'",
            "ALTER TABLE sim_trades ADD COLUMN cost REAL DEFAULT 0",
            "ALTER TABLE daily_signals ADD COLUMN mode TEXT DEFAULT 'live'",
            "ALTER TABLE pending_orders ADD COLUMN cancel_reason TEXT DEFAULT ''",
            "ALTER TABLE daily_signals ADD COLUMN exec_notes TEXT DEFAULT '{}'",
        ]:
            try:
                c.execute(col_sql)
            except sqlite3.OperationalError:
                pass

        # ── strategy_config: add mode column if missing ──
        sc_cols = {r[1] for r in c.execute("PRAGMA table_info(strategy_config)").fetchall()}
        if 'mode' not in sc_cols:
            c.executescript('''
                CREATE TABLE strategy_config_new (
                    strategy TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'live',
                    initial_capital REAL NOT NULL,
                    initialized INTEGER DEFAULT 0, updated_at TEXT,
                    PRIMARY KEY (strategy, mode)
                );
                INSERT OR IGNORE INTO strategy_config_new (strategy, initial_capital, initialized, updated_at)
                    SELECT strategy, initial_capital, COALESCE(initialized,0), updated_at FROM strategy_config;
                DROP TABLE strategy_config;
                ALTER TABLE strategy_config_new RENAME TO strategy_config;
            ''')
        # Ensure initialized flag
        c.execute("UPDATE strategy_config SET initialized = 1 WHERE initialized IS NULL OR initialized = 0")

        # ── Indexes ──
        c.executescript("""
            CREATE INDEX IF NOT EXISTS idx_st_strategy_id ON sim_trades(strategy, id);
            CREATE INDEX IF NOT EXISTS idx_st_positions ON sim_trades(strategy, side, symbol);
            CREATE INDEX IF NOT EXISTS idx_st_t1_check ON sim_trades(symbol, side, date, strategy);
            CREATE INDEX IF NOT EXISTS idx_po_status ON pending_orders(status, day);
        """)
        c.commit()

    # ── Capital ─────────────────────────────────────────────

    def get_cash(self, strategy: str = "quant", mode: str = "live") -> float:
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
        return round(initial + float(sells) - float(buys), 2)

    def is_initialized(self, strategy: str = "quant", mode: str = "live") -> bool:
        c = self._conn()
        row = c.execute(
            "SELECT COALESCE(initialized,0) FROM strategy_config WHERE strategy=?",
            (strategy,)).fetchone()
        return bool(row and row[0])

    def get_initial_capital(self, strategy: str = "quant", mode: str = "live") -> float:
        c = self._conn()
        row = c.execute(
            "SELECT initial_capital FROM strategy_config WHERE strategy=?",
            (strategy,)).fetchone()
        return float(row[0]) if row else 0.0

    def set_initial_capital(self, strategy: str, capital: float, mode: str = "live"):
        c = self._conn()
        c.execute(
            "INSERT OR REPLACE INTO strategy_config (strategy, mode, initial_capital, initialized, updated_at) "
            "VALUES (?, ?, ?, 1, datetime('now'))",
            (strategy, mode, capital))
        c.commit()
        logger.info(f"[capital] {strategy}/{mode} initial_capital={capital}")

    # ── Positions ───────────────────────────────────────────

    def get_positions(self, strategy: str = "quant", mode: str = "live") -> list[dict]:
        c = self._conn()
        buys = c.execute(
            "SELECT symbol, SUM(shares), SUM(price*shares)/SUM(shares), MAX(board_count), "
            "MIN(datetime(created_at, 'localtime')) "
            "FROM sim_trades WHERE side='buy' AND strategy=? AND mode=? GROUP BY symbol",
            (strategy, mode)).fetchall()
        sells = c.execute(
            "SELECT symbol, SUM(shares) FROM sim_trades "
            "WHERE side='sell' AND strategy=? AND mode=? GROUP BY symbol",
            (strategy, mode)).fetchall()
        sell_map = {r[0]: r[1] for r in sells}
        return [
            {"symbol": r[0], "price": round(r[2], 4) if r[2] else 0,
             "shares": max(0, r[1] - sell_map.get(r[0], 0)),
             "board_count": r[3] or 0, "buy_time": r[4]}
            for r in buys if r[1] > sell_map.get(r[0], 0)
        ]

    # ── Trades ──────────────────────────────────────────────

    def get_trades(self, strategy: str = "", mode: str = "live", limit: int = 20) -> list[dict]:
        c = self._conn()
        if strategy:
            rows = c.execute(
                "SELECT date,symbol,side,price,shares,pnl,pnl_pct,created_at "
                "FROM sim_trades WHERE strategy=? AND mode=? ORDER BY id DESC LIMIT ?",
                (strategy, mode, limit)).fetchall()
        else:
            rows = c.execute(
                "SELECT date,symbol,side,price,shares,pnl,pnl_pct,created_at "
                "FROM sim_trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [
            {"date": r[0], "symbol": r[1], "side": r[2], "price": r[3],
             "shares": r[4], "pnl": r[5], "pnl_pct": r[6], "created_at": r[7]}
            for r in rows
        ]

    def get_sells(self, strategy: str, mode: str = "live") -> list:
        c = self._conn()
        rows = c.execute(
            "SELECT pnl FROM sim_trades "
            "WHERE side='sell' AND strategy=? AND mode=? AND pnl IS NOT NULL",
            (strategy, mode)).fetchall()
        return [r[0] for r in rows]

    def get_pnl(self, strategy: str, mode: str = "live") -> float:
        c = self._conn()
        row = c.execute(
            "SELECT COALESCE(SUM(pnl),0) FROM sim_trades "
            "WHERE side='sell' AND strategy=? AND mode=? AND pnl IS NOT NULL",
            (strategy, mode)).fetchone()
        return row[0]

    def check_t1(self, strategy: str, symbol: str, date: str, mode: str = "live") -> bool:
        c = self._conn()
        cnt = c.execute(
            "SELECT COUNT(*) FROM sim_trades "
            "WHERE symbol=? AND side='buy' AND date=? AND strategy=? AND mode=?",
            (symbol, date, strategy, mode)).fetchone()[0]
        return cnt > 0

    def has_trades_today(self, strategy: str, date_str: str, mode: str = "live") -> bool:
        c = self._conn()
        cnt = c.execute(
            "SELECT COUNT(*) FROM sim_trades "
            "WHERE date=? AND strategy=? AND mode=?",
            (date_str, strategy, mode)).fetchone()[0]
        return cnt > 0

    def get_counts(self, strategy: str, mode: str = "live") -> tuple:
        c = self._conn()
        buys = c.execute(
            "SELECT COUNT(*) FROM sim_trades "
            "WHERE side='buy' AND strategy=? AND mode=?", (strategy, mode)).fetchone()[0]
        sells = c.execute(
            "SELECT COUNT(*) FROM sim_trades "
            "WHERE side='sell' AND strategy=? AND mode=?", (strategy, mode)).fetchone()[0]
        win = c.execute(
            "SELECT COUNT(*) FROM sim_trades "
            "WHERE side='sell' AND strategy=? AND mode=? AND pnl>0",
            (strategy, mode)).fetchone()[0]
        return buys, sells, win

    def get_date_range(self, strategy: str, mode: str = "live") -> tuple:
        c = self._conn()
        row = c.execute(
            "SELECT MIN(date), MAX(date) FROM sim_trades "
            "WHERE strategy=? AND mode=?", (strategy, mode)).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def record_trade(self, strategy: str, date: str, symbol: str,
                     side: str, price: float, shares: int,
                     pnl: float = 0.0, pnl_pct: float = 0.0,
                     board_count: int = 0, cost: float = 0.0,
                     mode: str = "live",
                     conn: "sqlite3.Connection | None" = None) -> None:
        own_conn = conn is None
        c = conn if conn is not None else self._conn()
        c.execute(
            "INSERT INTO sim_trades(date, symbol, side, price, shares, "
            "pnl, pnl_pct, strategy, board_count, mode, cost) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (date, symbol, side, price, shares, pnl, pnl_pct,
             strategy, board_count, mode, cost))
        if own_conn:
            c.commit()

    # ── Cost / PnL helpers ──────────────────────────────────

    def get_last_buy_price(self, strategy: str, symbol: str, mode: str = "live") -> tuple | None:
        c = self._conn()
        row = c.execute(
            "SELECT price, shares FROM sim_trades "
            "WHERE symbol=? AND side='buy' AND strategy=? AND mode=? "
            "ORDER BY id DESC LIMIT 1",
            (symbol, strategy, mode)).fetchone()
        return (float(row[0]), int(row[1])) if row else None

    def get_average_cost(self, strategy: str, symbol: str, mode: str = "live") -> float:
        c = self._conn()
        row = c.execute(
            "SELECT SUM(price * shares) / NULLIF(SUM(shares), 0) FROM sim_trades "
            "WHERE symbol=? AND side='buy' AND strategy=? AND mode=?",
            (symbol, strategy, mode)).fetchone()
        return float(row[0]) if row and row[0] else 0.0

    def get_open_position_cost(self, strategy: str, mode: str = "live") -> float:
        c = self._conn()
        row = c.execute(
            "SELECT COALESCE(SUM(price*shares),0) FROM sim_trades "
            "WHERE side='buy' AND strategy=? AND mode=? "
            "AND symbol NOT IN ("
            "  SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=? AND mode=?"
            ")",
            (strategy, mode, strategy, mode)).fetchone()
        return float(row[0]) if row else 0.0

    # ── daily_equity ────────────────────────────────────────

    def record_daily_equity(self, date: str, cash: float, position_value: float,
                            strategy: str = "quant"):
        c = self._conn()
        total = cash + position_value
        peak_row = c.execute("SELECT MAX(total_equity) FROM daily_equity").fetchone()
        peak = max(peak_row[0] or total, total)
        dd_pct = round((peak - total) / peak * 100, 2) if peak > 0 else 0.0
        c.execute(
            "INSERT OR REPLACE INTO daily_equity (date, cash, position_value, total_equity, drawdown_pct) "
            "VALUES (?, ?, ?, ?, ?)",
            (date, cash, position_value, total, dd_pct))
        c.commit()

    def get_max_drawdown(self, lookback_days: int = 60) -> float:
        c = self._conn()
        row = c.execute(
            "SELECT MAX(drawdown_pct) FROM daily_equity "
            "WHERE date >= date('now', ? || ' days')",
            (f"-{lookback_days}",)).fetchone()
        return float(row[0]) if row and row[0] else 0.0

    # ── daily_signals ───────────────────────────────────────

    def save_signals(self, date_str: str, targets: list, capital: float,
                     strategy: str = "quant", mode: str = "live"):
        c = self._conn()
        c.execute(
            "INSERT OR REPLACE INTO daily_signals (date, strategy, signals_json, capital) "
            "VALUES (?, ?, ?, ?)",
            (date_str, strategy, _json.dumps(targets), capital))
        c.commit()
        logger.info(f"[signals] saved {len(targets)} targets for {date_str} to daily_signals")

    def get_latest_signals(self, strategy: str = "quant", mode: str = "live") -> dict | None:
        c = self._conn()
        row = c.execute(
            "SELECT date, signals_json, capital FROM daily_signals "
            "WHERE strategy=? AND mode=? ORDER BY date DESC LIMIT 1",
            (strategy, mode)).fetchone()
        if row:
            return {"date": row[0], "targets": _json.loads(row[1]), "capital": row[2]}
        return None

    def get_signal_exec_notes(self, date_str: str) -> dict:
        c = self._conn()
        row = c.execute(
            "SELECT exec_notes FROM daily_signals WHERE date=? ORDER BY generated_at DESC LIMIT 1",
            (date_str,)).fetchone()
        if row and row[0]:
            try:
                return _json.loads(row[0])
            except Exception:
                return {}
        return {}

    def update_signal_exec_note(self, date_str: str, symbol: str, note: str):
        c = self._conn()
        row = c.execute(
            "SELECT exec_notes FROM daily_signals WHERE date=? ORDER BY generated_at DESC LIMIT 1",
            (date_str,)).fetchone()
        if not row:
            return
        try:
            notes = _json.loads(row[0]) if row[0] else {}
        except Exception:
            notes = {}
        notes[symbol] = note
        c.execute(
            "UPDATE daily_signals SET exec_notes=? WHERE date=?",
            (_json.dumps(notes, ensure_ascii=False), date_str))
        c.commit()

    # ── position_meta ───────────────────────────────────────

    def save_position_meta(self, symbol: str, day: str, tp1_hit: bool, peak_price: float):
        c = self._conn()
        c.execute(
            "INSERT OR REPLACE INTO position_meta (symbol, day, tp1_hit, peak_price) "
            "VALUES (?, ?, ?, ?)",
            (symbol, day, int(tp1_hit), peak_price))
        c.commit()

    def get_position_meta(self, symbol: str, day: str) -> dict:
        c = self._conn()
        row = c.execute(
            "SELECT tp1_hit, peak_price FROM position_meta WHERE symbol=? AND day=?",
            (symbol, day)).fetchone()
        if row:
            return {"_tp1_hit": bool(row[0]), "_peak": row[1] if row[1] > 0 else None}
        return {}
