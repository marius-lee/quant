"""P0-3 (CODE-REVIEW 2026-08-07): 评估链断链回归.

修复前:
  - phase7_wf._run_train_phase 调用 screen_factors(prefilter_from_diagnostics=) —
    该参数已不存在 → TypeError → 每 fold 恒空 → walk-forward 永不产出 fold
  - phase7 读 p2.get("passed"), phase2 输出键为 active (v346 对齐) → 恒空
  - run_store.save_phase n_passed 读 passed, phase2 无此键 → 恒 0
  - 失败路径返回 dict → run_walkforward `if not certified` 恒真 → 误入回测
"""
import pytest

from quant.evaluation.run_store import save_phase


class _FakeRepo:
    """代替 EvaluationRepo: 捕获 save 参数, 不碰真库."""

    def __init__(self):
        self.seen = []

    def save_evaluation(self, phase, data_json, n_factors, n_passed):
        self.seen.append({"phase": phase, "n_factors": n_factors,
                          "n_passed": n_passed})
        return 1

    def get_latest(self, phase):
        return None


@pytest.fixture
def fake_phase2_data():
    return {
        "active": ["size", "momentum_63d"],
        "probation": [],
        "archived": {},
        "ic_means": {},
        "ic_irs": {},
        "decay": {},
        "n_factors": 12,
    }


def test_save_phase_reads_active_key(fake_phase2_data, monkeypatch):
    """n_passed 从 active 键计数 (原读 passed → 恒 0)."""
    repo = _FakeRepo()
    monkeypatch.setattr("quant.data.repos.EvaluationRepo", lambda: repo)
    # quant.evaluation.run_store 里是 `from quant.data.repos import EvaluationRepo`
    import quant.data.repos as repos
    monkeypatch.setattr(repos, "EvaluationRepo", lambda: repo)

    save_phase("phase2", fake_phase2_data)
    assert repo.seen[0]["n_passed"] == 2


def test_phase7_no_stale_kwarg():
    """screen_factors 已无 prefilter_from_diagnostics → phase7 调用不得再传."""
    src = open("quant/evaluation/phase7_wf.py", encoding="utf-8").read()
    import re
    code = re.sub(r"#.*$", "", src, flags=re.M)
    assert "prefilter_from_diagnostics" not in code
    assert "p2.get(\"active\") or p2.get(\"passed\")" in code


def test_phase7_failure_returns_empty_list():
    """Phase 3/4 失败返回 [] 而非 dict (dict 恒真 → 误激活)."""
    src = open("quant/evaluation/phase7_wf.py", encoding="utf-8").read()
    assert 'return {"error"' not in src


def test_wf_window_injected_into_phase2_3():
    """训练窗口 (PIT) 注入到 screen_factors / validate_oos."""
    src = open("quant/evaluation/phase7_wf.py", encoding="utf-8").read()
    assert "screen_factors(eval_start=train_start, eval_end=train_end)" in src
    assert "validate_oos(eval_start=train_start, eval_end=train_end)" in src