"""测试风险约束 — 流动性、股价、ST 过滤 + 持仓限制检查.

模板 3 (TDD): 确定性输入验证.
"""
import pytest
import pandas as pd
import numpy as np
from quant.risk.constraints import (
    RiskLimits, filter_by_liquidity, filter_by_price,
    filter_st_stocks, apply_all_filters, position_limit_check,
)


class TestFilterByLiquidity:
    """流动性过滤: min_daily_amount."""

    def test_filters_out_low_liquidity(self):
        df = pd.DataFrame({
            "amount": [50, 5000, 200],
            "close": [10, 20, 30],
        }, index=["A", "B", "C"])
        result = filter_by_liquidity(df, min_daily_amount=500_000)
        # amount in db unit = 千元, *1000 = 元
        # A: 50*1000=50000 < 500000 → removed
        # B: 5000*1000=5000000 >= 500000 → kept
        # C: 200*1000=200000 < 500000 → removed
        assert len(result) == 1
        assert "B" in result.index

    def test_all_pass(self):
        df = pd.DataFrame({
            "amount": [10000, 20000],
            "close": [10, 20],
        }, index=["A", "B"])
        result = filter_by_liquidity(df, min_daily_amount=1_000_000)
        # A: 10000K = 10M → pass, B: 20000K = 20M → pass
        assert len(result) == 2

    def test_no_amount_column(self):
        df = pd.DataFrame({"close": [10, 20]}, index=["A", "B"])
        with pytest.raises(Exception):
            filter_by_liquidity(df, min_daily_amount=500_000)


class TestFilterByPrice:
    """股价过滤: min_price."""

    def test_filters_out_penny_stocks(self):
        df = pd.DataFrame({"close": [0.5, 3.0, 1.5]}, index=["A", "B", "C"])
        result = filter_by_price(df, min_price=2.0)
        assert len(result) == 1
        assert "B" in result.index

    def test_all_pass_high_prices(self):
        df = pd.DataFrame({"close": [50, 100, 200]}, index=["A", "B", "C"])
        result = filter_by_price(df, min_price=2.0)
        assert len(result) == 3


class TestFilterSTStocks:
    """ST 股过滤."""

    def test_filters_st_stocks(self):
        df = pd.DataFrame({"close": [10, 20, 30]}, index=["A", "B", "C"])
        names = {"A": "平安银行", "B": "*ST 华信", "C": "ST 中安"}
        result = filter_st_stocks(df, stock_names=names)
        assert len(result) == 1
        assert "A" in result.index

    def test_no_stock_names_passes_all(self):
        df = pd.DataFrame({"close": [10, 20]}, index=["A", "B"])
        result = filter_st_stocks(df)
        assert len(result) == 2


class TestApplyAllFilters:
    """集成: 全部风险过滤."""

    def test_combined_filters(self):
        df = pd.DataFrame({
            "amount": [100, 5000, 10000, 500],
            "close": [1.0, 10.0, 50.0, 0.5],
        }, index=["A", "B", "C", "D"])
        # A: 100K=100K < 500K → liquid fail + price fail (1.0<2.0)
        # B: 5000K=5M → pass liquid, price=10 → pass price
        # C: 10000K=10M → pass liquid, price=50 → pass price
        # D: 500K=500K → pass liquid, price=0.5<2.0 → price fail
        limits = RiskLimits(
            min_daily_amount=500_000, min_price=2.0,
            exclude_star_st=True, max_single_position=0.05,
            max_positions=20, max_sector_exposure=0.40,
        )
        result = apply_all_filters(df, limits=limits)
        assert len(result) >= 2
        assert "B" in result.index
        assert "C" in result.index

    def test_uses_default_limits(self):
        """apply_all_filters 无 limits 参数时从 config 读取默认值."""
        df = pd.DataFrame({
            "amount": [10000, 20000],
            "close": [10, 20],
        }, index=["A", "B"])
        result = apply_all_filters(df)
        assert len(result) == 2


class TestPositionLimitCheck:
    """持仓约束检查."""

    def test_valid_weights(self):
        weights = pd.Series([0.03, 0.04, 0.02], index=["A", "B", "C"])
        is_valid, msg = position_limit_check(weights, max_single=0.05, max_positions=10)
        assert is_valid
        assert msg == "OK"

    def test_too_many_positions(self):
        weights = pd.Series(np.ones(25) * 0.01)
        is_valid, msg = position_limit_check(weights, max_single=0.05, max_positions=20)
        assert not is_valid

    def test_single_position_exceeded(self):
        weights = pd.Series([0.10, 0.03], index=["A", "B"])
        is_valid, msg = position_limit_check(weights, max_single=0.05, max_positions=10)
        assert not is_valid
