"""DatabaseManager — SQLite connection factory.

每个调用方获取自己的连接，用完自行关闭。无连接池，无共享状态。

Usage:
    from quant.data.repos._base import DatabaseManager

    conn = DatabaseManager.market()    # 新开 market.db 连接
    conn.execute("SELECT ...")
    conn.close()

    conn = DatabaseManager.trades()    # 新开 trades.db 连接
    ...

Repos 层通过 get_connection(path) 取连接，语义同上（每次新开）。
"""

from __future__ import annotations

import sqlite3
import os
import logging
import threading

from quant.config.paths import MARKET_DB, TRADE_DB, FACTOR_CACHE_DB
from quant.config.constants import _require_cfg

logger = logging.getLogger(__name__)


class DatabaseManager:
    """SQLite 连接工厂。

    语义化访问器 (每次调用返回新连接):
        DatabaseManager.market()       → market.db
        DatabaseManager.trades()       → trades.db
        DatabaseManager.factor_cache() → factor_cache.db
    """

    # DB 路径 — 全部来自 quant.config.paths
    _MARKET_DB = MARKET_DB
    _TRADE_DB = TRADE_DB
    _FACTOR_CACHE_DB = FACTOR_CACHE_DB

    # ── 语义化访问器 (每次新开) ─────────────────────────────

    @staticmethod
    def market() -> sqlite3.Connection:
        return _open(MARKET_DB)

    @staticmethod
    def trades() -> sqlite3.Connection:
        return _open(TRADE_DB)

    @staticmethod
    def factor_cache() -> sqlite3.Connection:
        return _open(FACTOR_CACHE_DB)

    # ── 通用访问 (repos 用，每次新开) ───────────────────────

    @staticmethod
    def get_connection(db_path: str = MARKET_DB) -> sqlite3.Connection:
        """根据路径新开连接。相对路径以项目根目录为基准。"""
        full = _resolve_path(db_path)
        return _open(full)


def _open(full_path: str) -> sqlite3.Connection:
    """新开一个 SQLite 连接，配置 WAL + busy_timeout."""
    c = sqlite3.connect(full_path, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(f"PRAGMA busy_timeout={_require_cfg('data.sqlite.busy_timeout')}")
    logger.debug("DatabaseManager: opened %s", full_path)
    return c


def _resolve_path(db_path: str) -> str:
    """相对路径 → 绝对路径 (基于项目根目录)。"""
    if not os.path.isabs(db_path):
        return os.path.join(_PROJECT_ROOT, db_path)
    return db_path


# ── 辅助查询函数 ────────────────────────────────────────────

def query_row(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    row = conn.execute(sql, params).fetchone()
    return row


def query_all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def query_scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()):
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None


_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
