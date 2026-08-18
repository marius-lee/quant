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

# ── 列名常量 (DDL 与查询共引) ──
# sim_trades
ST_DATE         = "date"
ST_SYMBOL       = "symbol"
ST_SIDE         = "side"
ST_PRICE        = "price"
ST_SHARES       = "shares"
ST_PNL          = "pnl"
ST_PNL_PCT      = "pnl_pct"
ST_CAPITAL_AFTER = "capital_after"
ST_STRATEGY     = "strategy"
ST_MODE         = "mode"
ST_BOARD_COUNT  = "board_count"
ST_COST         = "cost"
ST_CREATED_AT   = "created_at"

# strategy_config
SC_STRATEGY       = "strategy"
SC_MODE           = "mode"
SC_INITIAL_CAPITAL = "initial_capital"
SC_INITIALIZED    = "initialized"
SC_UPDATED_AT     = "updated_at"

# daily_signals
DS_DATE         = "date"
DS_STRATEGY     = "strategy"
DS_SIGNALS_JSON = "signals_json"
DS_CAPITAL      = "capital"
DS_GENERATED_AT = "generated_at"
DS_MODE         = "mode"
DS_EXEC_NOTES   = "exec_notes"

# pending_orders
PO_ID             = "id"
PO_STRATEGY      = "strategy"
PO_SYMBOL        = "symbol"
PO_SIDE          = "side"
PO_TARGET_SHARES = "target_shares"
PO_LIMIT_PRICE   = "limit_price"
PO_REFERENCE_PRICE = "reference_price"
PO_STATUS        = "status"
PO_PLACED_AT     = "placed_at"
PO_FILLED_AT     = "filled_at"
PO_FILLED_SHARES = "filled_shares"
PO_FILLED_PRICE  = "filled_price"
PO_CHASE_COUNT   = "chase_count"
PO_CANCEL_REASON = "cancel_reason"
PO_DAY           = "day"
PO_MODE          = "mode"

# daily_equity
DE_DATE           = "date"
DE_CASH           = "cash"
DE_POSITION_VALUE = "position_value"
DE_TOTAL_EQUITY   = "total_equity"
DE_DRAWDOWN_PCT   = "drawdown_pct"

# position_meta
PM_SYMBOL     = "symbol"
PM_DAY        = "day"
PM_TP1_HIT    = "tp1_hit"
PM_PEAK_PRICE = "peak_price"

# benchmark_tracking
BT_DATE              = "date"
BT_STRATEGY_EQUITY   = "strategy_equity"
BT_STRATEGY_RETURN   = "strategy_return"
BT_BENCH_RETURN      = "bench_return"
BT_ALPHA             = "alpha"
BT_ROLLING_ALPHA_60D = "rolling_alpha_60d"
BT_ROLLING_IR_60D    = "rolling_ir_60d"
BT_ROLLING_BETA_60D  = "rolling_beta_60d"
BT_UP_CAPTURE_60D    = "up_capture_60d"
BT_DOWN_CAPTURE_60D  = "down_capture_60d"


class TradeRepo:
    """Trade data access — single source of truth for trades.db.

    Auto-creates schema on first init. Uses DatabaseManager for connection pooling.
    """

    def __init__(self, db_path: str = None):
        self._path = db_path or TRADE_DB
        self._ensure_schema()

    def _conn(self):
        """新开连接。调用方负责 commit + close。"""
        return DatabaseManager.get_connection(self._path)

    def _query(self, sql: str, params: tuple = ()):
        """只读查询，自动 close。返回 fetchall。"""
        conn = self._conn()
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _query_one(self, sql: str, params: tuple = ()):
        """只读查询，自动 close。返回单行。"""
        conn = self._conn()
        try:
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def _execute(self, sql: str, params: tuple = ()):
        """写操作，自动 commit + close。返回 cursor。"""
        conn = self._conn()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return cur
        finally:
            conn.close()

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
                created_at TEXT DEFAULT (datetime('now','localtime'))
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
                created_at TEXT DEFAULT (datetime('now','localtime')),
                updated_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS daily_signals (
                date        TEXT PRIMARY KEY,
                strategy    TEXT DEFAULT 'quant',
                signals_json TEXT NOT NULL,
                capital     REAL,
                generated_at TEXT DEFAULT (datetime('now','localtime')),
                mode TEXT DEFAULT 'live',
                exec_notes TEXT DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS daily_equity (
                date TEXT PRIMARY KEY,
                cash REAL,
                position_value REAL,
                total_equity REAL,
                drawdown_pct REAL,
                created_at TEXT DEFAULT (datetime('now','localtime'))
            );
            CREATE TABLE IF NOT EXISTS position_meta (
                symbol TEXT NOT NULL,
                day TEXT NOT NULL,
                tp1_hit INTEGER DEFAULT 0,
                peak_price REAL DEFAULT 0,
                PRIMARY KEY (symbol, day)
            );
            CREATE TABLE IF NOT EXISTS benchmark_tracking (
                date TEXT PRIMARY KEY,
                strategy_equity REAL NOT NULL,
                strategy_return REAL,
                bench_return REAL,
                alpha REAL,
                rolling_alpha_60d REAL,
                rolling_ir_60d REAL,
                rolling_beta_60d REAL,
                up_capture_60d REAL,
                down_capture_60d REAL
            );

        """)

        # ── Column migrations (idempotent via try/except) ──
        for col_sql in [
            "ALTER TABLE sim_trades ADD COLUMN mode TEXT DEFAULT 'live'",
            "ALTER TABLE sim_trades ADD COLUMN cost REAL DEFAULT 0",
            "ALTER TABLE daily_signals ADD COLUMN mode TEXT DEFAULT 'live'",
            "ALTER TABLE pending_orders ADD COLUMN mode TEXT DEFAULT 'live'",
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
            # 新库初始 schema 无 initialized 列 (旧库才有) — 分别处理,
            # 否则全新部署/测试库迁移直接 OperationalError: no such column
            _init_sel = "COALESCE(initialized,0)" if 'initialized' in sc_cols else "0"
            c.executescript(f'''
                CREATE TABLE strategy_config_new (
                    strategy TEXT NOT NULL, mode TEXT NOT NULL DEFAULT 'live',
                    initial_capital REAL NOT NULL,
                    initialized INTEGER DEFAULT 0, updated_at TEXT,
                    PRIMARY KEY (strategy, mode)
                );
                INSERT OR IGNORE INTO strategy_config_new (strategy, initial_capital, initialized, updated_at)
                    SELECT strategy, initial_capital, {_init_sel}, updated_at FROM strategy_config;
                DROP TABLE strategy_config;
                ALTER TABLE strategy_config_new RENAME TO strategy_config;
            ''')
        # Ensure initialized flag
        c.execute("UPDATE strategy_config SET initialized = 1 WHERE initialized IS NULL OR initialized = 0")

        # ── B-05/B-19: daily_equity 加 strategy 列, 主键 (date, strategy) ──
        # 旧表无主键 strategy 列, get_daily_equity_range 查询 strategy 列直接报错,
        # 且多策略互相覆盖。空表/小表, 重建迁移安全。
        de_cols = {r[1] for r in c.execute("PRAGMA table_info(daily_equity)").fetchall()}
        if de_cols and 'strategy' not in de_cols:
            c.executescript('''
                CREATE TABLE daily_equity_new (
                    date TEXT NOT NULL,
                    strategy TEXT NOT NULL DEFAULT 'quant',
                    cash REAL,
                    position_value REAL,
                    total_equity REAL,
                    drawdown_pct REAL,
                    created_at TEXT DEFAULT (datetime('now','localtime')),
                    PRIMARY KEY (date, strategy)
                );
                INSERT OR IGNORE INTO daily_equity_new
                    (date, cash, position_value, total_equity, drawdown_pct, created_at)
                    SELECT date, cash, position_value, total_equity, drawdown_pct, created_at
                    FROM daily_equity;
                DROP TABLE daily_equity;
                ALTER TABLE daily_equity_new RENAME TO daily_equity;
            ''')

        # ── B-14: meta KV 表 (熔断标志等运行期开关) ──
        c.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")

        # ── B-19: daily_signals 主键 (date) → (date, strategy, mode) ──
        # 旧表 PK 只有 date — 多策略/多模式同日互相覆盖 (INSERT OR REPLACE 整行替换)。
        # 判据: PK 不含 strategy (旧表 date TEXT PRIMARY KEY 内联声明, 无 pk 列集合可查,
        # 用 sqlite_master sql 文本判定最稳)
        _ds_sql = c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='daily_signals'"
        ).fetchone()
        if _ds_sql and _ds_sql[0] and "PRIMARY KEY (date, strategy, mode)" not in _ds_sql[0]:
            c.executescript('''
                CREATE TABLE daily_signals_new (
                    date        TEXT NOT NULL,
                    strategy    TEXT NOT NULL DEFAULT 'quant',
                    mode        TEXT NOT NULL DEFAULT 'live',
                    signals_json TEXT NOT NULL,
                    capital     REAL,
                    generated_at TEXT DEFAULT (datetime('now','localtime')),
                    exec_notes  TEXT DEFAULT '{}',
                    PRIMARY KEY (date, strategy, mode)
                );
                INSERT OR IGNORE INTO daily_signals_new
                    (date, strategy, mode, signals_json, capital, generated_at, exec_notes)
                    SELECT date,
                           COALESCE(strategy, 'quant'),
                           COALESCE(mode, 'live'),
                           signals_json, capital, generated_at,
                           COALESCE(exec_notes, '{}')
                    FROM daily_signals;
                DROP TABLE daily_signals;
                ALTER TABLE daily_signals_new RENAME TO daily_signals;
            ''')

        # ── Indexes ──
        c.executescript("""
            CREATE INDEX IF NOT EXISTS idx_st_strategy_id ON sim_trades(strategy, id);
            CREATE INDEX IF NOT EXISTS idx_st_positions ON sim_trades(strategy, side, symbol);
            CREATE INDEX IF NOT EXISTS idx_st_t1_check ON sim_trades(symbol, side, date, strategy);
            CREATE INDEX IF NOT EXISTS idx_po_status ON pending_orders(status, day);
        """)
        c.commit()
        c.close()

    # ── Capital ─────────────────────────────────────────────

    def get_cash(self, strategy: str = "quant", mode: str = "live") -> float:
        conn = self._conn()
        try:
            row = conn.execute(
                f"SELECT COALESCE({SC_INITIAL_CAPITAL},0) FROM strategy_config WHERE {SC_STRATEGY}=?",
                (strategy,)).fetchone()
            initial = float(row[0]) if row else 0.0
            buys = conn.execute(
                "SELECT COALESCE(SUM(price*shares + COALESCE(cost,0)), 0) FROM sim_trades "
                "WHERE side='buy' AND strategy=? AND mode=?",
                (strategy, mode)).fetchone()[0]
            sells = conn.execute(
                "SELECT COALESCE(SUM(price*shares - COALESCE(cost,0)), 0) FROM sim_trades "
                "WHERE side='sell' AND strategy=? AND mode=?",
                (strategy, mode)).fetchone()[0]
            return round(initial + float(sells) - float(buys), 2)
        finally:
            conn.close()

    def get_daily_flow(self, day: str, strategy: str = "quant", mode: str = "live") -> tuple[float, float, float]:
        """返回 (sells, buys, fees) 截至 day 的累计流水 (全账本重算, 含当日).

        reconcile 现金对账需要 initial_capital + Σ卖 - Σ买 - Σ费 与当前现金
        全量核对 (docstring 即"全账本重算"); 只查当日会导致历史流水缺失 → 假 break.
        """
        conn = self._conn()
        try:
            sells = conn.execute(
                "SELECT COALESCE(SUM(price*shares - COALESCE(cost,0)), 0) FROM sim_trades "
                "WHERE side='sell' AND strategy=? AND mode=? AND date<=?",
                (strategy, mode, day)).fetchone()[0]
            buys = conn.execute(
                "SELECT COALESCE(SUM(price*shares + COALESCE(cost,0)), 0) FROM sim_trades "
                "WHERE side='buy' AND strategy=? AND mode=? AND date<=?",
                (strategy, mode, day)).fetchone()[0]
            fees = conn.execute(
                "SELECT COALESCE(SUM(COALESCE(cost,0)), 0) FROM sim_trades "
                "WHERE strategy=? AND mode=? AND date<=?",
                (strategy, mode, day)).fetchone()[0]
            return float(sells), float(buys), float(fees)
        finally:
            conn.close()

    def is_initialized(self, strategy: str = "quant", mode: str = "live") -> bool:
        row = self._query_one(
            f"SELECT COALESCE({SC_INITIALIZED},0) FROM strategy_config WHERE {SC_STRATEGY}=?",
            (strategy,))
        return bool(row and row[0])

    def get_initial_capital(self, strategy: str = "quant", mode: str = "live") -> float:
        row = self._query_one(
            f"SELECT {SC_INITIAL_CAPITAL} FROM strategy_config WHERE {SC_STRATEGY}=?",
            (strategy,))
        return float(row[0]) if row else 0.0

    def set_initial_capital(self, strategy: str, capital: float, mode: str = "live"):
        # 2026-08-18: REPLACE 整行重建 (PK strategy,mode) 会先删后插 → ON CONFLICT 原子更新
        self._execute(
            f"INSERT INTO strategy_config ({SC_STRATEGY}, {SC_MODE}, {SC_INITIAL_CAPITAL}, {SC_INITIALIZED}, {SC_UPDATED_AT}) "
            f"VALUES (?, ?, ?, 1, datetime('now','localtime')) "
            f"ON CONFLICT({SC_STRATEGY}, {SC_MODE}) DO UPDATE SET "
            f"{SC_INITIAL_CAPITAL}=excluded.{SC_INITIAL_CAPITAL}, {SC_INITIALIZED}=1, {SC_UPDATED_AT}=excluded.{SC_UPDATED_AT}",
            (strategy, mode, capital))
        logger.info(f"[capital] {strategy}/{mode} initial_capital={capital}")

    # ── Positions ───────────────────────────────────────────

    def get_positions(self, strategy: str = "quant", mode: str = "live") -> list[dict]:
        c = self._conn()
        try:
            buys = c.execute(
                f"SELECT {ST_SYMBOL}, SUM({ST_SHARES}), SUM({ST_PRICE}*{ST_SHARES})/SUM({ST_SHARES}), MAX({ST_BOARD_COUNT}), "
                f"MIN(datetime({ST_CREATED_AT}, 'localtime')) "
                f"FROM sim_trades WHERE {ST_SIDE}='buy' AND {ST_STRATEGY}=? AND {ST_MODE}=? GROUP BY {ST_SYMBOL}",
                (strategy, mode)).fetchall()
            sells = c.execute(
                f"SELECT {ST_SYMBOL}, SUM({ST_SHARES}) FROM sim_trades "
                f"WHERE {ST_SIDE}='sell' AND {ST_STRATEGY}=? AND {ST_MODE}=? GROUP BY {ST_SYMBOL}",
                (strategy, mode)).fetchall()
            sell_map = {r[0]: r[1] for r in sells}
            # B-20: 持仓成本价改用 FIFO (与 get_average_cost/卖出 pnl 同口径),
            # 原 SQL 是全历史买入加权平均
            # B-20 fix: FIFO 成本计算改用批量查询 (P1-19: 消除 N+1 per-symbol SQL)
            _all_syms = [r[0] for r in buys if r[1] > sell_map.get(r[0], 0)]
            _fifo_costs = self.get_fifo_costs_batch(strategy, _all_syms, mode)
            result = []
            for r in buys:
                if r[1] <= sell_map.get(r[0], 0):
                    continue
                fifo_cost = _fifo_costs.get(r[0], 0.0)
                result.append({
                    "symbol": r[0],
                    "price": round(fifo_cost, 4) if fifo_cost else (round(r[2], 4) if r[2] else 0),
                    "shares": max(0, r[1] - sell_map.get(r[0], 0)),
                    "board_count": r[3] or 0, "buy_time": r[4],
                })
            return result
        finally:
            c.close()

    # ── Trades ──────────────────────────────────────────────

    def get_trades(self, strategy: str = "", mode: str = "live", limit: int = 20) -> list[dict]:
        c = self._conn()
        try:
            if strategy:
                rows = c.execute(
                    f"SELECT {ST_DATE},{ST_SYMBOL},{ST_SIDE},{ST_PRICE},{ST_SHARES},{ST_PNL},{ST_PNL_PCT},{ST_CREATED_AT} "
                    f"FROM sim_trades WHERE {ST_STRATEGY}=? AND {ST_MODE}=? ORDER BY id DESC LIMIT ?",
                    (strategy, mode, limit)).fetchall()
            else:
                rows = c.execute(
                    f"SELECT {ST_DATE},{ST_SYMBOL},{ST_SIDE},{ST_PRICE},{ST_SHARES},{ST_PNL},{ST_PNL_PCT},{ST_CREATED_AT} "
                    f"FROM sim_trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
            return [
                {"date": r[0], "symbol": r[1], "side": r[2], "price": r[3],
                 "shares": r[4], "pnl": r[5], "pnl_pct": r[6], "created_at": r[7]}
                for r in rows
            ]
        finally:
            c.close()

    def get_sells(self, strategy: str, mode: str = "live") -> list:
        rows = self._query(
            f"SELECT {ST_PNL} FROM sim_trades "
            f"WHERE {ST_SIDE}='sell' AND {ST_STRATEGY}=? AND {ST_MODE}=? AND {ST_PNL} IS NOT NULL",
            (strategy, mode))
        return [r[0] for r in rows]

    def get_orders(self, day: str, strategy: str = "quant", mode: str = "live") -> list[dict]:
        """返回当日的订单列表 (含 pending/filled/cancelled)."""
        conn = self._conn()
        try:
            sql = (
                f"SELECT {PO_ID},{PO_STRATEGY},{PO_SYMBOL},{PO_SIDE},{PO_TARGET_SHARES},"
                f"{PO_LIMIT_PRICE},{PO_REFERENCE_PRICE},{PO_STATUS},{PO_PLACED_AT},"
                f"{PO_FILLED_AT},{PO_FILLED_SHARES},{PO_FILLED_PRICE},"
                f"{PO_CHASE_COUNT},{PO_CANCEL_REASON},{PO_DAY} "
                f"FROM pending_orders "
                f"WHERE {PO_STRATEGY}=? AND {PO_MODE}=? AND {PO_DAY}=? "
                f"ORDER BY {PO_PLACED_AT}"
            )
            rows = conn.execute(sql, (strategy, mode, day)).fetchall()
            return [
                {"id": r[0], "strategy": r[1], "symbol": r[2], "side": r[3],
                 "target_shares": r[4], "limit_price": r[5], "reference_price": r[6],
                 "status": r[7], "placed_at": r[8], "filled_at": r[9],
                 "filled_shares": r[10], "filled_price": r[11],
                 "chase_count": r[12], "cancel_reason": r[13], "day": r[14]}
                for r in rows
            ]
        finally:
            conn.close()

    def get_pnl(self, strategy: str, mode: str = "live") -> float:
        row = self._query_one(
            f"SELECT COALESCE(SUM({ST_PNL}),0) FROM sim_trades "
            f"WHERE {ST_SIDE}='sell' AND {ST_STRATEGY}=? AND {ST_MODE}=? AND {ST_PNL} IS NOT NULL",
            (strategy, mode))
        return row[0]

    def check_t1(self, strategy: str, symbol: str, date: str, mode: str = "live") -> bool:
        row = self._query_one(
            f"SELECT COUNT(*) FROM sim_trades "
            f"WHERE {ST_SYMBOL}=? AND {ST_SIDE}='buy' AND {ST_DATE}=? AND {ST_STRATEGY}=? AND {ST_MODE}=?",
            (symbol, date, strategy, mode))
        return row[0] > 0

    def has_trades_today(self, strategy: str, date_str: str, mode: str = "live") -> bool:
        row = self._query_one(
            f"SELECT COUNT(*) FROM sim_trades "
            f"WHERE {ST_DATE}=? AND {ST_STRATEGY}=? AND {ST_MODE}=?",
            (date_str, strategy, mode))
        return row[0] > 0

    def get_counts(self, strategy: str, mode: str = "live") -> tuple:
        c = self._conn()
        try:
            buys = c.execute(
                f"SELECT COUNT(*) FROM sim_trades "
                "WHERE side='buy' AND strategy=? AND mode=?", (strategy, mode)).fetchone()[0]
            sells = c.execute(
                f"SELECT COUNT(*) FROM sim_trades "
                "WHERE side='sell' AND strategy=? AND mode=?", (strategy, mode)).fetchone()[0]
            win = c.execute(
                f"SELECT COUNT(*) FROM sim_trades "
                f"WHERE {ST_SIDE}='sell' AND {ST_STRATEGY}=? AND {ST_MODE}=? AND {ST_PNL}>0",
                (strategy, mode)).fetchone()[0]
            return buys, sells, win
        finally:
            c.close()

    def get_date_range(self, strategy: str, mode: str = "live") -> tuple:
        row = self._query_one(
            f"SELECT MIN({ST_DATE}), MAX({ST_DATE}) FROM sim_trades "
            "WHERE strategy=? AND mode=?", (strategy, mode))
        return (row[0], row[1]) if row else (None, None)

    def record_trade(self, strategy: str, date: str, symbol: str,
                     side: str, price: float, shares: int,
                     pnl: float = 0.0, pnl_pct: float = 0.0,
                     board_count: int = 0, cost: float = 0.0,
                     mode: str = "live",
                     conn: "sqlite3.Connection | None" = None) -> None:
        own_conn = conn is None
        c = conn if conn is not None else self._conn()
        try:
            c.execute(
                "INSERT INTO sim_trades(date, symbol, side, price, shares, "
                "pnl, pnl_pct, strategy, board_count, mode, cost, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?, datetime('now','localtime'))",
                (date, symbol, side, price, shares, pnl, pnl_pct,
                 strategy, board_count, mode, cost))
            if own_conn:
                c.commit()
        finally:
            if own_conn:
                c.close()

    # ── Cost / PnL helpers ──────────────────────────────────

    def get_last_buy_price(self, strategy: str, symbol: str, mode: str = "live") -> tuple | None:
        row = self._query_one(
            "SELECT price, shares FROM sim_trades "
            "WHERE symbol=? AND side='buy' AND strategy=? AND mode=? "
            "ORDER BY id DESC LIMIT 1",
            (symbol, strategy, mode))
        return (float(row[0]), int(row[1])) if row else None

    def get_average_cost(self, strategy: str, symbol: str, mode: str = "live") -> float:
        """B-20 fix: 真正的 FIFO 成本法 — 逐笔撮合卖出, 返回剩余持仓批次的加权成本。

        原实现是全历史买入加权平均 (调用处注释却自称 FIFO): 卖出后回购的股票,
        成本被早已平仓的旧交易稀释, 导致卖出 pnl 与止损线计算失真。
        """
        rows = self._query(
            "SELECT side, price, shares FROM sim_trades "
            "WHERE symbol=? AND strategy=? AND mode=? ORDER BY id",
            (symbol, strategy, mode))
        return self._fifo_cost_from_rows(rows)

    def get_fifo_costs_batch(self, strategy: str, symbols: list[str], mode: str = "live") -> dict[str, float]:
        """P1-19 fix: 批量 FIFO 成本 — 单次 SQL 查询 (IN clause) 替代 N+1 per-symbol 查询.

        返回 dict[symbol] = fifo_cost (0.0 if flat).
        """
        if not symbols:
            return {}
        placeholders = ", ".join("?" * len(symbols))
        rows = self._query(
            f"SELECT symbol, side, price, shares FROM sim_trades "
            f"WHERE symbol IN ({placeholders}) AND strategy=? AND mode=? ORDER BY symbol, id",
            (*symbols, strategy, mode))
        # 按 symbol 分组
        grouped: dict[str, list] = {}
        for row in rows:
            sym = row[0]
            grouped.setdefault(sym, []).append(row[1:])
        return {sym: self._fifo_cost_from_rows(grouped.get(sym, [])) for sym in symbols}

    @staticmethod
    def _fifo_cost_from_rows(rows: list) -> float:
        """Static FIFO: rows = [(side, price, shares), ...] ordered by id."""
        lots: list[list] = []  # [price, remaining_shares]
        for side, price, shares in rows:
            if side == "buy":
                lots.append([float(price), int(shares)])
            else:
                rem = int(shares)
                while rem > 0 and lots:
                    take = min(lots[0][1], rem)
                    lots[0][1] -= take
                    rem -= take
                    if lots[0][1] == 0:
                        lots.pop(0)
        total_shares = sum(s for _, s in lots)
        if total_shares <= 0:
            return 0.0
        return sum(p * s for p, s in lots) / total_shares

    def get_open_position_cost(self, strategy: str, mode: str = "live") -> float:
        row = self._query_one(
            "SELECT COALESCE(SUM(price*shares),0) FROM sim_trades "
            "WHERE side='buy' AND strategy=? AND mode=? "
            "AND symbol NOT IN ("
            "  SELECT symbol FROM sim_trades WHERE side='sell' AND strategy=? AND mode=?"
            ")",
            (strategy, mode, strategy, mode))
        return float(row[0]) if row else 0.0

    # ── daily_equity ────────────────────────────────────────

    def record_daily_equity(self, date: str, cash: float, position_value: float,
                            strategy: str = "quant"):
        c = self._conn()
        try:
            total = cash + position_value
            peak_row = c.execute(
                f"SELECT MAX({DE_TOTAL_EQUITY}) FROM daily_equity WHERE strategy=?",
                (strategy,)).fetchone()
            peak = max(peak_row[0] or total, total)
            dd_pct = round((peak - total) / peak * 100, 2) if peak > 0 else 0.0
            c.execute(
                f"INSERT OR REPLACE INTO daily_equity ({DE_DATE}, strategy, {DE_CASH}, {DE_POSITION_VALUE}, {DE_TOTAL_EQUITY}, {DE_DRAWDOWN_PCT}) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (date, strategy, cash, position_value, total, dd_pct))
            c.commit()
        finally:
            c.close()

    def get_max_drawdown(self, lookback_days: int = 60, strategy: str = "quant") -> float:
        row = self._query_one(
            f"SELECT MAX({DE_DRAWDOWN_PCT}) FROM daily_equity "
            "WHERE strategy=? AND date >= date('now', ? || ' days')",
            (strategy, f"-{lookback_days}"))
        return float(row[0]) if row and row[0] else 0.0

    # ── daily_signals ───────────────────────────────────────

    def save_signals(self, date_str: str, targets: list, capital: float,
                     strategy: str = "quant", mode: str = "live"):
        # B-19 fix: mode 参数此前被静默忽略 (写入行总是 DEFAULT 'live'),
        # PK 升级为 (date, strategy, mode) 后必须显式写入
        self._execute(
            f"INSERT OR REPLACE INTO daily_signals ({DS_DATE}, {DS_STRATEGY}, {DS_MODE}, {DS_SIGNALS_JSON}, {DS_CAPITAL}) "
            "VALUES (?, ?, ?, ?, ?)",
            (date_str, strategy, mode, _json.dumps(targets), capital))
        logger.info(f"[signals] saved {len(targets)} targets for {date_str} to daily_signals")


    def get_daily_signals_range(self, start: str, end: str = None,
                                mode: str = "live", strategy: str = "quant") -> list[dict]:
        """Read all daily_signals rows in a date range. Returns list of dicts."""
        if end is None:
            end = start
        rows = self._query(
            f"SELECT {DS_DATE}, {DS_STRATEGY}, {DS_SIGNALS_JSON}, {DS_CAPITAL}, {DS_GENERATED_AT}, {DS_MODE}, {DS_EXEC_NOTES} "
            f"FROM daily_signals WHERE {DS_STRATEGY}=? AND {DS_MODE}=? AND {DS_DATE} >= ? AND {DS_DATE} <= ? "
            f"ORDER BY {DS_DATE}",
            (strategy, mode, start, end))
        return [{"date": r[0], "strategy": r[1], "signals_json": r[2],
                 "capital": r[3], "generated_at": r[4], "mode": r[5],
                 "exec_notes": r[6]} for r in rows]

    def get_daily_equity_range(self, start: str, end: str = None,
                               strategy: str = "quant") -> list[dict]:
        """Read daily_equity rows in a date range. Returns list of dicts."""
        if end is None:
            end = start
        rows = self._query(
            f"SELECT {DE_DATE}, {DE_CASH}, {DE_POSITION_VALUE}, {DE_TOTAL_EQUITY} FROM daily_equity "
            f"WHERE strategy=? AND {DE_DATE} >= ? AND {DE_DATE} <= ? ORDER BY {DE_DATE}",
            (strategy, start, end))
        return [{"date": r[0], "cash": r[1], "position_value": r[2], "equity": r[3]}
                for r in rows]

    def get_strategy_config(self, strategy: str = "quant") -> dict:
        """Read strategy_config row as dict. Returns {} if not found."""
        row = self._query_one(
            f"SELECT {SC_STRATEGY}, {SC_MODE}, {SC_INITIAL_CAPITAL}, {SC_INITIALIZED}, {SC_UPDATED_AT} "
            f"FROM strategy_config WHERE {SC_STRATEGY}=? ORDER BY {SC_UPDATED_AT} DESC LIMIT 1",
            (strategy,))
        if row:
            return {"strategy": row[0], "mode": row[1], "initial_capital": row[2],
                    "initialized": row[3], "updated_at": row[4]}
        return {}

    def get_latest_signals(self, strategy: str = "quant", mode: str = "live") -> dict | None:
        row = self._query_one(
            f"SELECT {DS_DATE}, {DS_SIGNALS_JSON}, {DS_CAPITAL} FROM daily_signals "
            f"WHERE {DS_STRATEGY}=? AND {DS_MODE}=? ORDER BY {DS_DATE} DESC LIMIT 1",
            (strategy, mode))
        if row:
            return {"date": row[0], "targets": _json.loads(row[1]), "capital": row[2]}
        return None

    def get_signal_exec_notes(self, date_str: str) -> dict:
        row = self._query_one(
            f"SELECT {DS_EXEC_NOTES} FROM daily_signals WHERE {DS_DATE}=? ORDER BY {DS_GENERATED_AT} DESC LIMIT 1",
            (date_str,))
        if row and row[0]:
            try:
                return _json.loads(row[0])
            except Exception:
                return {}
        return {}

    def update_signal_exec_note(self, date_str: str, symbol: str, note: str):
        c = self._conn()
        try:
            row = c.execute(
                f"SELECT {DS_EXEC_NOTES} FROM daily_signals WHERE {DS_DATE}=? ORDER BY {DS_GENERATED_AT} DESC LIMIT 1",
                (date_str,)).fetchone()
            if not row:
                return
            try:
                notes = _json.loads(row[0]) if row[0] else {}
            except Exception:
                notes = {}
            notes[symbol] = note
            c.execute(
                f"UPDATE daily_signals SET {DS_EXEC_NOTES}=? WHERE {DS_DATE}=?",
                (_json.dumps(notes, ensure_ascii=False), date_str))
            c.commit()
        finally:
            c.close()

    # ── position_meta ───────────────────────────────────────

    def save_position_meta(self, symbol: str, day: str, tp1_hit: bool, peak_price: float):
        self._execute(
            f"INSERT OR REPLACE INTO position_meta ({PM_SYMBOL}, {PM_DAY}, {PM_TP1_HIT}, {PM_PEAK_PRICE}) "
            "VALUES (?, ?, ?, ?)",
            (symbol, day, int(tp1_hit), peak_price))

    def get_position_meta(self, symbol: str, day: str) -> dict:
        row = self._query_one(
            f"SELECT {PM_TP1_HIT}, {PM_PEAK_PRICE} FROM position_meta WHERE {PM_SYMBOL}=? AND {PM_DAY}=?",
            (symbol, day))
        if row:
            return {"_tp1_hit": bool(row[0]), "_peak": row[1] if row[1] > 0 else None}
        return {}

    def get_last_sell_time(self, symbol: str) -> str:
        """v532: 最近一次卖出时间 (清仓重买判定 — 旧 peak/tp1 不残留)."""
        row = self._query_one(
            f"SELECT MAX(datetime({ST_CREATED_AT}, 'localtime')) FROM sim_trades "
            f"WHERE {ST_SYMBOL}=? AND {ST_SIDE}='sell'", (symbol,))
        return row[0] if row and row[0] else None

    def get_position_meta_max(self, symbol: str) -> dict:
        """B9: 回载某 symbol 的历史峰值/TP1 标记 (MAX 聚合, 跨日持久).

        与 get_position_meta(day) 的区别: 峰值与 tp1 标记单调不回退,
        取全历史 MAX 才能在进程重启/跨日后恢复 trailing stop 与 TP1 状态.
        """
        row = self._query_one(
            f"SELECT MAX({PM_TP1_HIT}), MAX({PM_PEAK_PRICE}) "
            f"FROM position_meta WHERE {PM_SYMBOL}=?",
            (symbol,))
        if row and row[0] is not None:
            return {"_tp1_hit": bool(row[0]), "_peak": row[1] if (row[1] or 0) > 0 else None}
        return {}

    # ── meta KV (运行期开关, 如熔断标志 B-14) ────────────────

    def set_flag(self, key: str, value: str):
        self._execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, value))

    def get_flag(self, key: str) -> str | None:
        row = self._query_one("SELECT value FROM meta WHERE key=?", (key,))
        return row[0] if row else None

    def clear_flag(self, key: str):
        self._execute("DELETE FROM meta WHERE key=?", (key,))
