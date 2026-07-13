"""日频任务编排器 — 单线程串行执行 signals → execute → monitor → attribution.

方案C: 消除 5 个独立 daemon 线程之间的竞态条件。
所有日频任务按时间顺序串行执行，确保后序任务读取前序任务的产出。

monitor 是连续循环 (09:35-14:55)，由编排器作为子线程启动和停止。
weekly 因子评估保持独立线程 (周六 06:00，不依赖交易日)。

状态映射: config.constants.STATUS_LABELS — 内部码→中文展示文本 (单一真相源)。

启动行为:
- 08:00 前启动: 等待到 08:30 开始
- 盘中介入 (如 10:16): 立即按顺序补跑已过期的任务 (signals → execute → monitor → ...)
- 盘后介入: 同上，但 monitor 跳过，直接到 attribution
"""
import time as _time, threading as _thr
from datetime import datetime, time
from config.constants import _require_cfg
from monitor.metrics import metrics as _m
from utils.logger import get_logger

_log = get_logger("quant.scheduler.orchestrator")

def _run():
    """编排器主循环 — 单线程，按时间顺序串行执行日频任务。"""
    from quant.scheduler.status import register, update
    from execution.calendar import is_trading_day

    # 注册所有日频任务 (供前端调度Tab展示)
    register("signals",    "08:30", has_multiprocess=True)
    register("execute",    "09:30", has_multiprocess=True)
    register("monitor",    "09:35-14:55", has_multiprocess=False)
    register("attribution","15:30", has_multiprocess=False)

    _log.info("orchestrator started — daily sequence: 08:30 signals → 09:30 execute → "
              "09:35-14:55 monitor → 15:30 attribution")

    POLL = _require_cfg("quant.scheduler.poll_interval")
    today = None
    done = {"signals": False, "execute": False, "attribution": False}
    _monitor_thread = None
    _monitor_stop = _thr.Event()

    def _monitor_daemon(current_day):
        """子线程: 盘中风控守护 (09:35-14:55 循环)."""
        from quant.scheduler.monitor import _run_continuous
        _log.info(f"[{current_day}] monitor daemon started (09:35-14:55)")
        while not _monitor_stop.is_set():
            now = datetime.now()
            hhmm = time(now.hour, now.minute)
            if time(9, 35) <= hhmm <= time(14, 55):
                update("monitor", status="running")
                _run_continuous(current_day)
            else:
                update("monitor", status="sleep (收市)")
            _monitor_stop.wait(timeout=POLL)
        _log.info(f"[{current_day}] monitor daemon stopped")
        update("monitor", status="idle")

    def _run_task(name, fn, task_today):
        """执行单个任务 → 更新 scheduler 状态。"""
        update(name, status="running")
        t0 = _time.time()
        fn(task_today)
        elapsed = _time.time() - t0
        update(name, status="idle", last_run=datetime.now().isoformat(),
               last_duration=elapsed, last_error=None)
        _log.info(f"[SCHEDULER] {task_today} | TASK={name} | STATUS=OK | elapsed={elapsed:.1f}s")
        _m.inc(f"scheduler.{name}.ok")

    while True:
        now = datetime.now()
        current_day = now.strftime("%Y-%m-%d")
        hhmm = time(now.hour, now.minute)

        # ── 新的一天: 重置 ──
        if current_day != today:
            if _monitor_thread and _monitor_thread.is_alive():
                _monitor_stop.set()
                _monitor_thread.join(timeout=5)
            _monitor_stop.clear()
            _monitor_thread = None

            today = current_day
            done = {"signals": False, "execute": False, "attribution": False}
            _log.info(f"[{today}] new day, orchestrator ready")

        # ── 非交易日: 全部跳过 ──
        if not is_trading_day():
            for name in done:
                update(name, status="sleep (非交易日)")
            update("monitor", status="sleep (非交易日)")
            _time.sleep(POLL)
            continue

        # ═══════════════════════════════════════════
        # 1. 08:30 — 信号生成
        # ═══════════════════════════════════════════
        if not done["signals"]:
            if hhmm >= time(8, 30):
                # 盘后补跑检查: 15:30后跳过signals（无执行窗口）
                if hhmm >= time(15, 30):
                    _log.info(f"[{today}] signals expired (past 15:30), skip")
                    update("signals", status="expired（已过执行窗口）")
                    done["signals"] = True
                else:
                    from quant.scheduler.signals import _run as _signals_run
                    _run_task("signals", _signals_run, today)
                    done["signals"] = True
            else:
                wait_m = (time(8, 30).hour * 60 + 30) - (hhmm.hour * 60 + hhmm.minute)
                update("signals", status=f"waiting ({wait_m}min)")

        # ═══════════════════════════════════════════
        # 2. 09:30 — 交易执行 (依赖 signals 完成)
        # ═══════════════════════════════════════════
        if done["signals"] and not done["execute"]:
            if hhmm >= time(9, 30):
                # 盘后补跑检查: 14:57后跳过execute（收盘了）
                if hhmm >= time(14, 57):
                    _log.info(f"[{today}] execute expired (past 14:57), skip")
                    update("execute", status="expired（已过执行窗口）")
                    done["execute"] = True
                else:
                    from quant.scheduler.execute import _run as _execute_run
                    _run_task("execute", _execute_run, today)
                    done["execute"] = True
            else:
                wait_m = (time(9, 30).hour * 60 + 30) - (hhmm.hour * 60 + hhmm.minute)
                update("execute", status=f"waiting ({wait_m}min)")

        # ═══════════════════════════════════════════
        # 3. 09:35-14:55 — 盘中风控 (子线程守护)
        # ═══════════════════════════════════════════
        if done["signals"]:
            if _monitor_thread is None and time(9, 35) <= hhmm <= time(14, 55):
                _monitor_stop.clear()
                _monitor_thread = _thr.Thread(
                    target=_monitor_daemon, args=(today,),
                    daemon=True, name="monitor-daemon"
                )
                _monitor_thread.start()
            elif _monitor_thread is not None and hhmm > time(14, 55):
                _monitor_stop.set()
                _monitor_thread.join(timeout=5)
                _monitor_thread = None
                update("monitor", status="idle")
            elif _monitor_thread is None and hhmm < time(9, 35):
                update("monitor", status=f"waiting")

        # ═══════════════════════════════════════════
        # 4. 15:30 — 盘后归因 (依赖 signals 完成)
        # ═══════════════════════════════════════════
        if done["signals"] and not done["attribution"]:
            if hhmm >= time(15, 30):
                from quant.scheduler.attribution import _run as _attr_run
                _run_task("attribution", _attr_run, today)
                done["attribution"] = True
                if _monitor_thread and _monitor_thread.is_alive():
                    _monitor_stop.set()
                    _monitor_thread.join(timeout=5)
                    _monitor_thread = None
            else:
                wait_m = (time(15, 30).hour * 60 + 30) - (hhmm.hour * 60 + hhmm.minute)
                update("attribution", status=f"waiting ({wait_m}min)")

        _time.sleep(POLL)


def start():
    """启动编排器 daemon 线程."""
    t = _thr.Thread(target=_run, daemon=True, name="orchestrator")
    t.start()
