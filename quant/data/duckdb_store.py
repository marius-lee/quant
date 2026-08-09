"""DuckDB Data Layer — 列式存储 + 并行查询 + 零拷贝 Arrow.

替代 SQLite (market.db) 为主数据仓库:
  - 列式存储: 因子计算仅读取所需列, I/O 减少 10x+
  - 并行查询: DuckDB 并行执行引擎, 多核加速
  - Arrow 零拷贝: 与 pandas/pyarrow 无缝互操作, 无内存拷贝
  - 兼容 SQLite: 保留 SQLite 作为事务日志/元数据存储, 双写模式平滑迁移

迁移策略 (v435):
  Phase 1: DuckDB 并行写入器 (后台异步同步 SQLite -> DuckDB)
  Phase 2: 只读查询切换到 DuckDB (因子计算/回测/归因)
  Phase 3: 双写模式 (SQLite 事务 + DuckDB 分析) -> 完全切换
"""

import os
import threading
import time
import logging
from contextlib import contextmanager
from typing import Optional, List, Dict, Any, Iterator
from pathlib import Path

import duckdb
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from quant.config.paths import MARKET_DB, DATA_DIR
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger

_log = get_logger("data.duckdb")

# ── 配置常量 ──
_DUCKDB_PATH = Path(DATA_DIR) / "market.duckdb"
_MIGRATION_BATCH_SIZE = _require_cfg("duckdb.migration_batch_size", default=100000)
_SYNC_INTERVAL_SEC = _require_cfg("duckdb.sync_interval_sec", default=300)
_MAX_WORKERS = _require_cfg("duckdb.max_workers", default=4)

# 表 Schema 定义 (DuckDB DDL)
_TABLE_SCHEMAS = {
    "daily": """
        CREATE TABLE IF NOT EXISTS daily (
            date DATE NOT NULL,
            symbol VARCHAR(10) NOT NULL,
            open DOUBLE,
            high DOUBLE,
            low DOUBLE,
            close DOUBLE,
            volume DOUBLE,
            amount DOUBLE,
            turnover DOUBLE,
            PRIMARY KEY (date, symbol)
        )
    """,
    "daily_valuation": """
        CREATE TABLE IF NOT EXISTS daily_valuation (
            symbol VARCHAR(10) NOT NULL,
            date DATE NOT NULL,
            pe_ttm DOUBLE,
            pb DOUBLE,
            ps_ttm DOUBLE,
            pcf_ttm DOUBLE,
            market_cap DOUBLE,
            turnover_rate DOUBLE,
            source VARCHAR(20) DEFAULT 'jqdata',
            PRIMARY KEY (symbol, date)
        )
    """,
    "stocks": """
        CREATE TABLE IF NOT EXISTS stocks (
            symbol VARCHAR(10) PRIMARY KEY,
            name VARCHAR(50),
            market VARCHAR(10),
            list_date DATE,
            industry VARCHAR(50),
            list_status VARCHAR(1) DEFAULT 'L',
            delist_date DATE,
            total_shares DOUBLE,
            pe DOUBLE,
            pb DOUBLE,
            total_mv DOUBLE,
            roe DOUBLE,
            high_52w DOUBLE,
            low_52w DOUBLE,
            circ_mv DOUBLE,
            eps DOUBLE,
            bvps DOUBLE,
            div_yield DOUBLE,
            turnover_rate DOUBLE,
            pe_ttm DOUBLE,
            cfps DOUBLE
        )
    """,
    "financial_income": """
        CREATE TABLE IF NOT EXISTS financial_income (
            symbol VARCHAR(10) NOT NULL,
            stat_date DATE NOT NULL,
            ann_date DATE,
            revenue DOUBLE,
            net_profit DOUBLE,
            operate_profit DOUBLE,
            total_assets DOUBLE,
            PRIMARY KEY (symbol, stat_date)
        )
    """,
    "financial_balance": """
        CREATE TABLE IF NOT EXISTS financial_balance (
            symbol VARCHAR(10) NOT NULL,
            stat_date DATE NOT NULL,
            ann_date DATE,
            total_assets DOUBLE,
            total_liab DOUBLE,
            total_hldr_eqy DOUBLE,
            PRIMARY KEY (symbol, stat_date)
        )
    """,
    "financial_cashflow": """
        CREATE TABLE IF NOT EXISTS financial_cashflow (
            symbol VARCHAR(10) NOT NULL,
            stat_date DATE NOT NULL,
            ann_date DATE,
            net_operate_cash_flow DOUBLE,
            net_invest_cash_flow DOUBLE,
            net_financing_cash_flow DOUBLE,
            PRIMARY KEY (symbol, stat_date)
        )
    """,
    "margin_detail": """
        CREATE TABLE IF NOT EXISTS margin_detail (
            symbol VARCHAR(10) NOT NULL,
            date DATE NOT NULL,
            market VARCHAR(10) NOT NULL,
            margin_buy DOUBLE,
            margin_balance DOUBLE,
            margin_repay DOUBLE,
            short_sell_vol DOUBLE,
            short_balance DOUBLE,
            short_total DOUBLE,
            margin_total DOUBLE,
            PRIMARY KEY (symbol, date, market)
        )
    """,
    "limit_up_pool": """
        CREATE TABLE IF NOT EXISTS limit_up_pool (
            date DATE NOT NULL,
            symbol VARCHAR(10) NOT NULL,
            seal_ratio DOUBLE,
            PRIMARY KEY (date, symbol)
        )
    """,
    "daily_signals": """
        CREATE TABLE IF NOT EXISTS daily_signals (
            date DATE NOT NULL,
            strategy VARCHAR(50) NOT NULL,
            mode VARCHAR(20) NOT NULL DEFAULT 'live',
            signals_json TEXT NOT NULL,
            capital DOUBLE,
            PRIMARY KEY (date, strategy, mode)
        )
    """,
    "sim_trades": """
        CREATE TABLE IF NOT EXISTS sim_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            symbol VARCHAR(10) NOT NULL,
            side TEXT NOT NULL,
            price DOUBLE NOT NULL,
            shares INTEGER NOT NULL,
            strategy TEXT NOT NULL,
            mode TEXT NOT NULL,
            cost DOUBLE,
            pnl DOUBLE,
            pnl_pct DOUBLE,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """,
    "daily_equity": """
        CREATE TABLE IF NOT EXISTS daily_equity (
            date DATE NOT NULL,
            strategy VARCHAR(50) NOT NULL,
            cash DOUBLE,
            position_value DOUBLE,
            total_equity DOUBLE,
            drawdown_pct DOUBLE,
            PRIMARY KEY (date, strategy)
        )
    """,
    "factor_ic_daily": """
        CREATE TABLE IF NOT EXISTS factor_ic_daily (
            date DATE NOT NULL,
            factor_name VARCHAR(100) NOT NULL,
            ic_value DOUBLE,
            n_stocks INTEGER,
            is_ir DOUBLE,
            oos_ir DOUBLE,
            scope TEXT NOT NULL DEFAULT 'live',
            created_at TEXT DEFAULT (datetime('now','localtime')),
            PRIMARY KEY (date, factor_name, scope)
        )
    """,
    "factor_registry": """
        CREATE TABLE IF NOT EXISTS factor_registry (
            name VARCHAR(100) PRIMARY KEY,
            expression TEXT,
            source VARCHAR(100),
            direction VARCHAR(20),
            category VARCHAR(50),
            status VARCHAR(20) DEFAULT 'evaluating',
            status_reason TEXT,
            retry_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """,
}


class DuckDBManager:
    """DuckDB 连接管理器 — 单例模式, 线程安全."""

    _instance: Optional["DuckDBManager"] = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True

        self._db_path = _DUCKDB_PATH
        self._conn: Optional[duckdb.DuckDBPyConnection] = None
        self._lock = threading.Lock()
        self._sync_thread: Optional[threading.Thread] = None
        self._stop_sync = threading.Event()
        self._sqlite_conn = None

        # 确保目录存在
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._init_db()
        _log.info(f"DuckDBManager initialized: {self._db_path}")

    def _init_db(self):
        """初始化 DuckDB 连接并创建表."""
        self._conn = duckdb.connect(str(self._db_path), read_only=False)
        # 配置并行度
        self._conn.execute(f"PRAGMA threads={_MAX_WORKERS}")
        self._conn.execute("PRAGMA memory_limit='4GB'")
        self._conn.execute("PRAGMA preserve_insertion_order=false")

        # 创建所有表
        for table_name, ddl in _TABLE_SCHEMAS.items():
            try:
                self._conn.execute(ddl)
            except Exception as e:
                _log.warning(f"create table {table_name} failed: {e}")

        # 创建索引
        self._create_indexes()

        # 连接 SQLite (用于同步)
        from quant.config.paths import MARKET_DB as SQLITE_PATH
        self._sqlite_conn = duckdb.connect(str(SQLITE_PATH), read_only=True)
        self._sqlite_conn.execute("PRAGMA journal_mode=WAL")

    def _create_indexes(self):
        """创建常用查询索引."""
        indexes = [
            "CREATE INDEX IF NOT EXISTS idx_daily_date ON daily(date)",
            "CREATE INDEX IF NOT EXISTS idx_daily_symbol ON daily(symbol)",
            "CREATE INDEX IF NOT EXISTS idx_daily_valuation_date ON daily_valuation(date)",
            "CREATE INDEX IF NOT EXISTS idx_daily_valuation_symbol ON daily_valuation(symbol)",
            "CREATE INDEX IF NOT EXISTS idx_stocks_market ON stocks(market)",
            "CREATE INDEX IF NOT EXISTS idx_stocks_industry ON stocks(industry)",
            "CREATE INDEX IF NOT EXISTS idx_financial_income_stat_date ON financial_income(stat_date)",
            "CREATE INDEX IF NOT EXISTS idx_financial_balance_stat_date ON financial_balance(stat_date)",
            "CREATE INDEX IF NOT EXISTS idx_financial_cashflow_stat_date ON financial_cashflow(stat_date)",
            "CREATE INDEX IF NOT EXISTS idx_margin_detail_date ON margin_detail(date)",
            "CREATE INDEX IF NOT EXISTS idx_limit_up_pool_date ON limit_up_pool(date)",
            "CREATE INDEX IF NOT EXISTS idx_daily_signals_date ON daily_signals(date)",
            "CREATE INDEX IF NOT EXISTS idx_sim_trades_date ON sim_trades(date)",
            "CREATE INDEX IF NOT EXISTS idx_sim_trades_strategy ON sim_trades(strategy)",
            "CREATE INDEX IF NOT EXISTS idx_daily_equity_date ON daily_equity(date)",
            "CREATE INDEX IF NOT EXISTS idx_factor_ic_daily_date ON factor_ic_daily(date)",
            "CREATE INDEX IF NOT EXISTS idx_factor_ic_daily_factor ON factor_ic_daily(factor_name)",
        ]
        for idx_sql in indexes:
            try:
                self._conn.execute(idx_sql)
            except Exception as e:
                _log.warning(f"create index failed: {e}")

    @contextmanager
    def connection(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """获取 DuckDB 连接 (线程安全)."""
        with self._lock:
            yield self._conn

    def execute(self, sql: str, params: tuple = ()) -> Any:
        """执行 SQL (线程安全)."""
        with self._lock:
            return self._conn.execute(sql, params)

    def query_df(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        """查询返回 DataFrame."""
        with self._lock:
            return self._conn.execute(sql, params).df()

    def query_arrow(self, sql: str, params: tuple = ()) -> pa.Table:
        """查询返回 Arrow Table (零拷贝)."""
        with self._lock:
            return self._conn.execute(sql, params).arrow()

    # ── 同步相关 ──
    def start_sync(self):
        """启动后台同步线程 (SQLite -> DuckDB)."""
        if self._sync_thread and self._sync_thread.is_alive():
            return
        self._stop_sync.clear()
        self._sync_thread = threading.Thread(target=self._sync_loop, daemon=True)
        self._sync_thread.start()
        _log.info("DuckDB sync thread started")

    def stop_sync(self):
        """停止同步线程."""
        self._stop_sync.set()
        if self._sync_thread:
            self._sync_thread.join(timeout=10)

    def _sync_loop(self):
        """同步循环: 定期从 SQLite 增量同步到 DuckDB."""
        while not self._stop_sync.is_set():
            try:
                self._sync_incremental()
            except Exception as e:
                _log.error(f"sync loop error: {e}")
            self._stop_sync.wait(_SYNC_INTERVAL_SEC)

    def _sync_incremental(self):
        """增量同步: 从 SQLite 读取新增/更新行 -> DuckDB UPSERT."""
        # 获取各表最大日期/ID
        tables_to_sync = [
            ("daily", "date", ["date", "symbol", "open", "high", "low", "close", "volume", "amount", "turnover"]),
            ("daily_valuation", "date", ["symbol", "date", "pe_ttm", "pb", "ps_ttm", "pcf_ttm", "market_cap", "turnover_rate", "source"]),
            ("stocks", "symbol", ["symbol", "name", "market", "list_date", "industry", "list_status", "delist_date", "total_shares",
                                "pe", "pb", "total_mv", "roe", "high_52w", "low_52w", "circ_mv", "eps", "bvps", "div_yield",
                                "turnover_rate", "pe_ttm", "cfps"]),
        ]

        for table, pk_col, cols in tables_to_sync:
            try:
                self._sync_table(table, pk_col, cols)
            except Exception as e:
                _log.error(f"sync table {table} failed: {e}")

    def _sync_table(self, table: str, pk_col: str, cols: List[str]):
        """同步单表."""
        col_str = ", ".join(cols)
        pk_vals = self.execute(f"SELECT MAX({pk_col}) FROM {table}").fetchone()
        last_pk = pk_vals[0] if pk_vals and pk_vals[0] else None

        if last_pk is None:
            # 全量同步
            sql = f"SELECT {col_str} FROM {table}"
            df = self._sqlite_conn.execute(sql).df()
        else:
            # 增量同步
            sql = f"SELECT {col_str} FROM {table} WHERE {pk_col} > ?"
            df = self._sqlite_conn.execute(sql, (last_pk,)).df()

        if df.empty:
            return

        # 批量 UPSERT 到 DuckDB
        placeholders = ", ".join(["?" for _ in cols])
        update_cols = [c for c in cols if c != pk_col]
        if len(update_cols) > 0:
            set_clause = ", ".join([f"{c} = EXCLUDED.{c}" for c in update_cols])
            upsert_sql = f"""
                INSERT INTO {table} ({col_str}) VALUES ({placeholders})
                ON CONFLICT ({pk_col}) DO UPDATE SET {set_clause}
            """
        else:
            upsert_sql = f"INSERT INTO {table} ({col_str}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"

        # 批量执行
        batch_size = _MIGRATION_BATCH_SIZE
        for i in range(0, len(df), batch_size):
            batch = df.iloc[i:i + batch_size]
            with self._lock:
                self._conn.executemany(upsert_sql, batch.values.tolist())

    # ── 查询接口 (供因子计算/回测/归因使用) ──

    def get_daily(self, symbols: List[str], start: str, end: str,
                  columns: Optional[List[str]] = None) -> pd.DataFrame:
        """获取日线数据 (按需列投影)."""
        if columns is None:
            columns = ["date", "symbol", "open", "high", "low", "close", "volume", "amount", "turnover"]
        col_str = ", ".join(["date", "symbol"] + [c for c in columns if c not in ("date", "symbol")])
        sql = f"""
            SELECT {col_str}
            FROM daily
            WHERE symbol IN ({','.join(['?']*len(symbols))})
              AND date BETWEEN ? AND ?
            ORDER BY date, symbol
        """
        params = list(symbols) + [start, end]
        return self.query_df(sql, tuple(params))

    def get_daily_arrow(self, symbols: List[str], start: str, end: str,
                        columns: Optional[List[str]] = None) -> pa.Table:
        """获取日线数据 (Arrow 零拷贝)."""
        if columns is None:
            columns = ["date", "symbol", "open", "high", "low", "close", "volume", "amount", "turnover"]
        col_str = ", ".join(["date", "symbol"] + [c for c in columns if c not in ("date", "symbol")])
        sql = f"""
            SELECT {col_str}
            FROM daily
            WHERE symbol IN ({','.join(['?']*len(symbols))})
              AND date BETWEEN ? AND ?
            ORDER BY date, symbol
        """
        params = list(symbols) + [start, end]
        return self.query_arrow(sql, tuple(params))

    def get_fundamentals(self, symbols: List[str], date: str) -> pd.DataFrame:
        """获取基本面数据 (PIT 安全: stat_date <= date)."""
        placeholders = ",".join(["?"] * len(symbols))
        sql = f"""
            SELECT s.symbol, s.pe, s.pb, s.total_mv, s.roe, s.high_52w, s.low_52w,
                   s.circ_mv, s.eps, s.bvps, s.div_yield, s.turnover_rate,
                   s.pe_ttm, s.cfps, s.total_shares
            FROM stocks s
            WHERE s.symbol IN ({placeholders})
        """
        return self.query_df(sql, tuple(symbols))

    def get_factor_ic(self, factor_name: str, n_days: int = 20,
                      scope: str = "live") -> pd.DataFrame:
        """获取因子 IC 历史."""
        sql = """
            SELECT date, ic_value, n_stocks, is_ir, oos_ir
            FROM factor_ic_daily
            WHERE factor_name = ? AND scope = ?
            ORDER BY date DESC
            LIMIT ?
        """
        return self.query_df(sql, (factor_name, scope, n_days))

    def save_factor_ic(self, date: str, factor_name: str, ic_value: float,
                       n_stocks: int, is_ir: float = None, oos_ir: float = None,
                       scope: str = "live"):
        """写入因子 IC."""
        self.execute(
            "INSERT OR REPLACE INTO factor_ic_daily "
            "(date, factor_name, ic_value, n_stocks, is_ir, oos_ir, scope) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date, factor_name, ic_value, n_stocks, is_ir, oos_ir, scope)
        )

    def get_universe(self, date: str, exclude_market: str = "BJ") -> List[str]:
        """获取股票池."""
        sql = f"""
            SELECT symbol FROM stocks
            WHERE market != ? AND list_date <= ? AND (delist_date IS NULL OR delist_date > ?)
        """
        rows = self.execute(sql, (exclude_market, date, date)).fetchall()
        return [r[0] for r in rows]

    # ── 因子注册表操作 ──

    def get_factor_registry(self, status: Optional[str] = None) -> pd.DataFrame:
        sql = "SELECT * FROM factor_registry"
        params = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY name"
        return self.query_df(sql, params)

    def register_factor(self, name: str, expression: str, source: str,
                        direction: str, category: str, status: str = "evaluating"):
        self.execute(
            "INSERT OR REPLACE INTO factor_registry "
            "(name, expression, source, direction, category, status) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, expression, source, direction, category, status)
        )

    def update_factor_status(self, name: str, status: str, reason: str = "",
                             retry_count: Optional[int] = None):
        sql = "UPDATE factor_registry SET status = ?, status_reason = ?"
        params = [status, reason]
        if retry_count is not None:
            sql += ", retry_count = ?"
            params.append(retry_count)
        sql += " WHERE name = ?"
        params.append(name)
        self.execute(sql, tuple(params))

    def close(self):
        """关闭连接."""
        if self._sync_thread:
            self.stop_sync()
        if self._conn:
            self._conn.close()
        if self._sqlite_conn:
            self._sqlite_conn.close()
        _log.info("DuckDBManager closed")


# ── 全局单例访问器 ──

_duckdb_manager: Optional[DuckDBManager] = None
_manager_lock = threading.Lock()


def get_duckdb_manager() -> DuckDBManager:
    """获取全局 DuckDBManager 实例."""
    global _duckdb_manager
    with _manager_lock:
        if _duckdb_manager is None:
            _duckdb_manager = DuckDBManager()
        return _duckdb_manager


# ── 兼容层: 逐步替换 DataStore 中的查询方法 ──

class DuckDBDataProxy:
    """DataStore 兼容代理: 逐步将查询重定向到 DuckDB.

    策略:
      - 只读查询 (get_daily, get_fundamentals 等) -> DuckDB
      - 写入/事务 (sync_stock_list, record_trade 等) -> SQLite (DataStore)
    """

    def __init__(self):
        self._duckdb = get_duckdb_manager()

    def get_daily(self, symbols: List[str], start: str, end: str,
                  columns: Optional[List[str]] = None) -> pd.DataFrame:
        return self._duckdb.get_daily(symbols, start, end, columns)

    def get_daily_arrow(self, symbols: List[str], start: str, end: str,
                        columns: Optional[List[str]] = None) -> pa.Table:
        return self._duckdb.get_daily_arrow(symbols, start, end, columns)

    def get_fundamentals(self, symbols: List[str], date: str) -> pd.DataFrame:
        return self._duckdb.get_fundamentals(symbols, date)

    def get_universe(self, date: str, exclude_market: str = "BJ") -> List[str]:
        return self._duckdb.get_universe(date, exclude_market)

    def get_factor_ic(self, factor_name: str, n_days: int = 20,
                      scope: str = "live") -> pd.DataFrame:
        return self._duckdb.get_factor_ic(factor_name, n_days, scope)


# 全局代理实例
_duckdb_proxy: Optional[DuckDBDataProxy] = None


def get_duckdb_proxy() -> DuckDBDataProxy:
    global _duckdb_proxy
    if _duckdb_proxy is None:
        _duckdb_proxy = DuckDBDataProxy()
    return _duckdb_proxy


if __name__ == "__main__":
    # 测试
    mgr = get_duckdb_manager()
    mgr.start_sync()
    time.sleep(5)
    print("DuckDB initialized and sync started")
    print(f"Tables: {mgr.execute('SHOW TABLES').df()}")
    mgr.stop_sync()
    mgr.close()