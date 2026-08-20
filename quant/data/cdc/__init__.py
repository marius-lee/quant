"""CDC (Change Data Capture) 增量同步引擎.

设计目标:
  1. 基于 SQLite WAL 模式的实时变更捕获
  2. Schema 演进自动处理
  3. Exactly-once 语义保证
  4. 多表依赖感知同步编排
  5. 高性能批量 UPSERT + 内存压力保护
  6. 监控告警体系
"""

from .wal_listener import ChangeEvent, ChangeType, WALListener, TriggerBasedListener, get_wal_listener, get_trigger_listener
from .syncer import IncrementalSyncer, SyncConfig, SyncResult
from .schema_evolution import SchemaEvolutionManager, get_schema_manager
from .orchestrator import CDCSyncerOrchestrator, SyncPlan, TableDependency, SyncConfig, SyncResult
from .performance import VectorizedUpserter, BatchUpsertConfig, ColumnPruner, PreparedStatementCache, get_column_pruner, get_statement_cache
from .monitoring import CDCMonitor, CDCMetrics, MetricsExporter, get_cdc_monitor, get_metrics_exporter

__all__ = [
    "ChangeEvent",
    "ChangeType",
    "WALListener",
    "TriggerBasedListener",
    "get_wal_listener",
    "get_trigger_listener",
    "IncrementalSyncer",
    "SyncConfig",
    "SyncResult",
    "SchemaEvolutionManager",
    "get_schema_manager",
    "CDCSyncerOrchestrator",
    "SyncPlan",
    "TableDependency",
    "SyncConfig",
    "SyncResult",
    "VectorizedUpserter",
    "BatchUpsertConfig",
    "ColumnPruner",
    "PreparedStatementCache",
    "get_column_pruner",
    "get_statement_cache",
    "CDCMonitor",
    "CDCMetrics",
    "MetricsExporter",
    "get_cdc_monitor",
    "get_metrics_exporter",
]