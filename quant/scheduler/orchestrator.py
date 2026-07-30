"""日频任务编排器 — 单线程串行执行 signals → execute → monitor → reconcile.
晚间链 (daily_data→factor_cache→attribution→lgb_train) 由 orchestrator
在 19:00 通过 subprocess 触发, 非阻塞轮询, 失败自动重试 (test-v288).

monitor 是连续循环 (09:35-11:30,13:00-14:55, 午休跳过)，由编排器作为子线程启动和停止。

状态: task_runs (market.db) 为单一调度真相源。每 30s 轮询一次，
已完成任务不重跑，失败任务不自动重试（需人工排查 bug 后手动触发）。
"""
import os, time as _time, threading as _thr, sqlite3, subprocess
from datetime import datetime, time
from quant.config.constants import _require_cfg
from quant.monitor.metrics import metrics as _m
from quant.utils.logger import get_logger
from quant.config.paths import MARKET_DB

_log = get_logger(__name__)


def _get_today_status(today: str) -> dict:
    """查询 task_runs 中今天每个任务的最新状态。

    返回: {"signals": "ok", "execute": "failed", ...}
    无该任务记录则 key 不存在。
    """
    conn = sqlite3.connect(MARKET_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={_require_cfg('data.sqlite.busy_timeout')}")
    rows = conn.execute(
        "SELECT task_name, status FROM task_runs WHERE date=? ORDER BY id DESC",
        (today,)
    ).fetchall()
    conn.close()
    status = {}
    for row in rows:
        if row[0] not in status:
            status[row[0]] = row[1]
    return status


def _get_today_aborted(today: str) -> dict:
    """查询今日各任务 aborted 次数 (B-23: 重试风暴抑制)."""
    conn = sqlite3.connect(MARKET_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={_require_cfg('data.sqlite.busy_timeout')}")
    rows = conn.execute(
        "SELECT task_name, COUNT(*) FROM task_runs WHERE date=? AND status='aborted' GROUP BY task_name",
        (today,)
    ).fetchall()
    conn.close()
    return dict(rows)


def _run():
    """编排器主循环 — 单线程，按时间顺序串行执行日频任务。"""
    from quant.scheduler.status import register_all
    from quant.execution.calendar import is_trading_day

    register_all()

    # 启动时清理今天剩下的僵尸 running 行 (上次进程被 kill 残留)
    _cleanup_zombie_tasks()

    _log.info("orchestrator started — daily sequence: 08:30 signals → 09:30 execute → "
              "09:35-11:30,13:00-14:55 monitor → 15:05 reconcile "
              "(晚间链由 cron 19:00 触发)")

    POLL = _require_cfg("quant.scheduler.poll_interval")
    today = None
    _monitor_thread = None
    _monitor_stop = _thr.Event()

    # ── 晚间链 subprocess 状态 (test-v288) ──
    _evening_proc = None     # subprocess.Popen handle
    _evening_retries = 0     # 失败重试计数
    _evening_done = False    # 今天已完成/放弃

    def _monitor_daemon(current_day):
        try:
            from quant.scheduler.monitor import _run_continuous
            _log.info(f"[{current_day}] monitor daemon started (09:35-11:30,13:00-14:55)")
            _run_continuous(current_day)
            _log.info(f"[{current_day}] monitor daemon stopped")
        except Exception as _e:
            _log.exception(f"[{current_day}] monitor daemon crashed: {_e}")

    def _run_task(name, fn, task_today):
        t0 = _time.time()
        try:
            fn(task_today)
            elapsed = _time.time() - t0
            _log.info(f"[SCHEDULER] {task_today} | TASK={name} | STATUS=OK | elapsed={elapsed:.1f}s")
            _m.inc(f"scheduler.{name}.ok")
        except Exception as e:
            elapsed = _time.time() - t0
            _log.error(f"[SCHEDULER] {task_today} | TASK={name} | STATUS=FAILED | elapsed={elapsed:.1f}s | error={e}")
            # _tk_finish("failed") handled by task's own finally block

    while True:
        now = datetime.now()
        current_day = now.strftime("%Y-%m-%d")
        hhmm = time(now.hour, now.minute)

        # ── 新的一天: 清理 monitor 线程 + 重置晚间状态 ──
        if current_day != today:
            if _monitor_thread and _monitor_thread.is_alive():
                _monitor_stop.set()
                _monitor_thread.join(timeout=5)
            _monitor_stop.clear()
            _monitor_thread = None
            today = current_day
            _evening_proc = None
            _evening_retries = 0
            _evening_done = False
            _log.info(f"[{today}] new day, orchestrator ready")

        # ── 非交易日 ──
        if not is_trading_day():
            _check_timeouts(today)
            _time.sleep(POLL)
            continue

        # ── 读取 DB 权威状态 ──
        status = _get_today_status(today)
        aborted = _get_today_aborted(today)

        def _retry_ok(name: str) -> bool:
            """B-23: aborted 重试次数未超限才允许再次触发."""
            return aborted.get(name, 0) < _MAX_TASK_RETRIES

        # ═══════════════════════════════════════════
        # 1. 08:30-15:30 — 信号生成
        # ═══════════════════════════════════════════
        s = status.get("signals")
        if s not in ("ok", "failed") and _retry_ok("signals"):
            if time(8, 30) <= hhmm < time(15, 30):
                from quant.scheduler.signals import _run as _signals_run
                _run_task("signals", _signals_run, today)

        # ═══════════════════════════════════════════
        # 2. 09:30-14:57 — 交易执行 (依赖 signals 尝试过)
        # ═══════════════════════════════════════════
        if "signals" in status:  # signals 已尝试（不管成败）
            s = status.get("execute")
            if s not in ("ok", "failed") and _retry_ok("execute"):
                if time(9, 30) <= hhmm < time(14, 57):
                    from quant.scheduler.execute import _run as _execute_run
                    _run_task("execute", _execute_run, today)

        # ═══════════════════════════════════════════
        # 3. 09:35-11:30,13:00-14:55 — 盘中风控 (daemon 线程)
        # ═══════════════════════════════════════════
        in_monitor_window = time(9, 30) <= hhmm <= time(14, 55)
        monitor_state = status.get("monitor")
        monitor_done = monitor_state in ("ok", "failed")

        if in_monitor_window and not monitor_done:
            if _monitor_thread is None or not _monitor_thread.is_alive():
                _monitor_stop.clear()
                _monitor_thread = _thr.Thread(
                    target=_monitor_daemon, args=(today,),
                    daemon=True, name="monitor-daemon"
                )
                _monitor_thread.start()
        elif not in_monitor_window and _monitor_thread is not None:
            _monitor_stop.set()
            _monitor_thread.join(timeout=5)
            _monitor_thread = None

        # ═══════════════════════════════════════════
        # 4. 15:05-15:30 — OMS 日终对账 (monitor 收盘后)
        # ═══════════════════════════════════════════
        s = status.get("reconcile")
        if s not in ("ok", "failed") and _retry_ok("reconcile"):
            if time(15, 5) <= hhmm < time(15, 30):
                from quant.scheduler.reconcile import _run as _recon_run
                _run_task("reconcile", _recon_run, today)

        # ═══════════════════════════════════════════
        # 5. 19:00+ — 晚间链 subprocess (test-v288)
        # 非阻塞 Popen, 每 30s poll, 失败重试, 不 import sklearn/scipy
        # ═══════════════════════════════════════════
        if hhmm >= time(19, 0) and not _evening_done:
            if _evening_proc is None:
                s = status.get("evening_chain")
                if s == "ok":
                    _log.info(f"[{today}] evening_chain already ok, skip")
                    _evening_done = True
                else:
                    _log.info(f"[{today}] 19:00 — spawning evening chain subprocess "
                              f"(retry={_evening_retries}/{_MAX_TASK_RETRIES})")
                    _evening_proc = subprocess.Popen(
                        [".venv/bin/python3", "-c",
                         "from quant.utils.excepthook import setup; setup();"
                         "from quant.scheduler.evening import _run;"
                         f"_run('{today}')"],
                        cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                        env={**os.environ, "PYTHONPATH": "."},
                    )
            else:
                ret = _evening_proc.poll()
                if ret is not None:
                    _evening_proc = None
                    if ret == 0:
                        _log.info(f"[{today}] evening chain subprocess exited OK")
                        _evening_done = True
                    else:
                        _evening_retries += 1
                        if _evening_retries < _MAX_TASK_RETRIES:
                            _log.warning(f"[{today}] evening chain failed (rc={ret}), "
                                         f"retry {_evening_retries}/{_MAX_TASK_RETRIES}")
                        else:
                            _log.error(f"[{today}] evening chain exhausted retries, giving up")
                            _evening_done = True

        # ── 主动超时检测 ──
        _check_timeouts(today)

        _time.sleep(POLL)


# ── 超时阈值 (秒) — test-v287: 晚间任务移出, 只保留白天任务.
_TIMEOUTS = {
    "signals": 1800,
    "execute": 1800,
    "monitor": None,
    "reconcile": 600,
    "evening_chain": 14400,  # test-v288: evening.py 引用, subprocess 超时
}

# B-23 fix: 同一任务当日最多重试次数 (aborted 后 orchestrator 会重新触发,
# 无上限导致 2026-07-23 factor_cache 一夜重跑 4 次的重试风暴)
_MAX_TASK_RETRIES = 2

def _cleanup_zombie_tasks():
    """启动时清理今天残留的 running 行 — 仅当 OS 进程已死才标 aborted (test-v281: PID 检测)."""
    try:
        import os as _os
        today = datetime.now().strftime("%Y-%m-%d")
        conn = sqlite3.connect(MARKET_DB)
        conn.execute("PRAGMA journal_mode=WAL")
        rows = conn.execute(
            "SELECT id, pid FROM task_runs WHERE date=? AND status='running'", (today,)
        ).fetchall()
        cleaned = 0
        for rid, pid in rows:
            if pid is None:
                # 旧记录无 pid → 保守: 不处理, 等超时兜底
                continue
            # 检查 OS 进程是否存活
            try:
                _os.kill(pid, 0)  # 信号0: 只检查不杀
            except (OSError, ProcessLookupError):
                # 进程不存在 → 真僵尸, 标 aborted
                conn.execute(
                    "UPDATE task_runs SET status='aborted', finished_at=datetime('now','localtime'), "
                    "error='进程已死 (PID ' || ? || ' 不存在)' WHERE id=?", (pid, rid))
                cleaned += 1
            # else: 进程存活 → 保留 running, 不处理
        conn.commit()
        conn.close()
        if cleaned:
            _log.info(f"orchestrator startup: cleaned {cleaned} dead-pid zombie rows for {today}")
    except Exception as _e:
        _log.warning(f"startup zombie check failed (non-fatal): {_e}")

def _check_timeouts(today: str):
    """扫描 task_runs 中 status='running' 的行, 超时则标为 aborted."""
    try:
        conn = sqlite3.connect(MARKET_DB)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_require_cfg('data.sqlite.busy_timeout')}")
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
            if task_name == "monitor":
                if now.hour >= 14 and now.minute >= 55:
                    limit = 1800
                else:
                    continue
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


def _pid_path():
    import tempfile
    return os.path.join(tempfile.gettempdir(), "quant-orchestrator.pid")

def _run_safe():
    """重启保护：_run() 任何未捕获异常 → 记录 + 3s后重启."""
    while True:
        try:
            _run()
        except Exception as _e:
            _log.exception(f"orchestrator crashed, restarting in 3s: {_e}")
            _time.sleep(3)

def start():
    """启动编排器 daemon 线程（PID 锁防重复启动）."""
    _pid = _pid_path()
    if os.path.exists(_pid):
        try:
            with open(_pid) as f:
                old_pid = int(f.read().strip())
            os.kill(old_pid, 0)
            _log.warning(f"orchestrator already running (PID={old_pid}), skip duplicate start")
            return
        except (OSError, ValueError):
            os.remove(_pid)
    with open(_pid, 'w') as f:
        f.write(str(os.getpid()))
    t = _thr.Thread(target=_run_safe, daemon=True, name="orchestrator")
    t.start()
