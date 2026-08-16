"""v523: 财务因子 aux 快路径 vs fallback (全史 SQL) 一致性回归。
口径说明 (v523-A2/A2b 定论):
- sue: 快路径窗口 = financial_lookback_days (1100d); 次新股 (301/688/603 等新板)
  招股书期 EPS (股本小 → 超大值) 在窗口外, tail(8) 只取上市后连续报告 → 与
  fallback 全史 (含 IPO 前不可比期) 存在系统性差。横截面序一致 (RankIC>=0.97),
  差异票均为次新股, 接受为口径改进 (fast 口径更符合 SUE 定义)。
- financial_anomaly / asset_growth: 全对称 (RankIC=1.0, maxdiff≈0)。
"""
import numpy as np
import pandas as pd
import pytest

from quant.data.store import DataStore
from quant.factor.compute._preload import preload_aux_data_chunk
from quant.factor.compute.fundamental import (
    compute_asset_growth,
    compute_financial_anomaly,
    compute_sue,
)

DATE = "2026-08-14"
RANKIC_MIN = 0.97


@pytest.fixture(scope="module")
def universe():
    st = DataStore()
    return [r[0] for r in st._connect().execute("SELECT symbol FROM stocks")]


@pytest.fixture(scope="module")
def aux_store(universe):
    return preload_aux_data_chunk(universe, "2025-08-04", DATE)


@pytest.fixture(scope="module")
def fundamentals(universe):
    return DataStore().get_fundamentals(universe, DATE)


def _consistency(fn, fundamentals, aux_store):
    fast = fn(fundamentals, DATE, aux=aux_store).dropna()
    slow = fn(fundamentals, DATE, aux=None).dropna()
    common = fast.index.intersection(slow.index)
    assert len(fast.index.difference(slow.index)) == 0, "fast-only 票不应存在"
    rankic = fast[common].rank().corr(slow[common].rank())
    return rankic, len(common), len(slow.index.difference(fast.index))


def test_sue_fast_slow_consistency(fundamentals, aux_store):
    rankic, common, _ = _consistency(compute_sue, fundamentals, aux_store)
    assert rankic >= RANKIC_MIN, f"sue RankIC(fast,slow)={rankic:.4f} 低于 {RANKIC_MIN}"
    assert common > 5000


def test_financial_anomaly_fast_slow_consistency(fundamentals, aux_store):
    rankic, common, slow_only = _consistency(compute_financial_anomaly, fundamentals, aux_store)
    assert rankic >= 0.999, f"financial_anomaly RankIC={rankic:.6f}"
    assert common > 5000
    assert slow_only < 50, "slow-only 应为窗口外 (1100d 无行) 的老/退市票, 少量"


def test_asset_growth_fast_slow_consistency(fundamentals, aux_store):
    rankic, common, _ = _consistency(compute_asset_growth, fundamentals, aux_store)
    assert rankic >= 0.999, f"asset_growth RankIC={rankic:.6f}"
    assert common > 5000