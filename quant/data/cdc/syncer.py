"""增量同步器 - CDC 变更应用到下游 (DuckDB/因子缓存/审计)."""

from __future__ import annotations
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Any, Optional, Callable
from quant.config.paths import MARKET_DB
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger

logger = get_logger("data.cdc.syncer")


@dataclass
class SyncConfig:
    """同步配置."""
    # 目标表映射: 源表 -> 目标配置
    target_mapping: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # 批量大小
    batch_size: int = 500
    # 刷新间隔
    flush_interval_sec: float = 2.0
    # 最大重试次数
    max_retries: int = 3
    # 重试延迟
    retry_delay_sec: float = 1.0
    # 是否启用 DuckDB 同步
    enable_duckdb: bool = True
    # 是否启用因子缓存失效
    enable_factor_cache_invalidation: bool = True
    # 是否启用审计更新
    enable_audit_update: bool = True


@dataclass
class SyncResult:
    """同步结果."""
    success: bool
    events_processed: int = 0
    rows_upserted: int = 0
    rows_deleted: int = 0
    elapsed_ms: float = 0.0
    error: Optional[str] = None
    failed_events: List[Dict] = field(default_factory=list)


class IncrementalSyncer:
    """增量同步器 — 后台运行, 消费 CDC 变更."""

    def __init__(self, config: Optional[SyncConfig] = None):
        self.config = config or SyncConfig()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._duckdb_proxy = None
        self._sqlite_conn = None
        self._position_manager = None

        # 默认目标映射
        if not self.config.target_mapping:
            self.config.target_mapping = {
                "daily": {"duckdb_table": "daily", "pk": ["symbol", "date"]},
                "daily_valuation": {"duckdb_table": "daily_valuation", "pk": ["symbol", "date"]},
                "fund_flow": {"duckdb_table": "fund_flow", "pk": ["symbol", "date"]},
                "margin_detail": {"duckdb_table": "margin_detail", "pk": ["symbol", "date"]},
                "lhb_detail": {"duckdb_table": "lhb_detail", "pk": ["symbol", "trade_date"]},
                "limit_up_pool": {"duckdb_table": "limit_up_pool", "pk": ["symbol", "date"]},
                "limit_down_pool": {"duckdb_table": "limit_down_pool", "pk": ["symbol", "date"]},
                "stocks": {"duckdb_table": "stocks", "pk": ["symbol"]},
                "adj_factor": {"duckdb_table": "adj_factor", "pk": ["symbol", "date"]},
            }

    def start(self):
        """启动同步器."""
        if self._running:
            return

        # 初始化连接
        self._sqlite_conn = sqlite3.connect(MARKET_DB, check_same_thread=False)
        self._sqlite_conn.row_factory = sqlite3.Row
        self._sqlite_conn.execute("PRAGMA journal_mode=WAL")
        self._sqlite_conn.execute(f"PRAGMA busy_timeout={_require_cfg('data.sqlite.busy_timeout')}")

        # 初始化 DuckDB
        if self.config.enable_duckdb:
            from quant.data.duckdb_store import get_duckdb_proxy
            self._duckdb_proxy = get_duckdb_proxy()
            self._duckdb_conn = self._duckdb_proxy._duckdb._conn

        # 位点管理
        from .position import get_position_manager
        self._position_manager = get_position_manager()

        # 注册 CDC 处理器
        from .capture import get_capture
        capture = get_capture()
        for table in self.config.target_mapping.keys():
            capture.register_handler(table, self._handle_changes)

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="cdc-sync")
        self._thread.start()
        logger.info("IncrementalSyncer started")

    def stop(self):
        """停止同步器."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        if self._sqlite_conn:
            self._sqlite_conn.close()
        logger.info("IncrementalSyncer stopped")

    def _handle_changes(self, events: List[Any]):
        """处理变更事件 (由 ChangeCapture 调用)."""
        if not events:
            return

        start = time.perf_counter()
        result = SyncResult(success=True)

        for event in events:
            table = event.table
            mapping = self.config.target_mapping.get(table)
            if not mapping:
                continue

            try:
                if event.change_type.value == "INSERT" or event.change_type.value == "UPDATE":
                    self._upsert_row(table, mapping, event.new_row)
                    result.rows_upserted += 1
                elif event.change_type.value == "DELETE":
                    self._delete_row(table, mapping, event.pk)
                    result.rows_deleted += 1

                result.events_processed += 1

            except Exception as e:
                logger.error(f"Sync failed for {table} event {event.sequence}: {e}")
                result.success = False
                result.failed_events.append({"lsn": event.sequence, "error": str(e)})

        # 更新位点
        if events:
            max_seq = max(e.sequence for e in events)
            self._position_manager.set_position(f"cdc_sync:{events[0].table}", max_seq)

        result.elapsed_ms = (time.perf_counter() - start) * 1000
        logger.debug(f"Synced {result.events_processed} events: {result.rows_upserted} upserts, "
                     f"{result.rows_deleted} deletes in {result.elapsed_ms:.1f}ms")

    def _upsert_row(self, source_table: str, mapping: Dict[str, Any], row: Dict[str, Any]):
        """UPSERT 单行到 DuckDB."""
        if not self.config.enable_duckdb or not row:
            return

        target_table = mapping["duckdb_table"]
        pk_cols = mapping["pk"]

        cols = list(row.keys())
        placeholders = ", ".join(["?" for _ in cols])
        col_list = ", ".join(cols)

        # 冲突处理: ON CONFLICT(pk) DO UPDATE SET ...
        conflict_cols = ", ".join(pk_cols)
        update_cols = [c for c in cols if c not in pk_cols]
        update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)

        sql = f"""
            INSERT INTO {target_table} ({col_list}) VALUES ({placeholders})
            ON CONFLICT ({conflict_cols}) DO UPDATE SET {update_set}
        """

        values = [row[c] for c in cols]

        if self._duckdb_conn:
            self._duckdb_conn.execute(sql, values)
        else:
            self._sqlite_conn.execute(sql, values)
            self._sqlite_conn.commit()

    def _delete_row(self, source_table: str, mapping: Dict[str, Any], pk: str):
        """删除行 (DuckDB)."""
        if not self.config.enable_duckdb:
            return

        target_table = mapping["duckdb_table"]
        pk_cols = mapping["pk"]

        pk_values = pk.split("|") if "|" in pk else [pk]
        if len(pk_cols) != len(pk_values):
            logger.warning(f"PK mismatch for {target_table}: {pk_cols} vs {pk_values}")
            return

        where_clause = " AND ".join(f"{c} = ?" for c in pk_cols)
        sql = f"DELETE FROM {target_table} WHERE {where_clause}"

        if self._duckdb_conn:
            self._duckdb_conn.execute(sql, pk_values)
        else:
            self._sqlite_conn.execute(sql, pk_values)
            self._sqlite_conn.commit()

    def sync_table_full(self, table: str) -> SyncResult:
        """全量同步单表 (用于初始化/修复)."""
        start = time.perf_counter()
        result = SyncResult(success=True)

        mapping = self.config.target_mapping.get(table)
        if not mapping:
            result.success = False
            result.error = f"No mapping for table {table}"
            return result

        target_table = mapping["duckdb_table"]
        pk_cols = mapping["pk"]

        try:
            rows = self._sqlite_conn.execute(f"SELECT * FROM {table}").fetchall()

            for row in rows:
                row_dict = dict(row)
                self._upsert_row(table, mapping, row_dict)
                result.rows_upserted += 1
                result.events_processed += 1

            self._duckdb_conn.commit() if self._duckdb_conn else self._sqlite_conn.commit()

        except Exception as e:
            result.success = False
            result.error = str(e)
            logger.error(f"Full sync failed for {table}: {e}")

        result.elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(f"Full sync {table}: {result.events_processed} rows in {result.elapsed_ms:.1f}ms")
        return result

    def verify_sync(self, table: str) -> Dict[str, Any]:
        """校验 SQLite 与 DuckDB 一致性."""
        mapping = self.config.target_mapping.get(table)
        if not mapping:
            return {"match": False, "error": "no mapping"}

        target_table = mapping["duckdb_table"]

        sqlite_count = self._sqlite_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        duckdb_count = self._duckdb_conn.execute(f"SELECT COUNT(*) FROM {target_table}").fetchone()[0]

        # 日期范围对比
        sqlite_dates = self._sqlite_conn.execute(
            f"SELECT MIN(date), MAX(date) FROM {table} WHERE date IS NOT NULL"
        ).fetchone()
        duckdb_dates = self._duckdb_conn.execute(
            f"SELECT MIN(date), MAX(date) FROM {target_table} WHERE date IS NOT NULL"
        ).fetchone()

        return {
            "match": sqlite_count == duckdb_count,
            "sqlite_rows": sqlite_count,
            "duckdb_rows": duckdb_count,
            "sqlite_date_range": list(sqlite_dates) if sqlite_dates[0] else None,
            "duckdb_date_range": list(duckdb_dates) if duckdb_dates[0] else None,
        }


# 全局实例
_syncer: Optional[IncrementalSyncer] = None


def get_syncer(config: Optional[SyncConfig] = None) -> IncrementalSyncer:
    global _syncer
    if _syncer is None:
        _syncer = IncrementalSyncer(config)
    return _syncer