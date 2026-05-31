"""共享 fixtures: 小型内存 SQLite 数据库 (10 stocks × 120 trading days)"""
import os, sys
import numpy as np
import pandas as pd
import pytest

# Ensure project root in path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def sample_ohlcv():
    """10只股票 × 120交易日的随机OHLCV数据"""
    np.random.seed(42)
    n_days, n_stocks = 120, 10
    symbols = [f"{i:06d}" for i in range(1, n_stocks + 1)]
    dates = pd.date_range("2024-01-01", periods=n_days, freq="B")

    # 随机游走生成价格
    r = np.random.randn(n_days, n_stocks) * 0.02 + 0.0005
    close = 10 * (1 + r).cumprod(axis=0)
    high = close * (1 + np.abs(np.random.randn(n_days, n_stocks) * 0.02))
    low = close * (1 - np.abs(np.random.randn(n_days, n_stocks) * 0.02))
    open_ = close * (1 + np.random.randn(n_days, n_stocks) * 0.005)
    volume = np.abs(np.random.randn(n_days, n_stocks) * 1e7 + 2e7)
    amount = close * volume

    data = {}
    for col, arr in [("open", open_), ("high", high), ("low", low),
                      ("close", close), ("volume", volume), ("amount", amount),
                      ("turnover", np.zeros_like(close))]:
        data[col] = pd.DataFrame(arr, index=dates, columns=symbols)
    return data


@pytest.fixture
def sample_close(sample_ohlcv):
    return sample_ohlcv["close"]


@pytest.fixture
def sample_factors(sample_close):
    """生成模拟因子DataFrame (date,stock) MultiIndex"""
    np.random.seed(42)
    dates = sample_close.index
    stocks = sample_close.columns.tolist()
    index = pd.MultiIndex.from_product([dates, stocks], names=["date", "stock"])

    n_factors = 10
    factor_names = [f"factor_{i:02d}" for i in range(n_factors)]
    data = np.random.randn(len(index), n_factors) * 0.5
    return pd.DataFrame(data, index=index, columns=factor_names)


@pytest.fixture
def sample_returns(sample_close):
    """5日向前收益 Series (date,stock) MultiIndex"""
    future_5d = sample_close.pct_change(5).shift(-5)
    return future_5d.stack(future_stack=True)
