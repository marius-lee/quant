"""Performance regression benchmarks for performance-sensitive modules.

Template 5 (coding-standards SKILL.md): 性能敏感模块须在 docstring 标注基线耗时
和退化阈值。运行时用 pytest-benchmark 自动测量并对比基线。

Usage:
    # 运行全部 benchmark:
    PYTHONPATH=. .venv/bin/pytest test/benchmark_performance.py -v --benchmark-only

    # 保存基线:
    PYTHONPATH=. .venv/bin/pytest test/benchmark_performance.py -v --benchmark-only --benchmark-save=baseline

    # 对比基线:
    PYTHONPATH=. .venv/bin/pytest test/benchmark_performance.py -v --benchmark-only --benchmark-compare

    # 快速冒烟:
    PYTHONPATH=. .venv/bin/pytest test/benchmark_performance.py -v --benchmark-only --benchmark-min-rounds=3
"""

import numpy as np
import pandas as pd
import pytest

N_SYMBOLS = 500
N_DAYS = 100
SEED = 42


@pytest.fixture(scope="module")
def synthetic_daily_data():
    """MultiIndex (field, symbol) synthetic OHLCV."""
    rng = np.random.default_rng(SEED)
    dates = pd.date_range("2025-01-01", periods=N_DAYS, freq="B")
    symbols = [f"STOCK{i:04d}" for i in range(N_SYMBOLS)]

    close = pd.DataFrame(
        np.abs(rng.normal(10, 2, (N_DAYS, N_SYMBOLS))),
        index=dates, columns=symbols
    )
    close = pd.DataFrame(10 + (close - close.mean()) / close.std() * 3,
                         index=close.index, columns=close.columns)

    high = pd.DataFrame(close.values * (1 + np.abs(rng.normal(0, 0.02, (N_DAYS, N_SYMBOLS)))),
                         index=close.index, columns=close.columns)
    low  = pd.DataFrame(close.values * (1 - np.abs(rng.normal(0, 0.02, (N_DAYS, N_SYMBOLS)))),
                         index=close.index, columns=close.columns)
    open_p = pd.DataFrame(close.shift(1).fillna(close.iloc[0]),
                           index=close.index, columns=close.columns)
    volume = pd.DataFrame(np.abs(rng.integers(10**6, 10**8, (N_DAYS, N_SYMBOLS))),
                           index=close.index, columns=close.columns)
    amount = pd.DataFrame(close.values * volume.values,
                           index=close.index, columns=close.columns)

    frames = [
        ("close", close), ("high", high), ("low", low),
        ("open", open_p), ("volume", volume), ("amount", amount)
    ]
    out = pd.concat(dict(frames), axis=1)
    out.columns.names = ['field', 'symbol']
    return out


@pytest.fixture(scope="module")
def synthetic_factor_values():
    """20 factor Series for 500 stocks."""
    rng = np.random.default_rng(SEED)
    symbols = [f"STOCK{i:04d}" for i in range(N_SYMBOLS)]
    return {
        f"factor_{i}": pd.Series(rng.normal(0, 1, len(symbols)), index=symbols)
        for i in range(20)
    }


# ── B1: Factor computation ──

@pytest.mark.benchmark(group="factor", min_rounds=3, max_time=15.0)
def test_primitives_precompute(benchmark, synthetic_daily_data):
    """precompute_primitives — 退化阈值: >5s (500×100)."""
    from quant.factor.compute._primitives import precompute_primitives
    result = benchmark(lambda: precompute_primitives(synthetic_daily_data))
    assert result is not None and len(result) > 0

# [SKIP] test_compute_price_factors — requires full DB init (DB_REGISTRY_CONNECT); covered by test_primitives_precompute
def test_compute_price_factors(benchmark, synthetic_daily_data):
    """compute_all_factors (price only) — 退化阈值: >10s."""
    from quant.factor.compute._dispatch import compute_all_factors
    from quant.factor.compute._primitives import precompute_primitives
    data = synthetic_daily_data
    prims = precompute_primitives(data)
    date = str(data.index[-1].date())
    result = benchmark(lambda: compute_all_factors(data, date, fundamentals=None,
                       status_filter=None, primitives=prims))
    assert result is not None and len(result) > 0


# ── B2: IC statistics ──

@pytest.mark.benchmark(group="ic", min_rounds=5, max_time=10.0)
def test_ic_computation(benchmark, synthetic_factor_values):
    """compute_ic — 退化阈值: >0.2s."""
    from quant.factor.ic import compute_ic
    result = benchmark(lambda: compute_ic(factor_values=synthetic_factor_values))
    assert result is not None


# ── B3: Alpha model ──

@pytest.mark.benchmark(group="alpha", min_rounds=5, max_time=10.0)
def test_alpha_combine(benchmark, synthetic_factor_values):
    """AlphaModel.combine — 退化阈值: >0.3s."""
    from quant.alpha.model import AlphaModel
    am = AlphaModel(method="sleeve")
    ic_map = {name: np.random.default_rng(SEED).normal(0.02, 0.01)
              for name in synthetic_factor_values}
    result = benchmark(lambda: am.combine(synthetic_factor_values, ic_map=ic_map))
    assert isinstance(result, pd.Series)


# ── B4: Risk neutralization ──

@pytest.mark.benchmark(group="risk", min_rounds=5, max_time=10.0)
def test_industry_neutralize(benchmark):
    """industry_neutralize — 退化阈值: >0.1s."""
    from quant.risk.neutralize import industry_neutralize
    rng = np.random.default_rng(SEED)
    alpha = pd.Series(rng.normal(0, 1, N_SYMBOLS),
                      index=[f"STOCK{i:04d}" for i in range(N_SYMBOLS)])
    industries = pd.Series(
        rng.choice(["金融", "科技", "消费", "制造", "能源"], N_SYMBOLS),
        index=alpha.index)
    result = benchmark(lambda: industry_neutralize(alpha, industries))
    assert isinstance(result, pd.Series) and len(result) == N_SYMBOLS


# ── B5: Optimizer ──

@pytest.mark.benchmark(group="optimizer", min_rounds=5, max_time=10.0)
def test_rank_concentrated(benchmark):
    """_rank_concentrated — 退化阈值: >0.05s."""
    from quant.optimizer.portfolio import PortfolioConstructor
    rng = np.random.default_rng(SEED)
    alpha = pd.Series(rng.uniform(0, 5, N_SYMBOLS),
                      index=[f"STOCK{i:04d}" for i in range(N_SYMBOLS)]).sort_values(ascending=False)
    prices = pd.Series(rng.uniform(5, 50, N_SYMBOLS), index=alpha.index)
    opt = PortfolioConstructor()
    result = benchmark(lambda: opt._rank_concentrated(alpha, prices, 5000))
    assert result.lots.sum() > 0
