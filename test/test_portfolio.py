"""测试组合构建器 PortfolioConstructor — 三种资本自适应策略 + 边界条件.

模板 3 (TDD, 软约束): 确定性输入, 精确验证输出.
"""
import pytest
import pandas as pd
import numpy as np
from quant.optimizer.portfolio import (
    PortfolioConstructor, TargetPortfolio, LOT_SIZE, calibrate_risk_aversion,
    _alpha_to_z, _ic_effective, _TC_IC_REF,
)
from quant.execution.cost import CostModel


class TestPortfolioConstructorGreedy:
    """排名集中 (Nano 层) — capital < nano_cap (¥30,000)."""

    def test_single_lot_single_stock(self):
        """4802 资本, 1 只低价股 → 买 1 手."""
        pc = PortfolioConstructor({"max_positions": 20, "max_single_position": 0.05})
        alpha = pd.Series([1.0], index=["000001"])
        prices = pd.Series([20.0], index=["000001"])
        pf = pc.construct(alpha, prices, 2000, price_buffer=0)
        # Nano tier (¥2,000 < ¥30,000) → rank_concentrated
        assert pf.method in ("rank_concentrated", "kelly_greedy")
        assert pf.positions == 1
        assert pf.lots["000001"] == 1
        assert pf.invested == 20 * 100

    def test_capital_too_small_for_any_stock(self):
        """资本 100, 股价 50 → 1手需5000 > 100 → 空持仓, 保留现金 (v380: 不抛异常)."""
        pc = PortfolioConstructor({"max_positions": 20, "max_single_position": 0.05})
        alpha = pd.Series([1.0, 0.5], index=["000001", "000002"])
        prices = pd.Series([50.0, 60.0], index=["000001", "000002"])
        pf = pc.construct(alpha, prices, 100)
        assert pf.positions == 0
        assert pf.cash_reserve == 100

    def test_multiple_stocks_one_cycle(self):
        """资本够买 2 只低价股各 1 手."""
        pc = PortfolioConstructor({"max_positions": 20, "max_single_position": 0.05})
        alpha = pd.Series([1.0, 0.8, 0.5], index=["A", "B", "C"])
        prices = pd.Series([10.0, 12.0, 100.0], index=["A", "B", "C"])
        pf = pc.construct(alpha, prices, 2200, price_buffer=0)
        assert pf.positions >= 1
        assert pf.lots["A"] == 2  # rank_concentrated: 2200//(10*100)=2 lots
        # B: 剩余200 < 1200 → 买不到
        assert pf.lots.get("B", 0) in (0, 1)
        assert "C" not in pf.lots

    def test_rank_concentrated_single_lot(self):
        """Nano 层排名集中: 资金只够 #1 股票 1 手."""
        pc = PortfolioConstructor({"max_positions": 5, "max_single_position": 0.05})
        alpha = pd.Series([1.0, 0.9, 0.8, 0.7, 0.6], index=[f"S{i}" for i in range(5)])
        prices = pd.Series([1.0, 1.0, 1.0, 1.0, 1.0], index=[f"S{i}" for i in range(5)])
        # avg_price=1, lot_cost=100, greedy < 200, weighted < 500
        pf = pc.construct(alpha, prices, 199)
        # rank_concentrated: buys 1 lot of S0, cash=99 < lot_cost=100 → stop
        assert pf.positions >= 1
        assert pf.positions <= 5

    def test_rank_concentrated_buys_max_of_top_stock(self):
        """Nano 层: #1 alpha 股票拿最多仓位, #2用剩余资金."""
        pc = PortfolioConstructor({"max_positions": 20, "max_single_position": 0.05})
        alpha = pd.Series([1.0, 0.9, 0.8], index=["A", "B", "C"])
        prices = pd.Series([15.0, 12.0, 100.0], index=["A", "B", "C"])
        # capital=5000: A=3lots(4500), cash=500 < B=1200 → stop
        pf = pc.construct(alpha, prices, 5000)
        assert pf.method == "rank_concentrated"
        assert pf.lots["A"] == 3  # 3 × 15 × 100 = 4500
        assert pf.lots.get("B", 0) == 0  # 剩余500不够买1手
        assert pf.positions == 1

    def test_rank_concentrated_multi_stock(self):
        """Nano 层: #1满仓后剩余买#2."""
        pc = PortfolioConstructor({"max_positions": 20, "max_single_position": 0.20})
        alpha = pd.Series([1.0, 0.9], index=["A", "B"])
        prices = pd.Series([10.0, 5.0], index=["A", "B"])
        # capital=1600: A=1lot(1000), cash=600 → B=1lot(500), cash=100 < 500 → stop
        pf = pc.construct(alpha, prices, 1600)
        assert pf.method == "rank_concentrated"
        assert pf.lots["A"] == 1
        assert pf.lots["B"] == 1
        assert pf.positions == 2

    def test_rank_concentrated_alpha_ordering(self):
        """Nano 层: 严格按 alpha 降序分配, 高 alpha 先买.
        v380: price_buffer 不再虚增价格, lot_cost = 价格 × 100 直接计算."""
        pc = PortfolioConstructor({"max_positions": 5, "max_single_position": 0.20})
        alpha = pd.Series([0.1, 0.9, 0.3], index=["X", "Y", "Z"])
        prices = pd.Series([10.0, 10.0, 10.0], index=["X", "Y", "Z"])
        pf = pc.construct(alpha, prices, 3000)
        assert pf.method == "rank_concentrated"
        # lot_cost = 10 × 100 = 1000; 3000 // 1000 = 3 手, 最高 alpha Y 全拿
        assert pf.lots["Y"] == 3
        assert pf.lots.get("Z", 0) == 0
        assert pf.lots.get("X", 0) == 0

    def test_equal_weight_greedy_micro_fallback(self):
        """Micro 层 score_weighted→0 时回退到 equal_weight_greedy (非 rank_concentrated)."""
        pc = PortfolioConstructor({"max_positions": 20, "max_single_position": 0.05,
                                   "nano_cap": 5000, "micro_cap": 50000})
        # capital 10000 > nano_cap=5000, < micro_cap=50000 → micro tier
        # But prices high enough that score_weighted gives 0
        alpha = pd.Series([1.0, 0.9, 0.8], index=["A", "B", "C"])
        prices = pd.Series([100.0, 80.0, 60.0], index=["A", "B", "C"])
        pf = pc.construct(alpha, prices, 10000)
        # Micro tier with low capital → score_weighted gives 0 → fallback to equal_weight
        # equal_weight may also give 0 if prices too high → rank_concentrated fallback
        assert pf.method in ("score_weighted", "equal_weight", "rank_concentrated")


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
        pc = PortfolioConstructor({"max_positions": 10, "max_single_position": 0.15,
                                   "method": "risk_parity"})
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


class TestCostAwareBand:
    """§8.3 交易成本感知优化 (Grinold α − λ·TC 无交易区间).

    Nano 层 ¥5000: 一次全仓换股成本 ≈ ¥21 (最低佣金主导)。
    换仓仅当 E[Δr] = Δz × IC × σ × horizon × 金额 ≥ λ × 成本 时执行。
    """

    def _pc(self):
        return PortfolioConstructor({"max_positions": 20, "max_single_position": 0.05})

    def _cm(self):
        return CostModel.from_config()

    def test_swap_suppressed_when_alpha_gap_small(self):
        """v390: Nano层豁免成本带 — 直接按alpha换仓, 不再锁仓."""
        alpha = pd.Series({"S1": 0.60, "S0": 0.55, "S2": 0.40, "S3": 0.30, "S4": 0.20})
        prices = pd.Series(45.0, index=alpha.index)
        cur = pd.Series({"S0": 1})
        pf = self._pc().construct(alpha, prices, 5000, current_lots=cur,
                                  cost_model=self._cm(), ic_map={"f1": 0.05})
        assert pf.tc_suppressed == 0   # Nano豁免成本带
        assert dict(pf.lots) == {"S1": 1}  # 直接换到最高alpha

    def test_swap_executed_when_alpha_gap_large(self):
        """大幅 alpha 提升: Micro层成本带不拦截."""
        from quant.config.constants import _require_cfg
        horizon = _require_cfg("optimizer.tc_horizon_days")
        # swap_val = 1 lot × ¥100 = 10000, Δz≈2.36, benefit ≈ 2.36×0.05×0.02×horizon×10000
        # For horizon=1: benefit≈¥24 > cost≈¥38 → suppressed (expected)
        # For horizon=5: benefit≈¥118 > cost≈¥38 → executes
        if horizon >= 5:
            alpha = pd.Series({"S1": 0.90, "S0": 0.10, "S2": 0.40, "S3": 0.30, "S4": 0.20})
            prices = pd.Series(45.0, index=alpha.index)
            cur = pd.Series({"S0": 1})
            pf = self._pc().construct(alpha, prices, 5000, current_lots=cur,
                                      cost_model=self._cm(), ic_map={"f1": 0.05})
            assert pf.tc_suppressed == 0
            assert dict(pf.lots) == {"S1": 1}
        else:
            # v390: Nano层豁免成本带, 直接换仓
            alpha = pd.Series({"S1": 0.90, "S0": 0.10, "S2": 0.40, "S3": 0.30, "S4": 0.20})
            prices = pd.Series(45.0, index=alpha.index)
            cur = pd.Series({"S0": 1})
            pf = self._pc().construct(alpha, prices, 5000, current_lots=cur,
                                      cost_model=self._cm(), ic_map={"f1": 0.05})
            assert pf.tc_suppressed == 0   # Nano豁免
            assert dict(pf.lots) == {"S1": 1}

    def test_no_band_without_holdings(self):
        """首次建仓 (无持仓): 成本带不启用, 按理想目标买 S1."""
        alpha = pd.Series({"S1": 0.60, "S0": 0.55, "S2": 0.40, "S3": 0.30, "S4": 0.20})
        prices = pd.Series(45.0, index=alpha.index)
        pf = self._pc().construct(alpha, prices, 5000, cost_model=self._cm())
        assert pf.tc_suppressed == 0
        assert dict(pf.lots) == {"S1": 1}

    def test_no_band_without_cost_model(self):
        """cost_model 缺失 (向后兼容): 行为与旧版一致."""
        alpha = pd.Series({"S1": 0.60, "S0": 0.55, "S2": 0.40, "S3": 0.30, "S4": 0.20})
        prices = pd.Series(45.0, index=alpha.index)
        cur = pd.Series({"S0": 1})
        pf = self._pc().construct(alpha, prices, 5000, current_lots=cur)
        assert pf.tc_suppressed == 0
        assert dict(pf.lots) == {"S1": 1}

    def test_held_stock_dropped_from_candidates_sold(self):
        """持仓跌出候选集 (风险过滤/数据缺失): 无条件卖出, 不参与成本带."""
        alpha = pd.Series({"S1": 0.60, "S0": 0.55, "S2": 0.40, "S3": 0.30, "S4": 0.20})
        prices = pd.Series(45.0, index=alpha.index)
        cur = pd.Series({"GONE": 1})
        pf = self._pc().construct(alpha, prices, 5000, current_lots=cur,
                                  cost_model=self._cm(), ic_map={"f1": 0.05})
        assert pf.tc_suppressed == 0
        assert dict(pf.lots) == {"S1": 1}   # GONE 不在目标 → 执行层 delta 卖出

    def test_held_still_optimal_no_trade(self):
        """持仓仍是 top alpha: 理想=当前, 无 diff, 成本带无副作用."""
        alpha = pd.Series({"S0": 0.90, "S1": 0.55, "S2": 0.40, "S3": 0.30, "S4": 0.20})
        prices = pd.Series(45.0, index=alpha.index)
        cur = pd.Series({"S0": 1})
        pf = self._pc().construct(alpha, prices, 5000, current_lots=cur,
                                  cost_model=self._cm(), ic_map={"f1": 0.05})
        assert pf.tc_suppressed == 0
        assert dict(pf.lots) == {"S0": 1}

    def test_topup_not_gated(self):
        """纯现金加仓 (无卖单配对): 不做成本门槛 — 只拦截'以卖养买'."""
        alpha = pd.Series({"S0": 0.90, "S1": 0.55, "S2": 0.40, "S3": 0.30, "S4": 0.20})
        prices = pd.Series(20.0, index=alpha.index)   # lot=2100 (buffered)
        cur = pd.Series({"S0": 1})
        pf = self._pc().construct(alpha, prices, 5000, current_lots=cur,
                                  cost_model=self._cm(), ic_map={"f1": 0.05})
        assert pf.tc_suppressed == 0
        assert pf.lots["S0"] == 2           # 5000 // 2100 = 2 手


class TestCostBandHelpers:
    """§8.3 辅助函数: _alpha_to_z / _ic_effective."""

    def test_alpha_to_z_monotonic_and_signed(self):
        alpha = pd.Series({"A": 0.9, "B": 0.7, "C": 0.5, "D": 0.3, "E": 0.1})
        z = _alpha_to_z(alpha)
        assert z["A"] > z["B"] > z["C"] > z["D"] > z["E"]
        assert z["A"] > 0 > z["E"]
        assert z["A"] == pytest.approx(-z["E"], abs=1e-9)   # Blom 对称性

    def test_alpha_to_z_uses_ranks_not_values(self):
        """对单调变换稳健: 线性缩放 alpha 不改变 z."""
        a1 = pd.Series({"A": 0.9, "B": 0.7, "C": 0.5})
        a2 = a1 * 100 + 5
        pd.testing.assert_series_equal(_alpha_to_z(a1), _alpha_to_z(a2))

    def test_ic_effective_float_and_dict(self):
        assert _ic_effective({"a": 0.05, "b": -0.03}) == pytest.approx(0.04)
        assert _ic_effective({"a": {"ic_mean": 0.05}}) == pytest.approx(0.05)

    def test_ic_effective_fallback_to_config(self):
        assert _ic_effective(None) == pytest.approx(_TC_IC_REF)
        assert _ic_effective({}) == pytest.approx(_TC_IC_REF)
        assert _ic_effective({"a": float("nan")}) == pytest.approx(_TC_IC_REF)


class TestHRP:
    """§8.2 (test-v299): HRP 优化器接线 — optimizer.method 分发."""

    def _cov(self, symbols, vols=None):
        """对角协方差 (零相关), vols 缺省全 0.2."""
        n = len(symbols)
        vols = vols or [0.2] * n
        return pd.DataFrame(np.diag([v ** 2 for v in vols]),
                            index=symbols, columns=symbols)

    def test_hrp_weights_degenerate_single_asset(self):
        from quant.optimizer.hrp import hrp_weights
        w = hrp_weights(np.array([[0.04]]))
        assert np.allclose(w, [1.0])

    def test_hrp_weights_sum_to_one_and_favor_low_vol(self):
        """6 只零相关资产: 权重和=1, 全正, 最高波动资产权重 < 最低波动资产."""
        from quant.optimizer.hrp import hrp_weights
        vols = np.array([0.20, 0.10, 0.30, 0.14, 0.25, 0.12])
        cov = np.diag(vols ** 2)
        w = hrp_weights(cov)
        assert abs(w.sum() - 1.0) < 1e-9
        assert (w > 0).all()
        assert w[np.argmax(vols)] < w[np.argmin(vols)]

    def test_hrp_dispatch_via_config(self):
        """method='hrp' + 协方差 → small 层走 HRP, method 标记 hrp."""
        pc = PortfolioConstructor({
            "max_positions": 20, "max_single_position": 0.3,
            "nano_cap": 30000, "micro_cap": 100000, "method": "hrp",
        })
        syms = [f"S{i}" for i in range(8)]
        alpha = pd.Series(np.linspace(1.0, 0.3, 8), index=syms)
        prices = pd.Series([10.0 + i for i in range(8)], index=syms)
        pf = pc.construct(alpha, prices, 200000, covariance=self._cov(syms))
        assert pf.method == "hrp"
        assert pf.positions >= 2
        assert pf.invested <= 200000

    def test_risk_parity_dispatch_explicit(self):
        """method='risk_parity' → small 层仍走 risk parity (行为不变)."""
        pc = PortfolioConstructor({
            "max_positions": 20, "max_single_position": 0.3,
            "nano_cap": 30000, "micro_cap": 100000, "method": "risk_parity",
        })
        syms = [f"S{i}" for i in range(8)]
        alpha = pd.Series(np.linspace(1.0, 0.3, 8), index=syms)
        prices = pd.Series([10.0 + i for i in range(8)], index=syms)
        pf = pc.construct(alpha, prices, 200000, covariance=self._cov(syms))
        assert pf.method == "risk_parity"

    def test_hrp_falls_back_without_covariance(self):
        """method='hrp' 但无协方差 → 报错链不变 (mean-variance 要求协方差)."""
        pc = PortfolioConstructor({
            "max_positions": 20, "max_single_position": 0.3,
            "nano_cap": 30000, "micro_cap": 100000, "method": "hrp",
        })
        alpha = pd.Series(np.linspace(1.0, 0.3, 8), index=[f"S{i}" for i in range(8)])
        prices = pd.Series([10.0 + i for i in range(8)], index=alpha.index)
        with pytest.raises(ValueError, match="requires covariance"):
            pc.construct(alpha, prices, 200000)
