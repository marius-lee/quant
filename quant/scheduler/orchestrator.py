"""日频任务编排器 — manifest 驱动 (v428 重构 + v433 Runner 拆分).

任务声明见 quant/scheduler/manifest.py — 时间窗/依赖/运行模式/超时单一真相源.
编排器每 30s 轮询, 对每个任务走 `_should_run` 决策 (窗口+星期+状态+依赖+重试预算):

  mode=inline     → InlineRunner  进程内同步执行 (signals/execute/snapshot/reconcile)
  mode=monitor    → MonitorRunner 盘中长驻窗口任务 (09:30-15:00 持续循环, 午休内部暂停, 窗口结束自退)
  mode=subprocess → SubprocessRunner 独立子进程 (晚间链 19:00 / 周度评估 周六 06:00)

状态: task_runs (market.db) 为单一调度真相源. 每 30s 轮询一次,
已完成 (ok) 不重跑; 真失败 (failed) 不自动重试 (需人工排查后手动触发);
超时/僵尸 (aborted) 在重试预算内可重触发.

架构 (v433 重构):
  - 共用决策函数: _should_run() (纯函数, 供所有 Runner 复用)
  - 三大 Runner: InlineRunner / MonitorRunner / SubprocessRunner
  - 编排器仅负责调度循环 + 调用 Runner
"""
import os, time as _time, threading as _thr, sqlite3, subprocess
from datetime import datetime, time
from quant.config.constants import _require_cfg
from quant.monitor.metrics import metrics as _m
from quant.utils.logger import get_logger
from quant.config.paths import MARKET_DB
from quant.scheduler.task_log import _pid_alive, start as _tk_start, finish as _tk_finish  # v424: 僵尸清理
from quant.scheduler.manifest import ALL, _PLAN_ORDER, TaskSpec
from quant.scheduler.runners import (
    run_inline_tasks, run_monitor, run_evening_chain, run_weekly_eval,
    _should_run, _cleanup_evening_children, _cleanup_zombie_tasks,
    _MAX_TASK_RETRIES,
)
from quant.execution.calendar import is_trading_day

_log = get_logger(__name__)

# 晚间链子进程崩溃时标记 failed 的子任务 (v382)
_EVENING_CHILDREN = ["daily_data", "factor_cache", "attribution", "lgb_train", "xgb_train", "adj_factor"]


def _get_today_status(today: str) -> dict:
    """查询 task_runs 中今天每个任务的最新状态.

    返回: {"signals": "ok", "execute": "failed", ...}
    无该任务记录则 key 不存在.
    """
    with sqlite3.connect(MARKET_DB) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_require_cfg('data.sqlite.busy_timeout')}")
        rows = conn.execute(
            "SELECT task_name, status FROM task_runs WHERE date=? ORDER BY id DESC",
            (today,)
        ).fetchall()
    status = {}
    for row in rows:
        if row[0] not in status:
            status[row[0]] = row[1]
    return status


def _get_today_aborted(today: str) -> dict:
    """查询今日各任务 aborted 次数 (B-23: 重试风暴抑制)."""
    with sqlite3.connect(MARKET_DB) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_require_cfg('data.sqlite.busy_timeout')}")
        rows = conn.execute(
            "SELECT task_name, COUNT(*) FROM task_runs WHERE date=? AND status='aborted' GROUP BY task_name",
            (today,)
        ).fetchall()
    return dict(rows)


def _get_monitor_failures(today: str) -> int:
    """今日 monitor 累计 failed 次数 (崩溃风暴保护).
    v369: aborted (僵尸清理产生) 不计预算, 仅 real crash (failed) 计入."""
    with sqlite3.connect(MARKET_DB) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_require_cfg('data.sqlite.busy_timeout')}")
        count = conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE date=? AND task_name='monitor' AND status='failed'",
            (today,)
        ).fetchone()[0]
    return count


# ══════════════════════════════════════════════════════════════════════
# v428: 调度决策 — manifest-driven 纯函数 (窗口/依赖/状态/预算)
# ═════════════════════════════════════════════════════════════════════

def _should_run(s: TaskSpec, hhmm: time, weekday: int,
                status: dict, aborted: dict) -> bool:
    """是否在当下应触发/保持任务 s.

    通过全部条件 → True:
      1. 时间窗 (manifest.window) 内且星期匹配
      2. 状态允许: 无记录 / running(由 grace 挡重入) /
         aborted(预算内重试) — ok/failed 均不再触发
      3. 依赖: depends_ok 全部 == "ok";  depends_attempt 全部今日尝试过
      4. aborted 次数 < _MAX_TASK_RETRIES
    """
    if s.weekday is not None and weekday != s.weekday:
        return False
    cur = status.get(s.name)
    if cur == "ok":
        return False
    if cur == "failed":
        # P0-11 fix: failed 允许在 max_retries 预算内重试 (主要针对 monitor 守护线程崩溃)
        if aborted.get(s.name, 0) >= _MAX_TASK_RETRIES:
            return False
        # fall through: 继续检查窗口 + 依赖 (允许重试)
    if cur == "running":
        return False
    if not s.in_window(hhmm, weekday):
        return False
    for dep in s.depends_ok:
        if status.get(dep) != "ok":
            return False
    for dep in s.depends_attempt:
        dep_status = status.get(dep)
        if dep_status is None or dep_status == "running":
            return False
    if aborted.get(s.name, 0) >= _MAX_TASK_RETRIES:
        return False
    return True


def _cleanup_evening_children(today: str):
    """晚间链子进程崩溃时, 将其残留的 running 子任务标为 failed.
    v382: 信号杀死进程 → Python finally 不执行 → task_runs 留 running 僵尸 → 后续调度永久阻塞."""
    try:
        with sqlite3.connect(MARKET_DB) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            ph = ",".join("?" * len(_EVENING_CHILDREN))
            n = conn.execute(
                f"UPDATE task_runs SET status='failed', finished_at=datetime('now','localtime'), "
                f"error='晚间链子进程崩溃(信号终止)' "
                f"WHERE date=? AND status='running' AND task_name IN ({ph})",
                [today] + _EVENING_CHILDREN
            ).rowcount
            conn.commit()
        if n:
            _log.warning(f"[{today}] cleaned {n} stuck child tasks after evening chain crash")
    except Exception as _e:
        _log.debug("cleanup_evening_children failed (non-fatal): %s", _e)


def _cleanup_zombie_tasks():
    """启动时清理今天旧进程残留的非 ok 行 (restart.sh kill 旧 orchestrator → 新启动).

    v369 重写: 不再把 dead-PID 行标为 aborted (aborted 仍消耗重试预算, 阻塞新进程),
    而是直接 DELETE。保留 ok 行 (已完成的工作), 保留 live-PID 行 (当前进程自己的任务).
    这样 restart 后新 orchestrator 从干净状态开始, 重试计数器自然归零。
    """
    import os as _os
    my_pid = _os.getpid()
    today = datetime.now().strftime("%Y-%m-%d")

    with sqlite3.connect(MARKET_DB) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_require_cfg('data.sqlite.busy_timeout')}")

        rows = conn.execute(
            "SELECT id, task_name, pid FROM task_runs WHERE date=? AND status='running'",
            (today,)
        ).fetchall()

        for row in rows:
            rid, task_name, pid = row
            if pid is None or pid == 0:
                # 无 PID 行 → 直接删 (可能是历史脏数据)
                _log.warning(f"cleanup: deleting task_runs#{rid} ({task_name}) no pid")
                conn.execute("DELETE FROM task_runs WHERE id=?", (rid,))
                continue
            if pid == os.getpid():
                # 自己进程的任务 → 保留
                continue
            if not _pid_alive(pid):
                _log.warning(f"cleanup: dead pid={pid} task={task_name} → delete task_runs#{rid}")
                conn.execute("DELETE FROM task_runs WHERE id=?", (rid,))

        conn.commit()
    _log.info("zombie task cleanup done")


# ══════════════════════════════════════════════════════════════════════
# v426 + v428: 超时检测与僵尸进程自愈
# ═════════════════════════════════════════════════════════════════════

def _check_timeouts(today: str):
    """检查 running 超时 → 标记 aborted (释放重试预算).

    v426: 任何日期 running + pid 死 → 也自愈 (不限定今日).
    """
    from quant.scheduler.manifest import ALL
    with sqlite3.connect(MARKET_DB) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_require_cfg('data.sqlite.busy_timeout')}")

        # 1. 所有 running 任务 (包括今日和历史)
        all_running = conn.execute(
            "SELECT id, task_name, started_at, date, pid FROM task_runs "
            "WHERE status='running'",
        ).fetchall()

    for row in all_running:
        rid, task_name, started_at, task_date, pid = row
        if not started_at:
            continue

        # 检查 pid 存活 (优先: pid 死 → 直接 aborted, v426)
        if pid is not None:
            import quant.scheduler.orchestrator as _orch
            if not _orch._pid_alive(pid):
                with sqlite3.connect(MARKET_DB) as conn2:
                    conn2.execute(
                        "UPDATE task_runs SET status='aborted', finished_at=datetime('now','localtime'), "
                        "error='进程已死 (pid={})' WHERE id=?".format(pid),
                        (rid,))
                    conn2.commit()
                _log.warning(f"task {task_name} pid={pid} dead → aborted")
                continue

        # 仅今日任务检查超时 (历史日期不做超时判定)
        if task_date == today:
            if not started_at:
                continue
            from datetime import datetime as _dt
            try:
                start = _dt.strptime(started_at, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                start = _dt.strptime(started_at, "%Y-%m-%dT%H:%M:%S")
            from quant.scheduler.manifest import ALL, EVENING_STAGE_GRACE
            s = ALL.get(task_name)
            # v474: 晚间链 stage (daily_data/factor_cache/...) 不在 ALL,
            # 原 fallback 300s 导致每晚 5-12min 即被误杀 (v428 回归)
            grace_s = s.grace_s if s else EVENING_STAGE_GRACE.get(task_name, 300)
            elapsed = (datetime.now() - start).total_seconds()
            if elapsed > grace_s:
                with sqlite3.connect(MARKET_DB) as conn3:
                    conn3.execute("PRAGMA journal_mode=WAL")
                    conn3.execute(
                        "UPDATE task_runs SET status='aborted', finished_at=datetime('now','localtime'), "
                        "error='timeout' WHERE id=?",
                        (rid,))
                    conn3.commit()
                _log.warning(f"[{today}] task {task_name} timeout ({elapsed:.0f}s > {grace_s}s) → aborted")


def _run():
    """编排器主循环 — manifest 驱动 (v433: 复用 Runner 类)."""
    from quant.scheduler.status import register_all
    from quant.execution.calendar import is_trading_day
    from quant.scheduler.runners import (
        InlineRunner, MonitorRunner, SubprocessRunner,
        _get_today_status, _get_today_aborted, _get_monitor_failures,
        _cleanup_zombie_tasks, _cleanup_evening_children,
        _MAX_TASK_RETRIES, POLL,
    )
    from quant.scheduler.manifest import ALL, _PLAN_ORDER
    import os as _os, time as _time, threading as _thr, subprocess
    from datetime import datetime, time

    register_all()
    _cleanup_zombie_tasks()

    _log.info("orchestrator started — manifest: "
              "08:30 signals → 09:30 execute → 10:00 snapshot_open → "
              "09:30-15:00 monitor → 15:00 snapshot_close → 15:05 reconcile → "
              "19:00 evening_chain → 周六 06:00 weekly_eval")

    today = None
    _monitor_runner: MonitorRunner | None = None
    _monitor_thread: _thr.Thread | None = None
    _evening_runner: SubprocessRunner | None = None
    _evening_retries = 0
    _evening_done = False
    _repair_done = False
    _weekly_done = False

    while True:
        now = datetime.now()
        current_day = now.strftime("%Y-%m-%d")
        hhmm = time(now.hour, now.minute)

        # —— 新的一天: 重置状态 ——
        if current_day != today:
            if _monitor_runner is not None:
                _monitor_runner.stop()
                _tk_finish("monitor", today, "ok")
            if _monitor_thread is not None:
                _monitor_thread.join(timeout=5)
            _monitor_runner = None
            _monitor_thread = None
            today = current_day
            _evening_runner = None
            _evening_retries = 0
            _evening_done = False
            _repair_done = False
            _weekly_done = False
            _log.info(f"[{today}] new day, orchestrator ready")

        # —— 读取 DB 权威状态 ——
        status = _get_today_status(today)
        aborted = _get_today_aborted(today)

        # —— 周度评估 (周六 06:00-12:00) ——
        _weekly = ALL.get("weekly_eval")
        if _weekly and not _weekly_done:
            if _should_run(_weekly, hhmm, now.weekday(), status, aborted):
                _log.info(f"[{today}] 06:00-12:00 — spawning weekly eval subprocess")
                if _evening_runner is None:
                    _evening_runner = SubprocessRunner(today)
                _evening_runner.run_weekly_eval()
                _weekly_done = True

        # —— 非交易日: 周度评估 + 早间补拉 + 超时检测 ——
        if not is_trading_day():
            from quant.scheduler.orchestrator import _check_timeouts
            _check_timeouts(today)
            # v479: 非交易日也允许早间补拉 (周末覆盖周五晚间链缝隙, 如 margin T+1)
            _rep = ALL.get("daily_repair")
            if _rep and not _repair_done and _should_run(_rep, hhmm, now.weekday(), status, aborted):
                _log.info(f"[{today}] 08:00 — spawning daily repair (weekend)")
                SubprocessRunner(today).run_daily_repair()
                _repair_done = True
            _time.sleep(POLL)
            continue

        # —— 日线任务: 按 manifest 顺序决策 ——
        for name in _PLAN_ORDER:
            s = ALL.get(name)
            if s is None or s.mode == "subprocess":
                continue
            if not _should_run(s, hhmm, now.weekday(), status, aborted):
                continue

            # monitor: 长驻窗口任务
            if s.mode == "monitor":
                monitor_done = status.get("monitor") == "ok"
                monitor_exhausted = _get_monitor_failures(today) >= _MAX_TASK_RETRIES
                if not monitor_done and not monitor_exhausted:
                    if _monitor_runner is None:
                        _monitor_runner = MonitorRunner(today)
                        # 记录 monitor 任务启动状态
                        _tk_start("monitor", today, grace_seconds=21600)
                        _monitor_thread = _thr.Thread(
                            target=_monitor_runner.run, daemon=True, name="monitor-daemon"
                        )
                        _monitor_thread.start()
                elif monitor_done and _monitor_runner is not None:
                    _monitor_runner.stop()
                    _tk_finish("monitor", today, "ok")
                    if _monitor_thread is not None:
                        _monitor_thread.join(timeout=5)
                    _monitor_runner = None
                    _monitor_thread = None
                continue

            # inline 单发任务
            runner = InlineRunner(today)
            runner.run_once(s.name)

        # —— monitor 窗口关闭后清理 ——
        if not ALL["monitor"].in_window(hhmm, now.weekday()) and _monitor_runner is not None:
            _monitor_runner.stop()
            _tk_finish("monitor", today, "ok")
            if _monitor_thread is not None:
                _monitor_thread.join(timeout=5)
            _monitor_runner = None
            _monitor_thread = None

        # —— 08:00 早间补拉链 (每日, signals 08:30 前修复 T+1 迟发缺口) ——
        _rep = ALL.get("daily_repair")
        if _rep and not _repair_done and _should_run(_rep, hhmm, now.weekday(), status, aborted):
            _log.info(f"[{today}] 08:00 — spawning daily repair subprocess")
            SubprocessRunner(today).run_daily_repair()
            _repair_done = True

        # —— 19:00+ — 晚间链 subprocess ——
        _even = ALL.get("evening_chain")
        if _even and _even.in_window(hhmm, now.weekday()) and not _evening_done:
            if _evening_runner is None:
                if _should_run(_even, hhmm, now.weekday(), status, aborted):
                    _log.info(f"[{today}] 19:00 — spawning evening chain subprocess "
                              f"(retry={_evening_retries}/{_MAX_TASK_RETRIES})")
                    _evening_runner = SubprocessRunner(today)
                    _evening_runner.run_evening_chain()
                    _evening_retries += 1
                    # run_evening_chain 内部轮询，完成后继续
                    if _evening_runner._proc is None:  # 已完成
                        _log.info(f"[{today}] evening chain subprocess exited OK")
                        _evening_done = True
                        _evening_runner = None
                    elif _evening_retries >= _MAX_TASK_RETRIES:
                        _log.error(f"[{today}] evening chain max retries exhausted")
                        _evening_done = True
                        _evening_runner = None

        _time.sleep(POLL)


if __name__ == "__main__":
    _run()