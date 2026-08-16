"""Prometheus/Grafana Monitoring — 指标聚合、告警路由、SLO 仪表盘.

功能:
  1. Prometheus Exporter: 推送/拉取模式指标暴露
  2. 业务指标: 交易/持仓/风控/因子/回测全链路指标
  3. 系统指标: CPU/内存/磁盘/网络/数据库连接池
  3. 告警规则: 阈值/趋势/异常检测 → Alertmanager → 多渠道通知
  4. Grafana Dashboard: 预置仪表盘 JSON (可导入 Grafana)
  5. SLO/SLI: 可用性/延迟/错误率/数据新鲜度

架构:
  应用 -> Prometheus Client (Push/Pull) -> Prometheus Server -> Alertmanager -> 通知渠道
                          ↘ Grafana Dashboard (可视化)
"""

import os
import json
import time
import threading
import socket
import psutil
import sqlite3
from contextlib import contextmanager
from typing import Dict, List, Optional, Any, Callable
from dataclasses import dataclass, field
from enum import Enum
from functools import wraps
from prometheus_client import (
    Counter, Gauge, Histogram, Summary, Info, Enum as PromEnum,
    CollectorRegistry, generate_latest, CONTENT_TYPE_LATEST,
    push_to_gateway, pushadd_to_gateway, delete_from_gateway,
)
from prometheus_client.core import REGISTRY

from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg
from quant.config.paths import MARKET_DB

_log = get_logger("monitoring.prometheus")


class MetricType(str, Enum):
    COUNTER = "counter"
    GAUGE = "gauge"
    HISTOGRAM = "histogram"
    SUMMARY = "summary"


# ── 全局指标注册表 (自定义 Registry, 避免污染全局 REGISTRY) ──

_metrics_registry = CollectorRegistry()
_metrics_cache: Dict[str, Any] = {}
_metrics_lock = threading.Lock()


def _get_or_create_metric(
    metric_type: MetricType,
    name: str,
    documentation: str,
    labels: List[str] = None,
    buckets: List[float] = None,
) -> Any:
    """获取或创建指标 (线程安全, 避免重复注册)."""
    key = f"{metric_type.value}:{name}"
    with _metrics_lock:
        if key in _metrics_cache:
            return _metrics_cache[key]

        labels = labels or []
        if metric_type == MetricType.COUNTER:
            metric = Counter(name, documentation, labels, registry=_metrics_registry)
        elif metric_type == MetricType.GAUGE:
            metric = Gauge(name, documentation, labels, registry=_metrics_registry)
        elif metric_type == MetricType.HISTOGRAM:
            metric = Histogram(name, documentation, labels, buckets=buckets, registry=_metrics_registry)
        elif metric_type == MetricType.SUMMARY:
            metric = Summary(name, documentation, labels, registry=_metrics_registry)
        else:
            raise ValueError(f"Unknown metric type: {metric_type}")

        _metrics_cache[key] = metric
        return metric


# ── 通用装饰器 ──

def monitor_latency(metric_name: str, labels: Dict[str, str] = None, buckets: List[float] = None):
    """函数耗时监控装饰器 (Histogram)."""
    histogram = _get_or_create_metric(
        MetricType.HISTOGRAM, metric_name, f"Latency of {metric_name}",
        labels=list(labels.keys()) if labels else None,
        buckets=buckets or [0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0],
    )

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start = time.time()
            try:
                return func(*args, **kwargs)
            finally:
                elapsed = time.time() - start
                labels_values = {**labels} if labels else {}
                histogram.labels(**labels_values).observe(time.time() - start)
        return wrapper
    return decorator


def monitor_count(metric_name: str, labels: Dict[str, str] = None, increment: float = 1.0):
    """计数器装饰器 (Counter)."""
    counter = _get_or_create_metric(
        MetricType.COUNTER, metric_name, f"Count of {metric_name}",
        labels=list(labels.keys()) if labels else None,
    )

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            finally:
                labels_values = {**labels} if labels else {}
                counter.labels(**labels_values).inc()
        return wrapper
    return decorator


def monitor_gauge(metric_name: str, labels: Dict[str, str] = None, value_func: Callable = None):
    """Gauge 监控装饰器 (设置当前值)."""
    gauge = _get_or_create_metric(
        MetricType.GAUGE, metric_name, f"Gauge of {metric_name}",
        labels=list(labels.keys()) if labels else None,
    )

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            result = func(*args, **kwargs)
            if value_func:
                value = value_func(result)
                labels_values = {**labels} if labels else {}
                gauge.labels(**labels_values).set(value)
            return result
        return wrapper
    return decorator


# ── 业务指标定义 (quant 专用) ──

class QuantMetrics:
    """Quant 业务指标集合 — 统一定义, 避免重复/冲突."""

    # ── 交易指标 ──
    TRADES_TOTAL = Counter(
        "quant_trades_total", "Total number of trades executed",
        ["strategy", "side", "symbol", "status"],
        registry=_metrics_registry,
    )
    TRADE_VOLUME = Counter(
        "quant_trade_volume_total", "Total trade volume (shares)",
        ["strategy", "symbol"],
        registry=_metrics_registry,
    )
    TRADE_PNL = Counter(
        "quant_trade_pnl_total", "Total PnL from trades",
        ["strategy", "symbol"],
        registry=_metrics_registry,
    )
    POSITION_VALUE = Gauge(
        "quant_position_value", "Current position market value",
        ["strategy", "symbol"],
        registry=_metrics_registry,
    )
    CASH_BALANCE = Gauge(
        "quant_cash_balance", "Available cash balance",
        ["strategy"],
        registry=_metrics_registry,
    )
    TOTAL_EQUITY = Gauge(
        "quant_total_equity", "Total equity (cash + positions)",
        ["strategy"],
        registry=_metrics_registry,
    )
    DRAWDOWN = Gauge(
        "quant_drawdown_pct", "Current drawdown percentage",
        ["strategy"],
        registry=_metrics_registry,
    )

    # ── 因子/Alpha 指标 ──
    FACTOR_IC = Gauge(
        "quant_factor_ic", "Factor IC value",
        ["factor_name", "scope"],
        registry=_metrics_registry,
    )
    FACTOR_ICIR = Gauge(
        "quant_factor_icir", "Factor ICIR",
        ["factor_name", "scope"],
        registry=_metrics_registry,
    )
    FACTOR_RANK = Gauge(
        "quant_factor_rank", "Factor rank percentile",
        ["factor_name"],
        registry=_metrics_registry,
    )
    ALPHA_SCORE = Gauge(
        "quant_alpha_score", "Alpha composite score",
        ["symbol", "strategy"],
        registry=_metrics_registry,
    )

    # ── 风控指标 ──
    VAR_95 = Gauge(
        "quant_var_95", "Value at Risk (95%)",
        ["strategy"],
        registry=_metrics_registry,
    )
    VAR_99 = Gauge(
        "quant_var_99", "Value at Risk (99%)",
        ["strategy"],
        registry=_metrics_registry,
    )
    MAX_DRAWDOWN = Gauge(
        "quant_max_drawdown_pct", "Maximum drawdown percentage",
        ["strategy"],
        registry=_metrics_registry,
    )
    LEVERAGE = Gauge(
        "quant_leverage", "Current leverage ratio",
        ["strategy"],
        registry=_metrics_registry,
    )
    TURNOVER = Gauge(
        "quant_turnover_ratio", "Portfolio turnover ratio",
        ["strategy"],
        registry=_metrics_registry,
    )
    CONCENTRATION = Gauge(
        "quant_concentration", "Single position concentration",
        ["strategy", "symbol"],
        registry=_metrics_registry,
    )

    # ── 数据质量指标 ──
    DATA_FRESHNESS = Gauge(
        "quant_data_freshness_hours", "Data freshness in hours",
        ["source", "table"],
        registry=_metrics_registry,
    )
    DATA_ROWS = Gauge(
        "quant_data_rows", "Number of rows in table",
        ["table"],
        registry=_metrics_registry,
    )
    DATA_STALENESS = Gauge(
        "quant_data_staleness", "Data staleness indicator (1=stale)",
        ["source", "table"],
        registry=_metrics_registry,
    )

    # ── 系统/基础设施指标 ──
    CPU_USAGE = Gauge(
        "quant_cpu_usage_percent", "CPU usage percentage",
        registry=_metrics_registry,
    )
    MEMORY_USAGE = Gauge(
        "quant_memory_usage_bytes", "Memory usage in bytes",
        registry=_metrics_registry,
    )
    DISK_USAGE = Gauge(
        "quant_disk_usage_bytes", "Disk usage in bytes",
        ["mountpoint"],
        registry=_metrics_registry,
    )
    DB_CONNECTIONS = Gauge(
        "quant_db_connections_active", "Active database connections",
        ["database"],
        registry=_metrics_registry,
    )
    QUEUE_SIZE = Gauge(
        "quant_queue_size", "Task queue size",
        ["queue_name"],
        registry=_metrics_registry,
    )

    # ── 调度器指标 ──
    TASK_DURATION = Histogram(
        "quant_task_duration_seconds", "Task execution duration",
        ["task_name", "status"],
        buckets=[1, 5, 10, 30, 60, 300, 600, 1800, 3600],
        registry=_metrics_registry,
    )
    TASK_STATUS = Counter(
        "quant_task_status_total", "Task execution status",
        ["task_name", "status"],
        registry=_metrics_registry,
    )
    SCHEDULER_POLL = Counter(
        "quant_scheduler_poll_total", "Scheduler poll count",
        ["status"],
        registry=_metrics_registry,
    )

    # ── 回测指标 ──
    BACKTEST_SHARPE = Gauge(
        "quant_backtest_sharpe", "Latest backtest Sharpe ratio",
        ["strategy"],
        registry=_metrics_registry,
    )
    BACKTEST_CAGR = Gauge(
        "quant_backtest_cagr_pct", "Latest backtest CAGR percentage",
        ["strategy"],
        registry=_metrics_registry,
    )
    BACKTEST_MAX_DD = Gauge(
        "quant_backtest_max_drawdown_pct", "Latest backtest max drawdown",
        ["strategy"],
        registry=_metrics_registry,
    )
    BACKTEST_DSR = Gauge(
        "quant_backtest_dsr", "Latest backtest Deflated Sharpe Ratio",
        ["strategy"],
        registry=_metrics_registry,
    )
    BACKTEST_RUNS = Counter(
        "quant_backtest_runs_total", "Total backtest runs",
        ["strategy", "status"],
        registry=_metrics_registry,
    )


# ── 指标收集器 ──

class MetricsCollector:
    """定期收集系统/业务指标并更新到 Prometheus.

    间隔参数来自 config.yaml (prometheus.collector_interval / data_rows_interval):
    大表 (daily 9.5M 行) 全表 COUNT 需 1-2.4s — 低频 + MAX(rowid) 索引替代, 避免 WAL 压力 (模板 5/8).
    """

    def __init__(self, interval_sec: int = None):
        self.interval = interval_sec if interval_sec is not None \
            else _require_cfg("prometheus.collector_interval", default=30)
        self._rows_interval = _require_cfg("prometheus.data_rows_interval", default=600)
        self._rows_tick = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        _log.info("MetricsCollector started")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)

    def _run(self):
        while not self._stop.is_set():
            try:
                self._collect_system_metrics()
                self._collect_business_metrics()
            except Exception as e:
                _log.error(f"Metrics collection error: {e}")
            self._rows_tick += 1
            self._stop.wait(self.interval)

    def _collect_system_metrics(self):
        """收集系统级指标."""
        # CPU
        QuantMetrics.CPU_USAGE.set(psutil.cpu_percent(interval=0.1))

        # Memory
        mem = psutil.virtual_memory()
        QuantMetrics.MEMORY_USAGE.set(mem.used)

        # Disk
        for part in psutil.disk_partitions():
            try:
                usage = psutil.disk_usage(part.mountpoint)
                QuantMetrics.DISK_USAGE.labels(mountpoint=part.mountpoint).set(usage.used)
            except Exception:
                pass

        # DB Connections
        for db_name in ["market", "trades", "metrics", "backtest"]:
            try:
                from quant.config.paths import MARKET_DB, TRADE_DB, METRICS_DB, BACKTEST_DB
                db_map = {"market": MARKET_DB, "trades": TRADE_DB, "metrics": METRICS_DB, "backtest": BACKTEST_DB}
                conn = sqlite3.connect(db_map[db_name])
                # SQLite 不直接暴露连接数, 这里用简化逻辑
                QuantMetrics.DB_CONNECTIONS.labels(database=db_name).set(1)
            except Exception:
                pass

    def _collect_business_metrics(self):
        """收集业务指标 (从 DB 读取最新值)."""
        conn = None
        try:
            conn = sqlite3.connect(MARKET_DB, timeout=10)
            # Data freshness
            row = conn.execute("SELECT MAX(date) FROM daily").fetchone()
            if row and row[0]:
                from datetime import datetime
                latest = datetime.strptime(row[0], "%Y-%m-%d")
                hours = (datetime.now() - latest).total_seconds() / 3600
                QuantMetrics.DATA_FRESHNESS.labels(source="tushare", table="daily").set(hours)

            # Data rows — 低频 (data_rows_interval): 大表 COUNT 全扫 1-2.4s, 且日表 rowid 单调可用
            if self._rows_tick % max(1, self._rows_interval // self.interval) == 0:
                self._collect_data_rows(conn)
        except sqlite3.Error as e:
            _log.warning(f"business metrics (market): {e}")
        finally:
            if conn is not None:
                conn.close()
        self._collect_trade_metrics()
        self._collect_backtest_metrics()

    def _collect_data_rows(self, conn):
        """表行数 (模板5: 大表用 MAX(rowid) 索引, 小表 COUNT)."""
        heavy = {"daily", "daily_valuation"}
        for table in ["daily", "stocks", "daily_valuation", "financial_income",
                      "financial_balance", "financial_cashflow"]:
            try:
                if table in heavy:
                    row = conn.execute(f"SELECT MAX(rowid) FROM {table}").fetchone()
                else:
                    row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                if row and row[0]:
                    QuantMetrics.DATA_ROWS.labels(table=table).set(row[0])
            except sqlite3.Error as e:
                _log.warning(f"data_rows[{table}]: {e}")

    def _collect_trade_metrics(self):
        """账户指标 — trades.db daily_equity 最新一行 (交易唯一真相源)."""
        conn = None
        try:
            from quant.config.paths import TRADE_DB
            conn = sqlite3.connect(TRADE_DB, timeout=10)
            row = conn.execute(
                "SELECT strategy, cash, position_value, total_equity, drawdown_pct "
                "FROM daily_equity ORDER BY date DESC, rowid DESC LIMIT 1").fetchone()
            if row:
                strategy, cash, pos_val, equity, dd = row
                QuantMetrics.TOTAL_EQUITY.labels(strategy=strategy).set(equity or 0.0)
                QuantMetrics.CASH_BALANCE.labels(strategy=strategy).set(cash or 0.0)
                QuantMetrics.POSITION_VALUE.labels(strategy=strategy, symbol="*").set(pos_val or 0.0)
                QuantMetrics.DRAWDOWN.labels(strategy=strategy).set(dd or 0.0)
        except sqlite3.Error as e:
            _log.warning(f"trade metrics: {e}")
        finally:
            if conn is not None:
                conn.close()

    def _collect_backtest_metrics(self):
        """回测指标 — backtest_runs 最近一次成功 run (单真值, 避免 run_id 高基数)."""
        conn = None
        try:
            from quant.config.paths import BACKTEST_DB
            conn = sqlite3.connect(BACKTEST_DB, timeout=10)
            row = conn.execute(
                "SELECT strategy, sharpe, cagr_pct, max_dd_pct, dsr FROM backtest_runs "
                "WHERE sharpe IS NOT NULL ORDER BY rowid DESC LIMIT 1").fetchone()
            if row:
                strategy, sharpe, cagr, mdd, dsr = row
                QuantMetrics.BACKTEST_SHARPE.labels(strategy=strategy).set(sharpe or 0.0)
                QuantMetrics.BACKTEST_CAGR.labels(strategy=strategy).set(cagr or 0.0)
                QuantMetrics.BACKTEST_MAX_DD.labels(strategy=strategy).set(mdd or 0.0)
                QuantMetrics.BACKTEST_DSR.labels(strategy=strategy).set(dsr or 0.0)
        except sqlite3.Error as e:
            _log.warning(f"backtest metrics: {e}")
        finally:
            if conn is not None:
                conn.close()


# ── Prometheus Push Gateway 集成 ──

class PrometheusPusher:
    """推送指标到 Pushgateway (适用于短生命周期任务/批处理)."""

    def __init__(self, gateway: str = None, job_name: str = "quant"):
        self.gateway = _require_cfg("prometheus.pushgateway", default="") if gateway is None else gateway
        self.job_name = job_name
        self._registry = _metrics_registry

    def push(self, grouping_key: Dict[str, str] = None):
        if not self.gateway:
            _log.debug("Pushgateway not configured, skipping push")
            return
        try:
            push_to_gateway(self.gateway, job=self.job_name, registry=self._registry, grouping_key=grouping_key)
            _log.debug(f"Pushed metrics to {self.gateway}")
        except Exception as e:
            _log.warning(f"Push to gateway failed: {e}")

    def push_add(self, grouping_key: Dict[str, str] = None):
        """增量推送 (Counter 累加)."""
        if not self.gateway:
            return
        try:
            pushadd_to_gateway(self.gateway, job=self.job_name, registry=self._registry, grouping_key=grouping_key)
        except Exception as e:
            _log.warning(f"Pushadd to gateway failed: {e}")

    def delete(self, grouping_key: Dict[str, str] = None):
        if not self.gateway:
            return
        try:
            delete_from_gateway(self.gateway, job=self.job_name, grouping_key=grouping_key)
        except Exception as e:
            _log.warning(f"Delete from gateway failed: {e}")


# ── Grafana Dashboard JSON 生成 ──

_DS_REF = {"type": "prometheus", "uid": "PBFA97CFB590B2093"}


def _panel_timeseries(title: str, expr: str, legend: str, x: int, y: int, w: int, h: int) -> dict:
    """Grafana 9+ timeseries 面板 (Grafana 13 已移除旧 graph 面板)."""
    return {
        "title": title,
        "type": "timeseries",
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "datasource": _DS_REF,
        "targets": [{"expr": expr, "legendFormat": legend}],
        "fieldConfig": {"defaults": {}, "overrides": []},
    }


def _panel_stat(title: str, expr: str, legend: str, x: int, y: int, w: int, h: int) -> dict:
    """stat 面板 (KPI 数值)."""
    return {
        "title": title,
        "type": "stat",
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "datasource": _DS_REF,
        "targets": [{"expr": expr, "legendFormat": legend}],
        "fieldConfig": {"defaults": {}, "overrides": []},
    }


def _dashboard_base(uid: str, title: str, tags: list[str], panels: list[dict]) -> dict:
    """provisioning 兼容 dashboard 本体 (schemaVersion 39 = Grafana 11+)."""
    return {
        "id": None,
        "uid": uid,
        "title": title,
        "tags": tags,
        "timezone": "browser",
        "schemaVersion": 39,
        "version": 1,
        "panels": panels,
        "time": {"from": "now-1d", "to": "now"},
        "refresh": "30s",
        "templating": {"list": []},
        "annotations": {"list": []},
    }


class GrafanaDashboardBuilder:
    """生成 Grafana Dashboard JSON (Grafana 9+ 面板类型, 可直接 provisioning)."""

    @staticmethod
    def build_overview_dashboard() -> dict[str, Any]:
        """生成总览仪表盘."""
        return _dashboard_base(
            uid="quant-overview",
            title="Quant Trading Overview",
            tags=["quant", "trading", "overview"],
            panels=[
                _panel_timeseries("Total Equity", "quant_total_equity", "{{strategy}}", 0, 0, 12, 8),
                _panel_timeseries("Cash Balance", "quant_cash_balance", "{{strategy}}", 12, 0, 6, 8),
                _panel_timeseries("Position Value", "quant_position_value", "{{strategy}}/{{symbol}}", 18, 0, 6, 8),
                _panel_timeseries("Drawdown %", "quant_drawdown_pct", "{{strategy}}", 0, 8, 12, 6),
                _panel_stat("Sharpe (latest backtest)", "quant_backtest_sharpe", "{{strategy}}", 12, 8, 6, 6),
                _panel_stat("Max Drawdown %", "quant_max_drawdown_pct", "{{strategy}}", 18, 8, 6, 6),
                _panel_stat("CAGR % (latest backtest)", "quant_backtest_cagr_pct", "{{strategy}}", 0, 14, 6, 5),
                _panel_stat("DSR (latest backtest)", "quant_backtest_dsr", "{{strategy}}", 6, 14, 6, 5),
                _panel_stat("Active Positions", "count(quant_position_value > 0)", "", 12, 14, 6, 5),
                _panel_stat("Data Freshness (h)", "quant_data_freshness_hours", "{{source}}/{{table}}", 18, 14, 6, 5),
            ],
        )

    @staticmethod
    def build_factor_dashboard() -> dict[str, Any]:
        """因子分析仪表盘."""
        return _dashboard_base(
            uid="quant-factors",
            title="Factor Analysis",
            tags=["quant", "factors"],
            panels=[
                _panel_timeseries("Factor IC", "quant_factor_ic", "{{factor_name}}/{{scope}}", 0, 0, 12, 8),
                _panel_timeseries("Factor ICIR", "quant_factor_icir", "{{factor_name}}/{{scope}}", 12, 0, 12, 8),
                _panel_timeseries("Factor Rank", "quant_factor_rank", "{{factor_name}}", 0, 8, 12, 8),
                _panel_timeseries("Alpha Score", "quant_alpha_score", "{{symbol}}", 12, 8, 12, 8),
            ],
        )

    @staticmethod
    def build_risk_dashboard() -> dict[str, Any]:
        """风控仪表盘."""
        return _dashboard_base(
            uid="quant-risk",
            title="Risk Management",
            tags=["quant", "risk"],
            panels=[
                _panel_timeseries("VaR 95%", "quant_var_95", "{{strategy}}", 0, 0, 8, 8),
                _panel_timeseries("Max Drawdown", "quant_max_drawdown_pct", "{{strategy}}", 8, 0, 8, 8),
                _panel_timeseries("Leverage", "quant_leverage", "{{strategy}}", 16, 0, 8, 8),
                _panel_timeseries("Turnover Ratio", "quant_turnover_ratio", "{{strategy}}", 0, 8, 8, 8),
                _panel_timeseries("Concentration", "quant_concentration", "{{strategy}}/{{symbol}}", 8, 8, 8, 8),
                _panel_timeseries("VaR 99%", "quant_var_99", "{{strategy}}", 16, 8, 8, 8),
                _panel_timeseries("CPU %", "quant_cpu_usage_percent", "", 0, 16, 8, 6),
                _panel_timeseries("Memory (bytes)", "quant_memory_usage_bytes", "", 8, 16, 8, 6),
                _panel_timeseries("DB Connections", "quant_db_connections_active", "{{database}}", 16, 16, 8, 6),
            ],
        )

    @classmethod
    def export_all(cls, output_dir: str = "grafana_dashboards"):
        """导出所有仪表盘到文件 (provisioning 格式, Grafana 自动加载)."""
        import os
        os.makedirs(output_dir, exist_ok=True)

        dashboards = {
            "quant-overview.json": cls.build_overview_dashboard(),
            "quant-factors.json": cls.build_factor_dashboard(),
            "quant-risk.json": cls.build_risk_dashboard(),
        }
        for name, content in dashboards.items():
            path = os.path.join(output_dir, name)
            with open(path, "w") as f:
                json.dump(content, f, indent=2, ensure_ascii=False)
            _log.info(f"Grafana dashboard exported: {path}")
        return dashboards


# ── Alertmanager 告警规则生成 ──

class AlertRuleBuilder:
    """生成 Alertmanager 告警规则 (YAML)."""

    @staticmethod
    def build_rules() -> str:
        return """
groups:
- name: quant.rules
  interval: 30s
  rules:
  # 交易异常
  - alert: QuantTradeFailure
    expr: increase(quant_trades_total{status="failed"}[5m]) > 0
    for: 1m
    labels:
      severity: critical
      component: trading
    annotations:
      summary: "Trade execution failed"
      description: "{{ $labels.strategy }} {{ $labels.symbol }} trade failed: {{ $labels.status }}"

  # 回撤告警
  - alert: QuantDrawdownCritical
    expr: quant_drawdown_pct > 20
    for: 5m
    labels:
      severity: critical
      component: risk
    annotations:
      summary: "Drawdown critical: {{ $value }}%"
      description: "Strategy {{ $labels.strategy }} drawdown exceeded 20%"

  - alert: QuantDrawdownWarning
    expr: quant_drawdown_pct > 10
    for: 5m
    labels:
      severity: warning
      component: risk
    annotations:
      summary: "Drawdown warning: {{ $value }}%"
      description: "Strategy {{ $labels.strategy }} drawdown exceeded 10%"

  # 因子失效
  - alert: QuantFactorDegraded
    expr: quant_factor_icir < 0.1
    for: 1h
    labels:
      severity: warning
      component: alpha
    annotations:
      summary: "Factor {{ $labels.factor_name }} ICIR degraded"
      description: "ICIR dropped below 0.1: {{ $value }}"

  # 数据延迟
  - alert: QuantDataStale
    expr: quant_data_freshness_hours > 24
    for: 10m
    labels:
      severity: critical
      component: data
    annotations:
      summary: "Data source stale"
      description: "{{ $labels.source }} {{ $labels.table }} stale for {{ $value }}h"

  # 系统资源
  - alert: QuantHighCPU
    expr: quant_cpu_usage_percent > 90
    for: 5m
    labels:
      severity: warning
      component: system
    annotations:
      summary: "High CPU usage: {{ $value }}%"

  - alert: QuantHighMemory
    expr: quant_memory_usage_bytes / (1024*1024*1024) > 8
    for: 5m
    labels:
      severity: warning
      component: system
    annotations:
      summary: "High memory usage: {{ $value }}GB"

  # 回测失败
  - alert: QuantBacktestFailed
    expr: increase(quant_backtest_runs_total{status="error"}[1h]) > 0
    for: 1m
    labels:
      severity: critical
      component: backtest
    annotations:
      summary: "Backtest run failed"
      description: "Strategy {{ $labels.strategy }} run {{ $labels.run_id }} failed"
"""


# ── 统一监控入口 ──

class MonitoringPlatform:
    """统一监控平台 — Prometheus + Grafana + Alertmanager."""

    def __init__(self):
        self.registry = _metrics_registry
        self.collector = MetricsCollector(interval_sec=30)
        self.pusher = PrometheusPusher(job_name="quant")

    def start(self):
        """启动监控平台."""
        self.collector.start()
        # 定期推送到 Pushgateway — 仅在配置了 gateway 时启动 (模板5: 未配置则无空转线程)
        if self.pusher.gateway:
            def _push_loop():
                while True:
                    time.sleep(60)
                    self.pusher.push()
            threading.Thread(target=_push_loop, daemon=True).start()
            _log.info(f"MonitoringPlatform started (pushgateway={self.pusher.gateway})")
        else:
            _log.info("MonitoringPlatform started (pull-only, pushgateway 未配置)")

    def stop(self):
        self.collector.stop()
        _log.info("MonitoringPlatform stopped")

    def get_metrics(self) -> bytes:
        """获取 Prometheus 格式指标 (用于 /metrics 端点)."""
        return generate_latest(self.registry)

    def export_grafana_dashboards(self, output_dir: str = "grafana_dashboards"):
        GrafanaDashboardBuilder.export_all(output_dir)

    def export_alert_rules(self, output_file: str = "alert_rules.yml"):
        with open(output_file, "w") as f:
            f.write(AlertRuleBuilder.build_rules())
        _log.info(f"Alert rules exported to {output_file}")


# 全局单例
_monitoring: Optional[MonitoringPlatform] = None


def get_monitoring() -> MonitoringPlatform:
    global _monitoring
    if _monitoring is None:
        _monitoring = MonitoringPlatform()
    return _monitoring


def init_monitoring(app=None) -> MonitoringPlatform:
    """初始化监控平台 (可集成 Flask/FastAPI)."""
    global _monitoring
    if _monitoring is None:
        _monitoring = MonitoringPlatform()
        _monitoring.start()
    return _monitoring


if __name__ == "__main__":
    # 测试导出仪表盘
    platform = MonitoringPlatform()
    platform.export_grafana_dashboards()
    platform.export_alert_rules()
    print("Monitoring platform initialized. Dashboards & alerts exported.")