"""v416 (2026-08-08, v428 重构后修订): 周六周度评估调度断链回归测试.

背景: weekly_eval 触发块放在 orchestrator 主循环 `if not is_trading_day(): continue`
之后 — 周六不是交易日 → 循环体永远被短路, 周六 06:00 的周度因子评估自 v301
引入以来从未触发.

v416 修复: ① 触发块前移到 is_trading_day 之前且窗口放宽 06:00-12:00;
② 启动脚本改走 scheduler.start_all() 拉起独立 weekly 线程; ③ cron 恢复.

v428 重构: weekly 触发统一收编进 manifest (窗口 06:00-12:00, 周六) +
orchestrator `_should_run` 决策 (前置于 is_trading_day 短路); 独立 weekly 线程
删除 (双触发之源). 本测试: ① manifest 周六窗口在非交易日可达; ② 决策函数前置;
③ start_all 单一编排器; ④ cron 兜底保留.
"""
from datetime import time

import quant.scheduler.orchestrator as orch_mod
import quant.scheduler as sched_mod
from quant.scheduler.manifest import ALL


class TestSaturdayControlFlow:
    """v428: weekly_eval 由 manifest 窗口 + 决策函数前置保证周六可达."""

    def test_weekly_window_saturday_0600_1200(self):
        w = ALL["weekly_eval"]
        assert w.weekday == 5
        assert w.window[0] == time(6, 0)
        assert w.window[1] == time(12, 0), "窗口应覆盖 06:00-12:00 (周六 restart 补跑)"

    def test_weekly_trigger_decidable_on_non_trading_day(self):
        """周六 (非交易日) 决策函数返回 True — 不再依赖循环体可达."""
        assert orch_mod._should_run(ALL["weekly_eval"], time(9, 0), 5, {}, {}) is True

    def test_weekly_blocked_on_weekday(self):
        assert orch_mod._should_run(ALL["weekly_eval"], time(9, 0), 4, {}, {}) is False


def test_start_all_single_orchestrator(monkeypatch):
    """v428: start_all() 只启动 orchestrator (weekly 已并入 manifest, 无独立线程)."""
    started = []

    def _fake_orch():
        started.append("orchestrator")

    monkeypatch.setattr(sched_mod, "_start_orch", _fake_orch)
    sched_mod.start_all()
    assert started == ["orchestrator"]


def test_restart_script_uses_start_all():
    sh = open("scripts/restart.sh", encoding="utf-8").read()
    assert "from quant.scheduler import start_all" in sh, "restart.sh 未走 start_all"


def test_cron_script_has_weekly_and_adj_factor():
    sh = open("scripts/setup_cron.sh", encoding="utf-8").read()
    assert "run_task.sh weekly" in sh
    assert "run_task.sh adj_factor" in sh


if __name__ == "__main__":
    pass