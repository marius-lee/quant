"""数据源注册表 — 单例管理所有数据源实例、配置、依赖注入."""

from __future__ import annotations
import threading
from typing import Any

from quant.utils.logger import get_logger
from quant.config.loader import load as _load_config
from quant.config.constants import _require_cfg

from .base import BaseDataSource, DataSourceConfig, DataSourceStatus
from .rate_limiter import DistributedRateLimiter, RateLimitConfig, TokenBucketLimiter
from .circuit_breaker import CircuitBreaker, CircuitBreakerConfig
from .audit import DataSourceAudit
from .tushare_source import TushareSource
from .baostock_source import BaostockSource
from .akshare_source import AkshareSource
from .tickflow_source import TickFlowSource
from .tencent_source import TencentSource
from .pytdx_source import PytdxSource

logger = get_logger("data.sources.registry")


class DataSourceRegistry:
    """数据源注册表 — 单例.

    职责:
      1. 管理所有数据源实例的生命周期
      2. 从 config.yaml 加载配置并创建实例
      3. 注入共享组件(限流器、熔断器、审计器)
      4. 提供统一的获取/健康检查/状态查询接口
      5. 支持按优先级/分组/标签查询
    """

    _instance: "DataSourceRegistry | None" = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True

        self._sources: dict[str, BaseDataSource] = {}
        self._configs: dict[str, DataSourceConfig] = {}
        self._source_classes: dict[str, type[BaseDataSource]] = {}

        # 自动注册内置源
        self._source_classes.update({
            "tushare": TushareSource,
            "baostock": BaostockSource,
            "akshare": AkshareSource,
            "tickflow": TickFlowSource,
            "tencent": TencentSource,
            "pytdx": PytdxSource,
        })

        # 共享组件
        self._rate_limiters: dict[str, DistributedRateLimiter | TokenBucketLimiter] = {}
        self._circuit_breakers: dict[str, CircuitBreaker] = {}
        self._audit: DataSourceAudit | None = None

        # 状态目录
        self._state_dir = _require_cfg("data.sources.state_dir", "/tmp/quant_sources")

    def register_source_class(self, name: str, cls: type[BaseDataSource]):
        """注册数据源实现类."""
        self._source_classes[name] = cls
        logger.debug(f"registered source class: {name} -> {cls.__name__}")

    def load_from_config(self):
        """从 config.yaml 加载并初始化所有数据源."""
        cfg = _load_config()
        sources_cfg = cfg.get("data", {}).get("sources", {})

        # 先创建共享审计器
        self._audit = DataSourceAudit(
            max_memory_entries=_require_cfg("data.sources.audit.max_memory_entries", 10000),
            enable_prometheus=_require_cfg("data.sources.audit.enable_prometheus", True),
        )

        for name, source_cfg in sources_cfg.items():
            if not source_cfg.get("enabled", True):
                logger.info(f"source {name} disabled in config, skipping")
                continue

            # 创建配置对象
            config = DataSourceConfig(
                name=name,
                rate_limit_rps=source_cfg.get("rate_limit_rps", 10.0),
                rate_limit_burst=source_cfg.get("rate_limit_burst", 20),
                rate_limit_daily=source_cfg.get("rate_limit_daily"),
                failure_threshold=source_cfg.get("failure_threshold", 5),
                timeout_threshold=source_cfg.get("timeout_threshold", 30.0),
                error_rate_threshold=source_cfg.get("error_rate_threshold", 0.5),
                recovery_timeout=source_cfg.get("recovery_timeout", 300.0),
                half_open_max_calls=source_cfg.get("half_open_max_calls", 3),
                max_retries=source_cfg.get("max_retries", 3),
                base_retry_delay=source_cfg.get("base_retry_delay", 1.0),
                max_retry_delay=source_cfg.get("max_retry_delay", 60.0),
                retry_jitter=source_cfg.get("retry_jitter", 0.1),
                connect_timeout=source_cfg.get("connect_timeout", 10.0),
                read_timeout=source_cfg.get("read_timeout", 30.0),
                fallback_sources=source_cfg.get("fallback_sources", []),
                enabled=source_cfg.get("enabled", True),
                priority=source_cfg.get("priority", 100),
            )
            self._configs[name] = config

            # 创建限流器(跨进程或单进程)
            if source_cfg.get("distributed_rate_limit", False):
                rl_config = RateLimitConfig(
                    rps=config.rate_limit_rps,
                    burst=config.rate_limit_burst,
                    daily_limit=config.rate_limit_daily,
                    key_prefix=f"src_{name}",
                )
                limiter = DistributedRateLimiter(rl_config, self._state_dir)
            else:
                limiter = TokenBucketLimiter(RateLimitConfig(
                    rps=config.rate_limit_rps,
                    burst=config.rate_limit_burst,
                    daily_limit=config.rate_limit_daily,
                ))
            self._rate_limiters[name] = limiter

            # 创建熔断器
            cb_config = CircuitBreakerConfig(
                failure_threshold=config.failure_threshold,
                timeout_threshold=config.timeout_threshold,
                error_rate_threshold=config.error_rate_threshold,
                recovery_timeout=config.recovery_timeout,
                half_open_max_calls=config.half_open_max_calls,
            )
            self._circuit_breakers[name] = CircuitBreaker(name, cb_config)

            # 实例化数据源
            if name in self._source_classes:
                source = self._source_classes[name](config)
                source.inject_dependencies(
                    rate_limiter=limiter,
                    circuit_breaker=self._circuit_breakers[name],
                    audit=self._audit,
                )
                self._sources[name] = source
                logger.info(f"initialized data source: {name} ({self._source_classes[name].__name__})")
            else:
                logger.warning(f"no implementation class registered for source: {name}")

    def get(self, name: str) -> BaseDataSource | None:
        """获取数据源实例."""
        return self._sources.get(name)

    def get_all(self) -> dict[str, BaseDataSource]:
        """获取所有数据源."""
        return dict(self._sources)

    def get_enabled(self) -> dict[str, BaseDataSource]:
        """获取所有启用的数据源."""
        return {k: v for k, v in self._sources.items() if v.config.enabled}

    def get_by_priority(self, ascending: bool = True) -> list[BaseDataSource]:
        """按优先级排序获取数据源."""
        return sorted(
            self._sources.values(),
            key=lambda s: s.config.priority,
            reverse=not ascending,
        )

    def get_by_type(self, source_type: str) -> list[BaseDataSource]:
        """按源类型获取数据源."""
        return [s for s in self._sources.values() if s.get_source_type() == source_type]

    def health_check_all(self) -> dict[str, bool]:
        """全量健康检查."""
        results = {}
        for name, source in self._sources.items():
            try:
                results[name] = source.health_check()
            except Exception as e:
                logger.warning(f"health check failed for {name}: {e}")
                results[name] = False
        return results

    def get_status_summary(self) -> dict[str, Any]:
        """获取所有数据源状态摘要."""
        summary = {}
        for name, source in self._sources.items():
            cb = self._circuit_breakers.get(name)
            rl = self._rate_limiters.get(name)
            summary[name] = {
                "status": source.status.value,
                "source_type": source.get_source_type(),
                "enabled": source.config.enabled,
                "priority": source.config.priority,
                "fallback_sources": source.config.fallback_sources,
                "circuit_breaker": cb.stats.__dict__ if cb else None,
                "rate_limiter": rl.get_stats() if hasattr(rl, "get_stats") else {"type": "local"},
            }
        return summary

    def get_audit_stats(self, source: str | None = None, since_seconds: float = 3600) -> dict:
        """获取审计统计."""
        if self._audit:
            return self._audit.get_stats(source, since_seconds)
        return {}

    def shutdown(self):
        """关闭所有资源."""
        if self._audit:
            self._audit.close()
        logger.info("data source registry shutdown")


def get_registry() -> DataSourceRegistry:
    """获取全局注册表单例."""
    return DataSourceRegistry()