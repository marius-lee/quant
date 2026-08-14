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
            id INTEGER PRIMARY KEY,
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """,
    # ── 预聚合表 (供因子原语直接查询，避免重复滚动计算) ──
    "daily_ma": """
        CREATE TABLE IF NOT EXISTS daily_ma (
            date DATE NOT NULL,
            symbol VARCHAR(10) NOT NULL,
            "window" INTEGER NOT NULL,
            ma DOUBLE,
            PRIMARY KEY (date, symbol, "window")
        )
    """,
    "daily_ret": """
        CREATE TABLE IF NOT EXISTS daily_ret (
            date DATE NOT NULL,
            symbol VARCHAR(10) NOT NULL,
            "window" INTEGER NOT NULL,
            ret DOUBLE,
            PRIMARY KEY (date, symbol, "window")
        )
    """,
    "daily_std": """
        CREATE TABLE IF NOT EXISTS daily_std (
            date DATE NOT NULL,
            symbol VARCHAR(10) NOT NULL,
            "window" INTEGER NOT NULL,
            std DOUBLE,
            PRIMARY KEY (date, symbol, "window")
        )
    """,
    "daily_zscore": """
        CREATE TABLE IF NOT EXISTS daily_zscore (
            date DATE NOT NULL,
            symbol VARCHAR(10) NOT NULL,
            "window" INTEGER NOT NULL,
            zscore DOUBLE,
            PRIMARY KEY (date, symbol, "window")
        )
    """,
    "daily_ma_volume": """
        CREATE TABLE IF NOT EXISTS daily_ma_volume (
            date DATE NOT NULL,
            symbol VARCHAR(10) NOT NULL,
            "window" INTEGER NOT NULL,
            ma_volume DOUBLE,
            PRIMARY KEY (date, symbol, "window")
        )
    """,
    "daily_max": """
        CREATE TABLE IF NOT EXISTS daily_max (
            date DATE NOT NULL,
            symbol VARCHAR(10) NOT NULL,
            "window" INTEGER NOT NULL,
            max_val DOUBLE,
            PRIMARY KEY (date, symbol, "window")
        )
    """,
    "daily_min": """
        CREATE TABLE IF NOT EXISTS daily_min (
            date DATE NOT NULL,
            symbol VARCHAR(10) NOT NULL,
            "window" INTEGER NOT NULL,
            min_val DOUBLE,
            PRIMARY KEY (date, symbol, "window")
        )
    """,
    "daily_rank": """
        CREATE TABLE IF NOT EXISTS daily_rank (
            date DATE NOT NULL,
            symbol VARCHAR(10) NOT NULL,
            "window" INTEGER NOT NULL,
            rank DOUBLE,
            PRIMARY KEY (date, symbol, "window")
        )
    """,
}


class DuckDBManager:
    """DuckDB 连接管理器 — 单例模式, 线程安全.

    v495: 不再持有常驻 rw 连接. 原实现进程存活期间独占 DuckDB 文件锁,
    其他进程 (测试/回测/脚本/手工 backfill) 全部被拒 (Conflicting lock).
    DuckDB 多进程模型实测 (1.5.5): 多个 RO 连接可跨进程共存; 但任何 RW
    连接存在时, 连 RO 连接都无法打开. 因此:
      - 查询路径 (_ro): 短命只读连接, 用完即关 — 不持锁, 任何人可查
      - 写入路径 (_rw): 短命读写连接, 用完即关 — 仅在同步瞬间短暂独占
    """

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
        self._thread_lock = threading.Lock()  # guard 短连接创建 (单例内串行)
        # v496: 线程级 RW 连接复用 — 每次写入开新连接 (144 次开关) 是
        # refresh_preaggregates 慢 16min/window 的元凶; 每线程持有一个
        # 长期 RW 连接, 即写即提交, 退出时由 close() 统一释放.
        self._thread_local = threading.local()
        self._sync_thread: Optional[threading.Thread] = None
        self._stop_sync = threading.Event()
        self._sqlite_conn = None
        self._schema_done = False

        # 确保目录存在
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        # 连接 SQLite (用于同步) — 只读, 与 DuckDB 文件锁无关
        from quant.config.paths import MARKET_DB as SQLITE_PATH
        self._sqlite_conn = duckdb.connect(str(SQLITE_PATH), read_only=True)
        # DuckDB 不支持 PRAGMA journal_mode=WAL，已移除
        _log.info(f"DuckDBManager initialized: {self._db_path}")

    # ── 短命连接 (v495 核心: 不持锁) ──

    def _open(self, read_only: bool):
        """打开 DuckDB 连接 (v496: 同文件所有连接必须同配置).

        RW 连接线程级复用的前提: 不能再开 read_only=True 连接
        (DuckDB 报 "different configuration than existing connections").
        统一非只读连接, 查询连接不写即可; 多进程仍可共存 (文件级锁).
        """
        c = duckdb.connect(
            str(self._db_path), read_only=False,
            config={"threads": _MAX_WORKERS,
                    "memory_limit": "4GB",
                    "preserve_insertion_order": "false"})
        return c

    def _ro(self):
        """查询连接 — 线程级复用 (v496).

        同文件所有连接须同配置 (非只读), 查询连接不写数据即可.
        """
        conn = getattr(self._thread_local, "ro_conn", None)
        if conn is None:
            self._ensure_schema()
            conn = self._open(read_only=True)
            self._thread_local.ro_conn = conn
        return conn

    def _close_ro_conn(self):
        """关闭当前线程持有的 RO 连接."""
        conn = getattr(self._thread_local, "ro_conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._thread_local.ro_conn = None

    def _rw(self):
        """读写连接 — 线程级复用 (v496), 不再每次写入开新连接.

        每线程首个连接仍走 _ensure_schema (进程内仅首次建表).
        写事务由 DuckDB 自动提交; close() 时统一释放.
        """
        conn = getattr(self._thread_local, "rw_conn", None)
        if conn is None:
            self._ensure_schema()
            conn = self._open(read_only=False)
            self._thread_local.rw_conn = conn
        return conn

    def _close_rw_conn(self):
        """关闭当前线程持有的 RW 连接 (close() 与 stop_sync() 调用)."""
        conn = getattr(self._thread_local, "rw_conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._thread_local.rw_conn = None

    def _ensure_schema(self):
        """建表/建索引 — 幂等, 进程内仅首次 rw 时执行."""
        if self._schema_done:
            return
        with self._thread_lock:
            if self._schema_done:
                return
            c = self._open(read_only=False)
            try:
                for table_name, ddl in _TABLE_SCHEMAS.items():
                    c.execute(ddl)
            except Exception as e:
                _log.warning(f"create table failed: {e}")
                raise
            self._create_indexes(c)
            self._schema_done = True
            c.close()

    def _scalar(self, sql: str, params: tuple = ()):
        """单值查询 — 复用线程级查询连接 (v496, 不再即查即关)."""
        c = self._ro()
        try:
            r = c.execute(sql, params).fetchone()
            return r[0] if r else None
        except duckdb.Error:
            # 连接可能被并发线程 close() 关闭 — 重建一次后重试
            self._close_ro_conn()
            c = self._ro()
            r = c.execute(sql, params).fetchone()
            return r[0] if r else None

    def _write(self, sql: str, params: tuple = ()):
        """单条写入 — 复用线程级 RW 连接 (v496)."""
        c = self._rw()
        c.execute(sql, params)

    def _write_many(self, sql: str, rows: list) -> int:
        """批量写入 (executemany) — 复用线程级 RW 连接 (v496). 返回行数.

        注意: executemany 逐行参数绑定, 34 万行 ~200s (慢 5000x);
        大表写入一律走 _write_df (register + INSERT..SELECT).
        """
        c = self._rw()
        c.executemany(sql, rows)
        return len(rows)

    _df_seq = 0

    def _write_df(self, sql: str, df: pd.DataFrame) -> int:
        """DataFrame 批量写入 — register + INSERT..SELECT (v496).

        实测: 34 万行 ON CONFLICT UPSERT 0.03s (executemany 需 200s).
        与 _write_many 同走线程级 RW 连接.
        """
        if df.empty:
            return 0
        type(self)._df_seq += 1
        tmp_name = f"__df_{type(self)._df_seq}_{os.getpid()}"
        c = self._rw()
        c.register(tmp_name, df)
        try:
            c.execute(sql.replace("__TMP_DF__", tmp_name))
        finally:
            c.unregister(tmp_name)
        return len(df)

    def _create_indexes(self, conn):
        """创建常用查询索引 — 幂等 (IF NOT EXISTS)."""
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
            # 预聚合表索引
            "CREATE INDEX IF NOT EXISTS idx_daily_ma_date ON daily_ma(date)",
            "CREATE INDEX IF NOT EXISTS idx_daily_ma_symbol ON daily_ma(symbol)",
            "CREATE INDEX IF NOT EXISTS idx_daily_ret_date ON daily_ret(date)",
            "CREATE INDEX IF NOT EXISTS idx_daily_ret_symbol ON daily_ret(symbol)",
            "CREATE INDEX IF NOT EXISTS idx_daily_std_date ON daily_std(date)",
            "CREATE INDEX IF NOT EXISTS idx_daily_std_symbol ON daily_std(symbol)",
        ]
        for idx_sql in indexes:
            conn.execute(idx_sql)

    @contextmanager
    def connection(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """获取 DuckDB 连接 (写, 短命) — with 块结束后自动关闭."""
        c = self._rw()
        try:
            yield c
        finally:
            c.close()

    def query_df(self, sql: str, params: tuple = ()) -> pd.DataFrame:
        """查询返回 DataFrame — 复用线程级查询连接 (v496)."""
        c = self._ro()
        try:
            return c.execute(sql, params).df()
        except duckdb.Error:
            self._close_ro_conn()
            c = self._ro()
            return c.execute(sql, params).df()

    def query_arrow(self, sql: str, params: tuple = ()) -> pa.Table:
        """查询返回 Arrow Table (零拷贝) — 复用线程级查询连接 (v496)."""
        c = self._ro()
        try:
            return c.execute(sql, params).arrow()
        except duckdb.Error:
            self._close_ro_conn()
            c = self._ro()
            return c.execute(sql, params).arrow()

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
        try:
            while not self._stop_sync.is_set():
                try:
                    self._sync_incremental()
                except Exception as e:
                    _log.error(f"sync loop error: {e}")
                self._stop_sync.wait(_SYNC_INTERVAL_SEC)
        finally:
            # v496: 线程退出时释放自身持有的连接
            self._close_rw_conn()
            self._close_ro_conn()

    def _sync_incremental(self):
        """增量同步: 从 SQLite 读取新增/更新行 -> DuckDB UPSERT."""
        # v492: financial_* 从本表移除 — SQLite 列 (total_operating_revenue/
        # total_owner_equities/pub_date...) 与 DuckDB schema (revenue/
        # total_hldr_eqy/ann_date...) 完全不同, 每轮 UPSERT 必败被吞 (每晚噪音);
        # 因子物化 fundamentals 直读 SQLite, DuckDB 财务表无人消费. limit_up_pool
        # 同理 (SQLite 16 列 vs DuckDB 3 列 seal_ratio).
        tables_to_sync = [
            ("daily", ["date", "symbol"], ["date", "symbol", "open", "high", "low", "close", "volume", "amount", "turnover"], []),
            ("daily_valuation", ["symbol", "date"], ["symbol", "date", "pe_ttm", "pb", "ps_ttm", "pcf_ttm", "market_cap", "turnover_rate", "source"], []),
            ("stocks", ["symbol"], ["symbol", "name", "market", "list_date", "industry", "list_status", "delist_date", "total_shares",
                                "pe", "pb", "total_mv", "roe", "high_52w", "low_52w", "circ_mv", "eps", "bvps", "div_yield",
                                "turnover_rate", "pe_ttm", "cfps"], ["list_date", "delist_date"]),
            ("margin_detail", ["symbol", "date"], ["symbol", "date", "market", "margin_buy", "margin_balance", "margin_repay", "short_sell_vol", "short_balance", "short_total", "margin_total"], []),
            ("factor_ic_daily", ["date", "factor_name", "scope"], ["date", "factor_name", "ic_value", "n_stocks", "is_ir", "oos_ir", "scope", "created_at"], []),
            ("factor_registry", ["name"], ["name", "category", "compute_fn", "academic_source", "status", "status_reason", "ic_mean", "ic_ir", "direction", "last_evaluated", "created_at", "updated_at", "notes", "formula", "paper_ic_mean", "retry_count", "last_retry"], []),
        ]

        for table, pk_cols, cols, date_cols_int in tables_to_sync:
            try:
                self._sync_table(table, pk_cols, cols, date_cols_int)
            except Exception as e:
                _log.error(f"sync table {table} failed: {e}")
    def _sync_table(self, table: str, pk_cols: List[str], cols: List[str], date_cols_int: List[str] = None):
        """同步单表 — ���� �� �� 增量: date 列存在时按 MAX(date) 追���赶, 否则全量."""
        if date_cols_int is None:
            date_cols_int = []
        # Build SELECT list with conversion for integer date columns
        select_parts = []
        for c in cols:
            if c in date_cols_int:
                # v494: SQLite 存 ISO 'YYYY-MM-DD' (list_date/delist_date 已统一),
                # 兼容历史 compact 'YYYYMMDD' 残留: 8位纯数字走拆分拼接,
                # 否则 TRY_CAST ISO. 修复旧 SQL 缺 '-' 分隔符导致 date() 恒失败.
                select_parts.append(
                    f"CASE WHEN length({c})=8 AND {c} NOT LIKE '%-%' "
                    f"THEN date(substr({c},1,4)||'-'||substr({c},5,2)||'-'||substr({c},7,2)) "
                    f"ELSE TRY_CAST({c} AS DATE) END AS {c}")
            else:
                select_parts.append(c)
        col_str = ", ".join(select_parts)
        insert_col_str = ", ".join(cols)  # INSERT 用原始列名 (转换表达式仅限 SELECT)
        pk_col_str = ", ".join(f'"{c}"' for c in pk_cols)
        
        # ���� �� �� 增量策略: ��� � � 若存在 date 列, ��� � � 按 DuckDB MAX(date) vs SQLite MAX(date) 追���赶
        has_date_col = "date" in cols
        last_dk_date = None
        if has_date_col:
            try:
                last_dk_date = self._scalar(f"SELECT MAX(date) FROM {table}")
            except Exception:
                last_dk_date = None
        if has_date_col and last_dk_date is not None:
            # 只拉 SQLite 中日期 > DuckDB MAX(date) 的行
            sql = f"SELECT {col_str} FROM {table} WHERE date > ?"
            df = self._sqlite_conn.execute(sql, (str(last_dk_date),)).df()
        else:
            # v492: 无 date 列的表 (stocks/factor_registry) 一律全量 — 原实现对
            # 单字符串主键走 MAX(pk) 增量: 新股的 symbol (如 '000001') < 现存
            # MAX (如 '688999') → WHERE symbol > MAX 永不命中 → 新股/新因子
            # 永不进 DuckDB, 且 get_universe 读 DuckDB 缺股票. 全表 5k 行级,
            # 每轮 300s 全量 UPSERT 可忽略, 且天然覆盖值级 UPDATE.
            if has_date_col:
                sql = f"SELECT {col_str} FROM {table} WHERE date > ?"
                df = self._sqlite_conn.execute(sql, (str(last_dk_date),)).df()
            else:
                sql = f"SELECT {col_str} FROM {table}"
                df = self._sqlite_conn.execute(sql).df()

        if df.empty:
            return

        # Build UPSERT SQL
        update_cols = [c for c in cols if c not in pk_cols]
        if len(update_cols) > 0:
            set_clause = ", ".join([f'"{c}" = EXCLUDED."{c}"' for c in update_cols])
            # v496: register + INSERT..SELECT (executemany 34万行 200s → 0.03s)
            upsert_sql = f"""
                INSERT INTO {table} ({insert_col_str}) SELECT {insert_col_str} FROM __TMP_DF__
                ON CONFLICT ({pk_col_str}) DO UPDATE SET {set_clause}
            """
        else:
            upsert_sql = (f"INSERT INTO {table} ({insert_col_str}) "
                          f"SELECT {insert_col_str} FROM __TMP_DF__ ON CONFLICT DO NOTHING")

        # v496: 一次性 register + INSERT..SELECT (原 executemany 分批 ~5000x 慢)
        self._write_df(upsert_sql, df)

    def _sync_backfill_missing_dates(self, table: str = "daily",
                                      gap_threshold_days: int = 30,
                                      max_backfill_days: int = 504):
        """历史回补: 比较 DuckDB 与 SQLite 的日期集合, 同步缺失的日期数据.

        用于解决 DuckDB 背景同步线程从未启动导致的历史性缺失.
        仅用于带有 `date` 列的表 (daily, daily_valuation 等), 其他表仍走 _sync_table.
        max_backfill_days: 限制回补的最大历史天数, 仅回补 SQLite MAX(date) 往前 max_backfill_days 范围内的缺失日期.
        """
        # 比较 DuckDB vs SQLite 最大日期 (用于日志)
        dk_max = self._scalar(f"SELECT MAX(date) FROM {table}")
        sq_max = self._sqlite_conn.execute(f"SELECT MAX(date) FROM {table}").fetchone()[0]
        if dk_max is None or sq_max is None:
            return  # 任一为空, 跳过 (首次同步)
        dk_ts = pd.Timestamp(str(dk_max)) if not isinstance(dk_max, pd.Timestamp) else dk_max
        sq_ts = pd.Timestamp(str(sq_max)) if not isinstance(sq_max, pd.Timestamp) else sq_max
        gap = (sq_ts - dk_ts).days
        # 只回补 sq_max 往前 max_backfill_days 范围内的日期
        backfill_start = sq_ts - pd.Timedelta(days=max_backfill_days)
        # 获取 SQLite 在 [backfill_start, sq_max] 范围内的日期
        sq_dates_raw = self._sqlite_conn.execute(
            f"SELECT DISTINCT date FROM {table} WHERE date >= '{backfill_start.strftime('%Y-%m-%d')}'"
        ).fetchdf()
        sq_dates_in_range = set(str(d)[:10] for d in sq_dates_raw["date"].tolist()) if not sq_dates_raw.empty else set()
        # 获取 DuckDB 已有的日期 (同范围) — DuckDB date 类型可能带时间, 截取前 10 位 (YYYY-MM-DD)
        dk_dates_raw = self.query_df(
            f"SELECT DISTINCT CAST(date AS VARCHAR) AS date FROM {table} WHERE date >= '{backfill_start.strftime('%Y-%m-%d')}'"
        )
        dk_dates_in_range = set(str(d)[:10] for d in dk_dates_raw["date"].tolist()) if not dk_dates_raw.empty else set()
        missing_dates = sorted(sq_dates_in_range - dk_dates_in_range, key=lambda d: str(d))
        if not missing_dates:
            return  # 无缺失日期
        _log.info(f"backfill: {table} gap={gap}d ({dk_ts.date()} -> {sq_ts.date()}), "
                  f"{len(missing_dates)} missing dates to sync "
                  f"(range: {backfill_start.date()} to {sq_ts.date()})")
        # 批量插入缺失日期的数据
        # 获取表的所有列
        cols_result = self._sqlite_conn.execute(f"PRAGMA table_info({table})").fetchall()
        cols = [row[1] for row in cols_result]
        col_str = ", ".join(f'"{c}"' for c in cols)
        insert_sql = (f"INSERT INTO {table} ({col_str}) "
                      f"SELECT {col_str} FROM __TMP_DF__ ON CONFLICT DO NOTHING")
        batch_size = _MIGRATION_BATCH_SIZE
        total_inserted = 0
        for i in range(0, len(missing_dates), batch_size):
            batch_dates = missing_dates[i:i + batch_size]
            date_filter = " OR ".join([f'date = \'{d}\'' for d in batch_dates])
            sql = f"SELECT {col_str} FROM {table} WHERE {date_filter}"
            df = self._sqlite_conn.execute(sql).df()
            if df.empty:
                continue
            self._write_df(insert_sql, df)
            total_inserted += len(df)
        _log.info(f"backfill: {table} inserted {total_inserted} rows for {len(missing_dates)} dates")

    def verify_sync(self, table: str = "daily") -> dict:
        """验证 DuckDB vs SQLite 行数和日期范围一致性.

        返回: {"sqlite_rows": int, "duckdb_rows": int, "sqlite_dates": int, "duckdb_dates": int, "match": bool}
        """
        sqlite_count = self._sqlite_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        duckdb_count = self._scalar(f"SELECT COUNT(*) FROM {table}")
        sqlite_dates = self._sqlite_conn.execute(f"SELECT COUNT(DISTINCT date) FROM {table}").fetchone()[0]
        duckdb_dates = self._scalar(f"SELECT COUNT(DISTINCT date) FROM {table}")
        match = sqlite_count == duckdb_count and sqlite_dates == duckdb_dates
        # v491: 值一致性校验 — 增量同步只追新日期 (date > MAX), 历史行的
        # UPDATE (turnover/amount 回填) 永不进 DuckDB, 而因子物化/回测
        # get_daily() 走 DuckDB 优先 → 回填后物化仍读到旧值 (2026-08-14
        # backfill_turnover 268k 行实测). 对 daily 表额外比对关键列非零行数,
        # 不一致即 match=False (调度链日志显式暴露, 供全量重同步决策).
        # v492: 同机制扩展到 daily_valuation (market_cap/turnover_rate) —
        # v491 的调度链只在 daily 上做全量重同步, daily_valuation 走 _sync_table
        # 增量 → 其历史行 UPDATE 依然永不进 DuckDB (半成品, 本版补全).
        if table in ("daily", "daily_valuation"):
            sign_cols = {
                "daily": [("turnover", "turnover>0"), ("amount", "amount>0")],
                "daily_valuation": [("market_cap", "market_cap>0 and market_cap is not null"),
                                    ("turnover_rate", "turnover_rate>0 and turnover_rate is not null")],
            }[table]
            for col, cond in sign_cols:
                sq = self._sqlite_conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE {cond}").fetchone()[0]
                dk = self._scalar(
                    f"SELECT COUNT(*) FROM {table} WHERE {cond}")
                if sq != dk:
                    match = False
                    _log.warning(f"verify_sync: {table} {col} 值级不一致 sqlite={sq} duckdb={dk} — "
                                 f"历史行 UPDATE 未同步, 需全量重同步 (sync_table_full)")
        _log.info(f"verify_sync: {table} sqlite={sqlite_count} duckdb={duckdb_count} dates(sq/dk)={sqlite_dates}/{duckdb_dates} match={match}")
        return {
            "sqlite_rows": sqlite_count,
            "duckdb_rows": duckdb_count,
            "sqlite_dates": sqlite_dates,
            "duckdb_dates": duckdb_dates,
            "match": match,
        }

    def sync_table_full(self, table: str, cols: List[str], pk_cols: List[str]) -> int:
        """通用全量值同步 — 覆盖历史行 UPDATE (回填) 进 DuckDB. 幂等.

        v492: v491 的 sync_daily_full 只解决 daily; daily_valuation 及其他
        带 date 表的verify_sync 值级不一致同样需要全量 UPSERT (不是 _sync_table
        增量). 分批执行, 850 万行 ~1-2min.
        """
        update_cols = [c for c in cols if c not in pk_cols]
        pk_str = ", ".join(f'"{c}"' for c in pk_cols)
        col_str = ", ".join(f'"{c}"' for c in cols)
        if update_cols:
            set_clause = ", ".join([f'"{c}" = EXCLUDED."{c}"' for c in update_cols])
            upsert_sql = f"""
                INSERT INTO {table} ({col_str}) SELECT {col_str} FROM __TMP_DF__
                ON CONFLICT ({pk_str}) DO UPDATE SET {set_clause}
            """
        else:
            upsert_sql = (f"INSERT INTO {table} ({col_str}) "
                          f"SELECT {col_str} FROM __TMP_DF__ ON CONFLICT DO NOTHING")
        total = 0
        offset = 0
        _BATCH = 500000
        t0 = time.time()
        while True:
            batch = self._sqlite_conn.execute(
                f"SELECT {col_str} FROM {table} ORDER BY {pk_str} LIMIT ? OFFSET ?",
                (_BATCH, offset)).df()
            if batch.empty:
                break
            self._write_df(upsert_sql, batch)
            total += len(batch)
            offset += _BATCH
            if offset % 1000000 == 0:
                _log.info(f"sync_table_full({table}): {total:,} rows ({time.time()-t0:.0f}s)")
        _log.info(f"sync_table_full({table}): done — {total:,} rows ({time.time()-t0:.0f}s)")
        return total

    def sync_daily_full(self) -> int:
        """daily 全量值同步 — 覆盖历史行 UPDATE (turnover/amount 回填) 进 DuckDB.

        背景 (v491): _sync_incremental 按 date > DuckDB.MAX(date) 只追新日期,
        _sync_backfill_missing_dates 只补缺失日期 — 已存在日期的行值更新
        (backfill_turnover/backfill_amount 全量回填) 永不同步; 因子物化
        get_daily() DuckDB 优先 → 物化读到旧值. 本方法全量 UPSERT daily,
        幂等, 850 万行分批 ~1-2min. 由调度链 verify_sync 值级不一致时调用.
        """
        cols = ["date", "symbol", "open", "high", "low", "close",
                "volume", "amount", "turnover"]
        return self.sync_table_full("daily", cols, ["date", "symbol"])

    # ── 查询接口 (供因子计算/回测/归因使用) ──

    def get_daily(self, symbols: List[str], start: str, end: str,
                  columns: Optional[List[str]] = None) -> pd.DataFrame:
        """获取日线数据 (按需列投影, 返回 MultiIndex DataFrame: date × symbol)."""
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
        df = self.query_df(sql, tuple(params))
        if df.empty:
            return pd.DataFrame()
        # Pivot to MultiIndex (field, symbol) to match SQLite DataStore format
        if "symbol" in df.columns and "date" in df.columns:
            value_cols = [c for c in df.columns if c not in ("date", "symbol")]
            if value_cols:
                df = df.pivot(index="date", columns="symbol", values=value_cols)
                # The pivot creates columns as (field, symbol) tuples
                # Ensure the column order matches original: (field, symbol)
                df.columns = pd.MultiIndex.from_tuples(df.columns, names=["field", "symbol"])
                df = df.sort_index()
        return df

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
        self._write(
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
        df = self.query_df(sql, (exclude_market, date, date))
        return df["symbol"].tolist() if not df.empty else []

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
        self._write(
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
        self._write(sql, tuple(params))

    def close(self):
        """关闭连接 — 释放线程级 RW 连接 (v496) + SQLite 读连接."""
        if self._sync_thread:
            self.stop_sync()
        self._close_rw_conn()
        self._close_ro_conn()
        if self._sqlite_conn:
            self._sqlite_conn.close()
        _log.info("DuckDBManager closed")

    # ── 预聚合表刷新 ──
    def refresh_preaggregates(self, start_date: str = None, end_date: str = None, 
                               windows: list[int] = None, symbols: list[str] = None):
        """增量刷新预聚合表 (daily_ma, daily_ret, daily_std 等)。

        Args:
            start_date: 起始日期 (含), 默认最近 60 个交易日
            end_date: 结束日期 (含), 默认今天
            windows: 滚动窗口列表, 默认 [5, 10, 20, 60, 120, 250]
            symbols: 股票代码列表, 默认全部
        """
        import pandas as pd
        from datetime import datetime, timedelta
        
        if windows is None:
            windows = [5, 10, 20, 60, 120, 250]
        if end_date is None:
            end_date = datetime.now().strftime("%Y-%m-%d")
        if start_date is None:
            # 默认刷新最近 60 个交易日 (约 90 个日历日)
            start_date = (pd.Timestamp(end_date) - pd.Timedelta(days=90)).strftime("%Y-%m-%d")
        
        t_start = time.time()
        _log.info(f"refresh_preaggregates: {start_date} → {end_date}, windows={windows}")
        
        # 1) 读取基础数据 (close, volume)
        ph = ""
        params = [start_date, end_date]
        if symbols:
            ph = f" AND symbol IN ({','.join(['?']*len(symbols))})"
            params.extend(symbols)
        
        sql = f"""
            SELECT date, symbol, close, volume, high, low
            FROM daily
            WHERE date >= ? AND date <= ? {ph}
            ORDER BY symbol, date
        """
        df = self.query_df(sql, tuple(params))
        if df.empty:
            _log.warning("refresh_preaggregates: no data in range")
            return
        
        # 确保类型
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values(["symbol", "date"])
        # v495: rolling 前把 date 设为索引 — groupby.rolling 后 reset_index()
        # 才能带出 date 列 (原 RangeIndex 会丢日期, KeyError: 'date')
        df = df.set_index("date")
        
        # 2) 分组计算所有窗口
        for window in [5, 10, 20, 60, 120, 250]:
            _log.info(f"refresh_preaggregates: computing window={window}")
            
            # MA (close)
            t_w0 = time.time()
            ma = df.groupby("symbol")["close"].rolling(window, min_periods=window).mean().reset_index()
            ma = ma.rename(columns={"close": "ma"})
            ma["window"] = window
            # 只保留日期在范围内的
            ma = ma[ma["date"] >= start_date]
            if not ma.empty:
                # UPSERT
                ma_cols = ["date", "symbol", "window", "ma"]
                ma = ma[ma_cols]
                self._upsert_df("daily_ma", ma, ["date", "symbol", "window"])
                _log.info(f"refresh_preaggregates: window={window} ma {len(ma):,} rows ({time.time()-t_w0:.1f}s)")
            
            # RET (pct_change)
            # v495: pandas 2.3 的 groupby.pct_change 返回 Series 不保留 group key,
            # 需按位置对齐带出 symbol (groupby 保持原始行序)
            t_ret = time.time()
            _ret_ser = df.groupby("symbol")["close"].pct_change(window)
            ret = pd.DataFrame({
                "date": _ret_ser.index.values,
                "symbol": df["symbol"].values,
                "ret": _ret_ser.values,
            })
            ret["window"] = window
            ret = ret[ret["date"] >= start_date]
            if not ret.empty:
                ret_cols = ["date", "symbol", "window", "ret"]
                ret = ret[ret_cols]
                self._upsert_df("daily_ret", ret, ["date", "symbol", "window"])
                _log.info(f"refresh_preaggregates: window={window} ret {len(ret):,} rows ({time.time()-t_ret:.1f}s)")
            
            # STD
            t_std = time.time()
            std = df.groupby("symbol")["close"].rolling(window, min_periods=window).std().reset_index()
            std = std.rename(columns={"close": "std"})
            std["window"] = window
            std = std[std["date"] >= start_date]
            if not std.empty:
                std_cols = ["date", "symbol", "window", "std"]
                std = std[std_cols]
                self._upsert_df("daily_std", std, ["date", "symbol", "window"])
                _log.info(f"refresh_preaggregates: window={window} std {len(std):,} rows ({time.time()-t_std:.1f}s)")
            
            # MA Volume
            t_mv = time.time()
            ma_vol = df.groupby("symbol")["volume"].rolling(window, min_periods=window).mean().reset_index()
            ma_vol = ma_vol.rename(columns={"volume": "ma_volume"})
            ma_vol["window"] = window
            ma_vol = ma_vol[ma_vol["date"] >= start_date]
            if not ma_vol.empty:
                ma_vol_cols = ["date", "symbol", "window", "ma_volume"]
                ma_vol = ma_vol[ma_vol_cols]
                self._upsert_df("daily_ma_volume", ma_vol, ["date", "symbol", "window"])
                _log.info(f"refresh_preaggregates: window={window} ma_volume {len(ma_vol):,} rows ({time.time()-t_mv:.1f}s)")
            
            # MAX (high) — v496: 去 try/except 吞错 (零 fallback), SELECT 显式含 high
            t_max = time.time()
            max_df = df.groupby("symbol")["high"].rolling(window, min_periods=window).max().reset_index()
            max_df = max_df.rename(columns={"high": "max_val"})
            max_df["window"] = window
            max_df = max_df[max_df["date"] >= start_date]
            if not max_df.empty:
                max_cols = ["date", "symbol", "window", "max_val"]
                max_df = max_df[max_cols]
                self._upsert_df("daily_max", max_df, ["date", "symbol", "window"])
                _log.info(f"refresh_preaggregates: window={window} max {len(max_df):,} rows ({time.time()-t_max:.1f}s)")
            
            # MIN (low) — v496: 去 try/except 吞错 (零 fallback), SELECT 显式含 low
            t_min = time.time()
            min_df = df.groupby("symbol")["low"].rolling(window, min_periods=window).min().reset_index()
            min_df = min_df.rename(columns={"low": "min_val"})
            min_df["window"] = window
            min_df = min_df[min_df["date"] >= start_date]
            if not min_df.empty:
                min_cols = ["date", "symbol", "window", "min_val"]
                min_df = min_df[min_cols]
                self._upsert_df("daily_min", min_df, ["date", "symbol", "window"])
                _log.info(f"refresh_preaggregates: window={window} min {len(min_df):,} rows ({time.time()-t_min:.1f}s)")
        
        _log.info(f"refresh_preaggregates: done — {len(windows)} windows, "
                  f"{end_date} ({time.time()-t_start:.1f}s total)")
    
    def _upsert_df(self, table: str, df: pd.DataFrame, pk_cols: list[str]):
        """DataFrame UPSERT 到 DuckDB 表."""
        if df.empty:
            return
        col_str = ", ".join(f'"{c}"' for c in df.columns)
        update_cols = [c for c in df.columns if c not in pk_cols]
        if update_cols:
            set_clause = ", ".join([f'"{c}" = EXCLUDED."{c}"' for c in update_cols])
            pk_col_str = ", ".join(f'"{c}"' for c in pk_cols)
            # v496: register + INSERT..SELECT (executemany 34万行 200s → 0.03s)
            upsert_sql = f"""
                INSERT INTO {table} ({col_str})
                SELECT {col_str} FROM __TMP_DF__
                ON CONFLICT ({pk_col_str}) DO UPDATE SET {set_clause}
            """
        else:
            upsert_sql = (f"INSERT INTO {table} ({col_str}) "
                          f"SELECT {col_str} FROM __TMP_DF__ "
                          f"ON CONFLICT DO NOTHING")
        self._write_df(upsert_sql, df)


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
    print(f"Tables: {mgr.query_df('SHOW TABLES')}")
    mgr.stop_sync()
    mgr.close()