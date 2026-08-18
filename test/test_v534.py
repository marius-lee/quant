# v534 tests — 双路径执行 / 双指标合并 / 死代码清理 / verify_strict 修复 / 重构.
import pandas as pd
import pytest

from quant.execution.broker_adapter import BrokerAdapter, OrderResult, SimulatedAdapter
from quant.execution.cost import CostModel
from quant.execution.engine import ExecutionEngine, Order

TODAY = "2026-07-25"


class FakeBroker(BrokerAdapter):
    """真实券商适配器替身 — connected/sell 结果可配."""

    name = "fake_broker"

    def __init__(self, connected=True, sell_success=True):
        self._connected = connected
        self._sell_success = sell_success
        self.sell_calls = []

    def connect(self):
        return True

    def disconnect(self):
        pass

    def buy(self, symbol, price, shares, order_type="LIMIT"):
        return OrderResult(success=True, symbol=symbol, side="buy",
                           status="filled", is_simulated=True)

    def sell(self, symbol, price, shares, order_type="MARKET"):
        self.sell_calls.append((symbol, price, shares))
        return OrderResult(success=self._sell_success, symbol=symbol,
                           side="sell", status="filled" if self._sell_success else "rejected")

    def cancel(self, order_id):
        return True

    def get_positions(self):
        return []

    def get_account(self):
        from quant.execution.broker_adapter import AccountInfo
        return AccountInfo(cash=0.0)

    def get_orders(self, status=None):
        return []

    def is_connected(self):
        return self._connected


@pytest.fixture()
def engine(tmp_path):
    eng = ExecutionEngine(db_path=str(tmp_path / "trades.db"))
    from quant.data.repos import TradeRepo
    repo = TradeRepo(db_path=eng.db_path)
    repo.set_initial_capital("quant", 100000.0)
    return eng


def _seed_buy(engine, symbol="699999"):
    engine.execute([Order(symbol=symbol, side="buy", shares=100, price=10.0)],
                   TODAY, strategy="quant")


def _sell_orders():
    return [Order(symbol="699999", side="sell", shares=100, price=11.0, cost=0)]


def _sell(engine, orders=None):
    """卖出用次日日期 — 避开 T+1 (当日买入不可当日卖)."""
    return engine.execute(orders or _sell_orders(), "2026-07-27")


# ═══════════════════════════════════════════════════════════
# 优化1: engine.execute 模拟/实盘双路径 (ADR-036)
# ═══════════════════════════════════════════════════════════

def test_sell_no_adapter_pure_ledger(engine):
    """无 adapter (回测): 卖出纯账本, 券商路径不触发."""
    _seed_buy(engine)
    n = _sell(engine)
    assert n == 1
    assert engine.get_positions("quant") == []


def test_sell_simulated_adapter_no_double_write(engine):
    """SimulatedAdapter: 内部已写账本 → 排除, 不双重写入."""
    _seed_buy(engine)
    eng = ExecutionEngine(db_path=engine.db_path, broker_adapter=SimulatedAdapter())
    n = _sell(eng)
    assert n == 1
    assert eng.get_positions("quant") == []


def test_sell_real_adapter_disconnected_raises(engine):
    """真实券商未连接 → RuntimeError 零 fallback, 账本不写."""
    _seed_buy(engine)
    eng = ExecutionEngine(db_path=engine.db_path, broker_adapter=FakeBroker(connected=False))
    with pytest.raises(RuntimeError, match="未连接"):
        _sell(eng)
    assert len(eng.get_positions("quant")) == 1


def test_sell_real_adapter_connected_ledger_once(engine):
    """真实券商已连接: 先成交, 成功后账本写入一次."""
    _seed_buy(engine)
    fake = FakeBroker(connected=True, sell_success=True)
    eng = ExecutionEngine(db_path=engine.db_path, broker_adapter=fake)
    n = _sell(eng)
    assert n == 1
    assert len(fake.sell_calls) == 1
    assert eng.get_positions("quant") == []


def test_sell_real_adapter_failed_raises_no_ledger(engine):
    """券商卖出失败 → RuntimeError, 拒绝写账本 (双账根治)."""
    _seed_buy(engine)
    fake = FakeBroker(connected=True, sell_success=False)
    eng = ExecutionEngine(db_path=engine.db_path, broker_adapter=fake)
    with pytest.raises(RuntimeError, match="卖出失败"):
        _sell(eng)
    assert len(eng.get_positions("quant")) == 1


def test_buy_always_pure_ledger(engine):
    """买单恒纯账本: 即使真实 adapter 也不经 adapter.buy (OrderManager 自管)."""
    fake = FakeBroker(connected=True)
    eng = ExecutionEngine(db_path=engine.db_path, broker_adapter=fake)
    n = eng.execute([Order(symbol="699999", side="buy", shares=100, price=10.0)], TODAY)
    assert n == 1
    assert len(eng.get_positions("quant")) == 1


# ═══════════════════════════════════════════════════════════
# 优化4: preload_aux_data 单日 NameError 回归
# ═══════════════════════════════════════════════════════════

def test_preload_aux_data_no_nameerror():
    """单日 aux 预载不再 NameError (date_from→date, 原 verify_strict 瘫痪根因)."""
    from quant.factor.compute._preload import preload_aux_data
    aux = preload_aux_data(["600036"], "2026-07-25")
    assert "financial_income" in aux
    assert "financial_balance" in aux
    assert "financial_cashflow" in aux


# ═══════════════════════════════════════════════════════════
# 优化3: piotroski aux 序修复 — 升序装载 vs DESC 假设
# ═══════════════════════════════════════════════════════════

def _piotroski_aux(ascending=True):
    """三 symbol × 两期财务: 最新期 (2024-06-30) 按 symbol 区分盈利/亏损/中性,
    旧期 (2024-03-31) 全部亏损 — 序错时 cur/prv 互换会改变得分."""
    from datetime import date
    # 最新期: A 盈利, B 亏损, C 中性 (仅覆盖所需项为 0 难产, 用低盈利)
    symbols = ["699999", "699998", "699997"]
    if ascending:
        dates = [date(2024, 3, 31), date(2024, 6, 30)]
        np_ = {"699999": [5e8, -1e8], "699998": [-5e8, -1e8], "699997": [1e8, -1e8]}
        rev = {"699999": [1e9, 1e8], "699998": [1e8, 1e8], "699997": [5e8, 1e8]}
        cost = {"699999": [4e8, 2e8], "699998": [3e8, 2e8], "699997": [4e8, 2e8]}
        t_rev = {"699999": [1e9, 1e8], "699998": [1e8, 1e8], "699997": [5e8, 1e8]}
        tp = {"699999": [6e8, -2e8], "699998": [-6e8, -2e8], "699997": [1e8, -2e8]}
        op = {"699999": [5e8, -1e8], "699998": [-5e8, -1e8], "699997": [1e8, -1e8]}
        assets = {"699999": [1e10, 1e9], "699998": [1e10, 1e9], "699997": [1e10, 1e9]}
        liab = {"699999": [2e9, 2e8], "699998": [2e9, 2e8], "699997": [2e9, 2e8]}
        eq = {"699999": [8e9, 8e8], "699998": [8e9, 8e8], "699997": [8e9, 8e8]}
        cfo = {"699999": [3e8, -1e8], "699998": [-3e8, -1e8], "699997": [1e8, -1e8]}
    else:
        dates = [date(2024, 6, 30), date(2024, 3, 31)]
        np_ = {"699999": [-1e8, 5e8], "699998": [-1e8, -5e8], "699997": [-1e8, 1e8]}
        rev = {"699999": [1e8, 1e9], "699998": [1e8, 1e8], "699997": [1e8, 5e8]}
        cost = {"699999": [2e8, 4e8], "699998": [2e8, 3e8], "699997": [2e8, 4e8]}
        t_rev = {"699999": [1e8, 1e9], "699998": [1e8, 1e8], "699997": [1e8, 5e8]}
        tp = {"699999": [-2e8, 6e8], "699998": [-2e8, -6e8], "699997": [-2e8, 1e8]}
        op = {"699999": [-1e8, 5e8], "699998": [-1e8, -5e8], "699997": [-1e8, 1e8]}
        assets = {"699999": [1e9, 1e10], "699998": [1e9, 1e10], "699997": [1e9, 1e10]}
        liab = {"699999": [2e8, 2e9], "699998": [2e8, 2e9], "699997": [2e8, 2e9]}
        eq = {"699999": [8e8, 8e9], "699998": [8e8, 8e9], "699997": [8e8, 8e9]}
        cfo = {"699999": [-1e8, 3e8], "699998": [-1e8, -3e8], "699997": [-1e8, 1e8]}
    rows = []
    for sym in symbols:
        rows.append((sym, [dates[0], dates[1]], [np_[sym][0], np_[sym][1]],
                     [rev[sym][0], rev[sym][1]], [cost[sym][0], cost[sym][1]],
                     [t_rev[sym][0], t_rev[sym][1]], [tp[sym][0], tp[sym][1]],
                     [op[sym][0], op[sym][1]]))
    return {
        "financial_income": pd.DataFrame({
            "symbol": [r[0] for r in rows for _ in (0, 1)],
            "stat_date": [d for r in rows for d in r[1]],
            "net_profit": [v for r in rows for v in r[2]],
            "operating_revenue": [v for r in rows for v in r[3]],
            "operating_cost": [v for r in rows for v in r[4]],
            "total_operating_revenue": [v for r in rows for v in r[5]],
            "total_profit": [v for r in rows for v in r[6]],
            "operating_profit": [v for r in rows for v in r[7]]}),
        "financial_balance": pd.DataFrame({
            "symbol": [r[0] for r in rows for _ in (0, 1)],
            "stat_date": [d for r in rows for d in r[1]],
            "total_assets": [v for r in rows for v in assets[r[0]]],
            "total_liability": [v for r in rows for v in liab[r[0]]],
            "total_owner_equities": [v for r in rows for v in eq[r[0]]],
            "fixed_assets": [0.0] * 6, "intangible_assets": [0.0] * 6}),
        "financial_cashflow": pd.DataFrame({
            "symbol": [r[0] for r in rows for _ in (0, 1)],
            "stat_date": [d for r in rows for d in r[1]],
            "net_operate_cash_flow": [v for r in rows for v in cfo[r[0]]]}),
    }


def test_piotroski_aux_order_invariant():
    """aux 升序/降序装载结果一致, 且反映最新期 (修复前 cur/prv 互换)."""
    from quant.factor.compute.missing import compute_piotroski_fscore
    data = pd.DataFrame(index=["699999", "699998", "699997"])
    s_asc = compute_piotroski_fscore(data, "2024-06-30", aux=_piotroski_aux(ascending=True))
    s_desc = compute_piotroski_fscore(data, "2024-06-30", aux=_piotroski_aux(ascending=False))
    assert s_asc.equals(s_desc)
    # 修复前: asc 装载 (升序) 时 cur/prv 互换 → 两版得分不同; 修复后恒等


# ═══════════════════════════════════════════════════════════
# 重构2: strategy engine property 必崩修复 + 市值口径
# ═══════════════════════════════════════════════════════════

def test_strategy_engine_property_no_attr_error():
    """engine property: CapitalAllocation 无 db_path 字段 → 原 AttributeError 必崩."""
    from quant.strategy import StrategyConfig, StrategyInstance
    inst = StrategyInstance(StrategyConfig(name="t1", capital=None))
    assert isinstance(inst.engine, ExecutionEngine)


def test_strategy_position_market_value_units():
    """_position_market_value: 价×手×100股/手 = 市值(元), 非手数."""
    from unittest.mock import patch
    from quant.strategy import StrategyConfig, StrategyInstance
    inst = StrategyInstance(StrategyConfig(name="t2", capital=None))
    inst._current_positions = {"600036": 1}
    with patch("quant.strategy.DataStore.get_daily", return_value=pd.DataFrame(
            {"close": [38.2]}, index=pd.to_datetime(["2026-08-17"]))):
        mv = inst._position_market_value()
    assert mv["600036"] == pytest.approx(3820.0)  # 38.2元 × 1手 × 100股/手


# ═══════════════════════════════════════════════════════════
# 重构1: sigmoid 单调变换移除 — rank 恒等
# ═══════════════════════════════════════════════════════════

def test_alpha_rank_identity():
    """rank: sigmoid 移除后输出与输入一致 (单调变换对排名选股 no-op)."""
    import numpy as np
    from quant.alpha.model import AlphaModel
    am = AlphaModel(top_fraction=0.2)
    alpha = pd.Series(np.linspace(-1, 1, 100))
    out = am.rank(alpha)
    pd.testing.assert_series_equal(out, alpha)
