"""调度器任务定义 — 任务元数据单一真相源.

执行状态不在此维护, 统一从 market.db → task_runs 表读取.
"""

import threading
from datetime import datetime, timedelta

_lock = threading.Lock()
_tasks: dict[str, dict] = {}

_GROUPS = {
    "signals": "盘前", "execute": "盘中", "monitor": "盘中",
    "daily_data": "盘后", "attribution": "盘后", "weekly_eval": "研究",
}


def register(name: str, schedule: str, label: str = "", desc: str = "",
             has_multiprocess: bool = False):
    """注册任务定义 (不含运行时状态, 状态从 task_runs 表读取)."""
    with _lock:
        _tasks[name] = {
            "name": name, "label": label or name, "desc": desc,
            "group": _GROUPS.get(name, "其他"),
            "schedule": schedule, "has_multiprocess": has_multiprocess,
        }


def all_tasks() -> list[dict]:
    """返回所有任务定义, next_run 动态计算."""
    with _lock:
        result = []
        for t in _tasks.values():
            entry = dict(t)
            entry["next_run"] = _next_scheduled_time(t["schedule"])
            result.append(entry)
        return result


def register_all():
    """注册所有调度任务 — 单一真相源."""
    register("signals",      "08:30",       label="信号生成",
             desc="计算所有 using 因子，生成 Alpha 信号与目标持仓", has_multiprocess=True)
    register("execute",      "09:30",       label="交易执行",
             desc="读取信号、获取行情、执行调仓订单", has_multiprocess=True)
    register("monitor",      "09:35-14:55", label="盘中风控",
             desc="每30s轮询止损/止盈/熔断，触发后立即卖出")
    register("daily_data",   "19:00",       label="数据拉取",
             desc="拉取当日 A 股日线行情，更新 market.db")
    register("attribution",  "20:00",       label="盘后归因",
             desc="Brinson 归因 + IC 衰减 + OOS 验证 + 因子归因")
    register("weekly_eval",  "周六 06:00",   label="因子评估",
             desc="评估管线五阶段：回测诊断因子 → 正式认证 → 状态变更")


def _next_scheduled_time(schedule: str) -> str:
    """计算下次执行时间 (北京时间)."""
    _WEEKDAY_MAP = {"周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6}
    for wd_name, wd_num in _WEEKDAY_MAP.items():
        if schedule.startswith(wd_name):
            time_part = schedule[len(wd_name):].strip()
            hh, mm = (int(x) for x in time_part.split(":"))
            now = datetime.now()
            target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            days_ahead = (wd_num - now.weekday()) % 7
            if days_ahead == 0 and target <= now:
                days_ahead = 7
            return (target + timedelta(days=days_ahead)).strftime("%Y-%m-%d %H:%M")
    # 简单 HH:MM 格式
    parts = schedule.split("-")
    time_str = parts[-1].strip() if "-" in schedule else schedule.strip()
    hh, mm = (int(x) for x in time_str.split(":"))
    now = datetime.now()
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    # 跳过周末
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return target.strftime("%Y-%m-%d %H:%M")


def _reset():
    """仅测试使用."""
    with _lock:
        _tasks.clear()
