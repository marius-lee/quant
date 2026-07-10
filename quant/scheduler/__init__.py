"""调度器 — 单线程编排器 + 独立周频因子评估。

日频: orchestrator 串行 signals(08:30) → execute(09:30) → monitor(09:35-14:55) → attribution(15:30)
周频: weekly 独立线程 (周六 06:00 force_refresh_cache)
"""
import threading
from utils.logger import get_logger

_log = get_logger("quant.scheduler")


def start_orchestrator():
    from quant.scheduler.orchestrator import start as _start_orch
    _start_orch()
    _log.info("orchestrator launched (08:30→09:30→monitor→15:30)")


def start_weekly():
    from quant.scheduler.weekly import _run as _run_weekly, _loop as _weekly_loop
    t = threading.Thread(target=_weekly_loop, daemon=True, name="sch-weekly")
    t.start()
    _log.info("weekly factor eval scheduler launched (周六 06:00)")


def start_all():
    """启动编排器 + 周频因子评估 (2 线程)."""
    start_orchestrator()
    start_weekly()
    _log.info("all schedulers launched: 1 orchestrator + 1 weekly")


# 兼容旧 API
def start_scheduler():
    start_all()


# 保留旧接口供其他模块直接引用 (向后兼容)
def start_signals():
    from quant.scheduler.orchestrator import start as _start_orch
    _start_orch()

def start_execute():
    pass  # orchestrator handles this

def start_attribution():
    pass  # orchestrator handles this

def start_monitor():
    pass  # orchestrator handles this
