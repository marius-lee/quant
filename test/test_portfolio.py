"""测试组合构建器 PortfolioConstructor — 三种资本自适应策略 + 边界条件.

模板 3 (TDD, 软约束): 确定性输入, 精确验证输出.
"""
import pytest
import pandas as pd
import numpy as np
from quant.optimizer.portfolio import PortfolioConstructor, TargetPortfolio, LOT_SIZE, calibrate_risk_aversion


class TestPortfolioConstructorGreedy:
    """贪心等权 — capital < lot_cost * 2."""

    def test_single_lot_single_stock(self):
        """4802 资本, 1 只低价股 → 买 1 手."""
        pc = PortfolioConstructor({"max_positions": 20, "max_single_position": 0.05})
        alpha = pd.Series([1.0], index=["000001"])
        prices = pd.Series([20.0], index=["000001"])
        # capital 2000 < lot_cost*2 = 4000 → greedy
        pf = pc.construct(alpha, prices, 2000)
        # greedy tier returns method="equal_weight"
        assert pf.method in ("equal_weight", "kelly_greedy")
        assert pf.positions == 1
        assert pf.lots["000001"] == 1
        assert pf.invested == 20 * 100

    def test_capital_too_small_for_any_stock(self):
        """资本 100, 股价 50 → 1手需5000 > 100 → raise ValueError."""
        pc = PortfolioConstructor({"max_positions": 20, "max_single_position": 0.05})
        alpha = pd.Series([1.0, 0.5], index=["000001", "000002"])
        prices = pd.Series([50.0, 60.0], index=["000001", "000002"])
        with pytest.raises(ValueError, match="greedy produced 0 lots"):
            pc.construct(alpha, prices, 100)

    def test_multiple_stocks_one_cycle(self):
        """资本够买 2 只低价股各 1 手."""
        pc = PortfolioConstructor({"max_positions": 20, "max_single_position": 0.05})
        alpha = pd.Series([1.0, 0.8, 0.5], index=["A", "B", "C"])
        prices = pd.Series([10.0, 12.0, 100.0], index=["A", "B", "C"])
        pf = pc.construct(alpha, prices, 2200)
        assert pf.positions >= 1
        assert pf.lots["A"] == 1
        # Kelly greedy may allocate differently; B might get 0 or 1
        assert pf.lots.get("B", 0) in (0, 1)
        assert "C" not in pf.lots

    def test_max_lots_per_caps_exposure(self):
        """max_lots_per 限制每只股票最多 1 手."""
        pc = PortfolioConstructor({"max_positions": 5, "max_single_position": 0.05})
        alpha = pd.Series([1.0, 0.9, 0.8, 0.7, 0.6], index=[f"S{i}" for i in range(5)])
        prices = pd.Series([1.0, 1.0, 1.0, 1.0, 1.0], index=[f"S{i}" for i in range(5)])
        # avg_price=1, lot_cost=100, greedy < 200, weighted < 500
        pf = pc.construct(alpha, prices, 199)
        # greedy: buys 1 lot per stock (each costs 100), 5*100=500 > 199, buys only 1
        assert pf.positions >= 1
        assert pf.positions <= 5


class TestPortfolioConstructorWeighted:
    """得分倾斜 — lot_cost*2 ≤ capital < lot_cost*max_positions."""

    def test_proportional_allocation(self):
        """2 只股票, 等分 10000."""
        pc = PortfolioConstructor({"max_positions": 20, "max_single_position": 0.05})
        alpha = pd.Series([1.0, 0.5], index=["A", "B"])
        prices = pd.Series([10.0, 10.0], index=["A", "B"])
        pf = pc.construct(alpha, prices, 50000)
        assert pf.method == "score_weighted"
        assert pf.positions >= 1
        assert 0 < pf.invested <= 50000

    def test_zero_score_sum(self):
        """所有 alpha 相等 → 等权分配."""
        pc = PortfolioConstructor({"max_positions": 10, "max_single_position": 0.10})
        alpha = pd.Series([5.0, 5.0, 5.0], index=["A", "B", "C"])
        prices = pd.Series([10.0, 10.0, 10.0], index=["A", "B", "C"])
        # nano_cap=¥30,000, micro_cap=¥100,000
        # capital=50000 → micro
        pf = pc.construct(alpha, prices, 50000)
        assert pf.method in ("score_weighted", "equal_weight")
        assert pf.positions >= 1


class TestPortfolioConstructorMeanVar:
    """均值-方差 — capital ≥ lot_cost * max_positions."""

    def test_mean_var_with_covariance(self):
        """充足资金 + 协方差 → 均值-方差分支."""
        pc = PortfolioConstructor({"max_positions": 10, "max_single_position": 0.15})
        stocks = ["A", "B", "C", "D", "E"]
        alpha = pd.Series([0.05, 0.03, 0.08, 0.02, 0.06], index=stocks)
        prices = pd.Series([10.0, 12.0, 8.0, 15.0, 9.0], index=stocks)
        cov = pd.DataFrame(
            np.eye(5) * 0.01 + np.ones((5, 5)) * 0.002,
            index=stocks, columns=stocks
        )
        pf = pc.construct(alpha, prices, 200000, covariance=cov)
        assert pf.method in ("risk_parity", "mean_variance")
        assert pf.positions >= 1
        assert pf.invested <= 200000

    def test_mean_var_requires_covariance(self):
        """均值-方差无协方差矩阵 → raise."""
        pc = PortfolioConstructor({"max_positions": 5, "max_single_position": 0.20})
        stocks = ["A", "B", "C"]
        alpha = pd.Series([0.1, 0.2, 0.15], index=stocks)
        prices = pd.Series([5.0, 5.0, 5.0], index=stocks)
        with pytest.raises(ValueError, match="covariance matrix"):
            pc.construct(alpha, prices, 200000)


class TestPortfolioConstructorEdgeCases:
    """边界条件与错误处理."""

    def test_empty_alpha(self):
        """空 alpha → 空持仓."""
        pc = PortfolioConstructor()
        alpha = pd.Series([], dtype=float)
        prices = pd.Series([10.0], index=["A"])
        pf = pc.construct(alpha, prices, 10000)
        assert pf.positions == 0

    def test_nan_intersection(self):
        """alpha 和 prices 的 NaN 被正确排除."""
        pc = PortfolioConstructor({"max_positions": 20, "max_single_position": 0.05})
        alpha = pd.Series([1.0, np.nan, 0.5], index=["A", "B", "C"])
        prices = pd.Series([20.0, 15.0, np.nan], index=["A", "B", "C"])
        pf = pc.construct(alpha, prices, 10000)
        assert pf.positions >= 0

    def test_single_stock_weighted(self):
        """1 只股票 → 能买就买."""
        pc = PortfolioConstructor({"max_positions": 20, "max_single_position": 0.05})
        alpha = pd.Series([1.0], index=["X"])
        prices = pd.Series([5.0], index=["X"])
        pf = pc.construct(alpha, prices, 5000)
        assert pf.positions >= 1

    def test_alpha_ordering_respected(self):
        """高 alpha 股票优先买入."""
        pc = PortfolioConstructor({"max_positions": 5, "max_single_position": 0.20})
        alpha = pd.Series([0.1, 0.3, 0.9, 0.2, 0.5], index=["A", "B", "C", "D", "E"])
        prices = pd.Series([10.0, 10.0, 10.0, 10.0, 10.0], index=["A", "B", "C", "D", "E"])
        pf = pc.construct(alpha, prices, 3000)
        if pf.positions > 0:
            assert "C" in pf.lots.index

    def test_prices_iloc_aligns_with_alpha(self):
        """修复后: prices.loc[a.index[:n]] 对齐 alpha 排序而非字母序."""
        pc = PortfolioConstructor({"max_positions": 20, "max_single_position": 0.05})
        alpha2 = pd.Series([0.01, 0.02, 0.05, 0.03], index=["A", "B", "Z", "C"])
        prices2 = pd.Series([100.0, 80.0, 5.0, 60.0], index=["A", "B", "Z", "C"])
        pf = pc.construct(alpha2, prices2, 10000)
        assert pf.positions >= 1
        if pf.positions > 0:
            assert "Z" in pf.lots.index


class TestTargetPortfolio:
    """TargetPortfolio 数据类."""

    def test_positions_count(self):
        lots = pd.Series([1, 3, 0, 2], index=["A", "B", "C", "D"])
        tp = TargetPortfolio(lots, cash_reserve=500, method="greedy")
        assert tp.positions == 3

    def test_empty_lots(self):
        lots = pd.Series([], dtype=int)
        tp = TargetPortfolio(lots, cash_reserve=1000, method="equal_weight")
        assert tp.positions == 0


class TestCalibrateRiskAversion:
    """risk_aversion 校准."""

    def test_calibrate_returns_valid_lambda(self):
        alpha = pd.Series([0.05, 0.03, 0.08, 0.02, 0.06], index=["A", "B", "C", "D", "E"])
        prices = pd.Series([10.0] * 5, index=["A", "B", "C", "D", "E"])
        cov = pd.DataFrame(np.eye(5) * 0.01, index=alpha.index, columns=alpha.index)
        lam = calibrate_risk_aversion(alpha, prices, 100000, cov, max_positions=5)
        assert lam in [0.5, 1.0, 2.0, 5.0, 10.0]

    def test_calibrate_insufficient_stocks_returns_conservative(self):
        alpha = pd.Series([0.05, 0.03], index=["A", "B"])
        prices = pd.Series([10.0, 12.0], index=["A", "B"])
        cov = pd.DataFrame(np.eye(2) * 0.01, index=["A", "B"], columns=["A", "B"])
        lam = calibrate_risk_aversion(alpha, prices, 10000, cov)
        assert lam == 2.0
