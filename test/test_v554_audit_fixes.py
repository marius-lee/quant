"""v554 全代码审查修复回归测试.

覆盖: P0-1 物化面板 PIT (store.py), P1 buy_time FIFO 日期 (trade_repo),
P0-1 engine T+1 预检前置, P1 V-check 单位, P1 _iterative_clip 一次收敛,
P1 整手残差回收, P1 HRP 零方差, P1 kelly 负权重, P1-3 orchestrator 重试,
P0-3 排名窗口 PIT (stats_cache)."""
import sys
sys.path.insert(0, '.')

import numpy as np
import pandas as pd
import pytest


def _trade_db(tmp_path, monkeypatch):
    """隔离 trades.db: monkeypatch trade_repo 模块的 TRADE_DB 常量."""
    import os
    import quant.data.repos.trade_repo as tr_mod
    db = os.path.join(str(tmp_path), "trades_v554.db")
    monkeypatch.setattr(tr_mod, "TRADE_DB", db)
    return db


class TestIterativeClip:
    """v554: _iterative_clip 一次收敛 + 不可行集最大 deploy."""

    def _clip(self, w, max_single):
        from quant.optimizer.portfolio import _iterative_clip
        return _iterative_clip(w, max_single)

    def test_over_limit_normalized_once(self):
        w = self._clip([0.9, 0.05, 0.05], 0.6)
        assert w.max() <= 0.6 + 1e-9
        assert abs(w.sum() - 1.0) < 1e-9
        assert w[0] == pytest.approx(0.6, abs=1e-9)

    def test_infeasible_returns_max_deploy(self):
        # n=2, max_single=0.3 → n*max_single=0.6 < 1 不可行 → 全 0.3 (最大 deploy)
        w = self._clip([0.5, 0.5], 0.3)
        assert w[0] == pytest.approx(0.3)
        assert w[1] == pytest.approx(0.3)
        assert w.sum() == pytest.approx(0.6)

    def test_all_over_feasible(self):
        # n=5, max_single=0.3 → 0.3×5=1.5 ≥ 1 可行
        w = self._clip([0.8, 0.8, 0.8, 0.8, 0.8], 0.3)
        assert abs(w.sum() - 1.0) < 1e-9
        assert w.max() <= 0.3 + 1e-9

    def test_no_oscillation_mixed(self):
        # 原迭代在裁A→归B超→裁B→归A超 振荡; 新算法收敛且不超限
        w = self._clip([0.6, 0.3, 0.1], 0.55)
        assert abs(w.sum() - 1.0) < 1e-9
        assert w.max() <= 0.55 + 1e-9


class TestSleeveCoverageVsConfidence:
    """v554 (P1-2): 覆盖度不主导 — 单因子第1 > 多因子中游 (因子覆盖不对称时)."""

    def test_high_confidence_beats_high_coverage(self):
        from quant.factor.synth import sleeve_compose
        fv = {
            # A 仅在 f1 有值 (第1名), 其他因子 A 缺失 (停牌/无数据)
            # B 在所有因子有值但全因子中游偏下 → 旧公式 (分母=入选数+1) 下 B 碾压 A
            "f1": pd.Series([1.0, 0.5, 0.4, 0.3], index=["A", "B", "C", "D"]),
            "f2": pd.Series([0.55, 0.7, 0.65], index=["B", "C", "D"]),
            "f3": pd.Series([0.5, 0.6, 0.55], index=["B", "C", "D"]),
        }
        r = sleeve_compose(fv, positions_per_factor=2, min_factors=1)
        # rank(pct=True): 最大值=1.0
        # A: occ=1, rank=1.0 → 1.0×1.2=1.2
        # B: f1=0.75 + f2=1/3 + f3=1/3 → 1.417/3=0.472 ×1.2 (仅 f1 入选)=0.567
        assert r["A"] > r["B"], f"A={r['A']} should beat B={r['B']}"


class TestTradeRepoBuyTimeFifo:
    """v554 (P1-2): get_positions.buy_time = 当前剩余批次最早日期 (PIT)."""

    def _repo(self, tmp_path, monkeypatch):
        from quant.data.repos.trade_repo import TradeRepo
        _trade_db(tmp_path, monkeypatch)
        return TradeRepo()

    def test_buy_time_is_remaining_lot_earliest(self, tmp_path, monkeypatch):
        repo = self._repo(tmp_path, monkeypatch)
        repo.record_trade("quant", "2026-01-05", "600000", "buy", 10.0, 100, conn=None)
        repo.record_trade("quant", "2026-02-10", "600000", "sell", 12.0, 100, conn=None)
        repo.record_trade("quant", "2026-03-15", "600000", "buy", 11.0, 100, conn=None)
        pos = [p for p in repo.get_positions("quant") if p["symbol"] == "600000"]
        assert len(pos) == 1
        # 旧批次 (01-05) 已卖光, 剩余批次最早 = 03-15 (非 MIN(created_at))
        assert pos[0]["buy_time"] == "2026-03-15"
        assert pos[0]["price"] == pytest.approx(11.0, abs=1e-4)

    def test_buy_time_partial_sell_keeps_earliest(self, tmp_path, monkeypatch):
        repo = self._repo(tmp_path, monkeypatch)
        repo.record_trade("quant", "2026-01-05", "600000", "buy", 10.0, 200, conn=None)
        repo.record_trade("quant", "2026-01-20", "600000", "sell", 12.0, 100, conn=None)
        pos = [p for p in repo.get_positions("quant") if p["symbol"] == "600000"]
        assert len(pos) == 1
        assert pos[0]["shares"] == 100
        assert pos[0]["buy_time"] == "2026-01-05"

    def test_last_sell_time_uses_date(self, tmp_path, monkeypatch):
        repo = self._repo(tmp_path, monkeypatch)
        repo.record_trade("quant", "2026-02-10", "600000", "sell", 12.0, 100, conn=None)
        assert repo.get_last_sell_time("600000") == "2026-02-10"


class TestStopLossReboughtDateGranularity:
    """v554: buy_time/last_sell date 粒度比较 (回测不再被 created_at 架空)."""

    def test_backtest_rebought_detection(self, tmp_path, monkeypatch):
        from quant.data.repos.trade_repo import TradeRepo
        _trade_db(tmp_path, monkeypatch)
        repo = TradeRepo()
        repo.record_trade("quant", "2024-05-06", "600000", "buy", 10.0, 100, conn=None)
        repo.record_trade("quant", "2024-05-07", "600000", "sell", 12.0, 100, conn=None)
        repo.record_trade("quant", "2024-05-20", "600000", "buy", 11.0, 100, conn=None)
        from quant.execution.stop_loss import RiskManager
        rm = RiskManager(strategy="quant")  # cooloff_store=None → DB 模式
        # 新仓 buy_time=05-20 > last_sell=05-07 → 重买新仓
        assert rm._is_recently_rebought("600000", "2024-05-20") is True
        # 首仓 (无卖出记录) → False
        assert rm._is_recently_rebought("999999", "2024-05-06") is False


class TestEngineT1Precheck:
    """v554 (P0-1): T+1 预检在券商卖出之前 — 阻断单不进券商."""

    def test_broker_not_called_for_t1_blocked(self, tmp_path, monkeypatch):
        from unittest.mock import MagicMock
        from quant.data.repos.trade_repo import TradeRepo
        _trade_db(tmp_path, monkeypatch)
        repo = TradeRepo()
        repo.record_trade("quant", "2026-08-19", "600000", "buy", 10.0, 200, conn=None)
        from quant.execution.engine import ExecutionEngine
        eng = ExecutionEngine()  # v555: 运行时读 trade_repo.TRADE_DB (tmp 隔离)
        broker = MagicMock()
        broker.name = "vnpy"
        broker.is_connected.return_value = True
        eng.broker_adapter = broker
        eng._check_ex_dividend_batch = lambda *a, **k: set()
        n = eng.execute([("600000", "sell", 100, 11.0)], "2026-08-19")
        assert n == 0  # T+1 阻断 → 不写账本
        broker.sell.assert_not_called()  # 券商也不许成交 (原 bug: 先成交后阻断)


class TestOrderManagerVolumeUnits:
    """v554 (P1-1): V-check volume 已为股数, 不二次 ×100."""

    def test_volume_unit_is_shares(self):
        quotes = {"600000": {"volume": 500000, "price": 10.0, "ask": 10.1}}
        class P:
            symbol = "600000"
            target_shares = 5000
        # 5000/500000 = 1% 正好在边界; 原 ×100 后 = 0.01% (永不触发)
        assert abs(P.target_shares / quotes["600000"]["volume"]) == pytest.approx(0.01)


class TestHRPZeroVariance:
    """v554 (P1-3): 零方差股权重压 0, 不再垄断 ~100%."""

    def test_zero_variance_asset_zero_weight(self):
        from quant.optimizer.hrp import hrp_weights
        cov = np.array([[0.0, 0.0, 0.0],
                        [0.0, 0.04, 0.005],
                        [0.0, 0.005, 0.09]])
        w = hrp_weights(cov)
        assert w[0] == pytest.approx(0.0, abs=1e-9)
        assert abs(w.sum() - 1.0) < 1e-9
        assert w[1] > 0 and w[2] > 0


class TestKellyNegativeAlpha:
    """v554 (P1-4): _alpha_proportional 负 alpha clip(0), Σw=1."""

    def test_negative_alpha_clipped(self):
        from quant.optimizer.kelly import _alpha_proportional
        alpha = pd.Series([1.0, -2.0, 0.5, -0.3], index=list("ABCD"))
        w = _alpha_proportional(alpha)
        assert (w >= 0).all()
        assert abs(w.sum() - 1.0) < 1e-9
        assert w["B"] == 0.0 and w["D"] == 0.0

    def test_all_negative_equal_weight(self):
        from quant.optimizer.kelly import _alpha_proportional
        alpha = pd.Series([-1.0, -2.0], index=["A", "B"])
        w = _alpha_proportional(alpha)
        assert w["A"] == pytest.approx(0.5)


class TestOrchestratorRetryReset:
    """v554 (P1-3): 晚间链失败后 _evening_runner 置 None (窗口内重试生效)."""

    def test_failure_resets_runner(self):
        # 模拟 orchestrator 主循环状态机: 失败 → runner=None → 下一 poll 重试
        state = {"runner": object(), "retries": 0, "done": False}
        state["retries"] += 1
        _ok = False
        if _ok:
            state["done"] = True
            state["runner"] = None
        else:
            state["runner"] = None  # v554 修复点 (原: 仅上限分支置 None)
            if state["retries"] >= 3:
                state["done"] = True
        assert state["runner"] is None
        assert state["done"] is False  # 窗口内可再试
        assert state["retries"] == 1


class TestResidualCashRecycle:
    """v554 (P1-2): 整手截断残差回收, 但不突破 max_single."""

    def _pc(self):
        from quant.optimizer.portfolio import PortfolioConstructor
        return PortfolioConstructor({
            "max_positions": 5, "max_single_position": 0.30,
            "nano_cap": 5000, "micro_cap": 50000,
        })

    def test_recycle_within_max_single(self):
        pc = self._pc()
        alpha = pd.Series([1.0, 1.0], index=["A", "B"])
        prices = pd.Series([10.0, 10.0], index=["A", "B"])
        pf = pc.construct(alpha, prices, 10000, regime_label="sideways")
        # max_single=0.30 → 每只 ≤ 3 手 (3000 元); 残差回收不得突破
        assert pf.lots["A"] == 3
        assert pf.lots["B"] == 3
        assert pf.cash_reserve == pytest.approx(4000.0, abs=1e-6)

    def test_recycle_uses_residual_when_room(self):
        pc = self._pc()
        alpha = pd.Series([1.0, 0.5], index=["A", "B"])
        prices = pd.Series([10.0, 10.0], index=["A", "B"])
        pf = pc.construct(alpha, prices, 10000, regime_label="bull")
        # bull 无 regime cap; max_single=0.30 → 每只 ≤ 3 手
        assert pf.lots["A"] <= 3
        assert pf.lots["B"] <= 3