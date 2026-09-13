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
from quant.data.duckdb_manager import DuckDBManager  # backward compat

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
}




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