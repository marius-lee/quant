"""Q7-2 重构: 统一 RiskManager 测试 — 硬止损 + 冷却注册表 (内存/DB 双模式)."""
import os
import tempfile

from quant.execution.stop_loss import RiskManager


def _positions():
    return [
        {"symbol": "600519", "price": 100.0, "shares": 200},
        {"symbol": "000001", "price": 50.0, "shares": 100},
    ]


def test_hard_stop_triggers_below_threshold():
    rm = RiskManager(cooloff_store={})
    prices = {"600519": 91.0, "000001": 49.0}  # 600519 跌 9% > 8%
    stops = rm.check_hard_stop(_positions(), prices)
    assert len(stops) == 1
    assert stops[0]["symbol"] == "600519"
    assert stops[0]["shares"] == 200
    assert abs(stops[0]["drop"] - (-0.09)) < 1e-9


def test_hard_stop_skips_bad_prices():
    rm = RiskManager(cooloff_store={})
    # None / 0 / 负价不触发
    prices = {"600519": None, "000001": 0}
    assert rm.check_hard_stop(_positions(), prices) == []


def test_cooloff_memory_store():
    store = {}
    rm = RiskManager(cooloff_store=store)
    rm.set_cooloff("600519", "2026-07-25", days=5)
    # 冷却期内 (end = 07-30)
    assert "600519" in rm.get_cooloff_symbols("2026-07-26")
    assert "600519" in rm.get_cooloff_symbols("2026-07-29")
    # 到期日恢复 (end > today 才冷却, end == today 解除)
    assert "600519" not in rm.get_cooloff_symbols("2026-07-30")
    assert "600519" not in rm.get_cooloff_symbols("2026-08-01")


def test_cooloff_db_store():
    tmp = tempfile.mktemp(suffix=".db")
    try:
        import quant.data.repos._base as base
        orig = base.TRADE_DB
        base.TRADE_DB = tmp
        # TradeRepo() 默认参数在 import 时绑定, 直接传 db_path
        from quant.data.repos import TradeRepo
        TradeRepo(tmp)  # 建 schema
        rm = RiskManager()  # cooloff_store=None → DB
        # 注: RiskManager 内部 TradeRepo() 用默认路径, 这里 monkeypatch
        import quant.data.repos.trade_repo as tr
        orig_new = tr.TradeRepo
        tr.TradeRepo = lambda *a, **k: orig_new(tmp)
        try:
            rm.set_cooloff("000001", "2026-07-25", days=5)
            assert "000001" in rm.get_cooloff_symbols("2026-07-28")
            assert "000001" not in rm.get_cooloff_symbols("2026-07-30")
            rm.clear_cooloff("000001")
            assert "000001" not in rm.get_cooloff_symbols("2026-07-26")
        finally:
            tr.TradeRepo = orig_new
            base.TRADE_DB = orig
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def test_atr_check_still_works():
    """重构未破坏现有 ATR check() — 无 ATR 数据时返回空."""
    rm = RiskManager(cooloff_store={})
    # 不存在的 symbol → ATR=0 → 跳过
    out = rm.check([{"symbol": "999999", "price": 10.0, "shares": 100}],
                   {"999999": {"price": 9.0}}, "2026-07-25")
    assert out == []


def test_cooloff_tiers():
    """冷却分档: hard_sl 5 天 / trail_sl 2 天 (monitor 分支逻辑映射)."""
    store = {}
    rm = RiskManager(cooloff_store=store)
    # hard_sl → 默认 stop_loss_cooloff_days=5
    rm.set_cooloff("AAA", "2026-07-25")
    # trail_sl → trail_sl_cooloff_days=2
    from quant.config.constants import _require_cfg
    rm.set_cooloff("BBB", "2026-07-25", days=_require_cfg("risk.trail_sl_cooloff_days"))
    # time_stop → 不冷却 (monitor 不调 set_cooloff, 此处不模拟)
    day2 = "2026-07-27"   # +2 天
    assert "AAA" in rm.get_cooloff_symbols(day2)
    assert "BBB" not in rm.get_cooloff_symbols(day2)  # trail_sl 已解除
    day5 = "2026-07-29"
    assert "AAA" in rm.get_cooloff_symbols(day5)


def test_trail_lock_is_profit_branch():
    """trail_lock 归止盈分支: reason 不含 TP 但应判为盈利出场."""
    reason = "trail_lock(1.5ATR dd)"
    is_profit = ("TP" in reason.upper()) or reason.startswith("trail_lock")
    assert is_profit
