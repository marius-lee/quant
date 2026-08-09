"""调度器 — 单一编排器 + manifest 任务清单 (v428 重构 + v433 Runner 拆分).

v428: 废弃"每任务独立 _loop 线程"时代架构 (signals/execute/monitor/attribution/
weekly 各自的 _timed_loop/_weekly_loop 全部删除, _base.py 移除).
全部调度由 orchestrator 单进程驱动:
  - 日线任务: manifest._DAYLINE (时间窗+依赖+超时) → 主循环决策执行
  - 周频评估: manifest._WEEKLY (周六 06:00-12:00) → orchestrator subprocess
  - monitor: 长驻窗口任务 (09:30-15:00) → orchestrator 守护线程

v433 重构: 拆分为三大 Runner (InlineRunner/MonitorRunner/SubprocessRunner) + 共用决策函数.

启动入口: restart.sh → start_all() (兼容旧); 幂等, 双进程防御由 PID 锁 + grace dedup.
"""
from quant.utils.logger import get_logger
from quant.scheduler.runners import (
    run_inline_tasks as _run_inline_tasks,
    run_monitor as _run_monitor,
    run_evening_chain as _run_evening_chain,
    run_weekly_eval as _run_weekly_eval,
)

_log = get_logger(__name__)


def start_all():
    """启动编排器 (v428: 单任务源 — weekly 由 manifest 窗口并入 orchestrator).

    历史: v417 之前 start_all 另起 start_weekly 线程 — manifest 化后
    weekly_eval 触发条件统一收编进 orchestrator, 删除重复路径 (双触发之源).
    """
    # 使用模块级属性以便测试 monkeypatch
    _start_orch()


def start_scheduler():
    start_all()


# 向后兼容导出
from quant.scheduler.orchestrator import _run as _run_orch

# 兼容旧导入
def start():
    start_all()


# 向后兼容: 供测试使用 (如 test_weekly_sat_trigger_v416.py)
_start_orch = _run_orch

# 导出供外部使用
__all__ = ["start_all", "start_scheduler", "start", "_run_orch"]