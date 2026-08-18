"""v533: 闭环断裂点 2-6 修复的针对性测试 (2026-08-18).

覆盖:
  - 断裂点2: reconcile equity_cross 当日快照 (非"昨日最新"→ 交易日常态 break)
  - 断裂点3: attribution Brinson 基准 = 最近交易日当日收益 (非历史日均)
  - 断裂点4: 止损状态统一 — 回测跨日共享实例 (注入), 实盘清仓重买重置旧 peak/tp1
  - 断裂点5 (P0-1): 止损卖单经 broker_adapter — 未连接拒绝模拟 (账本/券商双账)
  - 断裂点6: phase8 D1 回放信号用 using 池 (与实盘同口径)
"""
import pytest


# ── 断裂点2: reconcile equity_cross 当日快照 ──
def test_recon_cash_equity_cross_same_day(monkeypatch, tmp_path):
    """equity_cross 只比较当日快照 (WHERE date=?); 当日无快照 → skip 不报 break."""
    from quant.scheduler import reconcile as rec
    from quant.data.repos import TradeRepo
    from quant.execution.engine import ExecutionEngine

    db = str(tmp_path / "trades.db")  # 相对路径会被解析到项目根 — 必须绝对路径

    captured = {}

    def fake_query_one(self, sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return None  # 当日无快照

    monkeypatch.setattr(TradeRepo, "_query_one", fake_query_one)
    monkeypatch.setattr(TradeRepo, "get_initial_capital",
                        lambda self, s: 100000.0)
    monkeypatch.setattr(ExecutionEngine, "get_cash", lambda self, s: 95000.0)

    rows = rec._recon_cash("2026-08-18", "quant", db)
    eq = [r for r in rows if r["check"] == "equity_cross"][0]
    assert "date=?" in captured["sql"], \
        f"equity_cross 必须按当日查询 (原取最近一条=昨日 → 常 break): {captured['sql']}"
    assert captured["params"] == ("quant", "2026-08-18"), \
        f"查询参数必须是当日 (strategy, day), got {captured['params']}"
    assert eq["status"] == "skip", "当日无快照 → skip (非 break)"


# ── 断裂点3: attribution Brinson 当日基准 ──
def test_sector_returns_same_day_basis():
    """sector_returns = 最近交易日当日收益 (等权), 非历史日均 (量纲统一)."""
    import pandas as pd
    from quant.scheduler.attribution import _sector_returns_from_df

    df = pd.DataFrame([
        # A: 前 4 日 10→11→12→13→14.4 → 最后一日 +10.77%
        {"symbol": "A", "close": 10.0, "sector": "X", "date": "2026-08-12"},
        {"symbol": "A", "close": 11.0, "sector": "X", "date": "2026-08-13"},
        {"symbol": "A", "close": 12.0, "sector": "X", "date": "2026-08-14"},
        {"symbol": "A", "close": 13.0, "sector": "X", "date": "2026-08-17"},
        {"symbol": "A", "close": 14.4, "sector": "X", "date": "2026-08-18"},
        # B: 同板块 14→14.7 → +5%
        {"symbol": "B", "close": 14.0, "sector": "X", "date": "2026-08-17"},
        {"symbol": "B", "close": 14.7, "sector": "X", "date": "2026-08-18"},
    ])
    ret = _sector_returns_from_df(df)
    expected = ((14.4 / 13.0 - 1) + (14.7 / 14.0 - 1)) / 2
    assert abs(ret["X"] - expected) < 1e-9, \
        f"应取当日收益等权均值 {expected:.4f}, got {ret}"
    # 对照: 历史日均会得到明显更小的值 (13→14.4 途中多日摊薄)
    assert ret["X"] > 0.05, "当日基准应显著高于历史日均口径"


# ── 断裂点4: 止损状态 — 实盘清仓重买重置旧 peak/tp1 ──
def test_stop_loss_rebuy_resets_old_meta(monkeypatch):
    """DB 模式: 最近卖出晚于本仓买入 → 新仓, 旧 peak/tp1 不回载 (trailing 不误触)."""
    from quant.execution.stop_loss import RiskManager
    from quant.data.repos import trade_repo as tr_mod

    monkeypatch.setattr(tr_mod.TradeRepo, "get_last_sell_time",
                        lambda self, sym: "2026-08-17 10:00:00")  # 旧仓卖出早于本仓买入
    # 旧仓: peak=15 (成本 10 上方 50%), tp1_hit=True — 若残留 → trail_sl 必触发
    monkeypatch.setattr(tr_mod.TradeRepo, "get_position_meta_max",
                        lambda self, sym: {"_tp1_hit": True, "_peak": 15.0})

    rm = RiskManager(strategy="quant", cooloff_store=None)  # DB 模式 (实盘)
    positions = [{"symbol": "600000", "price": 10.0, "shares": 1000,
                  "buy_time": "2026-08-18 09:30:00"}]
    quotes = {"600000": {"price": 9.5}}  # -5%, 低于硬止损线 (-2ATR=-20%)
    atr_panel = {"2026-08-18": {"600000": 1.0}}
    stops = rm.check(positions, quotes, "2026-08-18", atr_panel=atr_panel)
    assert stops == [], f"重买新仓不应触发 trailing (旧 peak 残留), got {stops}"
    assert positions[0]["_peak"] == 10.0, "新仓 peak 应从成本起算 (旧峰值已重置)"
    assert positions[0].get("_tp1_hit", False) is False, "新仓 tp1 标记应重置"

    # 对照: 若旧 meta 残留 (未重置) → trail_sl 触发
    positions[0]["_peak"] = 15.0
    positions[0]["_tp1_hit"] = True
    stops2 = rm.check(positions, quotes, "2026-08-18", atr_panel=atr_panel)
    assert any(s["symbol"] == "600000" for s in stops2), \
        f"旧峰值残留时 trail_sl 应触发, got {stops2}"


# ── 断裂点5 (P0-1): 止损卖单走 broker_adapter ──
class _FakeEngine:
    def __init__(self, adapter):
        self.broker_adapter = adapter
        self.executed = []

    def execute(self, orders, today, strategy):
        self.executed.extend(orders)


class _FakeAdapter:
    name = "fake"

    def __init__(self, connected=True):
        self._connected = connected
        self.sold = []

    def is_connected(self):
        return self._connected

    def sell(self, sym, price, shares, order_type=None):
        if self._connected:
            self.sold.append((sym, price, shares))
            return _FakeResult(success=True)
        return _FakeResult(success=False, error="not connected")


class _FakeResult:
    def __init__(self, success=True, status="filled", error=None):
        self.success = success
        self.status = status
        self.error = error


def _mk_ctx(engine):
    from quant.execution.execution_model import ExecutionContext
    from quant.execution.cost import CostModel
    return ExecutionContext(engine=engine, strategy="quant", today="2026-08-18",
                            prices={}, cost_model=CostModel.from_config())


def test_stop_orders_adapter_absent_uses_engine():
    """adapter=None (回测/未注入) → engine.execute 模拟成交."""
    from quant.execution.execution_model import LiveExecutionModel
    from quant.execution.engine import Order
    eng = _FakeEngine(adapter=None)
    model = LiveExecutionModel()
    model._execute_stop_orders(
        [Order(symbol="600000", side="sell", shares=100, price=9.5)], _mk_ctx(eng))
    assert len(eng.executed) == 1, "adapter=None 应走 engine.execute (回测语义)"


def test_stop_orders_adapter_disconnected_raises():
    """v534: 双路径下沉 engine.execute — 未连接 → RuntimeError 零 fallback.

    v533 中间态曾在此自管 adapter (连接检查抛 P0-1);
    v534 收敛后引擎内校验 (engine.execute 双路径), 本测试验证转发语义:
    _execute_stop_orders 把订单交 engine.execute, 未连接由引擎拒绝.
    """
    from quant.execution.execution_model import LiveExecutionModel
    from quant.execution.engine import Order, ExecutionEngine
    from test.test_v534 import FakeBroker
    import tempfile, os
    with tempfile.TemporaryDirectory() as td:
        eng = ExecutionEngine(db_path=os.path.join(td, "t.db"),
                              broker_adapter=FakeBroker(connected=False))
        model = LiveExecutionModel()
        with pytest.raises(RuntimeError, match="未连接"):
            model._execute_stop_orders(
                [Order(symbol="600000", side="sell", shares=100, price=9.5)], _mk_ctx(eng))


def test_stop_orders_adapter_connected_sells_via_broker():
    """v534: 已连接 → engine.execute 双路径 (券商成交 + 账本同步)."""
    from quant.execution.execution_model import LiveExecutionModel
    from quant.execution.engine import Order, ExecutionEngine
    from quant.data.repos import TradeRepo
    from test.test_v534 import FakeBroker
    import tempfile, os
    with tempfile.TemporaryDirectory() as td:
        db = os.path.join(td, "t.db")
        eng = ExecutionEngine(db_path=db, broker_adapter=FakeBroker(connected=True))
        repo = TradeRepo(db_path=db)
        repo.set_initial_capital("quant", 100000.0)
        eng.execute([Order(symbol="600000", side="buy", shares=100, price=9.0)],
                    "2026-08-17", strategy="quant")
        model = LiveExecutionModel()
        model._execute_stop_orders(
            [Order(symbol="600000", side="sell", shares=100, price=9.5)], _mk_ctx(eng))
        assert eng.get_positions("quant") == [], "券商成交后账本同步清仓 (双路径原子)"


# ── 断裂点6: phase8 D1 用 using 池 ──
def test_phase8_d1_uses_live_pool(monkeypatch):
    """D1 回放信号 status_filter='using' (与实盘同池, 原 backtesting 恒 divergent)."""
    from quant.evaluation import phase8_live_consistency as p8
    from quant.data.repos import trade_repo as tr_mod
    import quant.pipeline as pl
    from quant.execution import calendar as cal

    captured = {}

    def fake_gs(**kwargs):
        captured.update(kwargs)
        return {"target_positions": []}

    monkeypatch.setattr(pl, "generate_signals", fake_gs)
    monkeypatch.setattr(cal, "is_trading_day", lambda d: True)
    monkeypatch.setattr(tr_mod.TradeRepo, "get_daily_signals_range",
                        lambda self, start, end, mode: [{"date": "2026-08-17",
                                                         "signals_json": "[]"}])
    p8._compare_signals("2026-08-17", "2026-08-17")
    assert captured.get("status_filter") == "using", \
        f"D1 回放必须用 using 池 (实盘同口径), got {captured.get('status_filter')}"