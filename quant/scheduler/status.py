"""调度器任务定义 — 任务元数据单一真相源.

执行状态不在此维护, 统一从 market.db → task_runs 表读取.
"""

import threading
from datetime import datetime, timedelta

_lock = threading.Lock()
_tasks: dict[str, dict] = {}

_GROUPS = {
    "signals": "盘前", "execute": "盘中", "monitor": "盘中",
    "reconcile": "盘后", "daily_data": "盘后", "attribution": "盘后",
    "factor_cache": "盘后", "weekly_eval": "研究",
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
    register("monitor",      "09:35-11:30,13:00-14:55", label="盘中风控",
             desc="每30s轮询止损/止盈/熔断，触发后立即卖出")
    register("reconcile",    "15:05",       label="日终对账",
             desc="OMS 对账闭环: 持仓/现金/订单三账核对, break 超阈值告警")
    register("daily_data",   "19:00",       label="数据拉取",
             desc="拉取当日 A 股日线行情，更新 market.db")
    register("factor_cache", "daily_data完成后", label="因子物化",
             desc="增量更新 factor_cache，用当日行情计算回测因子值")
    register("attribution",  "factor_cache完成后", label="盘后归因",
             desc="Brinson 归因 + IC 衰减 + OOS 验证 + 因子归因")
    register("factor_curation", "周六 06:00", label="因子策展",
             desc="[weekly_eval Step 0] 检查内置库+用户提交, 编译→IC评估→注册新因子")
    register("eval_phase1", "周六 06:00 (因子策展后)", label="评估-数据准备",
             desc="[weekly_eval Step 1] 准备回测数据, 数据健康检查")
    register("eval_phase2", "周六 06:00 (Phase1后)", label="评估-单因子检验",
             desc="[weekly_eval Step 2] IC/|t|/ICIR/half-life 四维过滤")
    register("eval_phase3", "周六 06:00 (Phase2后)", label="评估-CPCV检验",
             desc="[weekly_eval Step 3] Purged CV + PBO 过拟合检测")
    register("eval_phase4", "周六 06:00 (Phase3后)", label="评估-成本验证",
             desc="[weekly_eval Step 4] 交易成本扣除后 Sharpe 验证")
    register("eval_phase5", "周六 06:00 (Phase4后)", label="评估-状态同步",
             desc="[weekly_eval Step 5] 综合裁决 → factor_registry 状态更新")
    register("weekly_eval",  "周六 06:00", label="因子评估(总)",
             desc="全自动五阶段评估: 策展→数据→IC→CPCV→成本→状态同步")
    register("lgb_train",    "周一/周四 factor_cache完成后", label="模型训练",
             desc="LightGBM 模型重训 (仅周一/周四)")


def _next_scheduled_time(schedule: str) -> str:
    """计算下次执行时间 (北京时间). 依赖型任务返回空串."""
    _WEEKDAY_MAP = {"周一": 0, "周二": 1, "周三": 2, "周四": 3, "周五": 4, "周六": 5, "周日": 6}
    # 依赖型任务 (无固定时间, 由上游触发)
    if "完成后" in schedule:
        return ""
    for wd_name, wd_num in _WEEKDAY_MAP.items():
        if schedule.startswith(wd_name):
            time_part = schedule[len(wd_name):].strip()
            if "(" in time_part:
                time_part = time_part.split("(")[0].strip()
            if not time_part:
                time_part = "00:00"  # 无时间 → 默认当天 00:00
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
    # strip 括号注释 + 依赖检查
    if "(" in time_str:
        time_str = time_str.split("(")[0].strip()
    if "完成后" in time_str:
        return ""
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
