"""告警规则引擎 (模板9 T1) — scheduler 每次 pipeline 后评估.

通过 broker.update() 推送告警 → SSE → 前端顶部横幅.
"""

from datetime import datetime, timedelta
from quant.config.constants import _require_cfg


def check_alerts(state: dict, metrics_snap: dict) -> list[dict]:
    """评估所有告警规则, 返回触发的告警列表.

    Args:
        state: broker state (total_pnl, capital, last_pipeline_run, etc.)
        metrics_snap: metrics.snapshot() 的返回值

    Returns:
        [{"rule": "drawdown", "level": "warning", "msg": "..."}, ...]
    """
    alerts = []

    # ── Rule 1: 回撤告警 — peak-to-trough drawdown (2026-07-21 audit H3) ──
    # 旧逻辑 total_pnl/capital 是累计收益率, 非真正的回撤
    # 新逻辑从 daily_equity 表读取持久化的滚动最大回撤
    critical_pct = _require_cfg("monitor.alert.drawdown_critical")
    warning_pct = _require_cfg("monitor.alert.drawdown_warning")
    try:
        from quant.data.trade_repo import TradeRepo
        dd_pct = TradeRepo().get_max_drawdown()
        if dd_pct >= critical_pct * 100:
            alerts.append({
                "rule": "drawdown",
                "level": "critical",
                "msg": f"最大回撤 {dd_pct:.1f}% (peak-to-trough, 60日窗口)"
            })
        elif dd_pct >= warning_pct * 100:
            alerts.append({
                "rule": "drawdown",
                "level": "warning",
                "msg": f"最大回撤 {dd_pct:.1f}% (peak-to-trough, 60日窗口)"
            })
    except Exception:
        pass  # daily_equity 表不存在时跳过

    # ── Rule 2: 数据同步滞后 (最近日线 > 2 天前) ──
    # 直接查 daily 表 MAX(date), 而非依赖从未写入的 last_daily_sync (2026-07-21 audit M7)
    try:
        from quant.data.store import DataStore
        ds = DataStore()
        row = ds._connect().execute("SELECT MAX(date) FROM daily").fetchone()
        if row and row[0]:
            last_date = row[0]
            from datetime import timedelta
            last_dt = datetime.strptime(last_date, "%Y-%m-%d") if isinstance(last_date, str) else last_date
            if isinstance(last_dt, str): last_dt = datetime.strptime(last_dt, "%Y-%m-%d")
            if datetime.now() - last_dt > timedelta(days=2):
                alerts.append({
                    "rule": "stale_data",
                    "level": "warning",
                    "msg": f"最近日线: {last_date} (超过2天未更新)"
                })
        ds.close()
    except Exception:
        pass  # daily 表不可用时跳过

    # ── Rule 3: 连续 pipeline 失败 ──
    err_count = int(metrics_snap.get("counters", {}).get("pipeline.errors", 0))
    if err_count >= 3:
        alerts.append({
            "rule": "pipeline_errors",
            "level": "critical",
            "msg": f"pipeline 累计失败 {err_count} 次"
        })

    return alerts


# 上次推送的告警集 (去重, 避免重复推送相同告警)
_LAST_ALERT_KEYS: set[str] = set()


def push_alerts(alerts: list[dict]):
    """推送告警到 SSE (通过 broker). 相同告警不重复推送."""
    from web.state_broker import broker

    current_keys = {a["rule"] for a in alerts} if alerts else set()
    global _LAST_ALERT_KEYS

    # 如果告警集没变化, 跳过
    if current_keys == _LAST_ALERT_KEYS:
        return
    _LAST_ALERT_KEYS = current_keys

    broker.update({"alerts": alerts})
