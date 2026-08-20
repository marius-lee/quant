"""CDC (Change Data Capture) 增量同步引擎.

设计目标:
  1. 消除全量回补: 基于 WAL/触发器捕获变更, 仅同步增量
  2. 多源支持: SQLite (主库) → DuckDB (分析库) + 因子缓存增量更新
  3. 精确一次: 幂等写入 + 位点管理 + 事务边界
  4. 低延迟: 秒级捕获, 分钟级同步
  5. 可观测: 同步延迟指标 + 积压监控 + 数据质量校验
"""

from .capture import ChangeCapture, CaptureConfig, ChangeEvent
from .sync import IncrementalSyncer, SyncConfig, SyncResult
from .position import PositionManager, PositionStore
from .validator import DataValidator, ValidationResult

__all__ = [
    "ChangeCapture",
    "CaptureConfig", 
    "ChangeEvent",
    "IncrementalSyncer",
    "SyncConfig",
    "SyncResult",
    "PositionManager",
    "PositionStore",
    "DataValidator",
    "ValidationResult",
]