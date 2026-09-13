"""SQLite 数据仓库 — 全A股 + 增量更新 (v435: 读查询分流到 DuckDB).

DataStoreCoreMixin: 初始化 + 连接管理 (模板2a: 计算层)
列名常量 + DDL 定义 (DDL 与查询共引，防 value→raw_value 类脱节)
"""
import os
import sqlite3
import threading
from typing import Optional
from quant.utils.date import to_str, to_compact, today_str, DEFAULT_START_DATE
import pandas as pd
from quant.config.paths import MARKET_DB
from quant.config.constants import _require_cfg
from quant.data.repos._base import DatabaseManager
from quant.data.duckdb_store import DuckDBDataProxy
from quant.utils.logger import get_logger

logger = get_logger("data.store")

# test-v303: 注册 key 无批量K线权限 (tickflow.PermissionError) 时置 True
_TICKFLOW_BATCH_NO_PERM = False

# ── 列名常量 ──
D_DATE, D_SYMBOL, D_OPEN, D_HIGH, D_LOW, D_CLOSE = "date", "symbol", "open", "high", "low", "close"
D_VOLUME, D_AMOUNT, D_TURNOVER, D_PE_TTM, D_PB, D_TOTAL_MV, D_CIRC_MV = "volume", "amount", "turnover", "pe_ttm", "pb", "total_mv", "circ_mv"
S_SYMBOL, S_NAME, S_MARKET, S_LIST_DATE, S_INDUSTRY = "symbol", "name", "market", "list_date", "industry"
F_PE, F_PB, F_TOTAL_MV, F_CIRC_MV, F_ROE, F_EPS, F_BVPS = "pe", "pb", "total_mv", "circ_mv", "roe", "eps", "bvps"


class DataStoreCoreMixin:
    """DataStore 核心类 — 初始化 + 连接管理 (模板2a: 计算层)."""

    def __init__(self, db_path: str = None, tushare_token: str = None):
        self.db_path = db_path or MARKET_DB
        _token = tushare_token or os.environ.get("TUSHARE_TOKEN", "") or _require_cfg("data.tushare_token")
        self.token = _token
        self._conn = None
        self._local = threading.local()
        self._lock = threading.Lock()
        self._query_cache: dict = {}
        self._backend = None
        self._stock_list_cache = None
        self._industry_cache = None
        self._tushare_limiter = None
        self._akshare_limiter = None
        self._duckdb_proxy: Optional["DuckDBDataProxy"] = None
        conn = self._connect()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS stocks (
                symbol TEXT PRIMARY KEY, name TEXT, market TEXT, list_date TEXT, industry TEXT
            );
            CREATE TABLE IF NOT EXISTS daily (
                symbol TEXT, date TEXT, open REAL, high REAL, low REAL,
                close REAL, volume REAL, amount REAL, turnover REAL,
                PRIMARY KEY (symbol, date)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_daily_pk ON daily(symbol, date);
            CREATE INDEX IF NOT EXISTS idx_daily_date ON daily(date);
            CREATE INDEX IF NOT EXISTS idx_stocks_market_sym ON stocks(market, symbol);
            CREATE TABLE IF NOT EXISTS meta_key_value (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS limit_up_pool (symbol TEXT PRIMARY KEY, date TEXT, pct REAL);
            CREATE TABLE IF NOT EXISTS limit_down_pool (symbol TEXT PRIMARY KEY, date TEXT, pct REAL);
        """)
        conn.close()

    def _connect(self):
        if hasattr(self._local, 'conn') and self._local.conn is not None:
            return self._local.conn
        return self._make_conn()

    def _make_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        self._local.conn = conn
        return conn

    def close(self):
        if hasattr(self._local, 'conn') and self._local.conn is not None:
            try:
                self._local.conn.close()
            except Exception:
                pass
            finally:
                self._local.conn = None

    def _init_cache_instance(self):
        from quant.data.cache import get_backend, DataCache, RateLimiter
        self._backend = get_backend()
        self._stock_list_cache = DataCache("stock_list", ttl=86400)
        self._industry_cache = DataCache("industry", ttl=86400)
        self._tushare_limiter = RateLimiter(max_calls=500, period=60)
        self._akshare_limiter = RateLimiter(max_calls=60, period=60)


__all__ = ['DataStoreCoreMixin', 'D_DATE', 'D_SYMBOL', 'D_OPEN', 'D_HIGH', 'D_LOW', 'D_CLOSE',
           'D_VOLUME', 'D_AMOUNT', 'D_TURNOVER', 'D_PE_TTM', 'D_PB', 'D_TOTAL_MV', 'D_CIRC_MV',
           'S_SYMBOL', 'S_NAME', 'S_MARKET', 'S_LIST_DATE', 'S_INDUSTRY',
           'F_PE', 'F_PB', 'F_TOTAL_MV', 'F_CIRC_MV', 'F_ROE', 'F_EPS', 'F_BVPS']