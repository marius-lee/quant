"""Grafana 仪表盘即代码 — JSON 定义, 版本控制, 自动部署.

功能:
  - 预置仪表盘: 交易/风控/数据质量/系统/调度器
  - Grafana HTTP API 自动部署
  - 变量模板化 (策略/数据源/时间范围)
  - 面板标准化: 指标、图例、阈值、告警链接
"""

from __future__ import annotations
import json
import requests
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Any, Optional
from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg

logger = get_logger("monitoring.grafana")


@dataclass
class GrafanaDashboardManager:
    """Grafana 仪表盘管理器."""

    url: str = "http://localhost:3000"
    api_key: Optional[str] = None
    org_id: int = 1
    folder_id: int = 0
    _session: Optional[requests.Session] = None

    def __post_init__(self):
        self._session = requests.Session()
        if self.api_key:
            self._session.headers.update({"Authorization": f"Bearer {self.api_key}"})
        self._session.headers.update({"Content-Type": "application/json"})

    def deploy_dashboard(self, dashboard_json: Dict[str, Any], overwrite: bool = True) -> Dict[str, Any]:
        """部署仪表盘."""
        payload = {
            "dashboard": dashboard_json,
            "folderId": self.folder_id,
            "overwrite": overwrite,
            "message": f"Auto-deployed by quant monitoring at {datetime.now().isoformat()}",
        }
        resp = self._session.post(f"{self.url}/api/dashboards/db", json=payload)
        resp.raise_for_status()
        logger.info(f"Deployed dashboard: {dashboard_json.get('title', 'unknown')}")
        return resp.json()

    def delete_dashboard(self, uid: str):
        """删除仪表盘."""
        resp = self._session.delete(f"{self.url}/api/dashboards/uid/{uid}")
        resp.raise_for_status()
        logger.info(f"Deleted dashboard: {uid}")

    def get_dashboard(self, uid: str) -> Dict[str, Any]:
        """获取仪表盘."""
        resp = self._session.get(f"{self.url}/api/dashboards/uid/{uid}")
        resp.raise_for_status()
        return resp.json()

    def list_dashboards(self) -> List[Dict[str, Any]]:
        """列出仪表盘."""
        resp = self._session.get(f"{self.url}/api/search?type=dash-db")
        resp.raise_for_status()
        return resp.json()


# ═══════════════════════════════════════════════════════════════════
# 预置仪表盘定义
# ═══════════════════════════════════════════════════════════════════

def create_trading_dashboard() -> Dict[str, Any]:
    """交易仪表盘 — PnL、持仓、信号、成交."""
    return {
        "uid": "quant-trading",
        "title": "Quant Trading Overview",
        "tags": ["quant", "trading"],
        "timezone": "Asia/Shanghai",
        "refresh": "30s",
        "time": {"from": "now-1h", "to": "now"},
        "templating": {
            "list": [
                {"name": "strategy", "type": "query", "datasource": "Prometheus",
                 "query": "label_values(quant_trades_total, strategy)", "refresh": 1},
                {"name": "symbol", "type": "query", "datasource": "Prometheus",
                 "query": "label_values(quant_trades_total, symbol)", "refresh": 1},
            ]
        },
        "panels": [
            # Row 1: PnL Summary
            {"type": "row", "title": "PnL Summary", "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1}},
            {"type": "stat", "title": "Total PnL (Today)", "datasource": "Prometheus",
             "targets": [{"expr": "sum(increase(quant_trade_pnl_sum{strategy=~\"$strategy\"}[1d])) by (strategy)"}],
             "gridPos": {"x": 0, "y": 1, "w": 6, "h": 4}},
            {"type": "stat", "title": "Win Rate (Today)", "datasource": "Prometheus",
             "targets": [{"expr": "sum(increase(quant_trades_total{strategy=~\"$strategy\",status=\"success\"}[1d])) / sum(increase(quant_trades_total{strategy=~\"$strategy\"}[1d])) * 100"}],
             "gridPos": {"x": 6, "y": 1, "w": 6, "h": 4}},
            {"type": "stat", "title": "Total Volume (Today)", "datasource": "Prometheus",
             "targets": [{"expr": "sum(increase(quant_trade_volume_total{strategy=~\"$strategy\"}[1d]))"}],
             "gridPos": {"x": 12, "y": 1, "w": 6, "h": 4}},
            {"type": "stat", "title": "Active Positions", "datasource": "Prometheus",
             "targets": [{"expr": "quant_positions_total{strategy=~\"$strategy\"}"}],
             "gridPos": {"x": 18, "y": 1, "w": 6, "h": 4}},

            # Row 2: PnL Time Series
            {"type": "row", "title": "PnL Time Series", "gridPos": {"x": 0, "y": 5, "w": 24, "h": 1}},
            {"type": "timeseries", "title": "Cumulative PnL", "datasource": "Prometheus",
             "targets": [
                 {"expr": "quant_trade_pnl_sum{strategy=~\"$strategy\"}", "legendFormat": "{{symbol}}"},
             ],
             "gridPos": {"x": 0, "y": 6, "w": 12, "h": 8}},
            {"type": "timeseries", "title": "Trade PnL Distribution", "datasource": "Prometheus",
             "targets": [
                 {"expr": "quant_trade_pnl_bucket{strategy=~\"$strategy\"}", "legendFormat": "{{le}}"},
             ],
             "gridPos": {"x": 12, "y": 6, "w": 12, "h": 8}},

            # Row 3: Signals & Trades
            {"type": "row", "title": "Signals & Trades", "gridPos": {"x": 0, "y": 14, "w": 24, "h": 1}},
            {"type": "timeseries", "title": "Signals Generated", "datasource": "Prometheus",
             "targets": [
                 {"expr": "increase(quant_signals_generated_total{strategy=~\"$strategy\"}[5m])", "legendFormat": "{{signal_type}}"},
             ],
             "gridPos": {"x": 0, "y": 15, "w": 12, "h": 8}},
            {"type": "timeseries", "title": "Trades Executed", "datasource": "Prometheus",
             "targets": [
                 {"expr": "increase(quant_trades_total{strategy=~\"$strategy\"}[5m])", "legendFormat": "{{side}} {{status}}"},
             ],
             "gridPos": {"x": 12, "y": 15, "w": 12, "h": 8}},

            # Row 4: Position Concentration
            {"type": "row", "title": "Position Concentration", "gridPos": {"x": 0, "y": 23, "w": 24, "h": 1}},
            {"type": "table", "title": "Top Positions by Concentration", "datasource": "Prometheus",
             "targets": [
                 {"expr": "quant_position_concentration{strategy=~\"$strategy\"}", "format": "table"},
             ],
             "gridPos": {"x": 0, "y": 24, "w": 24, "h": 8}},
        ],
    }


def create_risk_dashboard() -> Dict[str, Any]:
    """风控仪表盘 — VaR、回撤、止损、熔断."""
    return {
        "uid": "quant-risk",
        "title": "Quant Risk Management",
        "tags": ["quant", "risk"],
        "timezone": "Asia/Shanghai",
        "refresh": "30s",
        "time": {"from": "now-6h", "to": "now"},
        "templating": {
            "list": [
                {"name": "strategy", "type": "query", "datasource": "Prometheus",
                 "query": "label_values(quant_var_95, strategy)", "refresh": 1},
            ]
        },
        "panels": [
            {"type": "row", "title": "Risk Summary", "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1}},
            {"type": "gauge", "title": "VaR 95%", "datasource": "Prometheus",
             "targets": [{"expr": "quant_var_95{strategy=~\"$strategy\"}"}],
             "fieldConfig": {"defaults": {"thresholds": {"mode": "absolute", "steps": [
                 {"color": "green", "value": None}, {"color": "yellow", "value": 0.02},
                 {"color": "red", "value": 0.05}]}}},
             "gridPos": {"x": 0, "y": 1, "w": 6, "h": 6}},
            {"type": "gauge", "title": "Max Drawdown %", "datasource": "Prometheus",
             "targets": [{"expr": "quant_max_drawdown{strategy=~\"$strategy\"}"}],
             "fieldConfig": {"defaults": {"thresholds": {"mode": "absolute", "steps": [
                 {"color": "green", "value": None}, {"color": "yellow", "value": 5},
                 {"color": "red", "value": 10}]}}},
             "gridPos": {"x": 6, "y": 1, "w": 6, "h": 6}},
            {"type": "stat", "title": "Circuit Breaker", "datasource": "Prometheus",
             "targets": [{"expr": "quant_circuit_breaker_active{strategy=~\"$strategy\"}"}],
             "fieldConfig": {"defaults": {"mappings": [{"type": "value", "options": {"0": "Normal", "1": "ACTIVE"}}]}},
             "gridPos": {"x": 12, "y": 1, "w": 6, "h": 6}},
            {"type": "stat", "title": "Stop Loss Today", "datasource": "Prometheus",
             "targets": [{"expr": "sum(increase(quant_stop_loss_triggered_total{strategy=~\"$strategy\"}[1d]))"}],
             "gridPos": {"x": 18, "y": 1, "w": 6, "h": 6}},

            {"type": "row", "title": "Risk Time Series", "gridPos": {"x": 0, "y": 7, "w": 24, "h": 1}},
            {"type": "timeseries", "title": "VaR 95% Trend", "datasource": "Prometheus",
             "targets": [{"expr": "quant_var_95{strategy=~\"$strategy\"}", "legendFormat": "{{strategy}}"}],
             "gridPos": {"x": 0, "y": 8, "w": 12, "h": 8}},
            {"type": "timeseries", "title": "Max Drawdown Trend", "datasource": "Prometheus",
             "targets": [{"expr": "quant_max_drawdown{strategy=~\"$strategy\"}", "legendFormat": "{{strategy}}"}],
             "gridPos": {"x": 12, "y": 8, "w": 12, "h": 8}},

            {"type": "row", "title": "Stop Loss Details", "gridPos": {"x": 0, "y": 16, "w": 24, "h": 1}},
            {"type": "timeseries", "title": "Stop Loss by Type", "datasource": "Prometheus",
             "targets": [
                 {"expr": "increase(quant_stop_loss_triggered_total{strategy=~\"$strategy\"}[1h])", "legendFormat": "{{type}} {{symbol}}"},
             ],
             "gridPos": {"x": 0, "y": 17, "w": 24, "h": 8}},
        ],
    }


def create_data_quality_dashboard() -> Dict[str, Any]:
    """数据质量仪表盘 — 新鲜度、同步、校验."""
    return {
        "uid": "quant-data-quality",
        "title": "Quant Data Quality",
        "tags": ["quant", "data-quality"],
        "timezone": "Asia/Shanghai",
        "refresh": "1m",
        "time": {"from": "now-6h", "to": "now"},
        "templating": {
            "list": [
                {"name": "table", "type": "query", "datasource": "Prometheus",
                 "query": "label_values(quant_data_freshness_seconds, table)", "refresh": 1},
                {"name": "source", "type": "query", "datasource": "Prometheus",
                 "query": "label_values(quant_data_freshness_seconds, source)", "refresh": 1},
            ]
        },
        "panels": [
            {"type": "row", "title": "Data Freshness", "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1}},
            {"type": "timeseries", "title": "Freshness by Table", "datasource": "Prometheus",
             "targets": [
                 {"expr": "quant_data_freshness_seconds{table=~\"$table\",source=~\"$source\"}", "legendFormat": "{{table}} {{source}}"},
             ],
             "gridPos": {"x": 0, "y": 1, "w": 12, "h": 8}},
            {"type": "table", "title": "Current Freshness (seconds)", "datasource": "Prometheus",
             "targets": [
                 {"expr": "quant_data_freshness_seconds", "format": "table"},
             ],
             "gridPos": {"x": 12, "y": 1, "w": 12, "h": 8}},

            {"type": "row", "title": "Sync Performance", "gridPos": {"x": 0, "y": 9, "w": 24, "h": 1}},
            {"type": "timeseries", "title": "Sync Duration", "datasource": "Prometheus",
             "targets": [
                 {"expr": "quant_data_sync_duration_seconds_bucket", "legendFormat": "{{table}} {{operation}} {{le}}"},
             ],
             "gridPos": {"x": 0, "y": 10, "w": 12, "h": 8}},
            {"type": "timeseries", "title": "Sync Errors", "datasource": "Prometheus",
             "targets": [
                 {"expr": "increase(quant_data_sync_errors_total[5m])", "legendFormat": "{{table}} {{error_type}}"},
             ],
             "gridPos": {"x": 12, "y": 10, "w": 12, "h": 8}},

            {"type": "row", "title": "Validation Failures", "gridPos": {"x": 0, "y": 18, "w": 24, "h": 1}},
            {"type": "timeseries", "title": "Validation Failures by Rule", "datasource": "Prometheus",
             "targets": [
                 {"expr": "increase(quant_data_validation_failures_total[5m])", "legendFormat": "{{table}} {{rule}} {{severity}}"},
             ],
             "gridPos": {"x": 0, "y": 19, "w": 24, "h": 8}},

            {"type": "row", "title": "Table Row Counts", "gridPos": {"x": 0, "y": 27, "w": 24, "h": 1}},
            {"type": "timeseries", "title": "Rows per Table", "datasource": "Prometheus",
             "targets": [
                 {"expr": "quant_data_rows_total", "legendFormat": "{{table}}"},
             ],
             "gridPos": {"x": 0, "y": 28, "w": 24, "h": 8}},
        ],
    }


def create_system_dashboard() -> Dict[str, Any]:
    """系统仪表盘 — HTTP、DB、缓存、调度器."""
    return {
        "uid": "quant-system",
        "title": "Quant System Health",
        "tags": ["quant", "system"],
        "timezone": "Asia/Shanghai",
        "refresh": "30s",
        "time": {"from": "now-1h", "to": "now"},
        "panels": [
            {"type": "row", "title": "HTTP Requests", "gridPos": {"x": 0, "y": 0, "w": 24, "h": 1}},
            {"type": "timeseries", "title": "Request Rate", "datasource": "Prometheus",
             "targets": [
                 {"expr": "rate(quant_http_requests_total[1m])", "legendFormat": "{{method}} {{endpoint}} {{status}}"},
             ],
             "gridPos": {"x": 0, "y": 1, "w": 12, "h": 8}},
            {"type": "timeseries", "title": "Request Latency (p95)", "datasource": "Prometheus",
             "targets": [
                 {"expr": "histogram_quantile(0.95, rate(quant_http_request_duration_seconds_bucket[5m]))", "legendFormat": "{{method}} {{endpoint}}"},
             ],
             "gridPos": {"x": 12, "y": 1, "w": 12, "h": 8}},

            {"type": "row", "title": "Database", "gridPos": {"x": 0, "y": 9, "w": 24, "h": 1}},
            {"type": "timeseries", "title": "DB Query Latency (p95)", "datasource": "Prometheus",
             "targets": [
                 {"expr": "histogram_quantile(0.95, rate(quant_db_query_duration_seconds_bucket[5m]))", "legendFormat": "{{query_type}} {{table}}"},
             ],
             "gridPos": {"x": 0, "y": 10, "w": 12, "h": 8}},
            {"type": "timeseries", "title": "Active Connections", "datasource": "Prometheus",
             "targets": [
                 {"expr": "quant_db_connections_active", "legendFormat": "{{database}}"},
             ],
             "gridPos": {"x": 12, "y": 10, "w": 12, "h": 8}},

            {"type": "row", "title": "Cache", "gridPos": {"x": 0, "y": 18, "w": 24, "h": 1}},
            {"type": "gauge", "title": "Cache Hit Ratio", "datasource": "Prometheus",
             "targets": [{"expr": "quant_cache_hit_ratio", "legendFormat": "{{cache_name}}"}],
             "fieldConfig": {"defaults": {"thresholds": {"mode": "absolute", "steps": [
                 {"color": "red", "value": None}, {"color": "yellow", "value": 0.8}, {"color": "green", "value": 0.95}]}}},
             "gridPos": {"x": 0, "y": 19, "w": 12, "h": 6}},
            {"type": "timeseries", "title": "Scheduler Queue Depth", "datasource": "Prometheus",
             "targets": [
                 {"expr": "quant_scheduler_queue_depth", "legendFormat": "{{queue_name}}"},
             ],
             "gridPos": {"x": 12, "y": 19, "w": 12, "h": 6}},

            {"type": "row", "title": "Scheduler Tasks", "gridPos": {"x": 0, "y": 25, "w": 24, "h": 1}},
            {"type": "timeseries", "title": "Task Duration (p95)", "datasource": "Prometheus",
             "targets": [
                 {"expr": "histogram_quantile(0.95, rate(quant_scheduler_task_duration_seconds_bucket[5m]))", "legendFormat": "{{task_name}} {{status}}"},
             ],
             "gridPos": {"x": 0, "y": 26, "w": 12, "h": 8}},
            {"type": "timeseries", "title": "Task Success Rate", "datasource": "Prometheus",
             "targets": [
                 {"expr": "rate(quant_scheduler_task_total{status=\"success\"}[5m]) / rate(quant_scheduler_task_total[5m]) * 100", "legendFormat": "{{task_name}}"},
             ],
             "gridPos": {"x": 12, "y": 26, "w": 12, "h": 8}},
        ],
    }


# ═══════════════════════════════════════════════════════════════════
# 部署所有仪表盘
# ═══════════════════════════════════════════════════════════════════

def deploy_all_dashboards(manager: GrafanaDashboardManager):
    """部署所有预置仪表盘."""
    dashboards = [
        create_trading_dashboard(),
        create_risk_dashboard(),
        create_data_quality_dashboard(),
        create_system_dashboard(),
    ]
    for db in dashboards:
        try:
            manager.deploy_dashboard(db)
            logger.info(f"Deployed: {db['title']}")
        except Exception as e:
            logger.error(f"Failed to deploy {db['title']}: {e}")


# 全局实例
_dashboard_manager: Optional[GrafanaDashboardManager] = None


def get_dashboard_manager() -> GrafanaDashboardManager:
    global _dashboard_manager
    if _dashboard_manager is None:
        _dashboard_manager = GrafanaDashboardManager(
            url=_require_cfg("monitoring.grafana.url", "http://localhost:3000"),
            api_key=_require_cfg("monitoring.grafana.api_key", None),
        )
    return _dashboard_manager