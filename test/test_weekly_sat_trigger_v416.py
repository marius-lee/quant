"""v416 (2026-08-08): 周六周度评估调度断链回归测试.

背景: weekly_eval 触发块放在 orchestrator 主循环 `if not is_trading_day(): continue`
之后 — 周六不是交易日 → 循环体永远被短路, 周六 06:00 的周度因子评估自 v301
引入以来从未触发 (三路触发源全断: orchestrator 分支不可达 + _weekly_loop 线程
无人调用 + crontab 实际为空).

修复: ① 触发块前移到 is_trading_day 之前且窗口放宽 06:00-12:00; ② 启动脚本
改走 scheduler.start_all() 拉起独立 weekly 线程; ③ cron 恢复; ④ 内部日志诊断.

本测试: ① 控制流 — 读取 orchestrator 源码验证触发块位于非交易日短路之前;
       ② 依赖 — 验证 start_all() 同时启动 orchestrator + weekly 线程;
       ③ 窗口 — 触发窗口周六 06:00-12:00 覆盖早间 restart 场景.
"""
import re
from datetime import time

import quant.scheduler.orchestrator as orch_mod
import quant.scheduler as sched_mod


def _source(p):
    return open(p, encoding="utf-8").read()


def _strip_comments(s):
    s = re.sub(r"#.*$", "", s, flags=re.M)
    s = re.sub(r'"""(?:.|\n)*?"""', "", s, flags=re.M)
    return s


def _orch_source():
    return _strip_comments(_source(orch_mod.__file__))


class TestSaturdayControlFlow:
    """v416-C1: weekly_eval 触发块必须位于 is_trading_day continue 之前."""

    def test_saturday_branch_before_trading_day_gate(self):
        src = _orch_source()
        # 周六分支出现的位置 (去掉注释后直接查触发条件)
        idx_branch = src.index("weekly_eval")
        idx_gate = src.index("if not is_trading_day():")
        assert idx_branch < idx_gate, (
            "v416 修复要求 weekly_eval 触发块位于 is_trading_day continue 之前"
        )

    def test_branch_code_inside_loop(self):
        src = _orch_source()
        # 触发块在 `def _run` 的 while True 循环体内 (匹配更精确)
        idx_fn = src.index("def _run(")
        idx_while = src.index("while True:", idx_fn)
        idx_branch = src.index("weekly_eval", idx_while)
        assert idx_branch > idx_while

    def test_window_covers_restart_after_0600(self):
        src = _orch_source()
        m = re.search(
            r"now\.weekday\(\) == 5 and time\(6, 0\) <= hhmm < time\((\d+), 0\)",
            src,
        )
        assert m, "周六触发窗口未找到"
        assert int(m.group(1)) == 12, "窗口应覆盖 06:00-12:00 (周六 restart 补跑)"


def test_start_all_launches_both_threads(monkeypatch):
    """v416-C2: scheduler.start_all() 必须同时启动 orchestrator + weekly 线程."""
    started = []

    def _fake_orch():
        started.append("orchestrator")

    def _fake_weekly():
        started.append("weekly")

    monkeypatch.setattr(sched_mod, "_start_orch", _fake_orch)
    monkeypatch.setattr(sched_mod, "_weekly_loop", _fake_weekly)
    sched_mod.start_all()
    assert started == ["orchestrator", "weekly"]


def test_restart_script_uses_start_all():
    sh = open("scripts/restart.sh", encoding="utf-8").read()
    assert "from quant.scheduler import start_all" in sh, "restart.sh 未走 start_all"


def test_cron_script_has_weekly_and_adj_factor():
    sh = open("scripts/setup_cron.sh", encoding="utf-8").read()
    assert "run_task.sh weekly" in sh
    assert "run_task.sh adj_factor" in sh


if __name__ == "__main__":
    pass