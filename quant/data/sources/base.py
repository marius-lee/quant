"""数据源基类与配置定义."""

from __future__ import annotations
import abc
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional
from contextlib import contextmanager

from quant.utils.logger import get_logger

logger = get_logger("data.sources.base")


class DataSourceStatus(Enum):
    """数据源健康状态."""
    HEALTHY = "healthy"           # 正常
    DEGRADED = "degraded"         # 降级(部分功能不可用/高延迟)
    CIRCUIT_OPEN = "circuit_open" # 熔断开启
    MAINTENANCE = "maintenance"   # 维护中
    UNKNOWN = "unknown"           # 未知


@dataclass
class DataSourceConfig:
    """数据源配置."""
    name: str
    # 限流配置
    rate_limit_rps: float = 10.0          # 每秒请求数
    rate_limit_burst: int = 20            # 突发允许数
    rate_limit_daily: int | None = None   # 日限额
    # 熔断配置
    failure_threshold: int = 5            # 连续失败次数触发熔断
    timeout_threshold: float = 30.0       # 超时阈值(秒)
    error_rate_threshold: float = 0.5     # 错误率阈值(0-1)
    recovery_timeout: float = 300.0       # 熔断恢复等待(秒)
    half_open_max_calls: int = 3          # 半开状态最大探测次数
    # 重试配置
    max_retries: int = 3
    base_retry_delay: float = 1.0
    max_retry_delay: float = 60.0
    retry_jitter: float = 0.1
    # 超时配置
    connect_timeout: float = 10.0
    read_timeout: float = 30.0
    # 备源配置
    fallback_sources: list[str] = field(default_factory=list)
    # 启用配置
    enabled: bool = True
    priority: int = 100  # 越小优先级越高


@dataclass
class DataSourceResult:
    """数据源调用结果."""
    success: bool
    data: Any = None
    rows_affected: int = 0
    latency_ms: float = 0.0
    error: str | None = None
    error_code: str | None = None
    metadata: dict = field(default_factory=dict)
    timestamp: datetime = field(default_factory=datetime.now)


class BaseDataSource(abc.ABC):
    """数据源抽象基类.

    所有具体数据源必须实现:
      - _fetch_impl: 核心获取逻辑
      - _health_check_impl: 健康检查逻辑
      - get_source_type: 返回源类型标识

    基类提供:
      - 统一限流、熔断、重试、审计
      - 备源自动切换
      - 结构化日志与指标
    """

    def __init__(self, config: DataSourceConfig):
        self.config = config
        self._status = DataSourceStatus.UNKNOWN
        self._consecutive_failures = 0
        _total_calls = 0
        _total_errors = 0
        _circuit_open_at: float | None = None
        _half_open_calls = 0
        # 延迟初始化组件(由 Registry 注入)
        self._rate_limiter: "DistributedRateLimiter | None" = None
        self._circuit_breaker: "CircuitBreaker | None" = None
        self._audit: "DataSourceAudit | None" = None

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def status(self) -> DataSourceStatus:
        return self._status

    @abc.abstractmethod
    def get_source_type(self) -> str:
        """返回源类型标识, 如 'tushare', 'baostock', 'akshare'."""
        pass

    @abc.abstractmethod
    def _fetch_impl(self, **kwargs) -> DataSourceResult:
        """核心获取逻辑, 子类实现.

        Args:
            **kwargs: 业务参数, 如 symbols, start_date, end_date, table 等

        Returns:
            DataSourceResult
        """
        pass

    @abc.abstractmethod
    def _health_check_impl(self) -> bool:
        """健康检查逻辑, 子类实现.

        Returns:
            True=健康, False=不健康
        """
        pass

    # ═══════════════════════════════════════════════════════
    # 公共接口: 统一入口, 包含限流/熔断/重试/审计
    # ═══════════════════════════════════════════════════════

    def fetch(self, **kwargs) -> DataSourceResult:
        """统一获取入口 — 含限流、熔断、重试、审计、降级."""
        if not self.config.enabled:
            return DataSourceResult(
                success=False, error="source disabled", error_code="DISABLED"
            )

        # 熔断检查
        if self._circuit_breaker and not self._circuit_breaker.allow_request():
            return DataSourceResult(
                success=False,
                error="circuit breaker open",
                error_code="CIRCUIT_OPEN",
                latency_ms=0,
            )

        # 限流等待
        if self._rate_limiter:
            self._rate_limiter.acquire()

        # 重试循环
        last_error = None
        for attempt in range(self.config.max_retries + 1):
            start = time.perf_counter()
            try:
                result = self._fetch_impl(**kwargs)
                latency_ms = (time.perf_counter() - start) * 1000
                result.latency_ms = latency_ms

                # 记录审计
                if self._audit:
                    self._audit.record(
                        source=self.name,
                        operation="fetch",
                        success=result.success,
                        latency_ms=latency_ms,
                        rows=result.rows_affected,
                        error=result.error,
                        error_code=result.error_code,
                        metadata=result.metadata,
                    )

                # 更新熔断器状态
                if self._circuit_breaker:
                    self._circuit_breaker.record_result(result.success, latency_ms / 1000)

                if result.success:
                    self._on_success()
                    return result
                else:
                    last_error = result.error
                    self._on_failure(result.error_code)

            except Exception as e:
                latency_ms = (time.perf_counter() - start) * 1000
                last_error = str(e)
                self._on_failure("EXCEPTION")

                if self._audit:
                    self._audit.record(
                        source=self.name,
                        operation="fetch",
                        success=False,
                        latency_ms=latency_ms,
                        rows=0,
                        error=last_error,
                        error_code="EXCEPTION",
                    )

                if self._circuit_breaker:
                    self._circuit_breaker.record_result(False, latency_ms / 1000)

            # 重试延迟(指数退避 + 抖动)
            if attempt < self.config.max_retries:
                delay = min(
                    self.config.base_retry_delay * (2 ** attempt),
                    self.config.max_retry_delay,
                )
                import random
                delay *= (1 + random.uniform(-self.config.retry_jitter, self.config.retry_jitter))
                logger.warning(
                    f"[{self.name}] fetch attempt {attempt + 1} failed: {last_error}, "
                    f"retrying in {delay:.1f}s"
                )
                time.sleep(delay)

        # 所有重试失败, 尝试备源
        if self.config.fallback_sources:
            logger.warning(f"[{self.name}] all retries failed, trying fallbacks: {self.config.fallback_sources}")
            return self._try_fallbacks(kwargs)

        return DataSourceResult(
            success=False,
            error=last_error or "unknown error",
            error_code="MAX_RETRIES_EXCEEDED",
            latency_ms=0,
        )

    def _try_fallbacks(self, kwargs: dict) -> DataSourceResult:
        """尝试备源."""
        from .registry import get_registry
        registry = get_registry()
        for fb_name in self.config.fallback_sources:
            fb_source = registry.get(fb_name)
            if fb_source and fb_source.config.enabled:
                logger.info(f"[{self.name}] trying fallback: {fb_name}")
                result = fb_source.fetch(**kwargs)
                if result.success:
                    result.metadata["fallback_from"] = self.name
                    result.metadata["fallback_used"] = fb_name
                    return result
        return DataSourceResult(
            success=False,
            error="all sources including fallbacks failed",
            error_code="ALL_SOURCES_FAILED",
        )

    def health_check(self) -> bool:
        """健康检查入口."""
        if not self.config.enabled:
            self._status = DataSourceStatus.MAINTENANCE
            return False

        if self._circuit_breaker and self._circuit_breaker.is_open:
            self._status = DataSourceStatus.CIRCUIT_OPEN
            return False

        try:
            healthy = self._health_check_impl()
            self._status = DataSourceStatus.HEALTHY if healthy else DataSourceStatus.DEGRADED
            return healthy
        except Exception as e:
            logger.warning(f"[{self.name}] health check failed: {e}")
            self._status = DataSourceStatus.DEGRADED
            return False

    def _on_success(self):
        """成功回调."""
        self._consecutive_failures = 0
        if self._status != DataSourceStatus.HEALTHY:
            self._status = DataSourceStatus.HEALTHY
            logger.info(f"[{self.name}] status recovered to HEALTHY")

    def _on_failure(self, error_code: str | None):
        """失败回调."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.config.failure_threshold:
            if self._status != DataSourceStatus.CIRCUIT_OPEN:
                self._status = DataSourceStatus.CIRCUIT_OPEN
                logger.error(f"[{self.name}] circuit opened after {self._consecutive_failures} consecutive failures")

    def inject_dependencies(
        self,
        rate_limiter: "DistributedRateLimiter | None" = None,
        circuit_breaker: "CircuitBreaker | None" = None,
        audit: "DataSourceAudit | None" = None,
    ):
        """注入依赖(由 Registry 调用)."""
        if rate_limiter:
            self._rate_limiter = rate_limiter
        if circuit_breaker:
            self._circuit_breaker = circuit_breaker
        if audit:
            self._audit = audit

    @contextmanager
    def trace_context(self, operation: str, **kwargs):
        """追踪上下文管理器."""
        start = time.perf_counter()
        trace_id = kwargs.pop("trace_id", None)
        try:
            yield
        finally:
            latency_ms = (time.perf_counter() - start) * 1000
            if self._audit:
                self._audit.record(
                    source=self.name,
                    operation=operation,
                    success=True,
                    latency_ms=latency_ms,
                    metadata=kwargs,
                )