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
from quant.config.constants import _require_cfg
from quant.monitor.metrics import metrics as _m
from quant.utils.logger import get_logger

_log = get_logger(__name__)

def _run():
    """编排器主循环 — 单线程，按时间顺序串行执行日频任务。"""
    from quant.scheduler.status import register, update
    from quant.execution.calendar import is_trading_day

    # 注册所有日频任务 (供前端调度Tab展示)
    register("signals",    "08:30", has_multiprocess=True)
    register("execute",    "09:30", has_multiprocess=True)
    register("monitor",    "09:35-14:55", has_multiprocess=False)
    register("daily_data", "19:00")
    register("attribution","20:00", has_multiprocess=False)

    _log.info("orchestrator started — daily sequence: 08:30 signals → 09:30 execute → "
              "09:35-14:55 monitor → 15:30 attribution")

    POLL = _require_cfg("quant.scheduler.poll_interval")
    today = None
    done = {"signals": False, "execute": False, "daily_data": False, "attribution": False}
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
            done = {"signals": False, "execute": False, "daily_data": False, "attribution": False}
            _log.info(f"[{today}] new day, orchestrator ready")

        # ── 非交易日: 全部跳过 ──
        if not is_trading_day():
            for name in done:
                update(name, status="sleep (非交易日)")
            update("monitor", status="sleep (非交易日)")
            # ── 主动超时检测: 把僵尸 running 行标为 aborted ──
        _check_timeouts(today)

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
        # 4. 19:00 — 每日数据拉取
        # ═══════════════════════════════════════════
        if not done["daily_data"]:
            if hhmm >= time(19, 0):
                from quant.scheduler.daily_data import _run as _daily_data_run
                _run_task("daily_data", _daily_data_run, today)
                done["daily_data"] = True
            else:
                wait_m = (time(19, 0).hour * 60) - (hhmm.hour * 60 + hhmm.minute)
                update("daily_data", status=f"waiting ({wait_m}min)")

        # ═══════════════════════════════════════════
        # 5. 20:00 — 盘后归因 (依赖 daily_data 完成)
        # ═══════════════════════════════════════════
        if done["daily_data"] and not done["attribution"]:
            if hhmm >= time(20, 0):
                from quant.scheduler.attribution import _run as _attr_run
                _run_task("attribution", _attr_run, today)
                done["attribution"] = True
                if _monitor_thread and _monitor_thread.is_alive():
                    _monitor_stop.set()
                    _monitor_thread.join(timeout=5)
                    _monitor_thread = None
            else:
                wait_m = (time(20, 0).hour * 60) - (hhmm.hour * 60 + hhmm.minute)
                update("attribution", status=f"waiting ({wait_m}min)")

        # ── 主动超时检测: 把僵尸 running 行标为 aborted ──
        _check_timeouts(today)

        _time.sleep(POLL)


# ── 超时阈值 (秒) ──
_TIMEOUTS = {
    "signals": 900,       # 15 min (正常 ~5 min)
    "execute": 600,       # 10 min (正常 <1 min)
    "monitor": None,      # 持续运行, 不收市不超时 — 只在 14:55+ 检查
    "daily_data": 1800,   # 30 min (正常 ~5 min)
    "attribution": 900,   # 15 min (正常 ~3 min)
    "weekly_eval": 7200,  # 120 min (正常 ~30 min)
}

def _check_timeouts(today: str):
    """扫描 task_runs 中 status='running' 的行, 超时则标为 aborted."""
    import sqlite3
    from datetime import datetime
    from quant.config.paths import MARKET_DB
    try:
        conn = sqlite3.connect(MARKET_DB)
        conn.execute("PRAGMA busy_timeout=3000")
        rows = conn.execute(
            "SELECT id, task_name, started_at FROM task_runs "
            "WHERE date=? AND status='running' AND finished_at IS NULL",
            (today,)
        ).fetchall()
        if not rows:
            conn.close()
            return
        now = datetime.now()
        for rid, task_name, started_at in rows:
            if not started_at:
                continue
            dt = datetime.fromisoformat(started_at)
            elapsed = (now - dt).total_seconds()
            limit = _TIMEOUTS.get(task_name)
            # monitor: 只在收市后 (14:55+) 检查, 给 30 min 缓冲
            if task_name == "monitor":
                if now.hour >= 14 and now.minute >= 55:
                    limit = 1800  # 14:55 + 30 min = 15:25
                else:
                    continue  # 盘中不检查 monitor
            if limit is None:
                continue
            if elapsed > limit:
                conn.execute(
                    "UPDATE task_runs SET status='aborted', finished_at=?, "
                    "error='任务异常终止: 运行超时 (' || ? || 's)' WHERE id=?",
                    (now.isoformat(), int(elapsed), rid)
                )
                _log.warning(
                    f"[{today}] {task_name} running for {elapsed:.0f}s > {limit}s → aborted (zombie)"
                )
        conn.commit()
        conn.close()
    except Exception as e:
        _log.warning(f"[{today}] timeout check failed (non-fatal): {e}")


def start():
    """启动编排器 daemon 线程."""
    t = _thr.Thread(target=_run, daemon=True, name="orchestrator")
    t.start()
