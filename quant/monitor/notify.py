"""告警通知通道 — macOS 系统通知 / Telegram / 企业微信 / 本地日志。

依赖: requests (已安装), 无需额外 pip. macOS 通知用 osascript (本机弹窗+提示音).
配置: config.yaml monitor.telegram_bot_token / telegram_chat_id / wechat_webhook
无配置时通道静默跳过 (不阻塞), 兜底 logger.warning 必达.

Usage:
    from monitor.notify import send_alert
    send_alert({"level": "CRITICAL", "title": "Drawdown 10%", "body": "..."})
"""

import os, requests
from quant.utils.logger import get_logger
from quant.config.constants import _require_cfg
from quant.config.loader import get as cfg

_log = get_logger("monitor.notify")

# ── 配置读取 ──
def _telegram_token():
    from quant.config.constants import _require_cfg
    return _require_cfg("monitor.telegram_bot_token")  # (2026-07-21 audit H4)

def _telegram_chat_id():
    return cfg("monitor.telegram_chat_id") or ""

def _wechat_webhook():
    return cfg("monitor.wechat_webhook") or ""

def _serverchan_sendkey():
    return cfg("monitor.serverchan_sendkey") or ""


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


def _serverchan_send(text: str, title: str = "量化告警") -> bool:
    """通过 Server酱³ (微信服务号推送) 发送消息 — v515, 个人微信可达.

    API: POST https://sctapi.ftqq.com/<SendKey>.send, 参数 title/desp.
    需要 SendKey 已配置 (monitor.serverchan_sendkey); 未配置静默跳过.
    国内直连, 微信服务号必达.
    """
    key = _serverchan_sendkey()
    if not key:
        return False
    try:
        resp = requests.post(
            f"https://sctapi.ftqq.com/{key}.send",
            data={"title": title, "desp": text},
            timeout=10)
        if resp.status_code != 200:
            _log.warning(f"serverchan HTTP {resp.status_code}: {resp.text[:200]}")
            return False
        return resp.json().get("code") == 0
    except Exception as _e:
        _log.warning(f"serverchan send failed: {_e}")
        return False


def _macos_notify(title: str, body: str) -> bool:
    """macOS 系统通知 + 提示音 (osascript, 本机桌面弹窗, v513).

    仅 darwin 生效; 弹窗 Retry/Report 按钮静默 (display notification 无按钮).
    """
    import platform, subprocess
    if platform.system() != "Darwin":
        return False
    try:
        body_esc = body.replace("\\", "\\\\").replace('"', '\\"')
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{body_esc}" with title "{title}" sound name "Glass"'],
            timeout=8, capture_output=True)
        return True
    except Exception:
        return False


def _macos_sound() -> bool:
    """macOS 提示音 (afplay 内置提示音, 即使弹窗被系统静音也可听, v513)."""
    import platform, subprocess
    if platform.system() != "Darwin":
        return False
    try:
        subprocess.Popen(
            ["afplay", "/System/Library/Sounds/Glass.aiff"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


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
    if _macos_notify(title, text):
        sent = True
        _log.info(f"macOS notification sent: {title}")
    if _serverchan_send(text, title):
        sent = True
        _log.info(f"ServerChan alert sent: {title}")
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
    from quant.config.constants import _require_cfg
    warning_pct = _require_cfg("monitor.alert.drawdown_warning")
    critical_pct = _require_cfg("monitor.alert.drawdown_critical")
    level = "CRITICAL" if abs(current_drawdown) > critical_pct else "WARNING"
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


def send_baostock_quota_alert(count: int, limit: int, pending: int = 0) -> bool:
    """baostock 日请求上限告警 (v513) — macOS 通知+提示音 + 可选 IM 通道.

    达软上限: gate 全局闸口拦截一切 baostock 请求, 需用户换热点续跑
    (IP 变化自动检测恢复, v528 起全局生效, 不再局限于行业 PIT 同步).
    """
    _macos_sound()   # 提示音独立于弹窗 (系统勿扰模式也发声)
    tail = f", 剩余 {pending} 只未同步" if pending else ""
    return send_alert({
        "level": "CRITICAL",
        "title": "⚠ baostock 今日请求已达上限",
        "body": (f"今日 {count}/{limit} 次, 全局闸口已拦截后续请求{tail}. "
                 f"请更换网络热点 (新公网 IP) 后重跑同步任务, 系统自动检测 "
                 f"IP 变化并清零计数续跑."),
    })
