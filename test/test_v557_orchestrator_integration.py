"""v557: orchestrator 主循环集成测试 — F3/F4/F6 驱动真实 _run() 循环.

方法: _run() 内所有依赖 (datetime.now / time.sleep / is_trading_day /
SubprocessRunner / MonitorRunner / _tk_finish / status) 均为函数内 import
或模块级名字 → monkeypatch 源模块即可注入, 无需改生产代码.
sleep 第 N 次调用抛 SystemExit 终止主循环 (每轮迭代恰 1 次 sleep).
"""
import sys
sys.path.insert(0, '.')

import datetime as dt_mod
import time as time_mod
import pytest


class _FakeClock(dt_mod.datetime):
    """固定时刻的时钟 (datetime.datetime 子类, now() 返回固定时间)."""

    _fixed = None

    @classmethod
    def now(cls, tz=None):
        return cls(cls._fixed.year, cls._fixed.month, cls._fixed.day,
                   cls._fixed.hour, cls._fixed.minute, cls._fixed.second)


def _run_loop(monkeypatch, now, max_sleeps, patch_callbacks=None):
    """驱动 orchestrator._run() 直到 sleep 次数上限 (SystemExit 终止).

    patch_callbacks: dict 名称→替换对象/函数, 注入到 orchestrator 依赖.
    返回 (sleeps, calls) — calls 为各 patch 记录字典.
    """
    import quant.scheduler.orchestrator as orch
    import quant.scheduler.runners as runners_mod
    import quant.scheduler.manifest as manifest_mod
    import quant.execution.calendar as cal_mod
    import quant.scheduler.status as status_mod

    calls = {}

    _FakeClock._fixed = now
    monkeypatch.setattr(dt_mod, "datetime", _FakeClock)

    sleeps = []

    def _fake_sleep(s):
        import threading
        # 仅主线程 (orchestrator 主循环) 计数; 后台线程 (state_broker
        # quote-refresh 等) 直接快进返回, 不计数不抛
        if threading.current_thread() is not threading.main_thread():
            return
        sleeps.append(s)
        if len(sleeps) >= max_sleeps:
            raise SystemExit("test-exit")

    monkeypatch.setattr(time_mod, "sleep", _fake_sleep)

    monkeypatch.setattr(cal_mod, "is_trading_day", lambda: patch_callbacks.get("is_trading_day", False))

    def _count(name):
        calls.setdefault(name, 0)
        calls[name] += 1
        return calls[name]

    # 函数内 import (from quant.scheduler.runners import ...) → patch 源模块
    monkeypatch.setattr(runners_mod, "_get_today_status", lambda d: patch_callbacks.get("status", {}))
    monkeypatch.setattr(runners_mod, "_get_today_aborted", lambda d: {})
    monkeypatch.setattr(runners_mod, "_get_monitor_failures", lambda d: 0)
    monkeypatch.setattr(runners_mod, "_cleanup_zombie_tasks", lambda: None)
    monkeypatch.setattr(runners_mod, "_cleanup_evening_children", lambda d: None)
    monkeypatch.setattr(runners_mod, "POLL", 30)
    # 模块级 import (orchestrator 顶部) → patch orchestrator 模块属性
    monkeypatch.setattr(orch, "_tk_start", patch_callbacks.get(
        "tk_start", lambda *a, **k: _count("tk_start")))
    monkeypatch.setattr(orch, "_tk_finish", patch_callbacks.get("tk_finish", lambda *a, **k: None))
    # v560 fix (2026-08-19): _check_timeouts 连真实 MARKET_DB — 集成测试
    # patch 的 manifest.ALL 不含 evening_chain → fallback 300s → 把生产
    # task_runs 里真实 running 的晚间链行误标 aborted (当晚实测 20:51:30
    # 生产 evening_chain 被测试进程标 timeout, 6666s > 300s)。
    # 超时判定逻辑已有独立单测 (test_v556_audit_fixes.py), 此处隔离不碰库。
    monkeypatch.setattr(orch, "_check_timeouts", lambda today: None)

    if "runner_cls" in patch_callbacks:
        monkeypatch.setattr(runners_mod, "SubprocessRunner", patch_callbacks["runner_cls"])
    if "monitor_cls" in patch_callbacks:
        monkeypatch.setattr(runners_mod, "MonitorRunner", patch_callbacks["monitor_cls"])
    if "manifest_all" in patch_callbacks:
        monkeypatch.setattr(manifest_mod, "ALL", patch_callbacks["manifest_all"])

    if "sleep_limit" in patch_callbacks:
        pass

    with pytest.raises(SystemExit, match="test-exit"):
        orch._run()
    return sleeps, calls


def _manifest(weekly=True, repair=True):
    """最小 manifest: 仅 weekly_eval/daily_repair (PLAN_ORDER 其余 get 不到即跳过)."""
    from datetime import time as _tm
    from quant.scheduler.manifest import TaskSpec
    all_tasks = {}
    if weekly:
        all_tasks["weekly_eval"] = TaskSpec(
            name="weekly_eval", label="因子评估(总)", schedule="周六 06:00",
            window=(_tm(6, 0), _tm(12, 0)), weekday=5, grace_s=43200,
            timeout_s=43200, mode="subprocess", subprocess_cmd="x")
    if repair:
        all_tasks["daily_repair"] = TaskSpec(
            name="daily_repair", label="早间补拉", schedule="08:00",
            window=(_tm(8, 0), _tm(8, 30)), grace_s=1800, timeout_s=1800,
            mode="subprocess", subprocess_cmd="x")
    return all_tasks


class TestF6WeeklyNonBlocking:
    """F6: weekly spawn 非阻塞 — 主循环继续轮询, repair 不被吞, 无重复 spawn."""

    def test_weekly_spawn_then_poll_then_done(self, monkeypatch):
        calls = {"spawn": 0, "poll": 0, "repair": 0, "done": 0}

        class FakeSubprocessRunner:
            def __init__(self, today):
                self.today = today
                self._proc = None
                self._last_rc = None

            def run_daily_repair(self):
                calls["repair"] += 1
                return True

            def _run_subprocess(self, s):
                calls["spawn"] += 1
                self._proc = object()

            def _wait_subprocess(self, s):
                calls["poll"] += 1
                if calls["poll"] >= 2:
                    self._proc = None
                    self._last_rc = 0
                    calls["done"] += 1

        sleeps, _ = _run_loop(
            monkeypatch, dt_mod.datetime(2026, 8, 22, 8, 5),  # 周六 08:05
            max_sleeps=5,
            patch_callbacks={
                "runner_cls": FakeSubprocessRunner,
                "manifest_all": _manifest(),
                "is_trading_day": False,
            },
        )
        # repair 优先执行且未被 weekly 阻塞吞掉
        assert calls["repair"] == 1, "repair 必须在 weekly 运行期正常触发"
        # weekly 恰 spawn 1 次 (无重复 spawn), 轮询 2 轮后完成
        assert calls["spawn"] == 1, f"weekly 重复 spawn: {calls['spawn']}"
        assert calls["poll"] == 2
        assert calls["done"] == 1
        # 主循环未被冻结 — 修复前 run_weekly_eval 阻塞时 sleep 永不计数
        assert len(sleeps) >= 4, f"主循环冻结: 仅 {len(sleeps)} 轮"

    def test_weekly_fail_retries_in_window(self, monkeypatch):
        calls = {"spawn": 0, "poll": 0, "done": 0}

        class FakeSubprocessRunner:
            def __init__(self, today):
                self.today = today
                self._proc = None
                self._last_rc = None

            def _run_subprocess(self, s):
                calls["spawn"] += 1
                self._proc = object()

            def _wait_subprocess(self, s):
                calls["poll"] += 1
                if calls["poll"] == 1:
                    self._proc = None
                    self._last_rc = 1  # 首次失败
                elif calls["poll"] == 2:
                    self._proc = None
                    self._last_rc = 0  # 重试成功
                    calls["done"] += 1

        sleeps, _ = _run_loop(
            monkeypatch, dt_mod.datetime(2026, 8, 22, 6, 30),  # 周六 06:30
            max_sleeps=6,
            patch_callbacks={
                "runner_cls": FakeSubprocessRunner,
                "manifest_all": _manifest(),
                "is_trading_day": False,
            },
        )
        assert calls["spawn"] == 2, "失败后窗口内必须重试 spawn"
        assert calls["done"] == 1
        assert len(sleeps) >= 4


class TestF3SubprocessCrashIsolated:
    """F3: 子进程构造/Popen 异常不得杀死主循环."""

    def test_runner_ctor_crash_continues(self, monkeypatch):
        calls = {"repair": 0, "spawn": 0}

        class ExplodingRunner:
            def __init__(self, today):
                raise RuntimeError("Popen failed")

        sleeps, _ = _run_loop(
            monkeypatch, dt_mod.datetime(2026, 8, 22, 8, 10),
            max_sleeps=4,
            patch_callbacks={
                "runner_cls": ExplodingRunner,
                "manifest_all": _manifest(),
                "is_trading_day": False,
            },
        )
        # 修复前: 异常冒泡 → _run() 直接退出 (无 SystemExit 之外的路径,
        # 主循环死亡); 修复后: try/except 包裹, 循环继续
        assert len(sleeps) >= 3, f"主循环被异常杀死: 仅 {len(sleeps)} 轮"

    def test_repair_crash_then_weekly_recovers(self, monkeypatch):
        calls = {"repair": 0, "spawn": 0}

        class FlakyRunner:
            def __init__(self, today):
                self.today = today
                self._proc = None
                self._last_rc = None

            def run_daily_repair(self):
                calls["repair"] += 1
                if calls["repair"] == 1:
                    raise OSError("disk full")
                return True

            def _run_subprocess(self, s):
                calls["spawn"] += 1
                self._proc = object()

            def _wait_subprocess(self, s):
                self._proc = None
                self._last_rc = 0

        sleeps, _ = _run_loop(
            monkeypatch, dt_mod.datetime(2026, 8, 22, 8, 5),
            max_sleeps=6,
            patch_callbacks={
                "runner_cls": FlakyRunner,
                "manifest_all": _manifest(),
                "is_trading_day": False,
            },
        )
        # repair 首轮异常被捕获 → 下轮重试成功; weekly 不受影响照常完成
        assert calls["repair"] == 2
        assert calls["spawn"] == 1
        assert len(sleeps) >= 4


class TestF4MonitorCrashRestart:
    """F4: B23 窗口内 daemon 崩溃 → 兜底 failed → 下一轮重启 (闭环)."""

    def test_in_window_crash_marks_failed_and_restarts(self, monkeypatch):
        from datetime import time as _tm
        from quant.scheduler.manifest import TaskSpec

        calls = {"monitor_spawn": 0, "finish": []}
        status = {}  # 可变: fake _tk_start 落 running → _tk_finish 落 failed

        class FakeMonitorRunner:
            def __init__(self, today):
                self.today = today

            def is_alive(self):
                return False  # daemon 恒假死 (崩溃)

            def run(self):
                calls["monitor_spawn"] += 1

            def stop(self):
                pass

        def fake_tk_finish(task, date, st, error=None, summary=None):
            calls["finish"].append((st, error))
            # 模拟真实落库: 状态从 running → failed (供 _should_run 重试)
            if st == "failed":
                status["monitor"] = "failed"

        def fake_tk_start(task, date, **kw):
            calls.setdefault("tk_start", 0)
            calls["tk_start"] += 1
            # 模拟真实落库: spawn 后状态 running (供 B23 检测)
            status["monitor"] = "running"

        all_tasks = _manifest(weekly=False, repair=False)
        all_tasks["monitor"] = TaskSpec(
            name="monitor", label="盘中风控", schedule="09:35-15:00",
            window=(_tm(9, 30), _tm(15, 0)), grace_s=21600, timeout_s=21600,
            mode="monitor")

        sleeps, _ = _run_loop(
            monkeypatch, dt_mod.datetime(2026, 8, 19, 10, 0),  # 周三 10:00 窗口内
            max_sleeps=7,
            patch_callbacks={
                "monitor_cls": FakeMonitorRunner,
                "manifest_all": all_tasks,
                "is_trading_day": True,
                "status": status,
                "tk_finish": fake_tk_finish,
                "tk_start": fake_tk_start,
            },
        )
        # 轮1: monitor 启动; 轮2: B23 检测到线程死 + running + 窗口内 → finish failed
        assert ("failed", "monitor daemon died in window") in calls["finish"], \
            f"窗口内崩溃必须兜底 failed (触发重试), got {calls['finish']}"
        # 轮3: _should_run 对 failed 预算内 → 重启 (第二个 runner 实例)
        assert calls["monitor_spawn"] >= 2, \
            f"崩溃后必须重启, 仅 spawn {calls['monitor_spawn']} 次"
        assert len(sleeps) >= 4

    def test_out_of_window_crash_marks_ok(self, monkeypatch):
        from datetime import time as _tm
        from quant.scheduler.manifest import TaskSpec

        calls = {"finish": []}
        status = {}

        class FakeMonitorRunner:
            def __init__(self, today):
                self.today = today

            def is_alive(self):
                return False

            def run(self):
                pass

            def stop(self):
                pass

        def fake_tk_finish(task, date, st, error=None, summary=None):
            calls["finish"].append((st, error))
            if st == "ok":
                status["monitor"] = "ok"

        def fake_tk_start(task, date, **kw):
            calls.setdefault("tk_start", 0)
            calls["tk_start"] += 1
            status["monitor"] = "running"
            # 模拟真实时序: 窗口内 spawn → 窗口结束后线程死. 时钟推进到 15:31 (窗口外)
            _FakeClock._fixed = dt_mod.datetime(2026, 8, 19, 15, 31)

        all_tasks = _manifest(weekly=False, repair=False)
        all_tasks["monitor"] = TaskSpec(
            name="monitor", label="盘中风控", schedule="09:35-15:00",
            window=(_tm(9, 30), _tm(15, 0)), grace_s=21600, timeout_s=21600,
            mode="monitor")

        sleeps, _ = _run_loop(
            monkeypatch, dt_mod.datetime(2026, 8, 19, 10, 0),  # spawn 时窗口内
            max_sleeps=5,
            patch_callbacks={
                "monitor_cls": FakeMonitorRunner,
                "manifest_all": all_tasks,
                "is_trading_day": True,
                "status": status,
                "tk_finish": fake_tk_finish,
                "tk_start": fake_tk_start,
            },
        )
        # 窗口外线程死 = 正常自退 → 兜底 ok; 不重启 (monitor_done)
        assert ("ok", None) in calls["finish"], f"窗口外应兜底 ok, got {calls['finish']}"
        assert calls["tk_start"] == 1, f"窗口外 ok 后不得重启, tk_start={calls['tk_start']}"
        assert len(sleeps) >= 3