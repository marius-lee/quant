"""CDC (Change Data Capture) 增量同步引擎.

设计目标:
  1. 基于 SQLite WAL 模式的实时变更捕获
  2. Schema 演进自动处理
  3. Exactly-once 语义保证
  4. 多表依赖感知同步编排
  5. 高性能批量 UPSERT + 内存压力保护
"""

from .wal_listener import ChangeEvent, ChangeType, WALListener, get_wal_listener
from .syncer import IncrementalSyncer, SyncConfig, SyncResult
from .schema_evolution import SchemaEvolutionManager
from .orchestrator import CDCSyncerOrchestrator

__all__ = [
    "ChangeEvent",
    "ChangeType",
    "WALListener",
    "get_wal_listener",
    "IncrementalSyncer",
    "SyncConfig",
    "SyncResult",
    "SchemaEvolutionManager",
    "CDCSyncerOrchestrator",
]