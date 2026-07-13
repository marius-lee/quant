"""告警通知通道 — Telegram / 企业微信 / 本地日志。

依赖: requests (已安装), 无需额外 pip.
配置: config.yaml monitor.telegram_bot_token / telegram_chat_id / wechat_webhook
无配置时退化到本地 logger.warning (不阻塞).

Usage:
    from monitor.notify import send_alert
    send_alert({"level": "CRITICAL", "title": "Drawdown 10%", "body": "..."})
"""

import os, requests
from utils.logger import get_logger
from config.constants import _require_cfg

_log = get_logger("monitor.notify")

# ── 配置读取 ──
def _telegram_token():
    return _require_cfg("monitor.telegram_bot_token")

def _telegram_chat_id():
    return _require_cfg("monitor.telegram_chat_id")

def _wechat_webhook():
    return _require_cfg("monitor.wechat_webhook")


def _telegram_send(text: str) -> bool:
    """通过 Telegram Bot API 发送消息."""
    token = _telegram_token()
    chat_id = _telegram_chat_id()
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }, timeout=10)
    return resp.status_code == 200


def _wechat_send(text: str) -> bool:
    """通过企业微信 Webhook 发送消息."""
    webhook = _wechat_webhook()
    if not webhook:
        return False
    resp = requests.post(webhook, json={
        "msgtype": "text",
        "text": {"content": text}
    }, timeout=10)
    return resp.status_code == 200


def send_alert(alert: dict) -> bool:
    """发送告警到所有已配置通道.

    Args:
        alert: {level: "CRITICAL"/"WARNING", title: str, body: str}

    Returns:
        True if at least one channel succeeded.
    """
    level = alert.get("level", "WARNING")
    title = alert.get("title", "量化告警")
    body = alert.get("body", "")

    text = f"*[{level}] {title}*\n{body}"

    sent = False
    if _telegram_send(text):
        sent = True
        _log.info(f"Telegram alert sent: {title}")
    if _wechat_send(text):
        sent = True
        _log.info(f"WeChat alert sent: {title}")

    if not sent:
        # 兜底: 至少打印到日志
        _log.warning(f"[ALERT:{level}] {title}: {body}")

    return sent


def send_drawdown_alert(current_drawdown: float) -> bool:
    """便捷函数: 发送回撤告警."""
    level = "CRITICAL" if abs(current_drawdown) > 0.1 else "WARNING"
    return send_alert({
        "level": level,
        "title": f"回撤告警: {current_drawdown:.1%}",
        "body": f"当前最大回撤达到 {current_drawdown:.2%}",
    })


def send_error_alert(component: str, error: str) -> bool:
    """便捷函数: 发送组件错误告警."""
    return send_alert({
        "level": "CRITICAL",
        "title": f"组件错误: {component}",
        "body": error,
    })
