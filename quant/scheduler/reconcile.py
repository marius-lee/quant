"""OMS 日终对账闭环 (报告 §1.6) — 每日 15:05, monitor 收盘后运行.

业界标准 EOD reconciliation 三件事:
  1. 持仓对账 — 当日 daily_signals 目标 vs 实际持仓
     (仅调仓日有意义; weekly 非调仓日信号不更新, 记 skip 行)
  2. 现金对账 — initial_capital + Σ(卖净额) - Σ(买总额+费) vs 实际现金
     (全账本重算, 每日不变的完整性检查, 与信号无关)
  3. 订单审计 — 当日 pending_orders 按状态/撤单原因汇总
     + 残留 pending (= monitor 未跑完, 异常)

结果落 daily_recon 表 (幂等: 先删当日重写); break 超阈值 →
metrics counter + log error. 不自动纠偏下单 — 次日 09:30 execute
的 delta 天然修正, 自动补单会与信号流打架.

注意: task_log 由 Runner 统一管理，任务模块不再调用 _tk_start/_tk_finish。
"""
import json
import sqlite3
import time as _time
import uuid as _uuid
from datetime import time

from quant.config.constants import _require_cfg
from quant.config.paths import TRADE_DB
from quant.monitor.metrics import metrics as _m
from quant.utils.logger import get_logger, set_trace_id

_log = get_logger(__name__)


def _conn(db_path: str = TRADE_DB) -> sqlite3.Connection:
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute(f"PRAGMA busy_timeout={_require_cfg('data.sqlite.busy_timeout')}")
    return c


def ensure_table(db_path: str = TRADE_DB):
    c = _conn(db_path)
    c.execute("""
        CREATE TABLE IF NOT EXISTS daily_recon (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            strategy TEXT NOT NULL DEFAULT 'quant',
            kind TEXT NOT NULL,            -- position | cash | order
            symbol TEXT NOT NULL DEFAULT '',
            expected REAL,
            actual REAL,
            drift REAL,
            status TEXT NOT NULL DEFAULT 'ok',   -- ok | break | skip
            detail TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_recon_day "
              "ON daily_recon(date, strategy, kind)")
    c.commit()
    c.close()


# ── 1. 持仓对账 ──

def _recon_positions(day: str, strategy: str, db_path: str) -> list[dict]:
    from quant.data.repos import TradeRepo
    from quant.execution.engine import ExecutionEngine
    repo = TradeRepo(db_path=db_path)
    engine = ExecutionEngine(db_path=db_path)

    sig = repo.get_latest_signals(strategy)
    if not sig or sig["date"] != day:
        return [{"kind": "position", "symbol": "", "expected": None,
                 "actual": None, "drift": None, "status": "skip",
                 "detail": json.dumps(
                     {"reason": "no fresh signals (non-rebalance day)",
                      "signals_date": sig["date"] if sig else None},
                     ensure_ascii=False)}]

    targets = {t["symbol"]: t["shares"] for t in sig["targets"]}
    actual = {p["symbol"]: p["shares"]
              for p in engine.get_positions(strategy)}
    rows = []
    for sym in sorted(set(targets) | set(actual)):
        exp = targets.get(sym, 0)
        act = actual.get(sym, 0)
        drift = act - exp
        if abs(drift) > 0:
            rows.append({"kind": "position", "symbol": sym,
                         "expected": exp, "actual": act, "drift": drift,
                         "status": "break", "detail": "position drift"})
        else:
            rows.append({"kind": "position", "symbol": sym,
                         "expected": exp, "actual": act, "drift": 0,
                         "status": "ok", "detail": ""})
    return rows


# ── 2. 现金对账 ──

def _recon_cash(day: str, strategy: str, db_path: str) -> list[dict]:
    from quant.data.repos import TradeRepo
    from quant.execution.engine import ExecutionEngine
    repo = TradeRepo(db_path=db_path)
    engine = ExecutionEngine(db_path=db_path)

    initial = repo.get_initial_capital(strategy)
    if initial is None:
        _log.warning(f"[{day}] initial_capital not found for {strategy}")
        return []

    sells, buys, fees = repo.get_daily_flow(day, strategy)
    expected_cash = initial + sells - buys - fees
    actual_cash = engine.get_cash(strategy)
    drift = actual_cash - expected_cash

    # P0-2 fix: drift 容差由配置读取, 默认 1.0 (报告 §1.6)
    tol = _require_cfg("recon.cash_drift_tolerance")
    status = "break" if abs(drift) > tol else "ok"
    if status == "break":
        _m.inc("recon.cash_break")

    return [{"kind": "cash", "symbol": "", "expected": expected_cash,
             "actual": actual_cash, "drift": drift, "status": status,
             "detail": f"initial={initial} sells={sells} buys={buys} fees={fees} tol={tol}"}]


# ── 3. 订单审计 ──

def _recon_orders(day: str, strategy: str, db_path: str) -> list[dict]:
    from quant.data.repos import TradeRepo
    repo = TradeRepo(db_path=db_path)
    orders = repo.get_orders(day, strategy)
    rows = []
    for o in orders:
        if o["status"] == "pending":
            rows.append({"kind": "order", "symbol": o["symbol"],
                         "expected": o["shares"], "actual": 0,
                         "drift": -o["shares"], "status": "break",
                         "detail": f"pending: {o.get('reason','unknown')}"})
    return rows


def run_reconcile(day: str, strategy: str = "quant", db_path: str = TRADE_DB) -> dict:
    """完整对账流程, 结果写入 daily_recon 表."""
    ensure_table(db_path)
    
    # 先执行子对账函数（它们会各自建连接、自动关闭），避免长连接持锁导致死锁
    all_rows = []
    all_rows.extend(_recon_positions(day, strategy, db_path))
    all_rows.extend(_recon_cash(day, strategy, db_path))
    all_rows.extend(_recon_orders(day, strategy, db_path))
    
    # 再开新连接写入结果
    c = _conn(db_path)
    try:
        c.execute("DELETE FROM daily_recon WHERE date=? AND strategy=?", (day, strategy))
        for r in all_rows:
            c.execute("""
                INSERT INTO daily_recon (date, strategy, kind, symbol,
                    expected, actual, drift, status, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (day, strategy, r["kind"], r["symbol"],
                  r["expected"], r["actual"], r["drift"], r["status"], r["detail"]))
        c.commit()
    finally:
        c.close()

    breaks = [r for r in all_rows if r["status"] == "break"]
    if breaks:
        _m.inc("scheduler.reconcile.break", len(breaks))
    return {"status": "break" if breaks else "ok", "breaks": len(breaks), "rows": all_rows}


def get_recon(day: str = None, strategy: str = "quant",
              db_path: str = TRADE_DB) -> dict:
    """读取已落库的对账结果 (web API 用). day=None → 最近一个对账日."""
    ensure_table(db_path)
    c = _conn(db_path)
    if day is None:
        row = c.execute(
            "SELECT MAX(date) AS d FROM daily_recon WHERE strategy=?",
            (strategy,)).fetchone()
        day = row["d"] if row and row["d"] else None
    if day is None:
        c.close()
        return {"date": None, "status": "no_data", "rows": []}
    rows = [dict(r) for r in c.execute(
        "SELECT kind, symbol, expected, actual, drift, status, detail,"
        " created_at FROM daily_recon WHERE date=? AND strategy=?"
        " ORDER BY kind, symbol", (day, strategy)).fetchall()]
    c.close()
    breaks = [r for r in rows if r["status"] == "break"]
    return {"date": day, "strategy": strategy,
            "status": "break" if breaks else "ok",
            "breaks": len(breaks), "rows": rows}


def _run(today: str):
    _uuid.uuid4().hex[:12]
    set_trace_id(_uuid.uuid4().hex[:12])
    from quant.execution.engine import ExecutionEngine
    from quant.data.repos import TradeRepo

    result = run_reconcile(today)
    # v410: 写入 daily_equity 快照 (回撤告警 + Sharpe 计算依赖)
    engine = ExecutionEngine()
    cash = engine.get_cash("quant")
    positions = engine.get_positions("quant")
    pos_value = sum(p["shares"] * p.get("price", 0) for p in positions)
    TradeRepo().record_daily_equity(today, cash, pos_value)
    _m.inc("scheduler.reconcile.ok")
    return {"recon_status": result["status"], "breaks": result["breaks"], "elapsed": 0}


if __name__ == "__main__":
    import sys
    _run(sys.argv[1] if len(sys.argv) > 1 else "2026-08-10")