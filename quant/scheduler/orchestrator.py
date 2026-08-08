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
from quant.scheduler.task_log import _pid_alive  # v424: 僵尸清理

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


def _get_monitor_failures(today: str) -> int:
    """统计今日 monitor 累计 failed 次数 (用于崩溃风暴保护).
    v369: aborted (zombie cleanup 产生) 不计入重试预算, 仅 real crash (failed) 计入 (Bug D)."""
    conn = sqlite3.connect(MARKET_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={_require_cfg('data.sqlite.busy_timeout')}")
    count = conn.execute(
        "SELECT COUNT(*) FROM task_runs WHERE date=? AND task_name='monitor' AND status='failed'",
        (today,)
    ).fetchone()[0]
    conn.close()
    return count


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
            _run_continuous(current_day, stop_event=_monitor_stop)
            _log.info(f"[{current_day}] monitor daemon stopped")
        except Exception as _e:
            _log.exception(f"[{current_day}] monitor daemon crashed: {_e}")

    def _run_task(name, fn, task_today):
        t0 = _time.time()
        try:
            fn(task_today)
            elapsed = _time.time() - t0
            # test-v400: 回读 task_runs 确认真实状态, 不看异常
            # (execute._run 在 no-signals 分支 early-return 不抛异常,
            #  但 finally 写 status=failed → orchestrator 之前误报 OK)
            _db_status = _get_today_status(task_today).get(name)
            if _db_status == "ok":
                _log.info(f"[SCHEDULER] {task_today} | TASK={name} | STATUS=OK | elapsed={elapsed:.1f}s")
                _m.inc(f"scheduler.{name}.ok")
            elif _db_status == "failed":
                _log.error(f"[SCHEDULER] {task_today} | TASK={name} | STATUS=FAILED (DB) | elapsed={elapsed:.1f}s")
            elif _db_status == "aborted":
                _log.warning(f"[SCHEDULER] {task_today} | TASK={name} | STATUS=ABORTED (DB) | elapsed={elapsed:.1f}s")
            else:
                _log.info(f"[SCHEDULER] {task_today} | TASK={name} | STATUS={_db_status} | elapsed={elapsed:.1f}s")
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

        # ═══════════════════════════════════════════
        # v416 (CODE-REVIEW 调度修复): 状态读取提前 — 周度评估在非交易日
        # (周六) 也必须运行, 因此 status/_retry_ok 与触发块必须位于
        # `if not is_trading_day(): continue` 之前, 否则周六永远不可达.
        # ═══════════════════════════════════════════

        # ── 读取 DB 权威状态 ──
        status = _get_today_status(today)
        aborted = _get_today_aborted(today)

        def _retry_ok(name: str) -> bool:
            """B-23: aborted 重试次数未超限才允许再次触发."""
            return aborted.get(name, 0) < _MAX_TASK_RETRIES

        # ═══════════════════════════════════════════
        # 0. 周六 06:00 — 周度因子评估 subprocess
        # (test-v301 引入时放在 is_trading_day continue 之后 → 永不可达;
        #  test-v416 修复: 前移到非交易日短路之前, 周六照常触发,
        #  窗口放宽至 06:00-12:00 — 周六早间 restart 错过 06:00-06:05
        #  会漏掉整周评估; _tk_start dedup 保证三路触发不重复执行)
        # ═══════════════════════════════════════════
        if now.weekday() == 5 and time(6, 0) <= hhmm < time(12, 0):
            s = status.get("weekly_eval")
            if s not in ("ok", "failed") and _retry_ok("weekly_eval"):
                _log.info(f"[{today}] 06:00-12:00 — spawning weekly eval subprocess")
                subprocess.Popen(
                    [".venv/bin/python3", "-c",
                     "from quant.utils.excepthook import setup; setup();"
                     "from quant.scheduler.weekly import _run;"
                     f"_run('{today}')"],
                    cwd=os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
                    env={**os.environ, "PYTHONPATH": "."},
                )
            elif s != "ok":
                _log.debug(f"[{today}] weekly_eval window: status={s} (waiting for retry/stuck check)")

        # ── 非交易日 ──
        if not is_trading_day():
            _check_timeouts(today)
            _time.sleep(POLL)
            continue

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
        # 2.5 10:00 — 开盘30分钟价格快照 (test-v402: 09:30→10:00 修正)
        # 原 v324 在 09:30 触发，实际拉到的是开盘价而非开盘30分钟后价格，
        # 导致 intraday_reversal 因子退化为隔夜缺口因子。
        # 详见 HANDOFF.md §test-v402.
        # ═══════════════════════════════════════════
        s = status.get("snapshot_open")
        if s not in ("ok", "failed") and _retry_ok("snapshot_open"):
            if hhmm >= time(10, 0) and "execute" in status:
                from quant.scheduler.snapshot import snapshot_open
                _run_task("snapshot_open", snapshot_open, today)

        # ═══════════════════════════════════════════
        # 3. 09:35-11:30,13:00-14:55 — 盘中风控 (daemon 线程)
        # ═══════════════════════════════════════════
        in_monitor_window = time(9, 30) <= hhmm <= time(14, 55)
        monitor_state = status.get("monitor")
        # monitor 是持续 daemon: 仅 "ok" 表示自然结束, "failed"/"aborted" 应重启
        monitor_done = monitor_state == "ok"
        # 防崩溃风暴: 当日累计失败次数 (failed+aborted) 达上限则放弃
        monitor_exhausted = _get_monitor_failures(today) >= _MAX_TASK_RETRIES

        if in_monitor_window and not monitor_done and not monitor_exhausted:
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
        # 3.5 14:55 — 尾盘快照 (test-v328: 尾盘5分钟价格+成交量)
        # ═══════════════════════════════════════════
        s = status.get("snapshot_close")
        if s not in ("ok", "failed") and _retry_ok("snapshot_close"):
            if hhmm >= time(14, 55):
                from quant.scheduler.snapshot import snapshot_close
                _run_task("snapshot_close", snapshot_close, today)

        # ═══════════════════════════════════════════
        # 4. 15:05-15:30 — OMS 日终对账 (monitor 收盘后)
        # ═══════════════════════════════════════════
        s = status.get("reconcile")
        if s not in ("ok", "failed") and _retry_ok("reconcile"):
            if time(15, 5) <= hhmm < time(15, 30):
                from quant.scheduler.reconcile import _run as _recon_run
                _run_task("reconcile", _recon_run, today)

        # ═══════════════════════════════════════════
        # 6. 19:00+ — 晚间链 subprocess (test-v288)
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
                        env={**os.environ, "PYTHONPATH": ".", "_EVENING_SUBPROCESS": "1"},
                    )
            else:
                ret = _evening_proc.poll()
                if ret is not None:
                    _evening_proc = None
                    if ret == 0:
                        _log.info(f"[{today}] evening chain subprocess exited OK")
                        _evening_done = True
                    else:
                        # v382: 子进程崩溃 → 清理其残留的 running 子任务行
                        # (信号杀死进程时 Python finally 不执行, task_runs 留 running 僵尸)
                        _cleanup_evening_children(today)
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


def _cleanup_evening_children(today: str):
    """晚间链子进程崩溃时, 将其残留的 running 子任务标为 failed.
    v382: 信号杀死进程 → Python finally 不执行 → task_runs 留 running 僵尸 → 后续调度永久阻塞."""
    try:
        conn = sqlite3.connect(MARKET_DB)
        conn.execute("PRAGMA journal_mode=WAL")
        children = ["daily_data", "factor_cache", "attribution", "lgb_train", "xgb_train", "adj_factor"]
        ph = ",".join("?" * len(children))
        n = conn.execute(
            f"UPDATE task_runs SET status='failed', finished_at=datetime('now','localtime'), "
            f"error='晚间链子进程崩溃(信号终止)' "
            f"WHERE date=? AND status='running' AND task_name IN ({ph})",
            [today] + children
        ).rowcount
        conn.commit()
        conn.close()
        if n:
            _log.warning(f"[{today}] cleaned {n} stuck child tasks after evening chain crash")
    except Exception as _e:
        _log.debug("cleanup_evening_children failed (non-fatal): %s", _e)


# ── 超时阈值 (秒) — test-v287: 晚间任务移出, 只保留白天任务.
_TIMEOUTS = {
    "signals": 1800,
    "execute": 1800,
    "monitor": None,
    "reconcile": 600,
    "evening_chain": 14400,  # test-v288: evening.py 引用, subprocess 超时
    "weekly_eval": 43200,    # v416: 周度评估 subprocess 超时 (12h, 评估 5 阶段可能数小时)
}

# B-23 fix: 同一任务当日最多重试次数 (aborted 后 orchestrator 会重新触发,
# 无上限导致 2026-07-23 factor_cache 一夜重跑 4 次的重试风暴)
_MAX_TASK_RETRIES = 2

def _cleanup_zombie_tasks():
    """启动时清理今天旧进程残留的非 ok 行 (restart.sh kill 旧 orchestrator → 新启动).

    v369 重写: 不再把 dead-PID 行标为 aborted (aborted 仍消耗重试预算, 阻塞新进程),
    而是直接 DELETE。保留 ok 行 (已完成的工作), 保留 live-PID 行 (当前进程自己的任务)。
    这样 restart 后新 orchestrator 从干净状态开始, 重试计数器自然归零。
    """
    try:
        import os as _os
        my_pid = _os.getpid()
        today = datetime.now().strftime("%Y-%m-%d")
        conn = sqlite3.connect(MARKET_DB)
        conn.execute("PRAGMA journal_mode=WAL")
        # 取今天所有非 ok 行
        rows = conn.execute(
            "SELECT id, task_name, status, pid FROM task_runs "
            "WHERE date=? AND status!='ok'",
            (today,)
        ).fetchall()
        deleted = 0
        for rid, task_name, status, pid in rows:
            if pid is None:
                # 无 pid → 旧记录, 保守删除 (无法验证是否存活)
                conn.execute("DELETE FROM task_runs WHERE id=?", (rid,))
                deleted += 1
                continue
            if pid == my_pid:
                # 当前进程自己的行 → 保留 (可能是在 restart 前瞬间创建的)
                continue
            try:
                _os.kill(pid, 0)
            except (OSError, ProcessLookupError):
                # 进程已死 → 直接删除, 不标 aborted
                conn.execute("DELETE FROM task_runs WHERE id=?", (rid,))
                deleted += 1
            # else: 进程存活 → 保留 (异常情况: 两个 orchestrator 同时跑? 保留让 start() dedup 处理)
        conn.commit()
        conn.close()
        if deleted:
            _log.info("orchestrator startup: cleaned %d stale rows for %s (fresh start)", deleted, today)
    except Exception as _e:
        _log.warning("startup zombie cleanup failed (non-fatal): %s", _e)

def _check_timeouts(today: str):
    """扫描 task_runs 中 status='running' 的行, 超时则标为 aborted."""
    try:
        conn = sqlite3.connect(MARKET_DB)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_require_cfg('data.sqlite.busy_timeout')}")
        rows = conn.execute(
            "SELECT id, task_name, started_at, pid, date FROM task_runs "
            "WHERE status='running' AND finished_at IS NULL",
        ).fetchall()
        if not rows:
            conn.close()
            return
        now = datetime.now()
        for rid, task_name, started_at, pid, run_date in rows:
            if not started_at:
                continue
            if pid and not _pid_alive(pid):
                # v424: 记录进程已死 → 立即 aborted, 不等超时 (与 task_log.start 一致)
                # v426: 不限定 today — 历史日期 (回放/迁移) 的僵尸同样自愈
                conn.execute(
                    "UPDATE task_runs SET status='aborted', finished_at=?, "
                    "error='进程已死 (pid=' || ? || ') — auto-abort' WHERE id=?",
                    (now.isoformat(), str(pid), rid)
                )
                _log.warning(
                    f"[{today}] {task_name} (date={run_date}) pid={pid} dead → aborted (zombie cleanup)"
                )
                _m.inc(f"alerts.task_aborted.{task_name}")
                continue
            # v426: 超时判定仅限今日行 — 历史日期 (回放) 的 started_at 跨日, 不适用
            if run_date != today:
                continue
            dt = datetime.fromisoformat(started_at)
            elapsed = (now - dt).total_seconds()
            limit = _TIMEOUTS.get(task_name)
            if task_name == "monitor":
                if now.hour >= 14 and now.minute >= 55:
                    # v368: 与 _tk_start grace_seconds=21600 对齐 (全天交易窗口 ~5h20m)
                    limit = 21600
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
                _m.inc(f"alerts.task_aborted.{task_name}")
        # v408: 告警 — 回撤检查 + 数据滞后检查
        try:
            from quant.data.repos import TradeRepo
            repo = TradeRepo()
            base = repo.get_initial_capital("quant")
            if base > 0:
                equity = repo.get_cash("quant") + sum(
                    p["shares"] * p.get("price", 0) for p in repo.get_positions("quant")
                )
                drawdown = (equity - base) / base
                if drawdown < -0.20:
                    _log.critical(f"[{today}] ALERT: drawdown {drawdown:.1%} < -20%")
                    _m.inc("alerts.drawdown.critical")
                elif drawdown < -0.10:
                    _log.warning(f"[{today}] ALERT: drawdown {drawdown:.1%} < -10%")
                    _m.inc("alerts.drawdown.warning")
        except Exception:
            pass
        try:
            from quant.data.freshness import check_freshness
            fresh = check_freshness(today)
            stale = [r for r in fresh if r["stale"]]
            for r in stale:
                _log.critical(f"[{today}] ALERT: DATA STALE {r['table']} lag={r['lag_days']}d")
                _m.inc(f"alerts.data_stale.{r['table']}")
        except Exception:
            pass
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
    from quant.utils.logger import cleanup_old_logs
    cleanup_old_logs(keep_days=7)  # test-v321: 启动时清理7天前旧日志
    t = _thr.Thread(target=_run_safe, daemon=True, name="orchestrator")
    t.start()
