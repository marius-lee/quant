"""v428 (2026-08-08): manifest 调度决策单元测试.

覆盖 `_should_run` 决策矩阵: 时间窗 / 星期 / 状态 (ok/failed/running) /
依赖 (depends_ok / depends_attempt) / 重试预算 (aborted 次数).
"""
from datetime import time

import quant.scheduler.orchestrator as orch
from quant.scheduler.manifest import ALL, EVENING_STAGE_GRACE


def _st(**kw):
    return {k: v for k, v in kw.items() if v is not None}


def _ab(**kw):
    return {k: v for k, v in kw.items() if v is not None}


class TestWindow:
    def test_signals_before_window(self):
        assert orch._should_run(ALL["signals"], time(7, 59), 0, {}, {}) is False

    def test_signals_in_window(self):
        assert orch._should_run(ALL["signals"], time(8, 30), 0, {}, {}) is True

    def test_close_snapshot_before_1500_not_triggered(self):
        """v428 核心: 尾盘快照 14:55 前不会在尾盘 5min 内误触发."""
        assert orch._should_run(ALL["snapshot_close"], time(14, 55), 0, {}, {}) is False

    def test_close_snapshot_at_1500_triggered(self):
        assert orch._should_run(ALL["snapshot_close"], time(15, 0), 0, {}, {}) is True

    def test_close_snapshot_after_window(self):
        assert orch._should_run(ALL["snapshot_close"], time(15, 6), 0, {}, {}) is False


class TestStateMachine:
    def test_ok_blocks_rerun(self):
        assert orch._should_run(ALL["signals"], time(9, 0), 0,
                                _st(signals="ok"), {}) is False

    def test_failed_allows_rerun_within_budget(self):
        """P0-11 fix: failed 允许在 max_retries 预算内重试 (原: 永不重试).
        _MAX_TASK_RETRIES=2: aborted<2 可重试, aborted>=2 阻塞."""
        assert orch._should_run(ALL["signals"], time(9, 0), 0,
                                _st(signals="failed"), {}) is True
        assert orch._should_run(ALL["signals"], time(9, 0), 0,
                                _st(signals="failed"), _st(signals=1)) is True
        assert orch._should_run(ALL["signals"], time(9, 0), 0,
                                _st(signals="failed"), _st(signals=2)) is False

    def test_running_blocks_retrigger(self):
        assert orch._should_run(ALL["signals"], time(9, 0), 0,
                                _st(signals="running"), {}) is False

    def test_aborted_allows_retry(self):
        assert orch._should_run(ALL["signals"], time(9, 0), 0,
                                _st(signals="aborted"), {}) is True


class TestDependencies:
    def test_execute_requires_signals_attempt(self):
        """execute 依赖 attempt[signals] — signals 今日尝试过即可 (原语义)."""
        assert orch._should_run(ALL["execute"], time(9, 30), 0, {}, {}) is False
        assert orch._should_run(ALL["execute"], time(9, 30), 0,
                                _st(signals="failed"), {}) is True
        assert orch._should_run(ALL["execute"], time(9, 30), 0,
                                _st(signals="aborted"), {}) is True

    def test_reconcile_runs_after_monitor_attempt(self):
        """P0-11 fix: reconcile 依赖 attempt[monitor] — monitor 完成(ok/failed)即可,
        运行中(running)或未尝试时等待."""
        assert orch._should_run(ALL["reconcile"], time(15, 5), 0,
                                _st(monitor="running"), {}) is False
        assert orch._should_run(ALL["reconcile"], time(15, 5), 0,
                                _st(monitor="ok"), {}) is True
        assert orch._should_run(ALL["reconcile"], time(15, 5), 0,
                                _st(monitor="failed"), {}) is True


class TestRetryBudget:
    def test_aborted_within_budget(self):
        assert orch._should_run(ALL["signals"], time(9, 0), 0, {},
                                _st(signals=1)) is True

    def test_aborted_exhausted(self):
        assert orch._should_run(ALL["signals"], time(9, 0), 0, {},
                                _st(signals=2)) is False


class TestMonitorWindow:
    def test_monitor_open_in_window(self):
        """monitor 更侧模式: 窗口内应保持运行."""
        assert orch._should_run(ALL["monitor"], time(9, 35), 0, {}, {}) is True
        assert orch._should_run(ALL["monitor"], time(14, 59), 0, {}, {}) is True

    def test_monitor_closed_after_1500(self):
        assert orch._should_run(ALL["monitor"], time(15, 1), 0, {}, {}) is False


class TestWeeklyWindow:
    def test_weekly_saturday_only(self):
        assert orch._should_run(ALL["weekly_eval"], time(6, 0), 5, {}, {}) is True
        assert orch._should_run(ALL["weekly_eval"], time(6, 0), 4, {}, {}) is False

    def test_weekly_window_0600_1200(self):
        assert orch._should_run(ALL["weekly_eval"], time(5, 59), 5, {}, {}) is False
        assert orch._should_run(ALL["weekly_eval"], time(12, 0), 5, {}, {}) is True
        assert orch._should_run(ALL["weekly_eval"], time(12, 1), 5, {}, {}) is False


class TestEveningStageGrace:
    """v474: v428 回归 — 晚间链 stage 不在 manifest.ALL, _check_timeouts
    fallback 300s 每晚误杀 daily_data (5-12min abort) → factor_cache 跳过
    → 次日 signals "factor_store empty". 各 stage 必须有 >300s 的兜底超时."""

    EVENING_CHILDREN = ["daily_data", "factor_cache", "attribution",
                        "lgb_train", "xgb_train", "adj_factor"]

    def test_all_evening_stages_have_grace(self):
        missing = [t for t in self.EVENING_CHILDREN if t not in EVENING_STAGE_GRACE]
        assert missing == []

    def test_evening_stage_grace_exceeds_fallback(self):
        for t in self.EVENING_CHILDREN:
            assert EVENING_STAGE_GRACE[t] > 300, f"{t} grace 仍会被 300s fallback 掩盖"

    def test_known_durations_covered(self):
        # 实测 (task_runs 2026-08-05~07): daily_data 最长 4.4h, factor_cache 4.6h
        assert EVENING_STAGE_GRACE["daily_data"] >= 6 * 3600
        assert EVENING_STAGE_GRACE["factor_cache"] >= 6 * 3600