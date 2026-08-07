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
"""
import json
import sqlite3
import time as _time
import uuid as _uuid
from datetime import time

from quant.config.constants import _require_cfg
from quant.config.paths import TRADE_DB
from quant.monitor.metrics import metrics as _m
from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish
from quant.utils.logger import get_logger, set_trace_id

_log = get_logger(__name__)


def _conn(db_path: str = TRADE_DB) -> sqlite3.Connection:
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
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
        # weekly 非调仓日: 信号不更新, 目标 vs 持仓无对账意义
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
        rows.append({"kind": "position", "symbol": sym,
                     "expected": exp, "actual": act, "drift": drift,
                     "status": "ok" if drift == 0 else "break",
                     "detail": ""})
    return rows


# ── 2. 现金对账 ──
# 注: 本系统现金由 sim_trades 账本推导 (get_cash 同公式), 全账本重算与
# 之同源 = 循环论证, 无检查价值. 有意义的两项:
#   a) 不变量: cash >= 0 (负现金 = 记账/裁剪逻辑出 bug, 真实 break)
#   b) 跨源: 最近 daily_equity 快照现金 + (快照日, 今日] 全部净流水
#      vs 今日现金. 快照来自 attribution 盘后落库, 属独立第二源;
#      流水区间覆盖快照漏跑天数 (2026-07-26 生产假种子事件后加固,
#      原"仅当日流水"在快照滞后时必误报)

def _recon_cash(day: str, strategy: str, db_path: str) -> list[dict]:
    from quant.data.repos import TradeRepo
    from quant.execution.engine import ExecutionEngine
    repo = TradeRepo(db_path=db_path)
    engine = ExecutionEngine(db_path=db_path)
    actual = round(engine.get_cash(strategy), 2)
    rows = [{"kind": "cash", "symbol": "invariant", "expected": 0.0,
             "actual": actual, "drift": None,
             "status": "ok" if actual >= 0 else "break",
             "detail": json.dumps({"check": "cash >= 0"},
                                  ensure_ascii=False)}]
    c = _conn(db_path)
    y = c.execute(
        "SELECT date, cash FROM daily_equity WHERE strategy=? AND date < ? "
        "ORDER BY date DESC LIMIT 1", (strategy, day)).fetchone()
    if not y:
        c.close()
        rows.append({"kind": "cash", "symbol": "equity_cross",
                     "expected": None, "actual": actual, "drift": None,
                     "status": "skip",
                     "detail": json.dumps({"reason": "no prior daily_equity"},
                                          ensure_ascii=False)})
        return rows
    flow = c.execute(
        "SELECT side, SUM(price*shares) AS amt, SUM(COALESCE(cost,0)) AS fee "
        "FROM sim_trades WHERE strategy=? AND mode='live' "
        "AND date > ? AND date <= ? GROUP BY side",
        (strategy, y["date"], day)).fetchall()
    c.close()
    buy_amt = sell_amt = buy_fee = sell_fee = 0.0
    for r in flow:
        if r["side"] == "buy":
            buy_amt, buy_fee = r["amt"] or 0.0, r["fee"] or 0.0
        elif r["side"] == "sell":
            sell_amt, sell_fee = r["amt"] or 0.0, r["fee"] or 0.0
    expected = round(float(y["cash"]) + (sell_amt - sell_fee)
                     - (buy_amt + buy_fee), 2)
    drift = round(actual - expected, 2)
    tol = _require_cfg("recon.cash_drift_tolerance")
    rows.append({"kind": "cash", "symbol": "equity_cross",
                 "expected": expected, "actual": actual, "drift": drift,
                 "status": "ok" if abs(drift) <= tol else "break",
                 "detail": json.dumps(
                     {"prior_equity_date": y["date"],
                      "prior_cash": float(y["cash"]),
                      "today_buy": buy_amt, "today_sell": sell_amt,
                      "today_fees": buy_fee + sell_fee},
                     ensure_ascii=False)})
    return rows


# ── 3. 订单审计 ──

def _audit_orders(day: str, strategy: str, db_path: str) -> list[dict]:
    c = _conn(db_path)
    rows = c.execute(
        "SELECT status, COALESCE(cancel_reason,'') AS reason, COUNT(*) AS n, "
        "SUM(target_shares) AS shares FROM pending_orders "
        "WHERE day=? AND strategy=? GROUP BY status, reason",
        (day, strategy)).fetchall()
    out = []
    for r in rows:
        status = r["status"]
        # 收盘后仍有 pending = monitor 未跑完, 异常 break
        is_break = (status == "pending")
        out.append({"kind": "order",
                    "symbol": f"{status}:{r['reason']}",
                    "expected": None, "actual": r["n"],
                    "drift": None,
                    "status": "break" if is_break else "ok",
                    "detail": json.dumps({"order_status": status,
                                          "reason": r["reason"],
                                          "count": r["n"],
                                          "shares": r["shares"] or 0},
                                         ensure_ascii=False)})
    # ── filled vs 账本交叉核对 (跨源, 核心检查) ──
    # pending 标 filled 但 sim_trades 无对应当日买入 = OMS 状态与账本脱节
    # (B-29 修复前的 order_manager._fill 就会制造这种 break)
    filled = c.execute(
        "SELECT symbol, target_shares FROM pending_orders "
        "WHERE day=? AND strategy=? AND status='filled'",
        (day, strategy)).fetchall()
    for f in filled:
        t = c.execute(
            "SELECT COALESCE(SUM(shares),0) AS s FROM sim_trades "
            "WHERE date=? AND strategy=? AND mode='live' AND side='buy'"
            " AND symbol=?",
            (day, strategy, f["symbol"])).fetchone()
        if (t["s"] or 0) < f["target_shares"]:
            out.append({"kind": "order", "symbol": f["symbol"],
                        "expected": f["target_shares"],
                        "actual": t["s"] or 0,
                        "drift": (t["s"] or 0) - f["target_shares"],
                        "status": "break",
                        "detail": json.dumps(
                            {"check": "filled_but_no_trade",
                             "order_shares": f["target_shares"],
                             "ledger_shares": t["s"] or 0},
                            ensure_ascii=False)})
    c.close()
    return out


# ── 入口 ──

def run_reconcile(day: str, strategy: str = "quant",
                  db_path: str = TRADE_DB) -> dict:
    """跑三账对账并落库 (幂等). 返回汇总 dict — scheduler/web/test 共用."""
    ensure_table(db_path)
    rows = (_recon_positions(day, strategy, db_path)
            + _recon_cash(day, strategy, db_path)
            + _audit_orders(day, strategy, db_path))
    c = _conn(db_path)
    c.execute("DELETE FROM daily_recon WHERE date=? AND strategy=?",
              (day, strategy))
    for r in rows:
        c.execute(
            "INSERT INTO daily_recon (date, strategy, kind, symbol, expected,"
            " actual, drift, status, detail) VALUES (?,?,?,?,?,?,?,?,?)",
            (day, strategy, r["kind"], r["symbol"], r["expected"],
             r["actual"], r["drift"], r["status"], r["detail"]))
    c.commit()
    c.close()
    breaks = [r for r in rows if r["status"] == "break"]
    pos_rows = [r for r in rows if r["kind"] == "position"]
    summary = {
        "date": day, "strategy": strategy,
        "status": "break" if breaks else "ok",
        "breaks": len(breaks),
        "positions": {
            "checked": len([r for r in pos_rows if r["status"] != "skip"]),
            "drifted": len([r for r in pos_rows if r["status"] == "break"]),
            "skipped": any(r["status"] == "skip" for r in pos_rows),
        },
        "cash": [{"check": r["symbol"], "expected": r["expected"],
                  "actual": r["actual"], "drift": r["drift"],
                  "status": r["status"]}
                 for r in rows if r["kind"] == "cash"],
        "orders": [json.loads(r["detail"]) for r in rows
                   if r["kind"] == "order"],
    }
    if breaks:
        _m.inc("recon.break", len(breaks))
        _log.error(f"[{day}] RECON BREAK x{len(breaks)}: "
                   + "; ".join(f"{r['kind']}:{r['symbol']}" for r in breaks))
    else:
        _eq = next((r for r in summary["cash"] if r["check"] == "equity_cross"),
                   None)
        _log.info(f"[{day}] recon OK: pos={summary['positions']}, "
                  f"cash_drift={_eq['drift'] if _eq else 'n/a'}")
    return summary


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
    tid = _uuid.uuid4().hex[:12]
    set_trace_id(tid)
    rid = _tk_start("reconcile", today, grace_seconds=600)
    if rid is None:
        _log.info(f"[{today}] reconcile already running, skip duplicate trigger")
        return
    status = "failed"
    error_msg = None
    summary = {}
    t0 = _time.time()
    try:
        result = run_reconcile(today)
        status = "ok"
        summary = {"recon_status": result["status"],
                   "breaks": result["breaks"],
                   "elapsed": round(_time.time() - t0, 1)}
        # v410: 写入 daily_equity 快照 (回撤告警 + Sharpe 计算依赖)
        # P0-2: get_cash/get_positions 无 mode 参数 (engine.py:77/203),
        # 原 TypeError 被 except: pass 吞掉 → 快照天天缺失, 告警静默失效.
        from quant.execution.engine import ExecutionEngine
        from quant.data.repos import TradeRepo
        engine = ExecutionEngine()
        cash = engine.get_cash("quant")
        positions = engine.get_positions("quant")
        pos_value = sum(p["shares"] * p.get("price", 0) for p in positions)
        TradeRepo().record_daily_equity(today, cash, pos_value)
        _log.info(f"[SCHEDULER] {today} | TASK=reconcile | STATUS=OK | "
                  f"recon={result['status']} breaks={result['breaks']} | "
                  f"elapsed={summary['elapsed']}s")
        _m.inc("scheduler.reconcile.ok")
    except Exception as e:
        error_msg = str(e)
        _log.exception(f"[{today}] reconcile crashed: {e}")
        raise
    finally:
        _tk_finish("reconcile", today, status, error=error_msg, summary=summary)
