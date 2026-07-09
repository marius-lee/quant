"""独立调度器 — 四个任务各自运行，互不依赖。

signals:     每日 08:30 盘前信号生成
execute:     每日 09:30 开盘执行
monitor:     每日 09:35-14:55 盘中风控
attribution: 每日 15:30 盘后归因
"""
import threading
from utils.logger import get_logger

_log = get_logger("quant.scheduler")


def start_signals():
    from quant.scheduler.signals import _run as _run_signals, _loop as _signals_loop
    t = threading.Thread(target=_signals_loop, daemon=True, name="sch-signals")
    t.start()
    _log.info("signals scheduler launched (08:30)")


def start_execute():
    from quant.scheduler.execute import _run as _run_execute, _loop as _execute_loop
    t = threading.Thread(target=_execute_loop, daemon=True, name="sch-execute")
    t.start()
    _log.info("execute scheduler launched (09:30)")


def start_attribution():
    from quant.scheduler.attribution import _run as _run_attribution, _loop as _attribution_loop
    t = threading.Thread(target=_attribution_loop, daemon=True, name="sch-attribution")
    t.start()
    _log.info("attribution scheduler launched (15:30)")

def start_monitor():
    from quant.scheduler.monitor import _run_continuous as _run_monitor, _loop as _monitor_loop
    t = threading.Thread(target=_monitor_loop, daemon=True, name="sch-monitor")
    t.start()
    _log.info("monitor scheduler launched (09:35-14:55)")


def start_all():
    """启动四个独立调度器."""
    start_signals()
    start_execute()
    start_monitor()
    start_attribution()
    _log.info("all 4 schedulers launched")


# 兼容旧 API
def start_scheduler():
    start_all()
