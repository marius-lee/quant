"""Shared fixtures for all tests. 模板 3 (TDD, 软约束): 用 fixture 隔离外部依赖."""
import pytest
import pandas as pd
import numpy as np


@pytest.fixture
def sample_fundamentals():
    """100 只虚拟股票的基本面数据."""
    n = 500
    symbols = [f"{i:06d}" for i in range(1, n + 1)]
    df = pd.DataFrame({
        "pe": np.random.uniform(5, 50, n),
        "pe_ttm": np.random.uniform(5, 50, n),
        "pb": np.random.uniform(0.5, 10, n),
        "total_mv": np.random.uniform(1e9, 1e12, n),
        "roe": np.random.uniform(-0.5, 0.5, n),
        "industry": np.random.choice(["银行", "电子", "医药", "食品"], n),
        "high_52w": np.random.uniform(10, 500, n),
        "eps": np.random.uniform(0.1, 20, n),
        "bvps": np.random.uniform(1, 200, n),
        "close_latest": np.random.uniform(5, 400, n),
    }, index=symbols)
    return df


@pytest.fixture
def sample_financials():
    """10 只虚拟股票的财务报表数据 (合并三表).

    net_profit 从 total_owner_equities 推导 (roe ∈ [-0.3, 0.4]),
    net_operate_cash_flow 从 total_assets 推导 (accrual ∈ [-0.2, 0.3]),
    确保值通过 compute_* 的极端值过滤器.
    """
    n = 50
    symbols = [f"{i:06d}" for i in range(1, n + 1)]
    ta = np.random.uniform(1e9, 1e12, n)
    te = np.random.uniform(1e8, 5e11, n)
    df = pd.DataFrame({
        "stat_date": ["2025-12-31"] * n,
        "total_assets": ta,
        "total_liability": np.random.uniform(1e8, 5e11, n),
        "total_owner_equities": te,
        "net_profit": np.random.uniform(-0.3, 0.4, n) * te,
        "net_operate_cash_flow": np.random.uniform(-0.2, 0.3, n) * ta,
    }, index=symbols)
    return df
