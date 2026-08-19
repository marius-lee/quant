"""v560 (2026-08-19): 业界标准止损/加仓优化回归.

修复前根因复盘:
  - 600331: 8/14 最低 12.30 已击穿 -8% 固定止损位 12.576, 但盘中 monitor
    30s 循环只跑 ATR 止损 (当日止损位 12.213 未触及), 固定硬止损
    check_hard_stop 仅回测/开盘执行 → 阴跌中 ATR 收缩止损位随之下移
    (温水煮青蛙), 拖到 8/19 实亏 -9.9%。
  - 600744: 7/23 浮盈 +13.6% 时追高加仓 200股@8.42 (无信号, 执行层买入),
    次日回调 -10.5% 吐光利润 (净 -¥176)。

修复:
  1. monitor 盘中循环接入 check_hard_stop — 固定% 是绝对底线, ATR 是波动
     自适应, 双轨并存取更早触发。
  2. execute 开盘检查同步双轨。
  3. execution_model 加仓闸门: 只在盈利且回踩时加, 禁止追涨/亏损摊平。
  4. config risk.stop_loss_pct 0.08 → 0.05 (Van Tharp 单笔风险预算,
     Kestner 趋势策略 5% 上限)。
"""
import pytest
import pandas as pd

from quant.execution.cost import CostModel
from quant.execution.engine import ExecutionEngine
from quant.execution.execution_model import (
    BacktestExecutionModel, ExecutionContext,
)
from quant.execution.stop_loss import RiskManager

TODAY = "2026-08-19"


@pytest.fixture()
def engine(tmp_path):
    eng = ExecutionEngine(db_path=str(tmp_path / "trades.db"))
    from quant.data.repos import TradeRepo
    repo = TradeRepo(db_path=eng.db_path)
    repo.set_initial_capital("quant", 100000.0)
    return eng


def _ctx(engine, prices, repo=None, day_high=None):
    return ExecutionContext(
        engine=engine, strategy="quant", today=TODAY,
        prices=pd.Series(prices), cost_model=CostModel.from_config(),
        repo=repo, day_high=day_high,
    )


# ═══════════════════════════════════════════
# ① 固定硬止损 -5% 双轨
# ═══════════════════════════════════════════

def test_hard_stop_triggers_at_5pct():
    """固定硬止损: -5% 触发 (config stop_loss_pct=0.05 收紧后)."""
    rm = RiskManager(cooloff_store={})
    pos = {"symbol": "600331", "price": 13.67, "shares": 300}
    stops = rm.check_hard_stop([pos], {"600331": 12.98})
    assert len(stops) == 1
    assert stops[0]["symbol"] == "600331"
    assert stops[0]["shares"] == 300
    assert stops[0]["reason"].startswith("hard_sl")


def test_hard_stop_not_at_3pct():
    """-3% 不触发 (仍可容忍)."""
    rm = RiskManager(cooloff_store={})
    pos = {"symbol": "600331", "price": 13.67, "shares": 300}
    stops = rm.check_hard_stop([pos], {"600331": 13.26})
    assert stops == []


def test_execute_open_dual_track_merge(engine):
    """execute 开盘双轨: 硬止损与 ATR 信号合并, 同 symbol 不重复卖."""
    from quant.execution.execution_model import ExecutionContext as _EC
    from quant.execution.engine import Order
    prices = {"600331": 12.98}  # -5% 已触发硬止损, ATR 未触发
    ctx = _EC(engine=engine, strategy="quant", today=TODAY,
              prices=pd.Series(prices), cost_model=CostModel.from_config(),
              repo=None, day_high=None)
    # 预置持仓
    from quant.execution.stop_loss import RiskManager as _RM
    rm = _RM()
    rm._meta_store = {}
    rm._cooloff_store = {}
    # 构造 600331 持仓
    engine.execute([Order(symbol="600331", side="buy", shares=300,
                          price=13.67, cost=0.0)], TODAY, "quant")
    # 开盘 ATR + 硬止损双轨
    open_quotes = {"600331": {"price": 12.99}}
    open_sigs = rm.check(engine.get_positions("quant"), open_quotes, TODAY)
    hard = rm.check_hard_stop(engine.get_positions("quant"),
                              {"600331": 12.98})
    merged = open_sigs + [h for h in hard
                          if h["symbol"] not in {s["symbol"] for s in open_sigs}]
    assert len(merged) == 1  # 只卖一次
    assert merged[0]["symbol"] == "600331"
    assert merged[0]["shares"] == 300


# ═══════════════════════════════════════════
# ② 加仓闸门 (盈利 + 回踩才加, 禁追涨/摊平)
# ═══════════════════════════════════════════

def _place_position(engine, sym, price, shares=100):
    from quant.execution.engine import Order
    engine.execute([Order(symbol=sym, side="buy", shares=shares,
                          price=price, cost=0.0)], TODAY, "quant")


def test_add_gate_blocks_loss_avg_down(engine):
    """亏损摊平: 现价 < 成本 → 加仓单被拦截."""
    _place_position(engine, "600744", 7.37, 100)
    targets = [{"symbol": "600744", "shares": 300, "score": 9.0, "price": 7.0}]
    res = BacktestExecutionModel().run(
        targets, _ctx(engine, {"600744": 7.0}, day_high={"600744": 7.5}))
    pos = engine.get_positions("quant")
    assert len(pos) == 1 and pos[0]["symbol"] == "600744"
    assert pos[0]["shares"] == 100  # 未加仓


def test_add_gate_blocks_chase(engine):
    """追涨: 现价 ≥ 当日高点 → 加仓单被拦截 (600744 复盘场景)."""
    _place_position(engine, "600744", 7.37, 100)
    targets = [{"symbol": "600744", "shares": 300, "score": 9.0, "price": 8.42}]
    res = BacktestExecutionModel().run(
        targets, _ctx(engine, {"600744": 8.42}, day_high={"600744": 8.42}))
    pos = engine.get_positions("quant")
    assert len(pos) == 1 and pos[0]["shares"] == 100


def test_add_gate_allows_profit_pullback(engine):
    """盈利且回踩: 现价 > 成本 且 < 当日高点 → 加仓放行."""
    _place_position(engine, "600744", 7.37, 100)
    targets = [{"symbol": "600744", "shares": 300, "score": 9.0, "price": 7.9}]
    res = BacktestExecutionModel().run(
        targets, _ctx(engine, {"600744": 7.9}, day_high={"600744": 8.2}))
    pos = engine.get_positions("quant")
    assert len(pos) == 1 and pos[0]["symbol"] == "600744"
    assert pos[0]["shares"] == 300  # 加仓成功


def test_add_gate_new_position_untouched(engine):
    """新仓 (未持有) 不受闸门影响."""
    targets = [{"symbol": "699999", "shares": 100, "score": 9.0, "price": 10.0}]
    res = BacktestExecutionModel().run(
        targets, _ctx(engine, {"699999": 10.0}, day_high={"699999": 10.0}))
    assert res.buys == 1
    pos = engine.get_positions("quant")
    assert len(pos) == 1 and pos[0]["shares"] == 100


def test_add_gate_backtest_ohlc_high(engine):
    """回测无 day_high 时用 ohlc.high 判定追涨 (与实盘行为一致)."""
    _place_position(engine, "600744", 7.37, 100)
    targets = [{"symbol": "600744", "shares": 300, "score": 9.0, "price": 8.42}]
    ctx = ExecutionContext(
        engine=engine, strategy="quant", today=TODAY,
        prices=pd.Series({"600744": 8.42}),
        cost_model=CostModel.from_config(),
        ohlc={"600744": {"open": 8.30, "high": 8.42, "low": 8.10,
                          "prev_close": 8.37}},
    )
    BacktestExecutionModel().run(targets, ctx)
    pos = engine.get_positions("quant")
    assert len(pos) == 1 and pos[0]["shares"] == 100  # 追涨被拦截