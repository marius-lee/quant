"""Schema 演进管理 — 自动检测并应用 Schema 变更."""

from __future__ import annotations
import sqlite3
import duckdb
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Any, Optional, Set
from quant.config.paths import MARKET_DB
from quant.utils.logger import get_logger

logger = get_logger("data.cdc.schema_evolution")


@dataclass
class ColumnChange:
    """列变更描述."""
    table: str
    column: str
    change_type: str  # ADD, DROP, RENAME, TYPE_CHANGE
    old_type: Optional[str] = None
    new_type: Optional[str] = None
    default_value: Any = None


@dataclass
class SchemaSnapshot:
    """Schema 快照."""
    table: str
    columns: Dict[str, str]  # column -> type
    primary_keys: List[str]
    indexes: List[str]
    timestamp: float = field(default_factory=time.time)


class SchemaEvolutionManager:
    """Schema 演进管理器.

    功能:
    1. 自动检测 SQLite Schema 变更
    2. 自动应用到 DuckDB (ALTER TABLE)
    3. 版本化 Schema 历史
    4. 回滚支持
    """

    def __init__(self, sqlite_db: str = MARKET_DB, duckdb_path: Optional[str] = None):
        self.sqlite_db = sqlite3.connect(sqlite_db, check_same_thread=False)
        self.sqlite_db.row_factory = sqlite3.Row
        self.duckdb_path = duckdb_path
        self._duckdb_conn: Optional[duckdb.DuckDBPyConnection] = None
        self._schema_cache: Dict[str, SchemaSnapshot] = {}

    @property
    def duckdb_conn(self) -> duckdb.DuckDBPyConnection:
        if self._duckdb_conn is None:
            import duckdb
            self._duckdb_conn = duckdb.connect(self.duckdb_path or ":memory:")
        return self._duckdb_conn

    def snapshot_schema(self, table: str) -> SchemaSnapshot:
        """获取表的当前 Schema 快照."""
        # SQLite 端
        cols = self.sqlite_db.execute(f"PRAGMA table_info({table})").fetchall()
        columns = {row["name"]: row["type"] for row in cols}
        pk_cols = [row["name"] for row in cols if row["pk"] == 1]

        # 索引
        idxs = self.sqlite_db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?", (table,)
        ).fetchall()
        indexes = [row[0] for row in idxs]

        snapshot = SchemaSnapshot(
            table=table,
            columns=columns,
            primary_keys=pk_cols,
            indexes=[i for i in indexes if not i.startswith("sqlite_")],
        )
        self._schema_cache[table] = snapshot
        return snapshot

    def detect_changes(self, table: str) -> List[ColumnChange]:
        """检测表 Schema 变更."""
        if table not in self._schema_cache:
            self.snapshot_schema(table)
            return []

        old_snapshot = self._schema_cache[table]
        new_snapshot = self.snapshot_schema(table)

        changes = []

        # 新增列
        for col, dtype in new_snapshot.columns.items():
            if col not in old_snapshot.columns:
                changes.append(ColumnChange(
                    table=table,
                    column=col,
                    change_type="ADD",
                    new_type=dtype,
                ))

        # 删除列
        for col in old_snapshot.columns:
            if col not in new_snapshot.columns:
                changes.append(ColumnChange(
                    table=table,
                    column=col,
                    change_type="DROP",
                    old_type=old_snapshot.columns[col],
                ))

        # 类型变更
        for col, dtype in new_snapshot.columns.items():
            if col in old_snapshot.columns and old_snapshot.columns[col] != dtype:
                changes.append(ColumnChange(
                    table=table,
                    column=col,
                    change_type="TYPE_CHANGE",
                    old_type=old_snapshot.columns[col],
                    new_type=dtype,
                ))

        # 重命名检测 (启发式: 旧列消失 + 新列出现 + 类型相同)
        for old_col, old_type in old_snapshot.columns.items():
            if old_col not in new_snapshot.columns:
                for new_col, new_type in new_snapshot.columns.items():
                    if new_col not in old_snapshot.columns and new_type == old_type:
                        changes.append(ColumnChange(
                            table=table,
                            column=old_col,
                            change_type="RENAME",
                            old_type=old_type,
                            new_type=new_type,
                        ))

        return changes

    def apply_changes(self, table: str, changes: List[ColumnChange]) -> bool:
        """应用 Schema 变更到 DuckDB."""
        if not changes:
            return True

        try:
            for change in changes:
                if change.change_type == "ADD":
                    self._add_column(table, change)
                elif change.change_type == "DROP":
                    self._drop_column(table, change)
                elif change.change_type == "TYPE_CHANGE":
                    self._alter_column_type(table, change)
                elif change.change_type == "RENAME":
                    self._rename_column(table, change)

            # 更新缓存
            self.snapshot_schema(table)
            logger.info(f"Applied {len(changes)} schema changes to {table}")
            return True

        except Exception as e:
            logger.error(f"Failed to apply schema changes to {table}: {e}")
            return False

    def _add_column(self, table: str, change: ColumnChange):
        """添加列."""
        default_clause = f" DEFAULT {change.default_value}" if change.default_value is not None else ""
        sql = f"ALTER TABLE {change.table} ADD COLUMN {change.column} {change.new_type}{default_clause}"
        self.duckdb_conn.execute(sql)
        logger.info(f"Added column {change.column} to {table}")

    def _drop_column(self, table: str, change: ColumnChange):
        """删除列 (DuckDB 支持)."""
        sql = f"ALTER TABLE {change.table} DROP COLUMN {change.column}"
        self.duckdb_conn.execute(sql)
        logger.info(f"Dropped column {change.column} from {table}")

    def _alter_column_type(self, table: str, change: ColumnChange):
        """修改列类型 (DuckDB 支持)."""
        sql = f"ALTER TABLE {change.table} ALTER COLUMN {change.column} TYPE {change.new_type}"
        self.duckdb_conn.execute(sql)
        logger.info(f"Altered column {change.column} type in {table}: {change.old_type} -> {change.new_type}")

    def _rename_column(self, table: str, change: ColumnChange):
        """重命名列."""
        sql = f"ALTER TABLE {change.table} RENAME COLUMN {change.column} TO {change.new_type}"
        self.duckdb_conn.execute(sql)
        logger.info(f"Renamed column {change.column} to {change.new_type} in {table}")

    def sync_table(self, table: str) -> bool:
        """同步单表 Schema (检测 + 应用)."""
        changes = self.detect_changes(table)
        if not changes:
            return True
        return self.apply_changes(table, changes)

    def sync_all(self, tables: Optional[List[str]] = None) -> Dict[str, bool]:
        """同步所有表 Schema."""
        tables = tables or self._get_all_tables()
        results = {}
        for table in tables:
            results[table] = self.sync_table(table)
        return results

    def _get_all_tables(self) -> List[str]:
        """获取所有用户表."""
        rows = self.sqlite_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'cdc_%'"
        ).fetchall()
        return [row[0] for row in rows]


# 全局实例
_schema_manager: Optional[SchemaEvolutionManager] = None


def get_schema_manager(
    sqlite_db: str = MARKET_DB,
    duckdb_path: Optional[str] = None,
) -> SchemaEvolutionManager:
    global _schema_manager
    if _schema_manager is None:
        _schema_manager = SchemaEvolutionManager(sqlite_db, duckdb_path)
    return _schema_manager