# -*- coding: utf-8 -*-
"""v550: neutralize 单 None 降级修复 — market_caps/industries 缺一时
不再 AttributeError (None.dropna), 而是只投影可用维度 (industry-only / 市值-only).
v501 设计语义 "pivot 无 PIT → 列缺失, 下游 neutralize 自动降级" 的落实."""

import numpy as np
import pandas as pd
import pytest

from quant.risk.neutralize import neutralize_factors_batch

_SYMS = [f"S{i:04d}" for i in range(100)]
_IND = pd.Series(
    [f"ind{i % 8}" for i in range(100)], index=_SYMS, dtype="category"
)
_MCAP = pd.Series(np.logspace(8, 11, 100), index=_SYMS)


def _mk_factors():
    rng = np.random.default_rng(42)
    return {
        "f1": pd.Series(rng.normal(size=100), index=_SYMS),
        "f2": pd.Series(rng.normal(size=100), index=_SYMS),
    }


def test_v550_mcap_none_industry_only():
    """market_caps=None → industry-only 投影, 不崩, 输出与输入同索引."""
    fv = _mk_factors()
    out = neutralize_factors_batch(fv, industries=_IND, market_caps=None)
    assert set(out) == set(fv)
    assert all(list(s.index) == _SYMS for s in out.values())
    assert all(np.isfinite(s.dropna()).all() for s in out.values())


def test_v550_industry_none_mcap_only():
    """industries=None → 市值-only 投影, 不崩."""
    fv = _mk_factors()
    out = neutralize_factors_batch(fv, industries=None, market_caps=_MCAP)
    assert set(out) == set(fv)
    assert all(list(s.index) == _SYMS for s in out.values())


def test_v550_both_none_unchanged():
    """两者都 None → 原样返回 (batch 顶层语义)."""
    fv = _mk_factors()
    out = neutralize_factors_batch(fv, industries=None, market_caps=None)
    assert out is fv


def test_v550_both_present_batch_preserved():
    """两者都有 → 与原逻辑一致 (有效值非空且有限)."""
    fv = _mk_factors()
    out = neutralize_factors_batch(fv, industries=_IND, market_caps=_MCAP)
    assert set(out) == set(fv)
    assert all(np.isfinite(s.dropna()).all() for s in out.values())


def test_v550_mcap_none_with_nan_scores():
    """industry-only 投影下含 NaN 的因子 → 只对有效子集投影, 无 NaN 传染."""
    fv = _mk_factors()
    fv["f3"] = fv["f1"].copy()
    fv["f3"].iloc[10:20] = np.nan
    out = neutralize_factors_batch(fv, industries=_IND, market_caps=None)
    assert np.isfinite(out["f3"].dropna()).all()