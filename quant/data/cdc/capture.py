"""变更捕获 — 基于 SQLite WAL + 触发器.

设计:
  - SQLite WAL 模式下, 通过 CREATE TRIGGER 在写入表上捕获 INSERT/UPDATE/DELETE
  - 变更写入 cdc_changes 表 (append-only, 含: table, pk, op, old_row, new_row, ts, lsn)
  - 支持批量刷新 + 位点持久化
  - 兼容现有 DataStore 写入路径 (无侵入)
"""

from __future__ import annotations
import sqlite3
import json
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Any, Optional, Callable
from enum import Enum
from quant.config.constants import _require_cfg
from quant.config.paths import MARKET_DB
from quant.utils.logger import get_logger

logger = get_logger("data.cdc.capture")


class OpType(Enum):
    """操作类型."""
    INSERT = "INSERT"
    UPDATE = "UPDATE"
    DELETE = "DELETE"


@dataclass
class ChangeEvent:
    """单条变更事件."""
    lsn: int                    # 全局递增序列号 (Log Sequence Number)
    table: str                  # 表名
    pk: str                     # 主键值 (JSON 字符串, 支持复合主键)
    op: OpType                  # 操作类型
    old_row: Optional[Dict[str, Any]] = None   # 旧行 (UPDATE/DELETE)
    new_row: Optional[Dict[str, Any]] = None   # 新行 (INSERT/UPDATE)
    timestamp: float = field(default_factory=time.time)
    transaction_id: Optional[int] = None       # 事务 ID (用于分组)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "lsn": self.lsn,
            "table": self.table,
            "pk": self.pk,
            "op": self.op.value,
            "old_row": self.old_row,
            "new_row": self.new_row,
            "timestamp": self.timestamp,
            "transaction_id": self.transaction_id,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ChangeEvent":
        return cls(
            lsn=row["lsn"],
            table=row["table"],
            pk=row["pk"],
            op=OpType(row["op"]),
            old_row=json.loads(row["old_row"]) if row["old_row"] else None,
            new_row=json.loads(row["new_row"]) if row["new_row"] else None,
            timestamp=row["timestamp"],
            transaction_id=row["transaction_id"],
        )


@dataclass
class CaptureConfig:
    """捕获配置."""
    # 监控的表 (空 = 所有有触发器的表)
    tables: List[str] = field(default_factory=list)
    # 批量大小
    batch_size: int = 1000
    # 刷新间隔 (秒)
    flush_interval_sec: float = 5.0
    # 最大内存缓冲条数
    max_buffer_size: int = 10000
    # 是否捕获 UPDATE 前后镜像
    capture_old_row: bool = True
    # 忽略的列 (如 updated_at 等自动更新列)
    ignore_columns: List[str] = field(default_factory=lambda: ["updated_at", "_synced_at"])


class ChangeCapture:
    """变更捕获器 — 单例, 后台线程运行.

    架构:
      1. 启动时为配置表创建触发器 (幂等)
      2. 后台线程轮询 cdc_changes 表, 批量读取变更
      3. 调用注册的处理器处理变更 (同步到 DuckDB/因子缓存/审计)
      4. 更新位点 (PositionManager)
    """

    _instance: Optional["ChangeCapture"] = None
    _lock = threading.Lock()

    def __new__(cls, config: Optional[CaptureConfig] = None):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, config: Optional[CaptureConfig] = None):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True

        self.config = config or CaptureConfig()
        self._conn: Optional[sqlite3.Connection] = None
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._handlers: Dict[str, List[Callable[[List[ChangeEvent]], None]]] = {}
        self._position_manager = None
        self._last_lsn = 0

        # 触发器创建 SQL 模板
        self._trigger_templates = {
            "daily": self._build_daily_trigger,
            "daily_valuation": self._build_valuation_trigger,
            "fund_flow": self._build_generic_trigger,
            "margin_detail": self._build_generic_trigger,
            "lhb_detail": self._build_generic_trigger,
            "limit_up_pool": self._build_generic_trigger,
            "limit_down_pool": self._build_generic_trigger,
            "stocks": self._build_stocks_trigger,
            "adj_factor": self._build_adj_factor_trigger,
        }

    def start(self):
        """启动捕获器."""
        if self._running:
            return

        self._conn = sqlite3.connect(MARKET_DB, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA busy_timeout={_require_cfg('data.sqlite.busy_timeout')}")

        # 创建变更表 + 触发器
        self._ensure_schema()
        self._create_triggers()

        # 从位点管理器恢复 LSN
        from .position import PositionManager
        self._position_manager = PositionManager()
        self._last_lsn = self._position_manager.get_position("cdc_capture") or 0

        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="cdc-capture")
        self._thread.start()
        logger.info("ChangeCapture started")

    def stop(self):
        """停止捕获器."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        if self._conn:
            self._conn.close()
        logger.info("ChangeCapture stopped")

    def register_handler(self, table: str, handler: Callable[[List[ChangeEvent]], None]):
        """注册变更处理器."""
        if table not in self._handlers:
            self._handlers[table] = []
        self._handlers[table].append(handler)
        logger.debug(f"Registered handler for {table}: {handler.__name__}")

    def _ensure_schema(self):
        """创建变更表."""
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS cdc_changes (
                lsn INTEGER PRIMARY KEY AUTOINCREMENT,
                table_name TEXT NOT NULL,
                pk TEXT NOT NULL,
                op TEXT NOT NULL,              -- INSERT/UPDATE/DELETE
                old_row TEXT,                  -- JSON
                new_row TEXT,                  -- JSON
                timestamp REAL NOT NULL,
                transaction_id INTEGER,
                processed INTEGER DEFAULT 0,   -- 0=未处理, 1=已处理
                processed_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_cdc_changes_lsn ON cdc_changes(lsn);
            CREATE INDEX IF NOT EXISTS idx_cdc_changes_table_lsn ON cdc_changes(table_name, lsn);
            CREATE INDEX IF NOT EXISTS idx_cdc_changes_processed ON cdc_changes(processed, lsn);
        """)
        self._conn.commit()

    def _create_triggers(self):
        """为监控表创建触发器."""
        tables = self.config.tables or list(self._trigger_templates.keys())
        for table in tables:
            if table not in self._trigger_templates:
                logger.warning(f"No trigger template for table: {table}")
                continue
            self._trigger_templates[table](table)

    def _build_generic_trigger(self, table: str):
        """通用触发器: 适用于单主键表."""
        # 获取主键列
        pk_info = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        pk_cols = [c["name"] for c in pk_info if c["pk"] == 1]
        if not pk_cols:
            logger.warning(f"Table {table} has no primary key, skipping trigger")
            return
        pk_col = pk_cols[0]  # 假设单列主键

        # 构建排除列
        ignore = ", ".join(f"'{c}'" for c in self.config.ignore_columns)
        cols = [c["name"] for c in pk_info if c["name"] not in self.config.ignore_columns]
        col_list = ", ".join(cols)
        new_vals = ", ".join(f"NEW.{c}" for c in cols)
        old_vals = ", ".join(f"OLD.{c}" for c in cols)

        sql = f"""
            CREATE TRIGGER IF NOT EXISTS cdc_{table}_insert
            AFTER INSERT ON {table}
            BEGIN
                INSERT INTO cdc_changes (table_name, pk, op, new_row, timestamp, transaction_id)
                VALUES ('{table}', NEW.{pk_col}, 'INSERT',
                    json_object({', '.join(f"'{c}', NEW.{c}" for c in cols)}),
                    (strftime('%s','now') + (strftime('%f','now') - strftime('%s','now'))),
                    (SELECT COALESCE(MAX(transaction_id), 0) + 1 FROM cdc_changes));
            END;

            CREATE TRIGGER IF NOT EXISTS cdc_{table}_update
            AFTER UPDATE ON {table}
            BEGIN
                INSERT INTO cdc_changes (table_name, pk, op, old_row, new_row, timestamp, transaction_id)
                VALUES ('{table}', NEW.{pk_col}, 'UPDATE',
                    json_object({', '.join(f"'{c}', OLD.{c}" for c in cols)}),
                    json_object({', '.join(f"'{c}', NEW.{c}" for c in cols)}),
                    (strftime('%s','now') + (strftime('%f','now') - strftime('%s','now'))),
                    (SELECT COALESCE(MAX(transaction_id), 0) + 1 FROM cdc_changes));
            END;

            CREATE TRIGGER IF NOT EXISTS cdc_{table}_delete
            AFTER DELETE ON {table}
            BEGIN
                INSERT INTO cdc_changes (table_name, pk, op, old_row, timestamp, transaction_id)
                VALUES ('{table}', OLD.{pk_col}, 'DELETE',
                    json_object({', '.join(f"'{c}', OLD.{c}" for c in cols)}),
                    (strftime('%s','now') + (strftime('%f','now') - strftime('%s','now'))),
                    (SELECT COALESCE(MAX(transaction_id), 0) + 1 FROM cdc_changes));
            END;
        """
        self._conn.executescript(sql)
        self._conn.commit()
        logger.debug(f"Created CDC triggers for {table}")

    def _build_daily_trigger(self, table: str):
        """daily 表触发器: 复合主键 (symbol, date)."""
        ignore = ", ".join(f"'{c}'" for c in self.config.ignore_columns)
        cols = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "turnover"]
        col_list = ", ".join(cols)
        new_json = ", ".join(f"'{c}', NEW.{c}" for c in cols)
        old_json = ", ".join(f"'{c}', OLD.{c}" for c in cols)

        sql = f"""
            CREATE TRIGGER IF NOT EXISTS cdc_{table}_insert
            AFTER INSERT ON {table}
            BEGIN
                INSERT INTO cdc_changes (table_name, pk, op, new_row, timestamp, transaction_id)
                VALUES ('{table}', NEW.symbol || '|' || NEW.date, 'INSERT',
                    json_object({new_json}),
                    (strftime('%s','now') + (strftime('%f','now') - strftime('%s','now'))),
                    (SELECT COALESCE(MAX(transaction_id), 0) + 1 FROM cdc_changes));
            END;

            CREATE TRIGGER IF NOT EXISTS cdc_{table}_update
            AFTER UPDATE ON {table}
            BEGIN
                INSERT INTO cdc_changes (table_name, pk, op, old_row, new_row, timestamp, transaction_id)
                VALUES ('{table}', NEW.symbol || '|' || NEW.date, 'UPDATE',
                    json_object({old_json}),
                    json_object({new_json}),
                    (strftime('%s','now') + (strftime('%f','now') - strftime('%s','now'))),
                    (SELECT COALESCE(MAX(transaction_id), 0) + 1 FROM cdc_changes));
            END;

            CREATE TRIGGER IF NOT EXISTS cdc_{table}_delete
            AFTER DELETE ON {table}
            BEGIN
                INSERT INTO cdc_changes (table_name, pk, op, old_row, timestamp, transaction_id)
                VALUES ('{table}', OLD.symbol || '|' || OLD.date, 'DELETE',
                    json_object({old_json}),
                    (strftime('%s','now') + (strftime('%f','now') - strftime('%s','now'))),
                    (SELECT COALESCE(MAX(transaction_id), 0) + 1 FROM cdc_changes));
            END;
        """
        self._conn.executescript(sql)
        self._conn.commit()
        logger.debug(f"Created CDC triggers for {table} (composite PK)")

    def _build_valuation_trigger(self, table: str):
        """daily_valuation 触发器: 复合主键 (symbol, date)."""
        self._build_daily_trigger(table)  # 结构相同

    def _build_stocks_trigger(self, table: str):
        """stocks 表触发器: 单主键 symbol."""
        self._build_generic_trigger(table)

    def _build_adj_factor_trigger(self, table: str):
        """adj_factor 表触发器: 复合主键 (symbol, date)."""
        self._build_daily_trigger(table)

    def _run_loop(self):
        """后台轮询循环."""
        while self._running:
            try:
                self._poll_changes()
            except Exception as e:
                logger.error(f"CDC poll error: {e}")
            time.sleep(self.config.flush_interval_sec)

    def _poll_changes(self):
        """轮询未处理变更."""
        rows = self._conn.execute(
            "SELECT * FROM cdc_changes WHERE lsn > ? AND processed = 0 ORDER BY lsn LIMIT ?",
            (self._last_lsn, self.config.batch_size)
        ).fetchall()

        if not rows:
            return

        # 按表分组
        by_table: Dict[str, List[ChangeEvent]] = {}
        for row in rows:
            event = ChangeEvent.from_row(row)
            by_table.setdefault(event.table, []).append(event)

        # 调用处理器
        for table, events in by_table.items():
            handlers = self._handlers.get(table, [])
            for handler in handlers:
                try:
                    handler(events)
                except Exception as e:
                    logger.error(f"Handler failed for {table}: {e}")

        # 标记已处理 + 更新位点
        max_lsn = max(r["lsn"] for r in rows)
        self._conn.execute(
            "UPDATE cdc_changes SET processed = 1, processed_at = ? WHERE lsn <= ?",
            (time.time(), max_lsn)
        )
        self._conn.commit()

        self._last_lsn = max_lsn
        self._position_manager.set_position("cdc_capture", max_lsn)

        logger.debug(f"CDC processed {len(rows)} events up to LSN {max_lsn}")


def get_capture(config: Optional[CaptureConfig] = None) -> ChangeCapture:
    """获取全局捕获器单例."""
    return ChangeCapture(config)