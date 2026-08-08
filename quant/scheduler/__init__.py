"""调度器 — 单一编排器 + manifest 任务清单 (v428 重构).

v428: 废弃"每任务独立 _loop 线程"时代架构 (signals/execute/monitor/attribution/
weekly 各自的 _timed_loop/_weekly_loop 全部删除, _base.py 移除).
全部调度由 orchestrator 单进程驱动:
  - 日线任务: manifest._DAYLINE (时间窗+依赖+超时) → 主循环决策执行
  - 周频评估: manifest._WEEKLY (周六 06:00-12:00) → orchestrator subprocess
  - monitor: 长驻窗口任务 (09:30-15:00) → orchestrator 守护线程

启动入口: restart.sh → start_all() (兼容旧); 幂等, 双进程防御由 PID 锁 + grace dedup.
"""
from quant.utils.logger import get_logger
from quant.scheduler.orchestrator import start as _start_orch, _run as _run_orch

_log = get_logger(__name__)


def start_all():
    """启动编排器 (v428: 单任务源 — weekly 由 manifest 窗口并入 orchestrator).

    历史: v417 之前 start_all 另起 start_weekly 线程 — manifest 化后
    weekly_eval 触发条件统一收编进 orchestrator, 删除重复路径 (双触发之源).
    """
    _start_orch()
    _log.info("all schedulers launched (orchestrator only, manifest-driven)")


def start_scheduler():
    start_all()


# 兼容旧 API (无操作 — 全部并入 orchestrator)
def start_signals():
    pass  # orchestrator handles this

def start_execute():
    pass  # orchestrator handles this

def start_attribution():
    pass  # orchestrator handles this

def start_monitor():
    pass  # orchestrator handles this