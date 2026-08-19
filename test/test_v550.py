# -*- coding: utf-8 -*-
"""v551: neutralize 不降级 — 单 None / 双 None / 样本不足一律抛错阻断 (B32),
数据不完整 = 风控缺失 = 硬失败. 修复被 v550 误实现的"降级投影"."""

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


def test_v551_mcap_none_raises():
    """market_caps=None → 抛错 (原 v550 降级为 industry-only, 撤销)."""
    with pytest.raises(ValueError, match="market_caps"):
        neutralize_factors_batch(_mk_factors(), industries=_IND, market_caps=None)


def test_v551_industry_none_raises():
    """industries=None → 抛错."""
    with pytest.raises(ValueError, match="industries"):
        neutralize_factors_batch(_mk_factors(), industries=None, market_caps=_MCAP)


def test_v551_both_none_raises():
    """两者都 None → 抛错 (原静默 return, 撤销)."""
    with pytest.raises(ValueError):
        neutralize_factors_batch(_mk_factors(), industries=None, market_caps=None)


def test_v551_both_present_ok():
    """两者都有 → 正常中性化, 输出有限且同索引."""
    fv = _mk_factors()
    out = neutralize_factors_batch(fv, industries=_IND, market_caps=_MCAP)
    assert set(out) == set(fv)
    assert all(list(s.index) == _SYMS for s in out.values())
    assert all(np.isfinite(s.dropna()).all() for s in out.values())


def test_v551_too_few_samples_raises():
    """样本不足 (_MIN_COMMON) → 抛错 (原 warning+skip 降级, 撤销)."""
    syms = [f"T{i:04d}" for i in range(5)]
    ind = pd.Series(["a"] * 5, index=syms)
    mcap = pd.Series(np.logspace(8, 9, 5), index=syms)
    with pytest.raises(ValueError, match="common stocks"):
        neutralize_factors_batch(
            {"f1": pd.Series(np.arange(5.0), index=syms)},
            industries=ind, market_caps=mcap)