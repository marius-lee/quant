"""v519: 因子晋升单一裁决入口 + 模拟盘期四重门槛回归测试.

覆盖:
  Fix 1  — EVAL_PASS 转移存在 (修复 phase5b 死路径)
  Fix 2  — phase5b 综合裁决 (p2+p3+p4+DSR) 实际落库 active
  Fix 3  — Phase 0 改为只读预判报告, 不直接改状态
  Fix 4  — probation→active: 观察期 + live consistency + DSR 门槛
  Fix 5  — DSR 未显著的三阶段全 pass 因子 → probation (模拟盘期), 不直升
"""
import sqlite3

import numpy as np
import pytest

from quant.config.paths import MARKET_DB

_TEST_PREFIX = "TX_TESTV519_"


def _inject_factor(name: str, status: str, ic_mean: float = 0.0) -> None:
    conn = sqlite3.connect(MARKET_DB, timeout=10)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO factor_registry "
            "(name, category, compute_fn, academic_source, status, status_reason, ic_mean, ic_ir)"
            " VALUES (?, 'test', 'test_fn', 'test', ?, 'v519 test fixture', ?, 0.0)",
            (name, status, ic_mean),
        )
        conn.commit()
    finally:
        conn.close()


def _cleanup(*names: str) -> None:
    conn = sqlite3.connect(MARKET_DB, timeout=10)
    try:
        for n in names:
            conn.execute("DELETE FROM factor_registry WHERE name=?", (n,))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def _p2_p3_p4_all_pass(monkeypatch):
    """monkeypatch load_latest → p2/p3/p4 三阶段全 pass (因子名参数化工厂)."""
    from types import SimpleNamespace
    state = SimpleNamespace(fname=_TEST_PREFIX + "F1")

    def fake_load(phase: str) -> dict:
        fname = state.fname
        if phase == "phase2":
            return {
                "active": [fname],
                "probation": [],
                "archived": {},
                "ic_means": {fname: 0.04},
                "ic_irs": {fname: 0.5},
                "decay": {},
                "meta": {},
            }
        if phase == "phase3":
            return {"kept": [fname], "marginal": [], "dropped": [],
                    "note": "", "oos_irs": [], "pbo_result": {}}
        if phase == "phase4":
            return {"final_factors": [fname], "marginal": [],
                    "dropped": [], "insufficient_data": False}
        return {}

    monkeypatch.setattr("quant.evaluation.run_store.load_latest", fake_load)
    return state


def _fake_ic_series(mean: float, std: float, n: int = 80, seed: int = 42):
    """构造 n 天日频 IC 序列 (确定性)."""
    rng = np.random.default_rng(seed)
    return [{"ic_value": float(v)} for v in rng.normal(mean, std, n)]


# ────────────────────────────────
# Fix 1 + Fix 5: 状态机转移表
# ────────────────────────────────

def test_eval_pass_transition_exists():
    from quant.factor.state_machine import _TRANSITIONS, _VALID_EVENTS, FactorEvent
    assert ("evaluating", FactorEvent.EVAL_PASS) in _TRANSITIONS
    assert _TRANSITIONS[("evaluating", FactorEvent.EVAL_PASS)] == "active"
    # 合法事件集合由转移表派生 — EVAL_PASS 必须可被 transition() 接受
    assert FactorEvent.EVAL_PASS in _VALID_EVENTS


# ────────────────────────────────
# Fix 2: phase5b 综合裁决实际落库
# ────────────────────────────────

def test_phase5_full_pass_with_dsr_promotes_evaluating(monkeypatch, _p2_p3_p4_all_pass):
    """三阶段全 pass + DSR 显著 → evaluating 因子实际晋升 active (EVAL_PASS)."""
    from quant.evaluation.phase5_monitor import sync_factor_status
    _p2_p3_p4_all_pass.fname = name = _TEST_PREFIX + "F1"
    _inject_factor(name, "evaluating", ic_mean=0.04)
    monkeypatch.setattr("quant.data.repos.factor_repo.FactorRepo.get_ic_rolling",
                        lambda self, fn, n_days=20, scope=None:  # v532: phase5 改用 backtest scope
                        _fake_ic_series(0.03, 0.015))
    try:
        result = sync_factor_status()
        assert name in result["active"], f"active list should contain {name}: {result}"
        conn = sqlite3.connect(MARKET_DB, timeout=10)
        row = conn.execute(
            "SELECT status, status_reason FROM factor_registry WHERE name=?", (name,)).fetchone()
        conn.close()
        assert row and row[0] == "active"
        assert "DSR significant" in str(row[1])
    finally:
        _cleanup(name)


def test_phase5_full_pass_no_dsr_goes_probation(monkeypatch, _p2_p3_p4_all_pass):
    """三阶段全 pass 但 DSR 不显著 (IC≈0 噪声) → probation 半权观察, 不直升 (v519)."""
    from quant.evaluation.phase5_monitor import sync_factor_status
    _p2_p3_p4_all_pass.fname = name = _TEST_PREFIX + "F2"
    _inject_factor(name, "evaluating", ic_mean=0.004)
    monkeypatch.setattr("quant.data.repos.factor_repo.FactorRepo.get_ic_rolling",
                        lambda self, fn, n_days=20, scope=None:  # v532: phase5 改用 backtest scope
                        _fake_ic_series(0.0, 0.05, seed=7))
    try:
        result = sync_factor_status()
        assert name not in result["active"]
        assert name in result["monitoring"], f"{name} should route to probation: {result}"
        conn = sqlite3.connect(MARKET_DB, timeout=10)
        row = conn.execute(
            "SELECT status, status_reason FROM factor_registry WHERE name=?", (name,)).fetchone()
        conn.close()
        assert row and row[0] == "probation"
        assert "DSR=degraded" in str(row[1])
    finally:
        _cleanup(name)


def test_phase5_probation_factor_untouched_by_eval(monkeypatch, _p2_p3_p4_all_pass):
    """实盘 probation 因子即使三阶段全 pass 也不被 phase5b 裁决 (归 attribution 通道)."""
    from quant.evaluation.phase5_monitor import sync_factor_status
    _p2_p3_p4_all_pass.fname = name = _TEST_PREFIX + "F3"
    _inject_factor(name, "probation", ic_mean=0.04)
    monkeypatch.setattr("quant.data.repos.factor_repo.FactorRepo.get_ic_rolling",
                        lambda self, fn, n_days=20, scope=None:  # v532: phase5 改用 backtest scope
                        _fake_ic_series(0.03, 0.015))
    try:
        result = sync_factor_status()
        assert name not in result["active"]
        assert name not in result["monitoring"]
        conn = sqlite3.connect(MARKET_DB, timeout=10)
        row = conn.execute(
            "SELECT status FROM factor_registry WHERE name=?", (name,)).fetchone()
        conn.close()
        assert row and row[0] == "probation"
    finally:
        _cleanup(name)


# ────────────────────────────────
# Fix 3: Phase 0 只读预判, 不直接改状态
# ────────────────────────────────

def test_phase0_report_only_keeps_status(monkeypatch):
    """evaluating 因子复查判定 would_archived, 但状态必须保持 evaluating (v519)."""
    from quant.scheduler.weekly import reevaluate_evaluating_factors
    name = _TEST_PREFIX + "F4"
    _inject_factor(name, "evaluating", ic_mean=0.001)

    def fake_load(phase: str) -> dict:
        if phase == "phase2":
            return {
                "ic_means": {name: 0.001},
                "ic_irs": {name: 0.0},
                "decay": {},
                "meta": {},
            }
        return {}

    monkeypatch.setattr("quant.evaluation.run_store.load_latest", fake_load)
    try:
        result = reevaluate_evaluating_factors("2026-08-15")
        assert result["would_archived"] >= 1
        assert result.get("note", "").startswith("report-only")
        conn = sqlite3.connect(MARKET_DB, timeout=10)
        row = conn.execute(
            "SELECT status FROM factor_registry WHERE name=?", (name,)).fetchone()
        conn.close()
        assert row and row[0] == "evaluating", "Phase 0 must not transition state"
    finally:
        _cleanup(name)


# ────────────────────────────────
# Fix 4: _promotion_eligible 四重门槛 (纯函数)
# ────────────────────────────────

def test_promotion_eligible_all_gates():
    from quant.scheduler.attribution import _promotion_eligible
    assert _promotion_eligible(
        n_trading_days=25, min_days=20, live_ic_mean=0.03, eval_ic=0.03,
        tolerance=0.02, dsr_verdict="significant", stable=True) == (True, "eligible")


def test_promotion_eligible_observation_period():
    from quant.scheduler.attribution import _promotion_eligible
    ok, why = _promotion_eligible(
        n_trading_days=10, min_days=20, live_ic_mean=0.03, eval_ic=0.03,
        tolerance=0.02, dsr_verdict="significant", stable=True)
    assert not ok and "observation" in why


def test_promotion_eligible_dsr_gate():
    from quant.scheduler.attribution import _promotion_eligible
    ok, why = _promotion_eligible(
        n_trading_days=25, min_days=20, live_ic_mean=0.03, eval_ic=0.03,
        tolerance=0.02, dsr_verdict="neutral", stable=True)
    assert not ok and "DSR" in why


def test_promotion_eligible_ic_consistency():
    from quant.scheduler.attribution import _promotion_eligible
    # 偏差超容差
    ok, why = _promotion_eligible(
        n_trading_days=25, min_days=20, live_ic_mean=0.06, eval_ic=0.03,
        tolerance=0.02, dsr_verdict="significant", stable=True)
    assert not ok and "deviates" in why
    # 符号翻转
    ok2, why2 = _promotion_eligible(
        n_trading_days=25, min_days=20, live_ic_mean=-0.03, eval_ic=0.03,
        tolerance=0.02, dsr_verdict="significant", stable=True)
    assert not ok2 and "sign flipped" in why2


def test_count_trading_days():
    """周末区间 → 0 交易日; 周一~周五 → 5 交易日."""
    from quant.scheduler.attribution import _count_trading_days
    n_wknd = _count_trading_days("2026-08-15", "2026-08-16")  # 周六→周日
    n_week = _count_trading_days("2026-08-10", "2026-08-14")  # 周一→周五
    assert n_wknd == 0
    assert n_week == 5