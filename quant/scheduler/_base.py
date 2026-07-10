"""调度器基类 — 提供定时循环逻辑 + 状态上报。"""
import time as _time
from datetime import datetime, time
from config.constants import _require_cfg
from utils.logger import get_logger


def _timed_loop(name: str, target_time: time, run_fn, skip_deadline: time = None,
                has_multiprocess: bool = False):
    """通用每日定时循环: 每个交易日 target_time 执行一次 run_fn。

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


def _weekly_loop(name: str, target_weekday: int, target_time: time, run_fn):
    """通用每周定时循环: 每周 target_weekday (0=Mon, 6=Sun) 的 target_time 执行 run_fn。

    不检查交易日 — 因子评估不需要在交易日执行，周末跑即可。
    """
    from quant.scheduler.status import register, update

    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    schedule_str = f"{weekday_names[target_weekday]} {target_time.strftime('%H:%M')}"
    register(name, schedule_str, has_multiprocess=False)

    log = get_logger(f"quant.scheduler.{name}")
    log.info(f"scheduler started — weekly on {schedule_str}")

    today = None
    ran = False

    while True:
        now = datetime.now()
        current_day = now.strftime("%Y-%m-%d")

        if current_day != today:
            today = current_day
            ran = False
            update(name, status="idle", last_error=None)

        # 只检查星期几，不检查交易日
        if now.weekday() != target_weekday:
            days_until = (target_weekday - now.weekday()) % 7
            if days_until == 0:
                days_until = 7
            update(name, status=f"sleep (距下次 {days_until}d)")
            _time.sleep(_require_cfg("quant.scheduler.poll_interval"))
            continue

        hhmm = time(now.hour, now.minute)

        if not ran and hhmm >= target_time:
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
