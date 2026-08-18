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
from quant.scheduler.manifest import ALL
from quant.scheduler.runners import (
    run_inline_tasks, run_monitor, run_evening_chain, run_weekly_eval,
    _should_run, _cleanup_evening_children, _cleanup_zombie_tasks,
    _MAX_TASK_RETRIES,
)
from quant.execution.calendar import is_trading_day

_log = get_logger(__name__)


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
    # v536: 告警/指标落盘节流 — check_alerts 查 daily_equity/daily 表,
    # 每次 POLL 跑一次过频 → 60s 评估一次; metrics.persist 每日 4 次落盘
    _last_alert_ts = 0.0
    _last_persist_ts = 0.0

    while True:
        now = datetime.now()
        current_day = now.strftime("%Y-%m-%d")
        hhmm = time(now.hour, now.minute)

        # —— 监控闭环 (v536): 告警评估 + SSE 推送 + 指标落盘 ——
        # 原 check_alerts 仅 web /api/health 被动触发, push_alerts 零调用 →
        # 回撤/数据滞后/pipeline 失败告警从不主动推送; metrics.db 恒空表.
        _now_ts = _time.time()
        if _now_ts - _last_alert_ts >= 60:
            _last_alert_ts = _now_ts
            try:
                from quant.core.state_broker import broker as _broker
                from quant.monitor.metrics import metrics as _mm
                from quant.monitor.alerts import check_alerts, push_alerts
                push_alerts(check_alerts(_broker.get(), _mm.snapshot()))
            except Exception as _ae:
                _log.warning("alert evaluation failed: %s", _ae)
        if _now_ts - _last_persist_ts >= 6 * 3600:
            _last_persist_ts = _now_ts
            try:
                from quant.monitor.metrics import metrics as _mm
                _mm.persist()
            except Exception as _pe:
                _log.warning("metrics persist failed: %s", _pe)

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

        # —— B23: monitor daemon 线程已退出 (崩溃或自退) → 重置, 下一轮可重启 ——
        # 原 _monitor_runner 非 None 永不重置 → 崩溃后盘中风控静默丢失
        if _monitor_runner is not None and not _monitor_runner.is_alive():
            _log.warning(f"[{today}] monitor daemon thread exited "
                         f"(status={status.get('monitor')}); will restart next poll")
            _monitor_runner = None
            _monitor_thread = None

        # —— 周度评估 (周六 06:00-12:00) ——
        _weekly = ALL.get("weekly_eval")
        if _weekly and not _weekly_done:
            if _should_run(_weekly, hhmm, now.weekday(), status, aborted):
                _log.info(f"[{today}] 06:00-12:00 — spawning weekly eval subprocess")
                if _evening_runner is None:
                    _evening_runner = SubprocessRunner(today)
                # v532: 失败不置 done — 窗口内由 _should_run (failed 预算) 重试
                _weekly_done = _evening_runner.run_weekly_eval()
                _evening_runner = None

        # —— 超时/僵尸自愈: 所有日期统一检测 (B22, 2026-08-18) ——
        # 原 _check_timeouts 仅非交易日分支调用 → 交易日内 inline 任务挂死
        # (signals/execute/reconcile) 无人清理, 行卡 running 永久阻塞调度.
        _check_timeouts(today)

        # —— 非交易日: 周度评估 + 早间补拉 ——
        if not is_trading_day():
            # v479: 非交易日也允许早间补拉 (周末覆盖周五晚间链缝隙, 如 margin T+1)
            _rep = ALL.get("daily_repair")
            if _rep and not _repair_done and _should_run(_rep, hhmm, now.weekday(), status, aborted):
                _log.info(f"[{today}] 08:00 — spawning daily repair (weekend)")
                # v532: 失败不置 done — 窗口内重试 (预算 2)
                _repair_done = SubprocessRunner(today).run_daily_repair()
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

            # inline 单发任务 — 崩溃不杀 orchestrator (v487: 2026-08-14 实证
            # signals RuntimeError 冒泡 → 主循环死亡 → 当日全部调度瘫痪,
            # daily_repair/execute/monitor/evening_chain 全部未跑)
            runner = InlineRunner(today)
            try:
                runner.run_once(s.name)
            except Exception as _e:
                _log.exception(f"[{today}] inline task {s.name} crashed "
                               f"(orchestrator continues): {_e}")

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
            _repair_done = SubprocessRunner(today).run_daily_repair()

        # —— 19:00+ — 晚间链 subprocess ——
        _even = ALL.get("evening_chain")
        if _even and _even.in_window(hhmm, now.weekday()) and not _evening_done \
                and _evening_retries < _MAX_TASK_RETRIES:
            if _evening_runner is None:
                if _should_run(_even, hhmm, now.weekday(), status, aborted):
                    _log.info(f"[{today}] 19:00 — spawning evening chain subprocess "
                              f"(attempt={_evening_retries + 1}/{_MAX_TASK_RETRIES})")
                    _evening_runner = SubprocessRunner(today)
                    # v532: 阻塞等待完成; 失败时预算内自动重跑, 返回成败
                    _ok = _evening_runner.run_evening_chain()
                    _evening_retries += 1
                    if _ok:
                        _log.info(f"[{today}] evening chain subprocess OK (attempt {_evening_retries})")
                        _evening_done = True
                        _evening_runner = None
                    elif _evening_retries >= _MAX_TASK_RETRIES:
                        _log.error(f"[{today}] evening chain max retries exhausted "
                                   f"({_MAX_TASK_RETRIES}) — factor_cache 缺口由次日 08:00 daily_repair 兜底")
                        _evening_done = True
                        _evening_runner = None

        _time.sleep(POLL)


if __name__ == "__main__":
    _run()