"""数据源抽象层 — 统一接口、限流、熔断、审计、降级.

设计目标:
  1. 统一接口: 所有数据源实现统一的 BaseDataSource 抽象类
  2. 限流内置: 令牌桶/漏桶算法, 支持分布式(跨进程)限流
  3. 熔断机制: 连续失败/超时/错误率触发熔断, 自动恢复
  4. 审计日志: 每次调用记录 latency、status、rows、error
  5. 优雅降级: 主源失败自动切换备源, 保证业务连续性
  6. 可观测: 结构化日志 + Prometheus 指标 + 分布式追踪
"""

from .base import BaseDataSource, DataSourceConfig, DataSourceStatus
from .registry import DataSourceRegistry, get_registry
from .rate_limiter import DistributedRateLimiter, TokenBucketLimiter
from .circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from .audit import DataSourceAudit, AuditEntry
from .tushare_source import TushareSource
from .baostock_source import BaostockSource
from .akshare_source import AkshareSource
from .tickflow_source import TickFlowSource
from .tencent_source import TencentSource
from .pytdx_source import PytdxSource

__all__ = [
    "BaseDataSource",
    "DataSourceConfig",
    "DataSourceStatus",
    "DataSourceRegistry",
    "get_registry",
    "DistributedRateLimiter",
    "TokenBucketLimiter",
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "DataSourceAudit",
    "AuditEntry",
    "TushareSource",
    "BaostockSource",
    "AkshareSource",
    "TickFlowSource",
    "TencentSource",
    "PytdxSource",
]