"""C10/C11 (CODE-REVIEW 2026-08-07): 调度任务状态契约回归.

C11: execute 调仓日无信号 = 业务空转 → 任务状态 ok (非 failed), 保留 no_targets metric.
    状态落库已由 Runner._dispatch 统一管理 (无异常 → _tk_finish("ok")), 本测试验证
    _run 的业务空转 dict + metric.
C10: lgb_train lightgbm 缺失 → finish(skipped) + reason + metric skip.
    (lgb_train 内部经 task_log.start/finish 报状态 — patch 打在 task_log 模块.)

注意: execute/lgb_train 内部是函数内 import (`from quant.data.repos import TradeRepo`),
patch 必须打在来源模块 (quant.data.repos / quant.alpha.qlib_model).
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


def test_execute_no_signals_status_ok(monkeypatch):
    """调仓日无信号: _run 返回业务空转 dict + no_targets metric.

    状态落库由 Runner 统一管理: _run 无异常 → 状态 ok (runners.py _dispatch).
    """
    fm = _FakeMetrics()
    monkeypatch.setattr(execute_mod, "_m", fm)
    result = execute_mod._run("2026-08-07")
    assert result == {"reason": "no signals", "targets": 0}
    assert fm.key == "scheduler.execute.no_targets"


def test_lgb_train_skipped_when_no_lightgbm(monkeypatch):
    """lightgbm 缺失 → 状态 skipped + summary reason, metric skip."""
    calls = {}
    fm = _FakeMetrics()

    def _fake_start(task, date, grace_seconds=3600):
        return 1

    def _fake_finish(task, date, status, error=None, summary=None):
        calls["finish"] = (task, date, status, error, summary)

    # lgb_train 模块级绑定了 task_log.start/finish (import ... as _tk_start),
    # patch 须打在模块绑定名上
    monkeypatch.setattr(lgb_mod, "_tk_start", _fake_start)
    monkeypatch.setattr(lgb_mod, "_tk_finish", _fake_finish)
    monkeypatch.setattr(lgb_mod, "_m", fm)
    lgb_mod._run("2026-08-07")
    assert calls["finish"] is not None
    assert calls["finish"][2] == "skipped"
    assert calls["finish"][4] == {"reason": "lightgbm not installed"}
    assert fm.key == "scheduler.lgb_train.skip"