"""v419 回归: phase5 sync_factor_status 不再因 NameError (f_repo 未定义) 崩溃.

根因: v346 重写 phase5_monitor 删除 FactorRepo 导入+初始化, 但保留
`f_repo.get_factor_by_name` 引用 → NameError, 每周六 phase5 必败.
修复: 改查 factor_registry(status 查询), 消除 repo 依赖.

测试策略: 全 mock — load_latest 注入假 phase2/3/4 数据,
DatabaseManager.market → 临时表, FactorStateManager 后续写库逻辑 stub 掉.
核心断言: 调用不抛 NameError 且流程完整.
"""
import sqlite3
import pytest


@pytest.fixture
def fake_market(monkeypatch, tmp_path):
    """临时 market.db: factor_registry 表 + DatabaseManager.market 指向它."""
    db = sqlite3.connect(tmp_path / "market.db")
    db.row_factory = sqlite3.Row
    db.execute("CREATE TABLE factor_registry (name TEXT PRIMARY KEY, status TEXT, "
               "retry_count INTEGER DEFAULT 0, ic_realm TEXT, status_reason TEXT)")
    db.execute("INSERT INTO factor_registry (name, status) VALUES ('F1', 'evaluating')")
    db.execute("INSERT INTO factor_registry (name, status) VALUES ('F2', 'probation')")
    db.commit()

    import quant.data.repos._base as _base
    monkeypatch.setattr(_base.DatabaseManager, "market", lambda: db)

    import quant.factor.state_manager as _sm
    class _StubFSM:
        def batch_transition(self, names, event, reason=None):
            return len(names)
        def transition(self, name, event, reason=None, retry_count=None):
            return True
    monkeypatch.setattr(_sm, "FactorStateManager", _StubFSM)
    return db


def _p2():
    return {"ic_means": {"F1": 0.001, "F2": 0.002},
            "ic_irs": {"F1": 0.1, "F2": 0.2},
            "active": [], "probation": ["F1", "F2"], "archived": {},
            "n_factors": 2}


def test_sync_no_f_repo_nameerror(fake_market, monkeypatch):
    """修复点: 全流程执行不抛 NameError (旧代码抛于 retired 分支)."""
    import quant.evaluation.phase5_monitor as ph5
    import quant.evaluation.run_store as run_store
    monkeypatch.setattr(run_store, "load_latest",
                        lambda name: {"phase2": _p2(), "phase3": None, "phase4": None}[name])
    cfg_default = {
        "factor.evaluation.max_retries": 3,
        "factor.evaluation.min_abs_ic": 0.02,
        "factor.evaluation.min_icir": 0.3,
    }
    monkeypatch.setattr(ph5, "_require_cfg", lambda k: cfg_default[k])

    result = ph5.sync_factor_status()
    assert isinstance(result, dict)
    assert "monitoring" in result


def test_sync_retired_path_picks_consistent_event(fake_market, monkeypatch):
    """retired 分支: F1(evaluating)→EVAL_FAIL(合法), F2(probation)→IC_PERSISTENT."""
    calls = []
    import quant.evaluation.phase5_monitor as ph5
    import quant.evaluation.run_store as run_store
    monkeypatch.setattr(run_store, "load_latest",
                        lambda name: {"phase2": _p2(), "phase3": None, "phase4": None}[name])
    monkeypatch.setattr(ph5, "_require_cfg", lambda k: 0.5)

    import quant.factor.state_manager as _sm
    class _RecorderFSM:
        def batch_transition(self, names, event, reason=None):
            return len(names)
        def transition(self, name, event, reason=None, retry_count=None):
            assert event in ("EVAL_FAIL", "IC_PERSISTENT")
            calls.append((name, event))
            return True
    monkeypatch.setattr(_sm, "FactorStateManager", _RecorderFSM)

    ph5.sync_factor_status()
    for name, event in calls:
        assert event == "IC_PERSISTENT", f"{name} got invalid event {event}"