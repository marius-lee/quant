"""test-v252 冒烟 — 验证 ALG1-5 + A1/A3 导入及基本功能"""
import numpy as np, pandas as pd, sys

passed = 0
def check(label, fn):
    global passed
    try:
        print(f"  {label}...", end=" ", flush=True)
        fn()
        passed += 1
        print("OK")
    except Exception as e:
        print(f"FAIL\n    {e}")

# ── 1. ALG1 sigmoid ──
def t1():
    from quant.alpha.model import AlphaModel
    am = AlphaModel(method="sleeve")
    x = pd.Series(np.random.randn(1000))
    y = am.combine({"test": x})
    assert y.notna().any(), "sigmoid combine returned empty"
check("ALG1 sigmoid", t1)

# ── 2. ALG2 Bayesian (heavy import chain) ──
def t2():
    from quant.factor.stats_cache import _bayesian_shrink_ic_map
    ic = {"f1": 0.05, "f2": 0.03, "f3": -0.01, "f4": 0.07, "f5": 0.02}
    shrunk = _bayesian_shrink_ic_map(ic)
    assert len(shrunk) == len(ic)
    raw_max = max(ic.values())
    shrunk_max = max(shrunk.values())
    assert shrunk_max < raw_max, f"extreme IC not shrunk: {raw_max:.3f} → {shrunk_max:.3f}"
check("ALG2 Bayesian IC", t2)

# ── 3. ALG3 HRP ──
def t3():
    from quant.optimizer.hrp import hrp_weights
    cov = np.array([[0.0004, 0.0002, 0.0001],
                    [0.0002, 0.0005, 0.00015],
                    [0.0001, 0.00015, 0.0003]])
    w = hrp_weights(cov)
    assert abs(w.sum() - 1.0) < 1e-6
    assert len(w) == 3
check("ALG3 HRP optimizer", t3)

# ── 4. ALG5 Kelly ──
def t4():
    from quant.optimizer.kelly import compute_kelly_fractions, compute_lot_allocation
    alpha = pd.Series({"s1": 0.04, "s2": 0.06, "s3": 0.90})
    prices = pd.Series({"s1": 10, "s2": 20, "s3": 30})
    # ALG5a: pure kelly fractions (no capital constraint)
    w = compute_kelly_fractions(alpha)
    assert abs(w.sum() - 1.0) < 1e-6
    # ALG5b: lot allocation with capital constraint
    lots, remaining = compute_lot_allocation(alpha, prices, capital=5000)
    assert lots.sum() >= 0
    assert remaining < 5000
check("ALG5 Kelly dynamic", t4)

# ── 5. A1/A3 PipelineContext ──
def t5():
    from quant.core.context import PipelineContext
    ctx = PipelineContext(suppress_push=True, db_path="test.db")
    assert ctx.suppress_push and ctx.db_path == "test.db"
check("A1/A3 PipelineContext", t5)

# ── 6. ALG4 Harvey ──
def t6():
    from quant.config.constants import _require_cfg
    t = _require_cfg("factor.harvey_t_threshold")
    assert float(t) == 3.0, f"expected 3.0, got {t}"
check("ALG4 Harvey config", t6)

# ── 7. Full pipeline import ──
def t7():
    from quant.pipeline import generate_signals
    assert callable(generate_signals)
check("Pipeline import", t7)

print(f"\n{'='*50}")
print(f"  {passed}/7 PASSED" if passed == 7 else f"  {passed}/7 — {7-passed} FAILED")
print(f"{'='*50}")
sys.exit(0 if passed == 7 else 1)
