"""CDC 位点管理 — 持久化同步进度.

设计:
  - 位点存储在独立表 cdc_positions (或复用现有 meta 表)
  - 支持多流位点 (每个同步任务独立)
  - 原子更新: 单条 UPSERT
  - 可选: 导出到文件/Redis 供外部监控
"""

from __future__ import annotations
import sqlite3
import threading
from typing import Optional, Dict, Any
from quant.config.paths import MARKET_DB
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger

logger = get_logger("data.cdc.position")


class PositionStore:
    """位点存储后端."""

    def __init__(self, db_path: str = MARKET_DB):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute(f"PRAGMA busy_timeout={_require_cfg('data.sqlite.busy_timeout')}")
        return self._conn

    def _init_db(self):
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cdc_positions (
                name TEXT PRIMARY KEY,      -- 位点名称 (如 'cdc_capture', 'duckdb_sync:daily')
                position TEXT NOT NULL,     -- 位点值 (JSON: LSN, timestamp, 等)
                updated_at REAL NOT NULL,   -- 更新时间戳
                metadata TEXT               -- 扩展元数据
            );
        """)
        conn.commit()

    def get(self, name: str) -> Optional[str]:
        conn = self._get_conn()
        with self._lock:
            row = conn.execute("SELECT position FROM cdc_positions WHERE name = ?", (name,)).fetchone()
            return row[0] if row else None

    def set(self, name: str, position: str, metadata: Optional[str] = None):
        conn = self._get_conn()
        with self._lock:
            import time
            conn.execute(
                """INSERT OR REPLACE INTO cdc_positions (name, position, updated_at, metadata)
                   VALUES (?, ?, ?, ?)""",
                (name, position, time.time(), metadata)
            )
            conn.commit()

    def delete(self, name: str):
        conn = self._get_conn()
        with self._lock:
            conn.execute("DELETE FROM cdc_positions WHERE name = ?", (name,))
            conn.commit()

    def list_all(self) -> Dict[str, str]:
        conn = self._get_conn()
        with self._lock:
            rows = conn.execute("SELECT name, position FROM cdc_positions").fetchall()
            return {r[0]: r[1] for r in rows}

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None


class PositionManager:
    """高层位点管理器."""

    def __init__(self, store: Optional[PositionStore] = None):
        self.store = store or PositionStore()

    def get_position(self, name: str) -> Optional[int]:
        """获取位点 (返回整数 LSN)."""
        val = self.store.get(name)
        if val is None:
            return None
        try:
            return int(val)
        except ValueError:
            # 兼容旧格式
            import json
            data = json.loads(val)
            return int(data.get("lsn", 0))

    def set_position(self, name: str, lsn: int, metadata: Optional[Dict[str, Any]] = None):
        """设置位点."""
        import json
        meta_str = json.dumps(metadata) if metadata else None
        self.store.set(name, str(lsn), meta_str)

    def advance_position(self, name: str, lsn: int) -> bool:
        """原子前进位点 (仅当新位点 > 旧位点时更新)."""
        import json, time
        conn = self.store._get_conn()
        with self.store._lock:
            # 原子比较并更新
            cur = conn.execute(
                "UPDATE cdc_positions SET position = ?, updated_at = ? WHERE name = ? AND CAST(position AS INTEGER) < ?",
                (str(lsn), time.time(), name, lsn)
            )
            conn.commit()
            return cur.rowcount > 0

    def get_all_positions(self) -> Dict[str, int]:
        """获取所有位点."""
        all_pos = self.store.list_all()
        result = {}
        for k, v in all_pos.items():
            try:
                result[k] = int(v)
            except ValueError:
                import json
                data = json.loads(v)
                result[k] = int(data.get("lsn", 0))
        return result


# 全局实例
_position_manager: Optional[PositionManager] = None


def get_position_manager() -> PositionManager:
    global _position_manager
    if _position_manager is None:
        _position_manager = PositionManager()
    return _position_manager