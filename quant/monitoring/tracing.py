"""分布式追踪 — OpenTelemetry 集成.

功能:
  - Trace Context 传播 (W3C TraceContext 标准)
  - 自动埋点: HTTP、DB、调度器、因子计算
  - 采样策略: 头部采样 + 尾部采样
  - 导出: OTLP (Jaeger/Zipkin/Grafana Tempo)
  - 与日志/指标关联 (exemplars)
"""

from __future__ import annotations
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, Callable
from quant.utils.logger import get_logger

logger = get_logger("monitoring.tracing")

# 延迟导入 OpenTelemetry (可选依赖)
_OTEL_AVAILABLE = False
try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    from opentelemetry.sdk.resources import Resource, SERVICE_NAME
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.propagate import set_global_textmap, get_global_textmap
    from opentelemetry.propagators.tracecontext import TraceContextTextMapPropagator
    from opentelemetry.instrumentation.requests import RequestsInstrumentor
    from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor
    from opentelemetry.trace import SpanKind, Status, StatusCode
    _OTEL_AVAILABLE = True
except ImportError:
    trace = None
    TracerProvider = None
    OTLPSpanExporter = None
    SpanKind = None
    Status = None
    StatusCode = None


@dataclass
class TracingConfig:
    """追踪配置."""
    service_name: str = "quant"
    enabled: bool = True
    sampler: str = "traceidratio"  # "always_on" | "always_off" | "traceidratio" | "parentbased"
    sample_rate: float = 0.1       # traceidratio 采样率
    exporter: str = "console"      # "console" | "otlp" | "jaeger" | "zipkin"
    otlp_endpoint: str = "http://localhost:4317"
    otlp_headers: Dict[str, str] = field(default_factory=dict)
    propagate_headers: bool = True
    instrument_requests: bool = True
    instrument_sqlite: bool = True


class TracingManager:
    """追踪管理器."""

    def __init__(self, config: Optional[TracingConfig] = None):
        self.config = config or TracingConfig()
        self._tracer_provider: Optional[Any] = None
        self._tracer: Optional[Any] = None
        self._initialized = False
        self._lock = threading.Lock()

        if self.config.enabled and _OTEL_AVAILABLE:
            self._initialize()

    def _initialize(self):
        """初始化 OpenTelemetry."""
        if self._initialized:
            return

        # 资源
        resource = Resource.create({
            SERVICE_NAME: self.config.service_name,
            "service.version": "1.0.0",
            "deployment.environment": os.getenv("ENV", "development"),
        })

        # TracerProvider
        self._tracer_provider = TracerProvider(resource=resource)

        # 采样器
        sampler = self._create_sampler()
        self._tracer_provider.sampler = sampler

        # 导出器
        exporter = self._create_exporter()
        if exporter:
            processor = BatchSpanProcessor(exporter)
            self._tracer_provider.add_span_processor(processor)

        # 设置全局
        trace.set_tracer_provider(self._tracer_provider)

        # 传播器
        if self.config.propagate_headers:
            set_global_textmap(TraceContextTextMapPropagator())

        # 自动埋点
        if self.config.instrument_requests:
            RequestsInstrumentor().instrument()
        if self.config.instrument_sqlite:
            SQLite3Instrumentor().instrument()

        self._tracer = trace.get_tracer(__name__)
        self._initialized = True
        logger.info(f"OpenTelemetry initialized: service={self.config.service_name}, "
                    f"sampler={self.config.sampler}, exporter={self.config.exporter}")

    def _create_sampler(self):
        """创建采样器."""
        from opentelemetry.sdk.trace.sampling import (
            TraceIdRatioBased, AlwaysOnSampler, AlwaysOffSampler, ParentBased
        )
        if self.config.sampler == "always_on":
            base = AlwaysOnSampler()
        elif self.config.sampler == "always_off":
            base = AlwaysOffSampler()
        elif self.config.sampler == "traceidratio":
            base = TraceIdRatioBased(self.config.sample_rate)
        else:
            base = TraceIdRatioBased(0.1)

        # 父级决策优先
        return ParentBased(base)

    def _create_exporter(self):
        """创建导出器."""
        if self.config.exporter == "console":
            return ConsoleSpanExporter()
        elif self.config.exporter == "otlp":
            return OTLPSpanExporter(
                endpoint=self.config.otlp_endpoint,
                headers=self.config.otlp_headers,
            )
        elif self.config.exporter == "jaeger":
            # 需额外依赖 opentelemetry-exporter-jaeger
            try:
                from opentelemetry.exporter.jaeger.proto.grpc import JaegerExporter
                return JaegerExporter(
                    agent_host_name=os.getenv("JAEGER_HOST", "localhost"),
                    agent_port=int(os.getenv("JAEGER_PORT", "6831")),
                )
            except ImportError:
                logger.warning("Jaeger exporter not available, falling back to console")
                return ConsoleSpanExporter()
        elif self.config.exporter == "zipkin":
            try:
                from opentelemetry.exporter.zipkin.proto.http import ZipkinExporter
                return ZipkinExporter(
                    endpoint=os.getenv("ZIPKIN_ENDPOINT", "http://localhost:9411/api/v2/spans"),
                )
            except ImportError:
                logger.warning("Zipkin exporter not available, falling back to console")
                return ConsoleSpanExporter()
        return ConsoleSpanExporter()

    def get_tracer(self):
        """获取 Tracer."""
        if not self._initialized:
            return None
        return self._tracer

    @contextmanager
    def start_span(
        self,
        name: str,
        kind: Any = None,
        attributes: Dict[str, Any] = None,
        links: list = None,
    ):
        """启动 Span 上下文管理器."""
        if not self._initialized or not self._tracer:
            yield None
            return

        if kind is None:
            kind = SpanKind.INTERNAL

        with self._tracer.start_as_current_span(
            name, kind=kind, attributes=attributes, links=links
        ) as span:
            try:
                yield span
            except Exception as e:
                if span:
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                    span.record_exception(e)
                raise

    def inject_context(self, carrier: Dict[str, str]):
        """注入 Trace Context 到 carrier (HTTP headers 等)."""
        if not self._initialized:
            return
        from opentelemetry.propagate import inject
        inject(carrier)

    def extract_context(self, carrier: Dict[str, str]):
        """从 carrier 提取 Trace Context."""
        if not self._initialized:
            return None
        from opentelemetry.propagate import extract
        return extract(carrier)

    def shutdown(self):
        """关闭追踪."""
        if self._tracer_provider:
            self._tracer_provider.shutdown()
            self._initialized = False
            logger.info("OpenTelemetry shutdown")


# 全局实例
_tracing_manager: Optional[TracingManager] = None


def get_tracing_manager(config: Optional[TracingConfig] = None) -> TracingManager:
    global _tracing_manager
    if _tracing_manager is None:
        _tracing_manager = TracingManager(config)
    return _tracing_manager


# ═══════════════════════════════════════════════════════════════════
# 便捷装饰器
# ═══════════════════════════════════════════════════════════════════

def traced(name: str = None, kind: Any = None, attributes: Dict[str, Any] = None):
    """函数追踪装饰器."""
    def decorator(func: Callable):
        def wrapper(*args, **kwargs):
            manager = get_tracing_manager()
            if not manager._initialized:
                return func(*args, **kwargs)

            span_name = name or f"{func.__module__}.{func.__qualname__}"
            with manager.start_span(span_name, kind=kind, attributes=attributes) as span:
                return func(*args, **kwargs)
        return wrapper
    return decorator


@contextmanager
def trace_context(name: str, attributes: Dict[str, Any] = None):
    """追踪上下文管理器."""
    manager = get_tracing_manager()
    if not manager._initialized:
        yield None
        return

    with manager.start_span(name, attributes=attributes) as span:
        yield span


def inject_trace_headers(headers: Dict[str, str]):
    """注入追踪头到 HTTP 请求."""
    manager = get_tracing_manager()
    if manager._initialized:
        manager.inject_context(headers)


def extract_trace_headers(headers: Dict[str, str]):
    """从 HTTP 响应头提取追踪上下文."""
    manager = get_tracing_manager()
    if manager._initialized:
        return manager.extract_context(headers)
    return None