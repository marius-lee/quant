"""调度器基类 — 提供定时循环逻辑 + 状态上报。"""
import time as _time
from datetime import datetime, time
from config.constants import _require_cfg
from utils.logger import get_logger


def _timed_loop(name: str, target_time: time, run_fn, skip_deadline: time = None,
                has_multiprocess: bool = False):
    """通用定时循环: 每天 target_time 执行一次 run_fn。

    skip_deadline: 如果当前时间超过此时间，跳过当日 (例如 15:45 后不再执行归因).
    has_multiprocess: 标记任务是否触发多进程 (用于界面告警).
    """
    from quant.scheduler.status import register, update

    schedule_str = target_time.strftime("%H:%M")
    register(name, schedule_str, has_multiprocess=has_multiprocess)

    log = get_logger(f"quant.scheduler.{name}")
    log.info(f"scheduler started — daily at {schedule_str}")

    from execution.calendar import is_trading_day

    today = None
    ran = False

    while True:
        now = datetime.now()
        current_day = now.strftime("%Y-%m-%d")

        if current_day != today:
            today = current_day
            ran = False
            update(name, status="idle", last_error=None)

        if not is_trading_day():
            update(name, status="sleep (非交易日)")
            _time.sleep(_require_cfg("quant.scheduler.poll_interval"))
            continue

        hhmm = time(now.hour, now.minute)

        if not ran:
            if skip_deadline and hhmm > skip_deadline:
                ran = True
                update(name, status="skipped (已过期)")
                continue

            if hhmm >= target_time:
                update(name, status="running")
                t0 = _time.time()
                try:
                    run_fn(today)
                    elapsed = _time.time() - t0
                    update(name, status="idle", last_run=now.isoformat(),
                           last_duration=elapsed, last_error=None)
                except Exception as e:
                    elapsed = _time.time() - t0
                    update(name, status="error", last_run=now.isoformat(),
                           last_duration=elapsed, last_error=str(e))
                ran = True
            else:
                wait_min = (target_time.hour * 60 + target_time.minute) - (hhmm.hour * 60 + hhmm.minute)
                update(name, status=f"waiting ({wait_min}min)")

        _time.sleep(_require_cfg("quant.scheduler.poll_interval"))
