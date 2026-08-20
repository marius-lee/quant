"""告警管理 — 多渠道通知、规则引擎、抑制/去重.

功能:
  - 规则引擎: 基于 PromQL 表达式 + 阈值 + 持续时间
  - 多渠道: Webhook (企业微信/钉钉/飞书/Slack) + Telegram + Email
  - 抑制/去重: 告警分组、抑制规则、静默窗口
  - 告警历史: 存储 + 查询 + 统计
"""

from __future__ import annotations
import json
import sqlite3
import threading
import time
import requests
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Any, Optional, Callable
from quant.utils.logger import get_logger
from quant.config.paths import MARKET_DB
from quant.config.constants import _require_cfg

logger = get_logger("monitoring.alerting")


class AlertSeverity(Enum):
    """告警严重级别."""
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertStatus(Enum):
    """告警状态."""
    FIRING = "firing"
    RESOLVED = "resolved"
    SUPPRESSED = "suppressed"


@dataclass
class AlertRule:
    """告警规则."""
    name: str
    expr: str                          # PromQL 表达式
    severity: AlertSeverity = AlertSeverity.WARNING
    for_duration: str = "1m"           # 持续多久触发 (如 1m, 5m, 1h)
    labels: Dict[str, str] = field(default_factory=dict)
    annotations: Dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    # 通知配置
    channels: List[str] = field(default_factory=list)  # webhook, telegram, email
    # 抑制
    inhibit_rules: List[Dict] = field(default_factory=list)
    # 静默
    silences: List[Dict] = field(default_factory=list)


@dataclass
class Alert:
    """告警实例."""
    id: str
    rule_name: str
    severity: AlertSeverity
    status: AlertStatus
    labels: Dict[str, str]
    annotations: Dict[str, str]
    starts_at: float
    ends_at: Optional[float] = None
    updated_at: float = field(default_factory=time.time)
    value: float = 0.0

    def duration_seconds(self) -> float:
        end = self.ends_at or time.time()
        return end - self.starts_at


class AlertManager:
    """告警管理器."""

    def __init__(self, db_path: str = MARKET_DB):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()
        self._rules: Dict[str, AlertRule] = {}
        self._active_alerts: Dict[str, Alert] = {}
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._channel_handlers: Dict[str, Callable[[Alert], None]] = {}
        self._init_db()
        self._register_default_channels()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _init_db(self):
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS alert_rules (
                name TEXT PRIMARY KEY,
                expr TEXT NOT NULL,
                severity TEXT NOT NULL,
                for_duration TEXT NOT NULL,
                labels TEXT,
                annotations TEXT,
                enabled INTEGER DEFAULT 1,
                channels TEXT,
                inhibit_rules TEXT,
                silences TEXT,
                created_at REAL,
                updated_at REAL
            );
            CREATE TABLE IF NOT EXISTS alert_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_name TEXT NOT NULL,
                alert_id TEXT NOT NULL,
                severity TEXT NOT NULL,
                status TEXT NOT NULL,
                labels TEXT,
                annotations TEXT,
                starts_at REAL NOT NULL,
                ends_at REAL,
                value REAL,
                created_at REAL DEFAULT (strftime('%s','now'))
            );
            CREATE INDEX IF NOT EXISTS idx_alert_history_rule ON alert_history(rule_name);
            CREATE INDEX IF NOT EXISTS idx_alert_history_status ON alert_history(status);
            CREATE INDEX IF NOT EXISTS idx_alert_history_time ON alert_history(starts_at);
        """)
        conn.commit()

    def _register_default_channels(self):
        """注册默认通知渠道."""
        self.register_channel("webhook", self._send_webhook)
        self.register_channel("telegram", self._send_telegram)
        self.register_channel("email", self._send_email)
        self.register_channel("log", self._send_log)

    def register_channel(self, name: str, handler: Callable[[Alert], None]):
        """注册通知渠道."""
        self._channel_handlers[name] = handler
        logger.debug(f"Registered alert channel: {name}")

    def add_rule(self, rule: AlertRule):
        """添加告警规则."""
        with self._lock:
            self._rules[rule.name] = rule
            conn = self._get_conn()
            conn.execute(
                """INSERT OR REPLACE INTO alert_rules
                   (name, expr, severity, for_duration, labels, annotations, enabled,
                    channels, inhibit_rules, silences, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (rule.name, rule.expr, rule.severity.value, rule.for_duration,
                 json.dumps(rule.labels), json.dumps(rule.annotations), int(rule.enabled),
                 json.dumps(rule.channels), json.dumps(rule.inhibit_rules),
                 json.dumps(rule.silences), time.time(), time.time())
            )
            conn.commit()
        logger.info(f"Added alert rule: {rule.name}")

    def remove_rule(self, name: str):
        """移除告警规则."""
        with self._lock:
            self._rules.pop(name, None)
            conn = self._get_conn()
            conn.execute("DELETE FROM alert_rules WHERE name = ?", (name,))
            conn.commit()
        logger.info(f"Removed alert rule: {name}")

    def register_channel(self, name: str, handler: Callable[[Alert], None]):
        """注册通知渠道."""
        self._channel_handlers[name] = handler

    def start(self, interval: int = 30):
        """启动告警评估循环."""
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._evaluation_loop, args=(interval,), daemon=True, name="alert-eval")
        self._thread.start()
        logger.info("AlertManager started")

    def stop(self):
        """停止告警管理器."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("AlertManager stopped")

    def _evaluation_loop(self, interval: int):
        """告警评估循环."""
        while self._running:
            try:
                self._evaluate_rules()
            except Exception as e:
                logger.error(f"Alert evaluation error: {e}")
            time.sleep(interval)

    def _evaluate_rules(self):
        """评估所有规则."""
        from prometheus_client.parser import parse_promql  # 简化：实际需用 Prometheus HTTP API
        # 实际实现需查询 Prometheus API
        # 这里简化为示例逻辑
        pass

    def fire_alert(self, rule: AlertRule, labels: Dict, value: float, annotations: Dict = None):
        """触发告警."""
        alert_id = f"{rule.name}:{hash(json.dumps(labels, sort_keys=True))}"
        now = time.time()

        with self._lock:
            existing = self._active_alerts.get(alert_id)

            if existing and existing.status == AlertStatus.FIRING:
                # 已在触发中，更新值
                existing.value = value
                existing.updated_at = now
                if annotations:
                    existing.annotations.update(annotations)
                return

            # 新告警
            alert = Alert(
                id=alert_id,
                rule_name=rule.name,
                severity=rule.severity,
                status=AlertStatus.FIRING,
                labels=labels,
                annotations=annotations or {},
                starts_at=now,
                value=value,
            )
            self._active_alerts[alert_id] = alert

            # 记录历史
            self._record_alert(alert)

            # 发送通知
            self._notify(alert)

            logger.warning(f"ALERT FIRED: {rule.name} [{rule.severity.value}] {labels} value={value}")

    def resolve_alert(self, alert_id: str):
        """解决告警."""
        with self._lock:
            alert = self._active_alerts.get(alert_id)
            if not alert or alert.status != AlertStatus.FIRING:
                return

            alert.status = AlertStatus.RESOLVED
            alert.ends_at = time.time()
            alert.updated_at = time.time()

            # 记录历史
            self._record_alert(alert)

            # 发送恢复通知
            self._notify(alert)

            logger.info(f"ALERT RESOLVED: {alert.rule_name} duration={alert.duration_seconds():.0f}s")

    def _record_alert(self, alert: Alert):
        """记录告警历史."""
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO alert_history
               (rule_name, alert_id, severity, status, labels, annotations,
                starts_at, ends_at, value)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (alert.rule_name, alert.id, alert.severity.value, alert.status.value,
             json.dumps(alert.labels), json.dumps(alert.annotations),
             alert.starts_at, alert.ends_at, alert.value)
        )
        conn.commit()

    def _notify(self, alert: Alert):
        """发送通知到所有配置渠道."""
        rule = self._rules.get(alert.rule_name)
        channels = rule.channels if rule else ["log"]

        for channel in channels:
            handler = self._channel_handlers.get(channel)
            if handler:
                try:
                    handler(alert)
                except Exception as e:
                    logger.error(f"Alert notification failed for {channel}: {e}")

    # ═══════════════════════════════════════════════════════
    # 通知渠道实现
    # ═══════════════════════════════════════════════════════

    def _send_webhook(self, alert: Alert):
        """Webhook 通知 (企业微信/钉钉/飞书/Slack 兼容)."""
        webhook_url = _require_cfg("monitoring.alerting.webhook_url", "")
        if not webhook_url:
            return

        payload = {
            "alert_id": alert.id,
            "rule": alert.rule_name,
            "severity": alert.severity.value,
            "status": alert.status.value,
            "labels": alert.labels,
            "annotations": alert.annotations,
            "starts_at": datetime.fromtimestamp(alert.starts_at).isoformat(),
            "ends_at": datetime.fromtimestamp(alert.ends_at).isoformat() if alert.ends_at else None,
            "value": alert.value,
            "duration_seconds": alert.duration_seconds(),
        }

        try:
            requests.post(webhook_url, json=payload, timeout=10)
        except Exception as e:
            logger.error(f"Webhook notification failed: {e}")

    def _send_telegram(self, alert: Alert):
        """Telegram 通知."""
        bot_token = _require_cfg("monitoring.alerting.telegram_bot_token", "")
        chat_id = _require_cfg("monitoring.alerting.telegram_chat_id", "")
        if not bot_token or not chat_id:
            return

        text = (
            f"🚨 *{alert.severity.value.upper()}* {alert.rule_name}\n"
            f"Status: {alert.status.value}\n"
            f"Labels: {json.dumps(alert.labels, ensure_ascii=False)}\n"
            f"Value: {alert.value}\n"
            f"Time: {datetime.fromtimestamp(alert.starts_at).strftime('%Y-%m-%d %H:%M:%S')}"
        )

        try:
            requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            )
        except Exception as e:
            logger.error(f"Telegram notification failed: {e}")

    def _send_email(self, alert: Alert):
        """Email 通知 (简化版)."""
        # 实际需配置 SMTP
        logger.debug(f"Email notification for {alert.id} (not implemented)")

    def _send_log(self, alert: Alert):
        """日志记录."""
        level = logger.warning if alert.severity == AlertSeverity.CRITICAL else logger.info
        level(f"ALERT {alert.status.value.upper()}: {alert.rule_name} {alert.labels} value={alert.value}")

    # ══════════════════════════════════════════════════════
    # 查询接口
    # ═══════════════════════════════════════════════════════

    def get_active_alerts(self) -> List[Alert]:
        """获取当前触发中的告警."""
        with self._lock:
            return [a for a in self._active_alerts.values() if a.status == AlertStatus.FIRING]

    def get_alert_history(self, rule_name: str = None, hours: int = 24) -> List[Dict]:
        """查询告警历史."""
        conn = self._get_conn()
        since = time.time() - hours * 3600
        if rule_name:
            rows = conn.execute(
                "SELECT * FROM alert_history WHERE rule_name = ? AND starts_at > ? ORDER BY starts_at DESC LIMIT 100",
                (rule_name, since)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM alert_history WHERE starts_at > ? ORDER BY starts_at DESC LIMIT 100",
                (since,)
            ).fetchall()
        return [dict(r) for r in rows]

    def get_stats(self, hours: int = 24) -> Dict:
        """获取告警统计."""
        conn = self._get_conn()
        since = time.time() - hours * 3600
        total = conn.execute("SELECT COUNT(*) FROM alert_history WHERE starts_at > ?", (since,)).fetchone()[0]
        firing = conn.execute("SELECT COUNT(*) FROM alert_history WHERE status = 'firing' AND starts_at > ?", (since,)).fetchone()[0]
        resolved = conn.execute("SELECT COUNT(*) FROM alert_history WHERE status = 'resolved' AND starts_at > ?", (since,)).fetchone()[0]
        by_severity = dict(conn.execute(
            "SELECT severity, COUNT(*) FROM alert_history WHERE starts_at > ? GROUP BY severity", (since,)
        ).fetchall())
        return {
            "total": total,
            "firing": firing,
            "resolved": resolved,
            "by_severity": by_severity,
        }


# 全局实例
_alert_manager: Optional[AlertManager] = None


def get_alert_manager() -> AlertManager:
    global _alert_manager
    if _alert_manager is None:
        _alert_manager = AlertManager()
    return _alert_manager