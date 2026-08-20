"""Dagster 原生指标导出与集成.

功能:
  - Asset 级指标: materialization 耗时、行数、成功率、数据新鲜度
  - Run 级指标: 总耗时、步骤数、资源消耗、重试次数
  - 资源指标: 连接池、内存、CPU、队列深度
  - 自动注册到 Prometheus
"""

from __future__ import annotations
import time
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Any, Optional
from collections import defaultdict
from contextlib import contextmanager
from prometheus_client import Counter, Histogram, Gauge, CollectorRegistry, push_to_gateway
from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

logger = get_logger("monitoring.dagster")


@dataclass
class DagsterMetricsExporter:
    """Dagster 指标导出器."""

    registry: CollectorRegistry = field(default_factory=CollectorRegistry)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # Asset 级指标
    asset_materialization_duration = Histogram(
        "dagster_asset_materialization_duration_seconds",
        "Asset materialization duration",
        ["asset_key", "status"],  # success/failed
        registry=None,  # 延迟设置
        buckets=[1, 5, 10, 30, 60, 120, 300, 600, 1800, 3600],
    )
    asset_rows_materialized = Counter(
        "dagster_asset_rows_materialized_total",
        "Total rows materialized per asset",
        ["asset_key"],
        registry=None,
    )
    asset_materialization_total = Counter(
        "dagster_asset_materialization_total",
        "Total materialization attempts per asset",
        ["asset_key", "status"],
        registry=None,
    )
    asset_freshness_seconds = Gauge(
        "dagster_asset_freshness_seconds",
        "Seconds since last successful materialization",
        ["asset_key"],
        registry=None,
    )

    # Run 级指标
    run_duration = Histogram(
        "dagster_run_duration_seconds",
        "Pipeline run duration",
        ["job_name", "status"],
        registry=None,
        buckets=[10, 30, 60, 120, 300, 600, 1800, 3600, 7200],
    )
    run_steps_total = Counter(
        "dagster_run_steps_total",
        "Total steps per run",
        ["job_name", "status"],
        registry=None,
    )
    run_retries_total = Counter(
        "dagster_run_retries_total",
        "Total retries per run",
        ["job_name"],
        registry=None,
    )

    # 资源指标
    resource_usage = Gauge(
        "dagster_resource_usage",
        "Resource usage",
        ["resource_name", "metric"],  # connections, memory_mb, cpu_percent
        registry=None,
    )
    queue_depth = Gauge(
        "dagster_queue_depth",
        "Execution queue depth",
        ["queue_name"],
        registry=None,
    )

    def __post_init__(self):
        # 设置 registry
        for metric in [
            self.asset_materialization_duration,
            self.asset_rows_materialized,
            self.asset_materialization_total,
            self.asset_freshness_seconds,
            self.run_duration,
            self.run_steps_total,
            self.run_retries_total,
            self.resource_usage,
            self.queue_depth,
        ]:
            metric.registry = self.registry

    def record_asset_materialization(
        self,
        asset_key: str,
        duration_seconds: float,
        rows: int = 0,
        success: bool = True,
    ):
        """记录 Asset 物化."""
        status = "success" if success else "failed"
        with self._lock:
            self.asset_materialization_duration.labels(
                asset_key=asset_key, status=status
            ).observe(duration_seconds)
            self.asset_materialization_total.labels(
                asset_key=asset_key, status=status
            ).inc()
            if rows > 0:
                self.asset_rows_materialized.labels(asset_key=asset_key).inc(rows)
            if success:
                self.asset_freshness_seconds.labels(asset_key=asset_key).set(0)
            else:
                # 失败时增加新鲜度 (由外部定时任务递增)
                pass

    def record_run(
        self,
        job_name: str,
        duration_seconds: float,
        steps: int,
        retries: int,
        success: bool,
    ):
        """记录 Run 完成."""
        status = "success" if success else "failed"
        with self._lock:
            self.run_duration.labels(job_name=job_name, status=status).observe(duration_seconds)
            self.run_steps_total.labels(job_name=job_name, status=status).inc(steps)
            if retries > 0:
                self.run_retries_total.labels(job_name=job_name).inc(retries)

    def update_resource_usage(self, resource_name: str, metric: str, value: float):
        """更新资源使用指标."""
        with self._lock:
            self.resource_usage.labels(resource_name=resource_name, metric=metric).set(value)

    def update_queue_depth(self, queue_name: str, depth: int):
        """更新队列深度."""
        with self._lock:
            self.queue_depth.labels(queue_name=queue_name).set(depth)

    def push_to_gateway(self, gateway_url: str, job_name: str = "dagster"):
        """推送到 Pushgateway (适合批量作业)."""
        try:
            push_to_gateway(gateway_url, job=job_name, registry=self.registry)
            logger.debug(f"Pushed metrics to {gateway_url}")
        except Exception as e:
            logger.warning(f"Push to gateway failed: {e}")

    def get_metrics(self) -> bytes:
        """获取 Prometheus 文本格式指标."""
        from prometheus_client import generate_latest
        return generate_latest(self.registry)


# 全局实例
_dagster_exporter: Optional[DagsterMetricsExporter] = None


def get_dagster_exporter() -> DagsterMetricsExporter:
    global _dagster_exporter
    if _dagster_exporter is None:
        _dagster_exporter = DagsterMetricsExporter()
    return _dagster_exporter


# ═══════════════════════════════════════════════════════════════════
# Dagster Hook 集成
# ═══════════════════════════════════════════════════════════════════

def setup_dagster_hooks():
    """设置 Dagster Hooks (在 repository/definitions 加载时调用)."""
    try:
        import dagster as dg
        from dagster import HookContext, hook, SuccessHookContext, FailureHookContext

        exporter = get_dagster_exporter()

        @hook(name="asset_materialization_metrics", required_resource_keys={})
        def asset_materialization_hook(context: HookContext):
            """Asset 物化后记录指标."""
            if hasattr(context, "asset_key"):
                asset_key = str(context.asset_key)
                # 从事件中提取元数据
                metadata = getattr(context, "metadata", {})
                duration = metadata.get("duration_seconds", 0)
                rows = metadata.get("rows", 0)
                success = getattr(context, "success", True)
                exporter.record_asset_materialization(asset_key, duration, rows, success)

        @hook(name="run_metrics", required_resource_keys={})
        def run_hook(context: HookContext):
            """Run 完成后记录指标."""
            if hasattr(context, "job_name"):
                # 从 run 统计中提取
                pass

        logger.info("Dagster hooks registered")
        return [asset_materialization_hook, run_hook]

    except ImportError:
        logger.warning("Dagster not available, hooks not registered")
        return []


# ═══════════════════════════════════════════════════════════════════
# 资源监控上下文管理器
# ═══════════════════════════════════════════════════════════════════

@contextmanager
def monitor_resource(resource_name: str):
    """监控资源使用 (连接数、内存、CPU)."""
    exporter = get_dagster_exporter()
    import psutil
    process = psutil.Process()

    start_mem = process.memory_info().rss / 1024 / 1024  # MB
    start_cpu = process.cpu_percent()

    try:
        yield
    finally:
        end_mem = process.memory_info().rss / 1024 / 1024
        end_cpu = process.cpu_percent()
        exporter.update_resource_usage(resource_name, "memory_mb", end_mem)
        exporter.update_resource_usage(resource_name, "cpu_percent", end_cpu)
        exporter.update_resource_usage(resource_name, "memory_delta_mb", end_mem - start_mem)


# ═══════════════════════════════════════════════════════════════════
# Asset 新鲜度定时更新 (供外部调度调用)
# ═══════════════════════════════════════════════════════════════════

def update_asset_freshness(asset_keys: List[str], last_materialization_times: Dict[str, float]):
    """更新 Asset 新鲜度 (秒)."""
    exporter = get_dagster_exporter()
    now = time.time()
    with exporter._lock:
        for key in asset_keys:
            last_time = last_materialization_times.get(key, 0)
            if last_time > 0:
                freshness = now - last_time
                exporter.asset_freshness_seconds.labels(asset_key=key).set(freshness)
            else:
                exporter.asset_freshness_seconds.labels(asset_key=key).set(-1)  # 未知