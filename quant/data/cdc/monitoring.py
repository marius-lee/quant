"""CDC 监控与告警 — 延迟/积压/错误率实时监控."""

from __future__ import annotations
import threading
import time
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Callable, Any
from collections import deque, defaultdict
from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

logger = get_logger("data.cdc.monitoring")


@dataclass
class CDCMetrics:
    """CDC 核心指标."""
    # 延迟指标
    capture_latency_ms: float = 0.0      # 捕获延迟 (WAL 到内存)
    sync_latency_ms: float = 0.0         # 同步延迟 (内存到 DuckDB)
    end_to_end_latency_ms: float = 0.0   # 端到端延迟

    # 吞吐指标
    events_per_sec: float = 0.0          # 事件/秒
    rows_per_sec: float = 0.0            # 行/秒
    batches_per_sec: float = 0.0         # 批次/秒

    # 积压指标
    pending_events: int = 0              # 待处理事件数
    pending_bytes: int = 0               # 待处理字节数
    oldest_pending_age_sec: float = 0.0  # 最老待处理事件年龄

    # 错误指标
    error_rate: float = 0.0              # 错误率
    errors_last_minute: int = 0          # 最近1分钟错误数
    consecutive_failures: int = 0        # 连续失败次数

    # 资源指标
    memory_usage_mb: float = 0.0         # 内存使用
    cpu_percent: float = 0.0             # CPU 占用
    disk_usage_mb: float = 0.0           # 磁盘占用 (WAL/变更表)

    timestamp: float = field(default_factory=time.time)


class CDCMonitor:
    """CDC 监控器 - 实时收集、聚合、告警."""

    def __init__(
        self,
        evaluation_interval: int = 10,       # 评估间隔(秒)
        alert_cooldown: int = 300,           # 告警冷却(秒)
        history_window: int = 300,           # 指标历史窗口(秒)
    ):
        self.evaluation_interval = evaluation_interval
        self.alert_cooldown = alert_cooldown
        self.history_window = history_window

        self._metrics_history: deque = deque(maxlen=history_window // evaluation_interval)
        self._alerts: List[Dict] = []
        self._alert_cooldowns: Dict[str, float] = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # 告警规则
        self._alert_rules: List[Dict] = [
            {
                "name": "high_sync_latency",
                "condition": lambda m: m.sync_latency_ms > 5000,
                "severity": "critical",
                "message": "CDC sync latency > 5s",
            },
            {
                "name": "high_end_to_end_latency",
                "condition": lambda m: m.end_to_end_latency_ms > 10000,
                "severity": "critical",
                "message": "End-to-end latency > 10s",
            },
            {
                "name": "high_pending_events",
                "condition": lambda m: m.pending_events > 100000,
                "severity": "warning",
                "message": "Pending events > 100k",
            },
            {
                "name": "high_error_rate",
                "condition": lambda m: m.error_rate > 0.05,
                "severity": "critical",
                "message": "Error rate > 5%",
            },
            {
                "name": "consecutive_failures",
                "condition": lambda m: m.consecutive_failures > 5,
                "severity": "critical",
                "message": "Consecutive failures > 5",
            },
            {
                "name": "high_memory_usage",
                "condition": lambda m: m.memory_usage_mb > 2048,
                "severity": "warning",
                "message": "Memory usage > 2GB",
            },
            {
                "name": "high_cpu_usage",
                "condition": lambda m: m.cpu_percent > 90,
                "severity": "warning",
                "message": "CPU usage > 90%",
            },
            {
                "name": "stale_data",
                "condition": lambda m: m.oldest_pending_age_sec > 300,
                "severity": "warning",
                "message": "Oldest pending event > 5min",
            },
        ]

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._current_metrics: Optional[CDCMetrics] = None
        self._handlers: Dict[str, List[Callable[[CDCMetrics, str, str], None]]] = defaultdict(list)

    def start(self):
        """启动监控."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True, name="cdc-monitor")
        self._thread.start()
        logger.info("CDC Monitor started")

    def stop(self):
        """停止监控."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("CDC Monitor stopped")

    def _monitor_loop(self):
        """监控主循环."""
        while self._running:
            try:
                metrics = self._collect_metrics()
                self._evaluate_alerts(metrics)
                self._store_metrics(metrics)
            except Exception as e:
                logger.error(f"CDC monitor error: {e}")
            time.sleep(self.evaluation_interval)

    def _collect_metrics(self) -> CDCMetrics:
        """收集当前指标 (需由外部组件注入数据)."""
        # 这里提供基础框架，实际指标由外部组件通过 record_* 方法注入
        metrics = CDCMetrics()

        # 从各组件收集 (简化版，实际需要集成到各组件)
        try:
            import psutil
            import psutil
            process = psutil.Process()
            metrics.memory_usage_mb = process.memory_info().rss / 1024 / 1024
            metrics.cpu_percent = process.cpu_percent()
        except Exception:
            pass

        self._current_metrics = metrics
        return metrics

    def _evaluate_alerts(self, metrics: CDCMetrics):
        """评估告警规则."""
        now = time.time()
        for rule in self._alert_rules:
            try:
                if rule["condition"](metrics):
                    alert_key = rule["name"]
                    last_alert = self._alert_cooldowns.get(alert_key, 0)

                    if now - last_alert >= 300:  # 5分钟冷却
                        self._fire_alert(rule, metrics)
                        self._alert_cooldowns[alert_key] = now
            except Exception as e:
                logger.error(f"Alert evaluation error for {rule['name']}: {e}")

    def _fire_alert(self, rule: Dict, metrics: CDCMetrics):
        """触发告警."""
        alert = {
            "name": rule["name"],
            "severity": rule["severity"],
            "message": rule["message"],
            "timestamp": time.time(),
            "metrics": {
                "sync_latency_ms": metrics.sync_latency_ms,
                "end_to_end_latency_ms": metrics.end_to_end_latency_ms,
                "pending_events": metrics.pending_events,
                "error_rate": metrics.error_rate,
                "memory_usage_mb": metrics.memory_usage_mb,
            },
        }
        self._alerts.append(alert)
        logger.warning(f"CDC ALERT [{rule['severity'].upper()}]: {rule['message']}")

        # 触发回调
        for handler in self._handlers.get(rule["severity"], []):
            try:
                handler(rule, metrics)
            except Exception as e:
                logger.error(f"Alert handler error: {e}")

    def record_capture(self, latency_ms: float, events_count: int, bytes_count: int):
        """记录捕获指标 (由 Capture 组件调用)."""
        pass  # 实际实现中更新内部计数器

    def record_sync(self, latency_ms: float, rows: int, success: bool, error: str = None):
        """记录同步指标 (由 Syncer 组件调用)."""
        pass

    def record_error(self, error: str, component: str):
        """记录错误 (由各组件调用)."""
        pass

    def get_current_metrics(self) -> Optional[CDCMetrics]:
        return self._current_metrics

    def get_alerts(self, since: Optional[float] = None) -> List[Dict]:
        if since is None:
            return list(self._alerts)
        return [a for a in self._alerts if a["timestamp"] >= since]

    def register_alert_handler(self, severity: str, handler: Callable[[Dict, CDCMetrics], None]):
        """注册告警处理器 (webhook/email/slack 等)."""
        pass


class MetricsExporter:
    """指标导出器 - Prometheus/Grafana 集成."""

    def __init__(self, monitor: CDCMonitor):
        self.monitor = monitor

    def generate_prometheus_metrics(self) -> str:
        """生成 Prometheus 格式指标."""
        metrics = self.monitor.get_current_metrics()
        if not metrics:
            return ""

        lines = []
        m = self.monitor._current_metrics
        if not m:
            return ""

        # 定义指标
        metrics_map = {
            "cdc_capture_latency_ms": m.capture_latency_ms,
            "cdc_sync_latency_ms": m.sync_latency_ms,
            "cdc_end_to_end_latency_ms": m.end_to_end_latency_ms,
            "cdc_events_per_sec": m.events_per_sec,
            "cdc_rows_per_sec": m.rows_per_sec,
            "cdc_pending_events": m.pending_events,
            "cdc_pending_bytes": m.pending_bytes,
            "cdc_oldest_pending_age_sec": m.oldest_pending_age_sec,
            "cdc_error_rate": m.error_rate,
            "cdc_errors_last_minute": m.errors_last_minute,
            "cdc_consecutive_failures": m.consecutive_failures,
            "cdc_memory_usage_mb": m.memory_usage_mb,
            "cdc_cpu_percent": m.cpu_percent,
        }

        for name, value in metrics_map.items():
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value}")

        return "\n".join(lines)


# 全局实例
_monitor: Optional[CDCMonitor] = None


def get_cdc_monitor(
    evaluation_interval: int = 10,
    alert_cooldown: int = 300,
) -> CDCMonitor:
    global _monitor
    if _monitor is None:
        _monitor = CDCMonitor(
            evaluation_interval=evaluation_interval,
            alert_cooldown=alert_cooldown,
        )
    return _monitor


def get_metrics_exporter() -> MetricsExporter:
    monitor = get_cdc_monitor()
    return MetricsExporter(monitor)