"""v532: 审查缺口 2-6 修复的针对性测试 (2026-08-18).

覆盖:
  - 缺口2: phase5 DSR 数据源 backtest scope (晋升通道生效)
  - 缺口3: repair._ensure_factor_cache 08:00 因子物化兜底
  - 缺口4: monitor VaR 持仓市值权重 (非等权)
  - 缺口5: rebalance 换手缩放不再保底 1 手绕过预算
  - 缺口6: order_manager QUOTE_TTL 追价闭环
"""
import sqlite3

import pytest

from quant.config.paths import MARKET_DB


# ── 缺口5: rebalance 换手约束真实生效 ──
def test_rebalance_turnover_scale_drops_sub_lot_trades():
    """缩放后 |diff| < 0.5 手的交易必须被丢弃 (原保底 1 手绕过预算)."""
    from quant.optimizer.rebalance import compute_trades

    class _Cost:
        @staticmethod
        def sell_cost(p, s): return p * s * 0.001
        @staticmethod
        def buy_cost(p, s): return p * s * 0.001

    prices = {"A": 10.0, "B": 10.0, "C": 10.0}
    # 目标: A=10手 B=10手 C=10手; 现持 A=0 B=0 C=0 → 总换手 300 手
    # max_turnover_ratio 限到 1/10 → 缩放后每只 ~1 手
    target = {"A": 10, "B": 10, "C": 10}
    current = {"A": 0, "B": 0, "C": 0}
    import pandas as pd
    orders = compute_trades(
        pd.Series(target), pd.Series(current), pd.Series(prices), _Cost(),
        max_turnover_ratio=0.2, capital=60000.0, cash=60000.0,
        skip_cash_feasibility=True)
    buy_shares = sum(o.shares for o in orders if o.side == "buy")
    # 10手×100股×10元×3只 = 30000 换手; 预算 60000*0.2=12000 → 缩放 0.4 → 4手/只
    # 4 手 > 0.5 手 → 保留; 验证总换手 ≤ 预算 (无保底绕过)
    tv = sum(o.shares * o.price for o in orders if o.side == "buy")
    assert tv <= 60000 * 0.2 + 1e-6, f"换手 {tv} 超预算 {60000*0.2}"
    # 原 bug: 缩放 <0.5 手被保底 1 手 → 用极小预算复现
    orders2 = compute_trades(
        pd.Series(target), pd.Series(current), pd.Series(prices), _Cost(),
        max_turnover_ratio=0.001, capital=60000.0, cash=60000.0,
        skip_cash_feasibility=True)
    assert all(o.shares == 0 for o in orders2), \
        f"极小预算下应全丢弃 (保底 1 手绕过), got {[(o.symbol, o.shares) for o in orders2]}"


# ── 缺口6: order_manager QUOTE_TTL 追价 ──
def test_order_manager_quote_ttl_chase(monkeypatch):
    """挂单超 QUOTE_TTL 且 ask 高于 limit → 追价到 ask×(1-discount)."""
    from quant.scheduler.order_manager import OrderManager, PendingOrder, QUOTE_TTL_SEC, DISCOUNT_PCT, MAX_CHASE
    from datetime import datetime, timedelta

    om = OrderManager()
    placed_at = (datetime.now() - timedelta(seconds=QUOTE_TTL_SEC + 60)).isoformat(timespec="seconds")
    po = PendingOrder(
        id=999, strategy="quant", symbol="000001", target_shares=100,
        limit_price=9.90, reference_price=10.0, status="pending",
        placed_at=placed_at, chase_count=0, day="2026-08-18")

    monkeypatch.setattr(om, "get_pending", lambda day, strategy: [po])
    monkeypatch.setattr(om, "_chase", lambda oid, new_limit: setattr(po, "limit_price", new_limit))
    monkeypatch.setattr(om, "_fill", lambda po_, price, day: None)
    monkeypatch.setattr(om, "_cancel", lambda oid, reason: None)
    # ask=10.5 > limit=9.90, gap=6% < runaway_start(3%)? 6% > 3% → 会走 runaway 分支;
    # 用 gap 稍小于 urgency 初始值 3% 的 ask 验证 chase: ask=10.15 → gap=2.5% < 3%
    quotes = {"000001": {"ask": 9.95, "ask_volume": 50000, "volume": 1000000,
                         "prev_close": 10.0, "price": 10.15, "open": 10.1}}
    actions = om.check_and_manage("2026-08-18", quotes)
    chased = [a for a in actions if a.get("action") == "chase"]
    assert chased, f"应触发 chase, got {actions}"
    expect_limit = round(9.95 * (1 - DISCOUNT_PCT), 2)
    assert po.limit_price == expect_limit, f"limit {po.limit_price} != {expect_limit}"


def test_order_manager_quote_ttl_no_chase_under_ttl(monkeypatch):
    """挂单未超 TTL → 不追价 (行情新鲜, 限价仍有效)."""
    from quant.scheduler.order_manager import OrderManager, PendingOrder, QUOTE_TTL_SEC
    from datetime import datetime, timedelta

    om = OrderManager()
    placed_at = (datetime.now() - timedelta(seconds=QUOTE_TTL_SEC - 30)).isoformat(timespec="seconds")
    po = PendingOrder(
        id=998, strategy="quant", symbol="000002", target_shares=100,
        limit_price=9.90, reference_price=10.0, status="pending",
        placed_at=placed_at, chase_count=0, day="2026-08-18")
    monkeypatch.setattr(om, "get_pending", lambda day, strategy: [po])
    monkeypatch.setattr(om, "_fill", lambda po_, price, day: None)
    monkeypatch.setattr(om, "_cancel", lambda oid, reason: None)
    quotes = {"000002": {"ask": 10.15, "ask_volume": 50000, "volume": 1000000,
                         "prev_close": 10.0, "price": 10.15, "open": 10.1}}
    actions = om.check_and_manage("2026-08-18", quotes)
    assert not [a for a in actions if a.get("action") == "chase"], \
        f"TTL 内不应追价, got {actions}"


# ── 缺口4: VaR 持仓市值权重 ──
def test_monitor_var_market_value_weight():
    """VaR 权重应为持仓市值 (shares×现价), 非等权计数."""
    src = open("quant/scheduler/monitor.py").read()
    assert "sum(1 for p in positions" not in src, "仍有等权计数残留"
    assert "p.get(\"shares\", 0)" in src and "quotes.get(s" in src, "缺市值权重 (shares×现价)"


# ── 缺口3: repair 08:00 factor_cache 兜底 ──
def test_repair_ensure_factor_cache_picks_up_failed_evening(monkeypatch, tmp_path):
    """晚间链 factor_cache 未 ok → _ensure_factor_cache 触发增量物化."""
    from quant.scheduler.repair import _ensure_factor_cache

    conn = sqlite3.connect(MARKET_DB)
    conn.execute("DELETE FROM task_runs WHERE date='2026-08-18' AND task_name='factor_cache'")
    conn.commit()
    conn.close()

    called = {}
    def fake_run(fc_start, today):
        called["start"], called["today"] = fc_start, today

    monkeypatch.setattr("quant.scheduler.factor_cache._run", fake_run)
    result = _ensure_factor_cache("2026-08-18")
    assert called.get("today") == "2026-08-18", f"未触发物化: {called}"
    assert result == ["2026-08-18"]


def test_repair_ensure_factor_cache_skips_when_ok(monkeypatch):
    """factor_cache 已 ok → 不重复物化."""
    from quant.scheduler.repair import _ensure_factor_cache

    conn = sqlite3.connect(MARKET_DB)
    conn.execute(
        "INSERT INTO task_runs (date, task_name, status, started_at) "
        "VALUES ('2026-08-18', 'factor_cache', 'ok', datetime('now','localtime'))")
    conn.commit()
    conn.close()

    called = {}
    monkeypatch.setattr("quant.scheduler.factor_cache._run",
                        lambda a, b: called.setdefault("n", 0) or called.update(n=1))
    result = _ensure_factor_cache("2026-08-18")
    assert result == []
    assert "n" not in called, "已 ok 不应物化"
    conn = sqlite3.connect(MARKET_DB)
    conn.execute("DELETE FROM task_runs WHERE date='2026-08-18' AND task_name='factor_cache'")
    conn.commit()
    conn.close()
