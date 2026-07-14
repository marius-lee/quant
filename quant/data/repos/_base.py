"""DatabaseManager — singleton SQLite connection factory for all Repository classes.

Usage:
    db = DatabaseManager()
    conn = db.get_connection("data/market.db")
    conn.execute("SELECT ...")
    conn.close()
"""

from __future__ import annotations

import sqlite3
import os
import threading
import logging

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))


class DatabaseManager:
    """Singleton SQLite connection manager.

    All Repository classes share one instance.
    Each thread gets its own connection (sqlite3 is not thread-safe).
    """

    _instance: DatabaseManager | None = None
    _lock = threading.Lock()

    def __init__(self):
        self._connections: dict[str, sqlite3.Connection] = {}

    @classmethod
    def get_instance(cls) -> DatabaseManager:
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    def _resolve_path(self, db_path: str) -> str:
        """Resolve relative paths to project root."""
        if not os.path.isabs(db_path):
            return os.path.join(_PROJECT_ROOT, db_path)
        return db_path

    def get_connection(self, db_path: str = "data/market.db") -> sqlite3.Connection:
        """Get or create a thread-local SQLite connection for the given db."""
        full = self._resolve_path(db_path)
        thread_id = threading.get_ident()
        key = f"{thread_id}:{full}"
        if key not in self._connections:
            conn = sqlite3.connect(full, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            self._connections[key] = conn
            logger.debug("DatabaseManager: opened %s", full)
        return self._connections[key]

    def close_all(self):
        """Close all connections held by this manager."""
        for key, conn in list(self._connections.items()):
            try:
                conn.close()
            except Exception:
                pass
            del self._connections[key]
        logger.debug("DatabaseManager: all connections closed")


def query_row(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> sqlite3.Row | None:
    """Return first row or None."""
    row = conn.execute(sql, params).fetchone()
    return row


def query_all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """Return all rows as list of Row objects."""
    return conn.execute(sql, params).fetchall()


def query_scalar(conn: sqlite3.Connection, sql: str, params: tuple = ()):
    """Return single scalar value or None."""
    row = conn.execute(sql, params).fetchone()
    return row[0] if row else None
