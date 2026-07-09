"""调度器基类 — 提供定时循环逻辑。"""
import time as _time
from datetime import datetime, time
from utils.logger import get_logger


def _timed_loop(name: str, target_time: time, run_fn, skip_deadline: time = None):
    """通用定时循环: 每天 target_time 执行一次 run_fn。

    skip_deadline: 如果当前时间超过此时间，跳过当日 (例如 15:45 后不再执行归因).
    """
    log = get_logger(f"quant.scheduler.{name}")
    log.info(f"scheduler started — daily at {target_time.strftime('%H:%M')}")

    from execution.calendar import is_trading_day

    today = None
    ran = False

    while True:
        now = datetime.now()
        current_day = now.strftime("%Y-%m-%d")

        if current_day != today:
            today = current_day
            ran = False

        if not is_trading_day():
            _time.sleep(30)
            continue

        hhmm = time(now.hour, now.minute)

        if not ran:
            if skip_deadline and hhmm > skip_deadline:
                ran = True  # 已过期，标记跳过
                continue

            if hhmm >= target_time:
                try:
                    run_fn(today)
                except Exception as e:
                    log.error(f"failed: {e}")
                ran = True

        _time.sleep(30)
