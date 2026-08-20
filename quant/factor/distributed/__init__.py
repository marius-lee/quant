"""分布式因子计算引擎 — 基于 Ray.

设计目标:
  1. 替代单进程 FactorStore.materialize() (4.6h 峰值 → 目标 <1h)
  2. 按日期分片并行 (每日独立, 无数据依赖)
  3. 容错: 任务级重试 + 精确一次语义
  4. 资源感知: 自动适配 CPU/内存, 支持弹性伸缩
  5. 可观测: Ray Dashboard + 结构化日志 + 指标导出
"""

from .engine import DistributedFactorEngine, FactorComputeTask, run_distributed_factorization
from .partitioner import DatePartitioner, FactorPartitioner, SymbolPartitioner, CompositePartitioner, create_partitioner, Partition
from .ray_config import get_ray_config, init_ray, shutdown_ray, factor_actor, factor_task
from .aggregator import FactorResultAggregator, ComputeResult

__all__ = [
    "DistributedFactorEngine",
    "FactorComputeTask",
    "run_distributed_factorization",
    "DatePartitioner",
    "FactorPartitioner", 
    "SymbolPartitioner",
    "CompositePartitioner",
    "create_partitioner",
    "Partition",
    "FactorResultAggregator",
    "ComputeResult",
    "get_ray_config",
    "init_ray",
    "shutdown_ray",
    "factor_actor",
    "factor_task",
]