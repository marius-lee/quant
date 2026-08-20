"""数据源审计 — 结构化日志 + 指标采集 + 追踪关联."""

from __future__ import annotations
import json
import time
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Any
from collections import deque
from pathlib import Path

from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg
from quant.config.paths import LOGS_DIR

logger = get_logger("data.sources.audit")


@dataclass
class AuditEntry:
    """单条审计记录."""
    timestamp: str                          # ISO 格式
    source: str                             # 数据源名称
    operation: str                          # 操作类型: fetch/health_check/sync
    success: bool                           # 是否成功
    latency_ms: float                       # 延迟(毫秒)
    rows: int = 0                           # 影响行数
    error: str | None = None                # 错误信息
    error_code: str | None = None           # 错误码
    trace_id: str | None = None             # 分布式追踪ID
    metadata: dict = field(default_factory=dict)  # 扩展元数据

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, s: str) -> "AuditEntry":
        return cls(**json.loads(s))


class DataSourceAudit:
    """数据源审计器.

    功能:
      1. 结构化日志落盘 (JSON Lines, 按天分文件)
      2. 内存环形缓冲 (最近 N 条, 供实时查询)
      3. Prometheus 指标导出
      4. 实时告警触发 (错误率/延迟/熔断)
    """

    def __init__(
        self,
        max_memory_entries: int = 10000,
        log_dir: str | None = None,
        enable_prometheus: bool = True,
    ):
        self.max_memory_entries = max_memory_entries
        self.log_dir = Path(log_dir or LOGS_DIR) / "sources_audit"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.enable_prometheus = enable_prometheus

        self._buffer: deque[AuditEntry] = deque(maxlen=max_memory_entries)
        self._lock = threading.Lock()
        self._current_log_file: Path | None = None
        self._log_file_handle = None
        self._log_file_date: str | None = None

        # Prometheus 指标(延迟导入)
        self._metrics = None
        if enable_prometheus:
            self._init_metrics()

    def _init_metrics(self):
        """初始化 Prometheus 指标."""
        try:
            from prometheus_client import Counter, Histogram, Gauge
            self._metrics = {
                "calls_total": Counter(
                    "datasource_calls_total",
                    "Total data source calls",
                    ["source", "operation", "status"],
                ),
                "latency_seconds": Histogram(
                    "datasource_latency_seconds",
                    "Data source call latency",
                    ["source", "operation"],
                    buckets=[0.01, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30],
                ),
                "rows_total": Counter(
                    "datasource_rows_total",
                    "Total rows processed",
                    ["source", "operation"],
                ),
                "errors_total": Counter(
                    "datasource_errors_total",
                    "Total errors by code",
                    ["source", "operation", "error_code"],
                ),
                "circuit_state": Gauge(
                    "datasource_circuit_state",
                    "Circuit breaker state (0=closed, 1=half_open, 2=open)",
                    ["source"],
                ),
            }
        except ImportError:
            self._metrics = None
            logger.warning("prometheus_client not installed, metrics disabled")

    def record(
        self,
        source: str,
        operation: str,
        success: bool,
        latency_ms: float,
        rows: int = 0,
        error: str | None = None,
        error_code: str | None = None,
        trace_id: str | None = None,
        metadata: dict | None = None,
    ):
        """记录一条审计条目."""
        entry = AuditEntry(
            timestamp=datetime.now().isoformat(),
            source=source,
            operation=operation,
            success=success,
            latency_ms=latency_ms,
            rows=rows,
            error=error,
            error_code=error_code,
            trace_id=trace_id,
            metadata=metadata or {},
        )

        # 内存缓冲
        with self._lock:
            self._buffer.append(entry)

        # 异步落盘(批量写入, 避免阻塞)
        self._write_async(entry)

        # Prometheus 指标
        if self._metrics:
            self._metrics["calls_total"].labels(
                source=source, operation=operation, status="success" if success else "failure"
            ).inc()
            self._metrics["latency_seconds"].labels(
                source=source, operation=operation
            ).observe(latency_ms / 1000)
            if rows:
                self._metrics["rows_total"].labels(source=source, operation=operation).inc(rows)
            if not success and error_code:
                self._metrics["errors_total"].labels(
                    source=source, operation=operation, error_code=error_code
                ).inc()

    def _write_async(self, entry: AuditEntry):
        """异步写入日志文件(简单实现: 直接写, 生产环境建议用队列+后台线程)."""
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            if self._log_file_date != today:
                if self._log_file_handle:
                    self._log_file_handle.close()
                self._log_file_date = today
                self._current_log_file = self.log_dir / f"audit_{today}.jsonl"
                self._log_file_handle = open(self._current_log_file, "a", encoding="utf-8")

            if self._log_file_handle:
                self._log_file_handle.write(entry.to_json() + "\n")
                self._log_file_handle.flush()
        except Exception as e:
            logger.warning(f"audit write failed: {e}")

    def get_recent(self, n: int = 100, source: str | None = None) -> list[AuditEntry]:
        """获取最近 N 条记录."""
        with self._lock:
            entries = list(self._buffer)
        if source:
            entries = [e for e in entries if e.source == source]
        return entries[-n:]

    def get_stats(self, source: str | None = None, since_seconds: float = 3600) -> dict:
        """获取统计摘要."""
        cutoff = time.time() - since_seconds
        with self._lock:
            entries = list(self._buffer)

        if source:
            entries = [e for e in entries if e.source == source]

        # 过滤时间窗口
        recent = []
        for e in entries:
            try:
                ts = datetime.fromisoformat(e.timestamp).timestamp()
                if ts >= cutoff:
                    recent.append(e)
            except ValueError:
                pass

        if not recent:
            return {"total": 0}

        total = len(recent)
        success = sum(1 for e in recent if e.success)
        failed = total - success
        total_latency = sum(e.latency_ms for e in recent)
        total_rows = sum(e.rows for e in recent)

        # 按错误码统计
        error_codes = {}
        for e in recent:
            if not e.success and e.error_code:
                error_codes[e.error_code] = error_codes.get(e.error_code, 0) + 1

        # 按操作统计
        by_op = {}
        for e in recent:
            op = e.operation
            if op not in by_op:
                by_op[op] = {"total": 0, "success": 0, "failed": 0, "avg_latency_ms": 0}
            by_op[op]["total"] += 1
            if e.success:
                by_op[op]["success"] += 1
            else:
                by_op[op]["failed"] += 1
            by_op[op]["avg_latency_ms"] += e.latency_ms
        for op in by_op:
            by_op[op]["avg_latency_ms"] /= by_op[op]["total"]

        return {
            "total": total,
            "success": success,
            "failed": failed,
            "success_rate": success / total if total else 0,
            "avg_latency_ms": total_latency / total if total else 0,
            "total_rows": total_rows,
            "error_codes": error_codes,
            "by_operation": by_op,
            "window_seconds": since_seconds,
        }

    def update_circuit_state(self, source: str, state: int):
        """更新熔断器状态指标."""
        if self._metrics:
            self._metrics["circuit_state"].labels(source=source).set(state)

    def close(self):
        """关闭资源."""
        if self._log_file_handle:
            self._log_file_handle.close()
            self._log_file_handle = None