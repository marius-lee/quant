"""调度器状态注册表 — 线程安全."""
import threading, time
from datetime import datetime

_lock = threading.Lock()
_tasks: dict[str, dict] = {}


def register(name: str, schedule: str, has_multiprocess: bool = False):
    """注册一个调度任务。"""
    with _lock:
        _group = {"signals": "盘前", "execute": "盘中", "monitor": "盘中", "attribution": "盘后", "weekly_eval": "研究"}.get(name, "其他")
        _tasks[name] = {
            "name": name,
            "group": _group,
            "schedule": schedule,
            "has_multiprocess": has_multiprocess,
            "status": "idle",
            "last_run": None,
            "last_duration": None,
            "last_error": None,
            "next_run": _next_scheduled_time(schedule),
        }


def update(name: str, *, status: str = None, last_run: str = None,
           last_duration: float = None, last_error: str = None):
    """更新任务状态。"""
    with _lock:
        t = _tasks.get(name)
        if t is None:
            return
        if status is not None:
            t["status"] = status
        if last_run is not None:
            t["last_run"] = last_run
        if last_duration is not None:
            t["last_duration"] = round(last_duration, 1)
        if last_error is not None:
            t["last_error"] = last_error
        t["next_run"] = _next_scheduled_time(t["schedule"])


def all_tasks() -> list[dict]:
    """返回所有任务状态列表。"""
    with _lock:
        return [dict(t) for t in _tasks.values()]


def _next_scheduled_time(schedule: str) -> str:
    """计算下次执行时间 (北京时间)."""
    # 时间范围格式 "09:35-14:55" → 取起始时间 "09:35"
    if "-" in schedule:
        schedule = schedule.split("-")[0]
    h, m = map(int, schedule.split(":"))
    now = datetime.now()
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        from datetime import timedelta
        target += timedelta(days=1)
    return target.strftime("%Y-%m-%d %H:%M")
