"""Prometheus 指标导出器 — 统一 /metrics 端点 + Pushgateway 支持.

功能:
  - 统一注册表管理 (业务/系统/数据质量指标)
  - Flask/FastAPI 中间件自动暴露 /metrics
  - Pushgateway 批量推送 (适合短生命周期 Job)
  - 多进程模式支持 (Gunicorn)
"""

from __future__ import annotations
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, Callable
from contextlib import contextmanager
from prometheus_client import (
    Counter, Histogram, Gauge, Summary,
    CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST,
    push_to_gateway, multiprocess, REGISTRY,
)
from prometheus_client.core import CollectorRegistry as CoreRegistry
from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

logger = get_logger("monitoring.prometheus")


@dataclass
class PrometheusExporter:
    """Prometheus 指标导出器."""

    registry: CollectorRegistry = field(default_factory=CollectorRegistry)
    multiprocess_mode: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ═══════════════════════════════════════════════════════
    # 业务指标
    # ═══════════════════════════════════════════════════════

    # 交易指标
    trades_total = Counter(
        "quant_trades_total",
        "Total trades executed",
        ["strategy", "side", "status"],  # buy/sell, success/failed/rejected
        registry=None,
    )
    trade_volume = Counter(
        "quant_trade_volume_total",
        "Total trade volume (shares)",
        ["strategy", "symbol"],
        registry=None,
    )
    trade_pnl = Histogram(
        "quant_trade_pnl",
        "Trade PnL distribution",
        ["strategy", "symbol"],
        registry=None,
        buckets=[-10000, -5000, -1000, -500, -100, -50, -10, 0, 10, 50, 100, 500, 1000, 5000, 10000],
    )

    # 信号指标
    signals_generated = Counter(
        "quant_signals_generated_total",
        "Total signals generated",
        ["strategy", "signal_type"],  # buy/sell/hold
        registry=None,
    )
    signal_score = Histogram(
        "quant_signal_score",
        "Signal score distribution",
        ["strategy"],
        registry=None,
        buckets=[0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0],
    )

    # 持仓指标
    positions_total = Gauge(
        "quant_positions_total",
        "Current number of positions",
        ["strategy"],
        registry=None,
    )
    portfolio_value = Gauge(
        "quant_portfolio_value",
        "Current portfolio value",
        ["strategy"],
        registry=None,
    )
    position_concentration = Gauge(
        "quant_position_concentration",
        "Single position concentration %",
        ["strategy", "symbol"],
        registry=None,
    )

    # 风控指标
    stop_loss_triggered = Counter(
        "quant_stop_loss_triggered_total",
        "Stop loss triggered count",
        ["strategy", "symbol", "type"],  # hard/trailing/time
        registry=None,
    )
    circuit_breaker_active = Gauge(
        "quant_circuit_breaker_active",
        "Circuit breaker active (1=yes)",
        ["strategy"],
        registry=None,
    )
    var_95 = Gauge(
        "quant_var_95",
        "Value at Risk 95%",
        ["strategy"],
        registry=None,
    )
    max_drawdown = Gauge(
        "quant_max_drawdown",
        "Maximum drawdown %",
        ["strategy"],
        registry=None,
    )

    # ═══════════════════════════════════════════════════════
    # 数据质量指标
    # ══════════════════════════════════════════════════════

    data_freshness_seconds = Gauge(
        "quant_data_freshness_seconds",
        "Data freshness in seconds",
        ["table", "source"],
        registry=None,
    )
    data_rows_total = Gauge(
        "quant_data_rows_total",
        "Total rows in table",
        ["table"],
        registry=None,
    )
    data_sync_duration = Histogram(
        "quant_data_sync_duration_seconds",
        "Data sync duration",
        ["table", "operation"],  # incremental/full
        registry=None,
        buckets=[1, 5, 10, 30, 60, 120, 300, 600, 1800, 3600],
    )
    data_sync_errors = Counter(
        "quant_data_sync_errors_total",
        "Data sync errors",
        ["table", "error_type"],
        registry=None,
    )
    data_validation_failures = Counter(
        "quant_data_validation_failures_total",
        "Data validation failures",
        ["table", "rule", "severity"],
        registry=None,
    )

    # ══════════════════════════════════════════════════════
    # 系统指标
    # ═════════════════════════════════════════════════════

    http_requests_total = Counter(
        "quant_http_requests_total",
        "HTTP requests total",
        ["method", "endpoint", "status"],
        registry=None,
    )
    http_request_duration = Histogram(
        "quant_http_request_duration_seconds",
        "HTTP request duration",
        ["method", "endpoint"],
        registry=None,
        buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10],
    )
    db_query_duration = Histogram(
        "quant_db_query_duration_seconds",
        "Database query duration",
        ["query_type", "table"],
        registry=None,
        buckets=[0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1, 5],
    )
    db_connections_active = Gauge(
        "quant_db_connections_active",
        "Active database connections",
        ["database"],
        registry=None,
    )
    cache_hit_ratio = Gauge(
        "quant_cache_hit_ratio",
        "Cache hit ratio",
        ["cache_name"],
        registry=None,
    )

    # ══════════════════════════════════════════════════════
    # 调度器指标
    # ════════════════════════════════════════════════════

    scheduler_task_duration = Histogram(
        "quant_scheduler_task_duration_seconds",
        "Scheduler task duration",
        ["task_name", "status"],
        registry=None,
        buckets=[1, 5, 10, 30, 60, 120, 300, 600, 1800, 3600, 7200],
    )
    scheduler_task_total = Counter(
        "quant_scheduler_task_total",
        "Scheduler task total",
        ["task_name", "status"],
        registry=None,
    )
    scheduler_queue_depth = Gauge(
        "quant_scheduler_queue_depth",
        "Scheduler queue depth",
        ["queue_name"],
        registry=None,
    )

    def __post_init__(self):
        # 设置 registry
        for attr_name in dir(self):
            attr = getattr(self, attr_name)
            if hasattr(attr, 'registry') and attr.registry is None:
                attr.registry = self.registry

    def setup_multiprocess(self, registry: Optional[CollectorRegistry] = None):
        """启用多进程模式 (Gunicorn pre-fork)."""
        if "PROMETHEUS_MULTIPROC_DIR" in os.environ:
            self.multiprocess_mode = True
            if registry is None:
                registry = self.registry
            multiprocess.MultiProcessCollector(registry)
            logger.info("Prometheus multiprocess mode enabled")

    def generate_metrics(self) -> bytes:
        """生成 Prometheus 文本格式指标."""
        with self._lock:
            return generate_latest(self.registry)

    def push_to_gateway(self, gateway_url: str, job_name: str = "quant", grouping_key: Optional[Dict] = None):
        """推送到 Pushgateway."""
        try:
            push_to_gateway(gateway_url, job=job_name, registry=self.registry, grouping_key=grouping_key)
            logger.debug(f"Pushed metrics to {gateway_url}")
        except Exception as e:
            logger.warning(f"Push to gateway failed: {e}")

    def start_push_loop(self, gateway_url: str, job_name: str = "quant", interval: int = 30):
        """启动后台推送循环."""
        def _push_loop():
            while True:
                try:
                    self.push_to_gateway(gateway_url, job_name)
                except Exception as e:
                    logger.warning(f"Push loop error: {e}")
                time.sleep(interval)

        thread = threading.Thread(target=_push_loop, daemon=True, name="prometheus-push")
        thread.start()
        logger.info(f"Started Pushgateway push loop: {gateway_url} every {interval}s")


# 全局实例
_prometheus_exporter: Optional[PrometheusExporter] = None


def get_prometheus_exporter() -> PrometheusExporter:
    global _prometheus_exporter
    if _prometheus_exporter is None:
        _prometheus_exporter = PrometheusExporter()
    return _prometheus_exporter


# ════════════════════════════════════════════════════════
# Flask/FastAPI 中间件
# ══════════════════════════════════════════════════════

def setup_metrics_endpoint(app, path: str = "/metrics"):
    """为 Flask/FastAPI 应用添加 /metrics 端点."""
    exporter = get_prometheus_exporter()

    # 检测框架类型
    if hasattr(app, "route"):  # Flask
        @app.route(path)
        def metrics():
            from flask import Response
            return Response(exporter.generate_metrics(), mimetype=CONTENT_TYPE_LATEST)
    elif hasattr(app, "add_route"):  # FastAPI/Starlette
        from fastapi import Response
        @app.get(path)
        async def metrics():
            return Response(content=exporter.generate_metrics(), media_type=CONTENT_TYPE_LATEST)
    else:
        logger.warning("Unknown framework, metrics endpoint not added")

    logger.info(f"Metrics endpoint registered at {path}")


# ══════════════════════════════════════════════════════
# 便捷装饰器
# ════════════════════════════════════════════════════

def timed_histogram(histogram: Histogram, labels: Optional[Dict[str, str]] = None):
    """函数耗时装饰器."""
    def decorator(func: Callable):
        def wrapper(*args, **kwargs):
            start = time.perf_counter()
            try:
                return func(*args, **kwargs)
            finally:
                duration = time.perf_counter() - start
                if labels:
                    histogram.labels(**labels).observe(duration)
                else:
                    histogram.observe(duration)
        return wrapper
    return decorator


def count_calls(counter: Counter, labels: Optional[Dict[str, str]] = None):
    """函数调用计数装饰器."""
    def decorator(func: Callable):
        def wrapper(*args, **kwargs):
            try:
                result = func(*args, **kwargs)
                if labels:
                    counter.labels(**labels).inc()
                else:
                    counter.inc()
                return result
            except Exception as e:
                if labels:
                    counter.labels(**{**labels, "status": "error"}).inc()
                else:
                    counter.labels(status="error").inc()
                raise
        return wrapper
    return decorator


@contextmanager
def measure_duration(histogram: Histogram, labels: Optional[Dict[str, str]] = None):
    """上下文管理器测量耗时."""
    start = time.perf_counter()
    try:
        yield
    finally:
        duration = time.perf_counter() - start
        if labels:
            histogram.labels(**labels).observe(duration)
        else:
            histogram.observe(duration)