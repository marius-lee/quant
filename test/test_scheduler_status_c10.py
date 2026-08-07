"""C10/C11 (CODE-REVIEW 2026-08-07): 调度任务状态契约回归.

C11: execute 调仓日无信号 = 业务空转 → 任务状态 ok (非 failed), 保留 no_targets metric.
修复前: finally 落 failed → /api/scheduler 显示红色"今日失败".
C10: lgb_train lightgbm 缺失 → 落 skipped 状态, /api/scheduler 渲染为"今日跳过"
(修复前落 has_cron → 误显示"未配置").

注意: execute/lgb_train 内部是函数内 import (`from quant.data.repos import TradeRepo`),
patch 必须打在来源模块 (quant.data.repos / quant.alpha.qlib_model) — 而非 task 模块.
"""
import pytest

import quant.alpha.qlib_model as qlib_mod
import quant.data.repos as repos_mod
import quant.execution.calendar as cal_mod
import quant.scheduler.execute as execute_mod
import quant.scheduler.lgb_train as lgb_mod


class _FakeRepo:
    def get_latest_signals(self):
        return None


class _FakeMetrics:
    key = None

    def inc(self, key):
        self.key = key


@pytest.fixture(autouse=True)
def _patch_daemons(monkeypatch):
    """必须 patch 源模块: _run() 内函数内 import 才生效."""
    orig_cal = cal_mod.is_rebalance_day
    orig_repo = repos_mod.TradeRepo
    orig_qlib = qlib_mod._check_lightgbm
    monkeypatch.setattr(cal_mod, "is_rebalance_day", lambda d: True)
    monkeypatch.setattr(repos_mod, "TradeRepo", _FakeRepo)
    monkeypatch.setattr(qlib_mod, "_check_lightgbm", lambda: False)
    yield
    cal_mod.is_rebalance_day = orig_cal
    repos_mod.TradeRepo = orig_repo
    qlib_mod._check_lightgbm = orig_qlib


def _run(module):
    """用假 start/finish/metrics 跑任务, 返回 (finish 参数, metric key)."""
    calls = {}
    fm = _FakeMetrics()

    def _fake_start(task, date, grace_seconds=120):
        return 1

    def _fake_finish(task, date, status, error=None, summary=None):
        calls["finish"] = (task, date, status, error, summary)

    orig_s, orig_f, orig_m = module._tk_start, module._tk_finish, module._m
    module._tk_start, module._tk_finish, module._m = _fake_start, _fake_finish, fm
    try:
        module._run("2026-08-07")
        return calls.get("finish"), fm.key
    finally:
        module._tk_start, module._tk_finish, module._m = orig_s, orig_f, orig_m


def test_execute_no_signals_status_ok():
    """调仓日无信号: 任务落 ok, no_targets metric, 不以 failed 收尾."""
    finish, metric_key = _run(execute_mod)
    assert finish is not None
    assert finish[2] == "ok"
    assert finish[4] == {"reason": "no signals", "targets": 0}
    assert metric_key == "scheduler.execute.no_targets"


def test_lgb_train_skipped_when_no_lightgbm():
    """lightgbm 缺失 → 状态 skipped + summary reason, metric skip."""
    finish, metric_key = _run(lgb_mod)
    assert finish is not None
    assert finish[2] == "skipped"
    assert finish[4] == {"reason": "lightgbm not installed"}
    assert metric_key == "scheduler.lgb_train.skip"