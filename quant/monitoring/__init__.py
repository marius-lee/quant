"""可观测性增强 — Dagster UI + Prometheus/Grafana + 告警 + 追踪.

设计目标:
  1. Dagster 原生集成: Asset 级指标、Run 级追踪、资源消耗
  2. Prometheus 导出: 统一 /metrics 端点, 含业务/系统/数据质量指标
  3. Grafana 仪表盘即代码: JSON 定义, 版本控制, 自动部署
  4. 告警规则: 基于指标阈值 + 多渠道通知 (Webhook/Telegram/Email)
  5. 分布式追踪: OpenTelemetry 集成, trace_id 贯穿全链路
"""

from .dagster_integration import DagsterMetricsExporter, get_dagster_exporter
from .prometheus_exporter import PrometheusExporter, get_prometheus_exporter, setup_metrics_endpoint
from .grafana_dashboards import GrafanaDashboardManager, get_dashboard_manager
from .alerting import AlertManager, AlertRule, AlertSeverity, get_alert_manager
from .tracing import TracingManager, get_tracing_manager, trace_context

__all__ = [
    "DagsterMetricsExporter",
    "get_dagster_exporter",
    "PrometheusExporter",
    "get_prometheus_exporter",
    "setup_metrics_endpoint",
    "GrafanaDashboardManager",
    "get_dashboard_manager",
    "AlertManager",
    "AlertRule",
    "AlertSeverity",
    "get_alert_manager",
    "TracingManager",
    "get_tracing_manager",
    "trace_context",
]