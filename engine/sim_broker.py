"""模拟持仓+交易记录 — 推荐即买入, 提供持仓监控和交易历史。

模拟规则:
  - 每次推荐入库时, 自动"买入"推荐股票(每只100股, 推荐价买入)
  - 如果有新的推荐, 先"卖出"旧持仓(按新推荐日的开盘价), 再"买入"新推荐
  - 所有交易写入 trade_history 表
  - 当前持仓实时估值写入 positions 表
"""
import sqlite3, os, json
from datetime import datetime
import numpy as np
import pandas as pd
from utils.logger import get_logger

logger = get_logger("simulation.broker")

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "results.db")


def init_simulation():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            name TEXT,
            shares INTEGER NOT NULL,
            cost_price REAL NOT NULL,          -- 加权平均成本
            total_cost REAL NOT NULL,           -- 总成本(含手续费)
            buy_date TEXT NOT NULL,             -- 买入日期(推荐日期)
            run_id INTEGER REFERENCES runs(id),
            status TEXT DEFAULT 'open'          -- open / closed
        );

        CREATE TABLE IF NOT EXISTS trade_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT,
            side TEXT NOT NULL,                 -- buy / sell
            shares INTEGER NOT NULL,
            price REAL NOT NULL,
            cost REAL NOT NULL,                 -- 总价
            commission REAL NOT NULL,           -- 手续费
            run_id INTEGER REFERENCES runs(id)
        );
    """)
    conn.commit()
    conn.close()
    logger.info("simulation tables initialized")


def execute_simulation(result: dict):
    """推荐入库后执行模拟交易: 卖出旧仓 → 买入新推荐"""
    recs = result.get("recommendations", [])
    if not recs:
        return

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    run_id = conn.execute("SELECT MAX(id) FROM runs").fetchone()[0]
    today = datetime.now().strftime("%Y-%m-%d")
    new_symbols = set(r["symbol"] for r in recs)

    # 卖出不再推荐的旧持仓
    old = conn.execute("SELECT * FROM positions WHERE status='open'").fetchall()
    for row in old:
        sym = row[1]  # column 1 = symbol
        if sym not in new_symbols:
            price = next((r["last_price"] for r in recs if r["symbol"] == sym), row[4])  # cost_price as fallback
            proceeds = row[3] * price
            commission = max(5.0, proceeds * 0.0003)
            stamp = proceeds * 0.001
            conn.execute(
                "UPDATE positions SET status='closed' WHERE id=?",
                (row[0],)
            )
            conn.execute(
                "INSERT INTO trade_history (trade_date, symbol, name, side, shares, price, cost, commission, run_id) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (today, sym, row[2], "sell", row[3], price, proceeds, commission + stamp, run_id)
            )
            logger.info(f"sim sell: {sym} {row[3]}@{price:.2f} proceeds={proceeds-commission-stamp:.1f}")

    # 买入新推荐(每只100股, 推荐价)
    for rec in recs:
        symbol = rec["symbol"]
        name = rec.get("name", "")
        price = rec["last_price"]
        if price <= 0:
            logger.warning(f"sim skip {symbol}: invalid price {price}")
            continue
        shares = 100
        cost = shares * price
        commission = max(5.0, cost * 0.0003)

        # 检查是否已经持有同一只(同一推荐日)
        existing = conn.execute(
            "SELECT id FROM positions WHERE symbol=? AND status='open' AND buy_date=?",
            (symbol, today)
        ).fetchone()
        if existing:
            continue

        conn.execute("""
            INSERT INTO positions (symbol, name, shares, cost_price, total_cost, buy_date, run_id, status)
            VALUES (?,?,?,?,?,?,?,'open')
        """, (symbol, name, shares, price, cost + commission, today, run_id))

        conn.execute("""
            INSERT INTO trade_history (trade_date, symbol, name, side, shares, price, cost, commission, run_id)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (today, symbol, name, "buy", shares, price, cost, commission, run_id))

        logger.info(f"sim buy: {symbol} {shares}@{price:.2f} cost={cost+commission:.1f}")

    conn.commit()
    conn.close()


def get_positions(store=None) -> list:
    """获取当前持仓列表(含最新估值)"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM positions WHERE status='open' ORDER BY buy_date DESC"
    ).fetchall()

    positions = []
    for r in rows:
        p = dict(r)
        # 尝试获取最新价格
        if store:
            try:
                raw = store.get_daily([p["symbol"]], start=datetime.now().strftime("%Y%m%d"))
                if not raw.empty and "close" in raw:
                    latest_close = raw["close"].iloc[-1, 0] if not raw["close"].empty else p["cost_price"]
                else:
                    latest_close = p["cost_price"]
            except Exception:
                latest_close = p["cost_price"]
        else:
            latest_close = p["cost_price"]

        current_value = p["shares"] * latest_close
        pnl = current_value - p["total_cost"]
        pnl_pct = (current_value / p["total_cost"] - 1) * 100 if p["total_cost"] > 0 else 0

        p["latest_price"] = round(latest_close, 2)
        p["current_value"] = round(current_value, 2)
        p["pnl"] = round(pnl, 2)
        p["pnl_pct"] = round(pnl_pct, 2)

        positions.append(p)

    conn.close()
    return positions


def get_trades(limit: int = 50) -> list:
    """获取交易历史记录"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM trade_history ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_portfolio_summary(store=None) -> dict:
    """投资组合摘要: 总资产、总盈亏、持仓数、现金"""
    positions = get_positions(store=store)
    total_cost = sum(p["total_cost"] for p in positions)
    total_value = sum(p.get("current_value", p["total_cost"]) for p in positions)
    total_pnl = total_value - total_cost

    return {
        "total_invested": round(total_cost, 2),
        "current_value": round(total_value, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / total_cost * 100, 2) if total_cost > 0 else 0,
        "n_positions": len(positions),
        "cash_remaining": round(max(0, 5000 - total_cost), 2),
    }
