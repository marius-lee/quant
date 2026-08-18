# OMS 日终对账测试 (报告 1.6) — 持仓/现金/订单三账 + filled_vs_trades 交叉核对.
import sqlite3

import pytest

from quant.data.repos import TradeRepo
from quant.execution.engine import ExecutionEngine, Order
from quant.scheduler.reconcile import get_recon, run_reconcile

DAY = "2026-07-25"
YDAY = "2026-07-24"


@pytest.fixture()
def env(tmp_path):
    db = str(tmp_path / "trades.db")
    repo = TradeRepo(db_path=db)
    repo.set_initial_capital("quant", 100000.0)
    eng = ExecutionEngine(db_path=db)
    return {"db": db, "repo": repo, "eng": eng}


def _seed_position(env, symbol="699999", price=10.0, shares=100, cost=5.0):
    env["eng"].execute([Order(symbol=symbol, side="buy", shares=shares,
                              price=price, cost=cost)], YDAY, "quant")


def _insert_pending(db, symbol="099999", status="filled", shares=100,
                    reason=None, day=DAY):
    c = sqlite3.connect(db)
    c.execute(
        "INSERT INTO pending_orders (day, strategy, symbol, side, target_shares,"
        " limit_price, status, placed_at, cancel_reason) "
        "VALUES (?, 'quant', ?, 'buy', ?, 10.0, ?, '2026-07-25 09:30:00', ?)",
        (day, symbol, shares, status, reason))
    c.commit()
    c.close()


def test_position_recon_ok(env):
    """信号目标 == 实际持仓 → 全部 ok."""
    _seed_position(env)
    env["repo"].save_signals(DAY, [{"symbol": "699999", "shares": 100,
                                    "score": 9, "price": 10.0}], 100000.0)
    r = run_reconcile(DAY, db_path=env["db"])
    assert r["status"] == "ok"
    assert r["positions"]["checked"] == 1 and r["positions"]["drifted"] == 0


def test_position_recon_drift(env):
    """目标有但持仓无 (封板未买到) → position break."""
    _seed_position(env)
    env["repo"].save_signals(DAY, [
        {"symbol": "699999", "shares": 100, "score": 9, "price": 10.0},
        {"symbol": "099999", "shares": 100, "score": 8, "price": 5.0},
    ], 100000.0)
    r = run_reconcile(DAY, db_path=env["db"])
    assert r["status"] == "break"
    assert r["positions"]["drifted"] == 1


def test_cash_invariant_and_equity_cross(env):
    """现金: 不变量 cash>=0 + 跨源 equity_cross 一致 → ok.

    v533: equity_cross 改为当日快照口径 — 原 YDAY 快照在 DAY 对账时
    恒 skip (行为变更: 跨日漂移由 daily_equity 曲线监控)."""
    _seed_position(env)
    env["repo"].record_daily_equity(DAY, env["eng"].get_cash("quant"),
                                    1000.0, strategy="quant")
    env["repo"].save_signals(DAY, [{"symbol": "699999", "shares": 100,
                                    "score": 9, "price": 10.0}], 100000.0)
    r = run_reconcile(DAY, db_path=env["db"])
    eq = next(c for c in r["cash"] if c["check"] == "equity_cross")
    assert eq["status"] == "ok" and abs(eq["drift"]) <= 1.0
    inv = next(c for c in r["cash"] if c["check"] == "invariant")
    assert inv["status"] == "ok" and inv["actual"] > 0


def test_equity_cross_detects_tampering(env):
    """当日快照后账本被改 (篡改 daily_equity 现金) → equity_cross break.

    v533: 篡改检测语义随当日快照口径更新 — 快照与对账同日 (原 YDAY 篡改
    属跨日, 改由 daily_equity 曲线 + alerts Rule 1 监控)."""
    _seed_position(env)
    env["repo"].record_daily_equity(DAY, env["eng"].get_cash("quant"),
                                    1000.0, strategy="quant")
    # 篡改场景: 同日快照现金被改 → drift 出现
    env["repo"].record_daily_equity(DAY, 90000.0, 1000.0, strategy="quant")
    r = run_reconcile(DAY, db_path=env["db"])
    eq = next(c for c in r["cash"] if c["check"] == "equity_cross")
    assert eq["status"] == "break" and abs(eq["drift"]) > 1.0


def test_order_audit_stale_pending(env):
    """收盘后残留 pending → break (monitor 未跑完)."""
    _insert_pending(env["db"], status="pending")
    r = run_reconcile(DAY, db_path=env["db"])
    stale = [o for o in r["orders"] if o.get("order_status") == "pending"]
    assert r["status"] == "break" and stale


def test_filled_but_no_trade(env):
    """B-29 场景: pending 标 filled 但账本无当日买入 → 跨源 break."""
    _insert_pending(env["db"], symbol="099999", status="filled", shares=100)
    r = run_reconcile(DAY, db_path=env["db"])
    cross = [o for o in r["orders"] if o.get("check") == "filled_but_no_trade"]
    assert r["status"] == "break" and len(cross) == 1
    assert cross[0]["order_shares"] == 100 and cross[0]["ledger_shares"] == 0


def test_filled_with_matching_trade_ok(env):
    """filled + 账本有对应买入 → 无 filled_but_no_trade break."""
    _seed_position(env, symbol="099999", price=5.0, shares=100, cost=3.0)
    # 买入日期须为 DAY (filled 当日)
    env["eng"].execute([Order(symbol="099999", side="buy", shares=100,
                              price=5.0, cost=3.0)], DAY, "quant")
    _insert_pending(env["db"], symbol="099999", status="filled", shares=100)
    r = run_reconcile(DAY, db_path=env["db"])
    cross = [o for o in r["orders"] if o.get("check") == "filled_but_no_trade"]
    assert cross == []


def test_get_recon_readback(env):
    """落库后 get_recon 读回: 幂等 + 结构完整."""
    _seed_position(env)
    env["repo"].save_signals(DAY, [{"symbol": "699999", "shares": 100,
                                    "score": 9, "price": 10.0}], 100000.0)
    run_reconcile(DAY, db_path=env["db"])
    run_reconcile(DAY, db_path=env["db"])  # 幂等: 重跑不重复
    g = get_recon(day=DAY, db_path=env["db"])
    assert g["date"] == DAY and g["status"] == "ok"
    pos = [r for r in g["rows"] if r["kind"] == "position"]
    assert len(pos) == 1  # 重跑未产生重复行
    # day=None → 最近对账日
    g2 = get_recon(day=None, db_path=env["db"])
    assert g2["date"] == DAY


def test_non_rebalance_day_skip(env):
    """weekly 非调仓日 (信号日期 < 今日): position 记 skip, 不算 break."""
    _seed_position(env)
    env["repo"].save_signals(YDAY, [{"symbol": "699999", "shares": 100,
                                     "score": 9, "price": 10.0}], 100000.0)
    r = run_reconcile(DAY, db_path=env["db"])
    assert r["positions"]["skipped"] is True
    assert r["positions"]["drifted"] == 0
