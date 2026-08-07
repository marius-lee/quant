# Unified ExecutionModel tests (report 1.2/6.1) - shared chain, subclass fill semantics.
import pandas as pd
import pytest

from quant.execution.cost import CostModel
from quant.execution.engine import ExecutionEngine, Order
from quant.execution.execution_model import (
    BacktestExecutionModel, ExecutionContext, LiveExecutionModel,
    trim_orders_by_alpha,
)

TODAY = "2026-07-25"


@pytest.fixture()
def engine(tmp_path):
    eng = ExecutionEngine(db_path=str(tmp_path / "trades.db"))
    # 初始资金 10w
    from quant.data.repos import TradeRepo
    repo = TradeRepo(db_path=eng.db_path)
    repo.set_initial_capital("quant", 100000.0)
    return eng


def _ctx(engine, prices, repo=None):
    return ExecutionContext(
        engine=engine, strategy="quant", today=TODAY,
        prices=pd.Series(prices), cost_model=CostModel.from_config(),
        repo=repo,
    )


def test_backtest_buy_fill(engine):
    """回测: 买单立即成交 (filled), 持仓/现金更新."""
    targets = [{"symbol": "699999", "shares": 100, "score": 9.0, "price": 10.0}]
    res = BacktestExecutionModel().run(targets, _ctx(engine, {"699999": 10.0}))
    assert res.buys_mode == "filled"
    assert res.buys == 1 and res.sells == 0
    pos = engine.get_positions("quant")
    assert len(pos) == 1 and pos[0]["symbol"] == "699999" and pos[0]["shares"] == 100
    assert engine.get_cash("quant") < 100000.0


def _ohlc(prev_close, open_, high, low):
    """B8: 构造 {symbol: {open,high,low,prev_close}} 字典."""
    return {"699999": {"prev_close": prev_close, "open": open_,
                       "high": high, "low": low}}


def _ctx_b8(engine, prices, prev_close, open_px, high, low):
    """带 ohlc 的执行上下文 (一字板判定用)."""
    return ExecutionContext(
        engine=engine, strategy="quant", today=TODAY,
        prices=pd.Series(prices), cost_model=CostModel.from_config(),
        ohlc=_ohlc(prev_close, open_px, high, low),
    )


def test_b8_sealed_limit_up_blocks_buy(engine):
    """B8: 一字涨停 (open==high==low==涨停价, 主板±10%) → 买单不成交."""
    targets = [{"symbol": "699999", "shares": 100, "score": 9.0, "price": 11.0}]
    res = BacktestExecutionModel().run(
        targets, _ctx_b8(engine, {"699999": 11.0}, prev_close=10.0,
                         open_px=11.0, high=11.0, low=11.0))
    assert engine.get_positions("quant") == []  # 一字涨停买入被阻断


def test_b8_sealed_limit_down_blocks_sell(engine):
    """B8: 一字跌停 (open==high==low==跌停价) → 卖单阻断, 持仓保留."""
    engine.execute([Order(symbol="699999", side="buy", shares=100, price=10.0, cost=0)],
                   "2026-07-24", "quant")
    targets = [{"symbol": "699999", "shares": 100, "score": 9.0, "price": 10.0}]
    # prev_close=11.0 → 跌停价 9.90; 持仓成本 10 → drop -1.0% (不触发 -8% 硬止损)
    res = BacktestExecutionModel().run(
        targets, _ctx_b8(engine, {"699999": 9.9}, prev_close=11.0,
                         open_px=9.9, high=9.9, low=9.9))
    pos = engine.get_positions("quant")
    assert len(pos) == 1 and pos[0]["symbol"] == "699999"  # 一字跌停卖不掉


def test_b8_not_sealed_normal_fill(engine):
    """B8: 非一字板 (high>open) → 正常成交."""
    targets = [{"symbol": "699999", "shares": 100, "score": 9.0, "price": 10.0}]
    res = BacktestExecutionModel().run(
        targets, _ctx_b8(engine, {"699999": 10.0}, prev_close=10.0,
                         open_px=10.0, high=10.5, low=9.8))
    assert res.buys_mode == "filled"
    assert len(engine.get_positions("quant")) == 1


def test_backtest_hard_stop(engine):
    """回测: 硬止损触发, 卖出 + stopped_out + 当日从 targets 剔除 (不买回)."""
    # 先建仓 (成本 10.0) — 前一交易日买入, 避开 T+1 卖出限制
    engine.execute([Order(symbol="699999", side="buy", shares=100, price=10.0, cost=0)],
                   "2026-07-24", "quant")
    targets = [{"symbol": "699999", "shares": 100, "score": 9.0, "price": 10.0}]
    # 跌 9% (> 8% 阈值) → 触发硬止损
    res = BacktestExecutionModel().run(targets, _ctx(engine, {"699999": 9.1}))
    assert res.stopped_out == ["699999"]
    # 止损卖出后不再买回 → 无持仓
    assert engine.get_positions("quant") == []


class _FakeRepo:
    """轻量 repo — 只实现 LiveExecutionModel 需要的熔断接口, 不碰 DB."""

    def __init__(self, cb_date=None):
        self._cb = cb_date
        self.notes = []

    def get_flag(self, key):
        if key == "circuit_breaker":
            return self._cb
        return None

    def update_signal_exec_note(self, day, symbol, note):
        self.notes.append((symbol, note))


def test_live_circuit_breaker(engine):
    """实盘: 熔断激活 → 买单全部阻断 (blocked), 卖单仍执行."""
    engine.execute([Order(symbol="099999", side="buy", shares=100, price=5.0, cost=0)],
                   "2026-07-24", "quant")
    targets = [{"symbol": "699999", "shares": 100, "score": 9.0, "price": 10.0}]
    prices = {"699999": 10.0, "099999": 5.0}
    repo = _FakeRepo(cb_date=TODAY)
    res = LiveExecutionModel().run(targets, _ctx(engine, prices, repo=repo))
    assert res.buys_mode == "blocked_circuit_breaker"
    assert ("699999", "blocked_circuit_breaker") in repo.notes
    # 699999 未成交
    assert all(p["symbol"] != "699999" for p in engine.get_positions("quant"))


def test_live_limit_placed(engine, monkeypatch):
    """实盘: 无熔断 → 买单挂限价 (OrderManager), 卖单立即成交."""
    placed = []

    class _FakeOM:
        def cancel_all(self, day, strategy):
            pass

        def place(self, day, strategy, symbol, shares, ref_price):
            placed.append((symbol, shares, ref_price))

    import quant.scheduler.order_manager as om_mod
    monkeypatch.setattr(om_mod, "OrderManager", _FakeOM)
    engine.execute([Order(symbol="099999", side="buy", shares=100, price=5.0, cost=0)],
                   "2026-07-24", "quant")
    # 目标: 卖 099999, 买 699999
    targets = [{"symbol": "699999", "shares": 100, "score": 9.0, "price": 10.0}]
    prices = {"699999": 10.0, "099999": 5.0}
    repo = _FakeRepo()
    res = LiveExecutionModel().run(targets, _ctx(engine, prices, repo=repo))
    assert res.buys_mode == "limit_placed"
    assert placed and placed[0][0] == "699999"
    # 卖单已立即成交 → 099999 无持仓
    assert all(p["symbol"] != "099999" for p in engine.get_positions("quant"))


def test_trim_orders_by_alpha():
    """B-13: 资金不足按 alpha 降序裁剪 — 高分保留, 低分丢弃, 股数整手."""
    cost = CostModel.from_config()
    orders = [
        Order(symbol="LOW", side="buy", shares=100, price=10.0,
              cost=cost.buy_cost(10.0, 100)),
        Order(symbol="HIGH", side="buy", shares=100, price=10.0,
              cost=cost.buy_cost(10.0, 100)),
    ]
    # 现金只够买 1 手
    kept = trim_orders_by_alpha(orders, cash=1050.0, cost_model=cost,
                                target_scores={"HIGH": 9.0, "LOW": 1.0})
    buys = [o for o in kept if o.side == "buy"]
    assert len(buys) == 1 and buys[0].symbol == "HIGH" and buys[0].shares == 100


def test_risk_only_no_rebalance(engine):
    """weekly 非调仓日: 只跑硬止损, 不建仓/不调仓 (targets 被忽略)."""
    # 持仓无止损 (涨) + 传入 targets → 全部忽略, 零成交
    engine.execute([Order(symbol="099999", side="buy", shares=100, price=5.0, cost=0)],
                   "2026-07-24", "quant")
    targets = [{"symbol": "699999", "shares": 100, "score": 9.0, "price": 10.0}]
    prices = {"699999": 10.0, "099999": 5.5}
    res = BacktestExecutionModel().run(targets, _ctx(engine, prices), risk_only=True)
    assert res.stopped_out == [] and res.buys == 0 and res.sells == 0
    assert res.buys_mode == "none"
    # 持仓不变, 未新买
    pos = engine.get_positions("quant")
    assert len(pos) == 1 and pos[0]["symbol"] == "099999"


def test_risk_only_stop_fires(engine):
    """weekly 非调仓日: 硬止损照跑 (风控每日不断), 但不买回."""
    engine.execute([Order(symbol="699999", side="buy", shares=100, price=10.0, cost=0)],
                   "2026-07-24", "quant")
    res = BacktestExecutionModel().run([], _ctx(engine, {"699999": 9.1}),
                                       risk_only=True)
    assert res.stopped_out == ["699999"]
    assert res.sells == 1 and res.buys == 0
    assert engine.get_positions("quant") == []


def test_is_rebalance_day():
    """is_rebalance_day: daily 恒 True; weekly 仅本周 anchor 起首个交易日."""
    from datetime import date
    from quant.execution.calendar import is_rebalance_day
    # 2026-07-20 周一 ~ 07-24 周五均为交易日 (真实交易日表)
    assert is_rebalance_day(date(2026, 7, 21), freq="daily") is True
    assert is_rebalance_day(date(2026, 7, 20), freq="weekly") is True   # 周一
    assert is_rebalance_day(date(2026, 7, 21), freq="weekly") is False  # 周二
    assert is_rebalance_day(date(2026, 7, 24), freq="weekly") is False  # 周五
    # anchor=周五: 周三还没到 anchor → False; 周五本身 → True
    assert is_rebalance_day(date(2026, 7, 22), freq="weekly", weekday=4) is False
    assert is_rebalance_day(date(2026, 7, 24), freq="weekly", weekday=4) is True
    # 周六非交易日 → False
    assert is_rebalance_day(date(2026, 7, 25), freq="weekly") is False
