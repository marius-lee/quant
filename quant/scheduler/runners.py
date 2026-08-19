"""Scheduler Runners — 将 orchestrator 拆分为三个独立 Runner + 共用决策函数.

原 orchestrator._run() 混杂了三种执行模式:
  1. inline    — 进程内同步执行 (signals/execute/snapshot/reconcile)
  2. monitor   — 盘中长驻窗口任务 (09:30-15:00 持续循环)
  3. subprocess — 独立子进程 (晚间链/周度评估)

重构后:
  - InlineRunner  : 同步执行 inline 任务
  - MonitorRunner : 监控 daemon 线程管理
  - SubprocessRunner: 子进程管理 + 重试/清理

共用决策函数 _should_run() 保持纯函数，供所有 Runner 复用。
"""

import os, time as _time, threading as _thr, sqlite3, subprocess
from datetime import datetime, time
from typing import Optional, Callable

from quant.config.constants import _require_cfg
from quant.monitor.metrics import metrics as _m
from quant.utils.logger import get_logger
from quant.config.paths import MARKET_DB
from quant.scheduler.task_log import _pid_alive as _pid_alive, start as _tk_start, finish as _tk_finish
from quant.scheduler.manifest import ALL, _PLAN_ORDER, TaskSpec
from quant.execution.calendar import is_trading_day

_log = get_logger("scheduler.runners")

# B-23 fix: 同一任务当日 aborted 最多重试次数 (2026-07-23 factor_cache 重试风暴)
_MAX_TASK_RETRIES = 2

# 晚间链子进程崩溃时标记 failed 的子任务 (v382)
_EVENING_CHILDREN = ["daily_data", "factor_cache", "attribution", "lgb_train", "xgb_train", "adj_factor"]

# 全局 Polling 间隔
POLL = _require_cfg("quant.scheduler.poll_interval")


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
    """查询今日各任务可重试次数 (B-23: 重试风暴抑制).

    B24 (2026-08-18): 统计 failed + aborted 合计 — 原仅 status='aborted',
    failed 行不计数 → 崩溃任务窗口内每 30s 无限重试 (signals 08:30-15:30
    可空转 ~840 次 × 重复挂单风险).
    """
    with sqlite3.connect(MARKET_DB) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(f"PRAGMA busy_timeout={_require_cfg('data.sqlite.busy_timeout')}")
        rows = conn.execute(
            "SELECT task_name, COUNT(*) FROM task_runs "
            "WHERE date=? AND status IN ('failed','aborted') GROUP BY task_name",
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
# ══════════════════════════════════════════════════════════════════════

def _should_run(s: TaskSpec, hhmm: time, weekday: int,
                status: dict, aborted: dict) -> bool:
    """是否在当下应触发/保持任务 s.

    通过全部条件 → True:
      1. 时间窗 (manifest.window) 内且星期匹配
      2. 状态允许: 无记录 / running(由 grace 挡重入) /
         failed+aborted(预算内重试, B24: failed 也消耗预算) — ok 不再触发
      3. 依赖: depends_ok 全部 == "ok";  depends_attempt 全部今日尝试过
      4. 重试次数 (failed+aborted) < _MAX_TASK_RETRIES
    """
    if s.weekday is not None and weekday != s.weekday:
        return False
    cur = status.get(s.name)
    if cur == "ok":
        return False
    if cur == "failed":
        # P0-11 + B24: failed 允许在重试预算内重试 (monitor 守护线程崩溃等),
        # 但必须消耗预算 — aborted 计数现含 failed (见 _get_today_aborted).
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
    而是直接 DELETE。保留 ok 行 (已完成的工作), 保留 live-PID 行 (当前进程自己的任务)。
    这样 restart 后新 orchestrator 从干净状态开始, 重试计数器自然归零。
    """
    import os as _os
    my_pid = os.getpid()
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
# Runner 基类 & 三个具体 Runner
# ══════════════════════════════════════════════════════════════════════

class BaseRunner:
    """Runner 基类 — 共用配置和工具方法."""

    def __init__(self, today: str):
        self.today = today
        self.status = _get_today_status(today)
        self.aborted = _get_today_aborted(today)
        self.hhmm = time(datetime.now().hour, datetime.now().minute)
        self.weekday = datetime.now().weekday()

    def _should_run(self, s: TaskSpec) -> bool:
        return _should_run(s, self.hhmm, self.weekday, self.status, self.aborted)

    def _dispatch(self, s: TaskSpec, run_fn: Callable) -> None:
        """调度执行器 — 调用任务模块的 _run()."""
        from quant.utils.logger import get_logger, set_trace_id
        import uuid as _uuid
        tid = _uuid.uuid4().hex[:12]
        set_trace_id(tid)
        rid = _tk_start(s.name, self.today, grace_seconds=s.grace_s)
        if rid is None:
            _log.info(f"[{s.name}] already running, skip")
            return
        try:
            if s.mode == "subprocess":
                import importlib as _importlib
                mod = _importlib.import_module(s.subprocess_cmd)
            elif s.name in ("snapshot_open", "snapshot_close"):
                from quant.scheduler.snapshot import snapshot_open, snapshot_close
                fn = snapshot_open if s.name == "snapshot_open" else snapshot_close
                fn(self.today)
                _tk_finish(s.name, self.today, "ok")
                return
            else:
                # inline: quant.scheduler.{task_name}
                mod = __import__(f"quant.scheduler.{s.name}", fromlist=["_run"])
            mod._run(self.today)
            _tk_finish(s.name, self.today, "ok")
        except Exception as e:
            _log.exception(f"[{self.today}] {s.name} crashed: {e}")
            _tk_finish(s.name, self.today, "failed", error=str(e))
            raise


class InlineRunner(BaseRunner):
    """Inline Runner — 进程内同步执行 inline 任务.

    处理: signals, execute, snapshot_open, snapshot_close, reconcile
    """

    def run(self) -> None:
        """按 _PLAN_ORDER 顺序执行所有 inline 任务."""
        for name in _PLAN_ORDER:
            s = ALL.get(name)
            if s is None or s.mode != "inline":
                continue
            if not self._should_run(s):
                continue
            self._dispatch(s, None)

    def run_once(self, name: str) -> None:
        """强制执行单个 inline 任务 (用于手动触发)."""
        s = ALL.get(name)
        if s is None or s.mode != "inline":
            raise ValueError(f"task {name} not found or not inline")
        self._dispatch(s, None)


class MonitorRunner(BaseRunner):
    """Monitor Runner — 盘中长驻窗口任务 (09:30-15:00).

    - 窗口内保活 daemon 线程
    - 午休内部暂停
    - 窗口结束自退
    """

    def __init__(self, today: str):
        super().__init__(today)
        self._monitor_thread: Optional[_thr.Thread] = None
        self._monitor_stop = _thr.Event()

    def run(self) -> None:
        """启动 monitor daemon，阻塞至窗口结束."""
        s = ALL.get("monitor")
        if s is None:
            _log.warning("monitor task not found in manifest")
            return

        if not self._should_run(s):
            _log.info(f"[{self.today}] monitor not in window or deps not met")
            return

        self._monitor_stop.clear()
        self._monitor_thread = _thr.Thread(
            target=self._monitor_daemon, args=(self.today,),
            daemon=True, name="monitor-daemon"
        )
        self._monitor_thread.start()
        _log.info(f"[{self.today}] monitor daemon started")

        # 阻塞等待窗口结束或停止信号
        s_spec = ALL["monitor"]
        while s_spec.in_window(time(datetime.now().hour, datetime.now().minute), datetime.now().weekday()):
            if self._monitor_stop.is_set():
                break
            _time.sleep(1)

        # 窗口结束，停止线程
        self.stop()
        # v555: 窗口结束必须写 ok — 原仅 log, task_runs 永卡 running,
        # web 显示"盘中风控运行中" (15:01 实证 2026-08-19).
        # daemon 崩溃路径已在 _monitor_daemon 写 failed, 此处兜底成功路径.
        _tk_finish("monitor", self.today, "ok")
        _log.info(f"[{self.today}] monitor window ended")

    def _monitor_daemon(self, today: str):
        """盘中循环: 每 30s 轮询 止损/止盈/熔断."""
        from quant.scheduler.monitor import _run_continuous_inner
        _log.info(f"[{today}] monitor_loop started")
        try:
            _run_continuous_inner(today, self._monitor_stop)
        except Exception as e:
            # B23 (2026-08-18): 崩溃必须写 failed 行 — 原仅 log, 行永卡 running,
            # 风暴保护 _get_monitor_failures 恒 0, orchestrator 永不重启,
            # 当日盘中风控静默丢失且无告警.
            _log.exception(f"[{today}] monitor_loop crashed: {e}")
            try:
                _tk_finish("monitor", today, "failed", error=str(e))
            except Exception as _e2:
                _log.warning(f"[{today}] monitor finish failed: {_e2}")
        finally:
            _log.info(f"[{today}] monitor_loop ended")

    def is_alive(self) -> bool:
        """B23: 内部 daemon 线程是否存活 — orchestrator 据此重置并重启."""
        return bool(self._monitor_thread and self._monitor_thread.is_alive())

    def stop(self) -> None:
        """停止 monitor daemon (B23: 原空操作, stop event 全程序无人 set)."""
        self._monitor_stop.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=5)


class SubprocessRunner(BaseRunner):
    """Subprocess Runner — 独立子进程 (晚间链/周度评估).

    - 非阻塞轮询
    - 失败重试 (最多 _MAX_TASK_RETRIES)
    - 子进程崩溃清理残留子任务
    """

    def __init__(self, today: str):
        super().__init__(today)
        self._proc: Optional[subprocess.Popen] = None
        self._retries = 0
        self._done = False
        self._last_rc: Optional[int] = None  # v532: 子进程最终退出码 (orchestrator 决策重试)

    def run_evening_chain(self) -> bool:
        """启动晚间链 subprocess (daily_data → factor_cache → attribution → lgb_train → xgb_train → adj_factor).

        v532: 阻塞等待完成并返回成败 (orchestrator 决策重试)。
        返回 True=成功 / False=失败 (重试预算已耗尽)。
        """
        s = ALL.get("evening_chain")
        if s is None:
            _log.warning("evening_chain not found in manifest")
            return False

        if not self._should_run(s):
            _log.info(f"[{self.today}] evening_chain not in window or deps not met")
            return False

        _log.info(f"[{self.today}] 19:00 — spawning evening chain subprocess")
        self._run_subprocess(s)
        self._wait_done(s)
        return self._last_rc == 0

    def run_weekly_eval(self) -> bool:
        """启动周度评估 subprocess (周六 06:00-12:00). 阻塞等待返回成败 (v532)."""
        s = ALL.get("weekly_eval")
        if s is None:
            _log.warning("weekly_eval not found in manifest")
            return False

        if not self._should_run(s):
            _log.info(f"[{self.today}] weekly_eval not in window or deps not met")
            return False

        _log.info(f"[{self.today}] 06:00 — spawning weekly eval subprocess")
        self._run_subprocess(s)
        self._wait_done(s)
        return self._last_rc == 0

    def run_daily_repair(self) -> bool:
        """启动早间补拉链 subprocess (每日 08:00, 交易日与非交易日均运行).
        阻塞等待返回成败 (v532)."""
        s = ALL.get("daily_repair")
        if s is None:
            _log.warning("daily_repair not found in manifest")
            return False

        if not self._should_run(s):
            _log.info(f"[{self.today}] daily_repair not in window or deps not met")
            return False

        _log.info(f"[{self.today}] 08:00 — spawning daily repair subprocess")
        self._run_subprocess(s)
        self._wait_done(s)
        return self._last_rc == 0

    def _wait_done(self, s: TaskSpec) -> None:
        """阻塞轮询直到子进程完成 (含重试预算内自动重跑)."""
        while self._proc is not None:
            self._wait_subprocess(s)
            if self._proc is not None:
                _time.sleep(POLL)

    def _run_subprocess(self, s: TaskSpec) -> None:
        """启动并监控子进程."""
        if self._proc is not None:
            _log.warning("subprocess already running")
            return

        env = {**os.environ, "PYTHONPATH": ".", "_EVENING_SUBPROCESS": "1"}
        cmd = [".venv/bin/python3", "-c", s.subprocess_cmd + f"_run('{self.today}')"]
        cwd = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))

        self._proc = subprocess.Popen(cmd, cwd=cwd, env=env)
        _log.info(f"[{self.today}] spawned subprocess {s.name} (pid={self._proc.pid})")

        # 非阻塞轮询
        self._wait_subprocess(s)

    def _wait_subprocess(self, s: TaskSpec) -> None:
        """轮询子进程，处理重试和清理."""
        if self._proc is None:
            return

        ret = self._proc.poll()
        if ret is None:
            _log.info(f"[{self.today}] subprocess running, will poll next cycle")
            return

        self._proc = None
        self._last_rc = ret
        if ret == 0:
            _log.info(f"[{self.today}] subprocess exited OK")
        else:
            _log.warning(f"[{self.today}] subprocess failed (rc={ret}), cleanup")
            _cleanup_evening_children(self.today)
            # v532 fix: 原重试逻辑只计数不重跑 (死代码) — 晚间链失败后
            # orchestrator 把失败当成功 (exit(1) 后 _proc=None → done),
            # signals 次日用旧缓存。现在预算内真正重新 spawn。
            if self._retries < _MAX_TASK_RETRIES:
                self._retries += 1
                _log.warning(f"[{self.today}] retry {self._retries}/{_MAX_TASK_RETRIES} — respawning")
                self._run_subprocess(s)
            else:
                _log.error(f"[{self.today}] subprocess exhausted retries ({_MAX_TASK_RETRIES})")

    def cleanup(self) -> None:
        """清理残留进程."""
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()


# ══════════════════════════════════════════════════════════════════════
# 统一编排入口 — 供外部调用
# ══════════════════════════════════════════════════════════════════════

def run_inline_tasks(today: str) -> None:
    """运行所有 inline 任务 (供 orchestrator 调用)."""
    InlineRunner(today).run()


def run_monitor(today: str) -> None:
    """运行 monitor daemon (阻塞至窗口结束)."""
    MonitorRunner(today).run()


def run_evening_chain(today: str) -> None:
    """运行晚间链 subprocess."""
    SubprocessRunner(today).run_evening_chain()


def run_weekly_eval(today: str) -> None:
    """运行周度评估 subprocess."""
    SubprocessRunner(today).run_weekly_eval()


def run_daily_repair(today: str) -> None:
    """运行早间补拉链 subprocess."""
    SubprocessRunner(today).run_daily_repair()