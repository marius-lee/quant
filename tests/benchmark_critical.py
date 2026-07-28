"""基准测试 — 覆盖所有关键热路径，防止性能退化。

用法:
    pytest tests/benchmark_critical.py --benchmark-only
    pytest tests/benchmark_critical.py --benchmark-only --benchmark-histogram=docs/benchmark
"""

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np


def test_bench_factorstore_load(benchmark):
    """FactorStore.load() — 因子缓存读取 (历史崩溃高发区)."""
    from quant.factor.store import FactorStore
    from quant.config.paths import FACTOR_CACHE_DB
    fs = FactorStore(db_path=FACTOR_CACHE_DB)
    result = benchmark(lambda: fs.load("2026-07-22", factor_names=None))
    assert isinstance(result, dict) and len(result) > 0
    fs.close()


def test_bench_datastore_get_daily(benchmark):
    """DataStore.get_daily() — 核心行情读取."""
    from quant.data.store import DataStore
    store = DataStore()
    symbols = ["600519", "000001", "300750", "601398", "000858",
               "600036", "601166", "600900", "002415", "300059"]
    result = benchmark(lambda: store.get_daily(symbols, start="2026-07-01", end="2026-07-22"))
    assert result is not None
    store.close()


import pytest
@pytest.mark.slow
def test_bench_compute_backtest_ic(benchmark):
    """compute_backtest_ic() — 回测 IC 计算."""
    from quant.factor.stats_cache import compute_backtest_ic
    result = benchmark(lambda: compute_backtest_ic("2026-07-15", n_train_days=30, status_filter="backtesting"))
    assert isinstance(result, dict)


def test_bench_bayesian_shrink(benchmark):
    """_bayesian_shrink_ic_map() — Bayesian IC 收缩."""
    from quant.factor.stats_cache import _bayesian_shrink_ic_map
    ic_map = {
        "momentum_63d": {"ic_mean": 0.03, "ic_ir": 0.25, "weight": 0.1},
        "reversal_5d": {"ic_mean": 0.02, "ic_ir": 0.18, "weight": 0.08},
        "volatility_126d": {"ic_mean": -0.01, "ic_ir": -0.12, "weight": 0.05},
        "amihud_250d": {"ic_mean": 0.015, "ic_ir": 0.15, "weight": 0.07},
        "zt_streak": {"ic_mean": 0.04, "ic_ir": 0.30, "weight": 0.12},
    }
    result = benchmark(lambda: _bayesian_shrink_ic_map(ic_map))
    assert len(result) == len(ic_map)


def test_bench_alphamodel_combine(benchmark):
    """AlphaModel.combine() — 因子合成."""
    from quant.alpha.model import AlphaModel
    am = AlphaModel()
    symbols = [f"{i:06d}" for i in range(100)]
    np.random.seed(42)
    factor_values = {
        "factor_a": pd.Series(np.random.randn(100), index=symbols),
        "factor_b": pd.Series(np.random.randn(100), index=symbols),
        "factor_c": pd.Series(np.random.randn(100), index=symbols),
    }
    ic_map = {"factor_a": 0.5, "factor_b": 0.3, "factor_c": 0.2}
    result = benchmark(lambda: am.combine(factor_values, ic_map=ic_map))
    assert len(result) > 0


def test_bench_traderepo_get_positions(benchmark):
    """TradeRepo.get_positions() — 持仓查询."""
    from quant.data.repos.trade_repo import TradeRepo
    from quant.config.paths import BACKTEST_DB
    repo = TradeRepo(db_path=BACKTEST_DB)
    result = benchmark(lambda: repo.get_positions(strategy="backtest_1"))
    assert isinstance(result, list)
