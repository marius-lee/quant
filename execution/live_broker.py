"""半自动券商桥 — 系统出指令, 人工执行, 系统追踪。

ManualBroker:
  - 生成买卖订单 (信号→目标持仓→买卖清单)
  - 记录人工确认的成交价/数量
  - 追踪持仓盈亏
  - 所有数据持久化到 data/live.db

流程:
  1. trading_loop 调用 generate_orders() → 生成待执行订单
  2. 人工在 Web 控制台查看订单 → 券商APP手动下单
  3. 人工在 Web 控制台填入成交价 → 调用 record_fill()
  4. 系统自动计算 P&L → update_positions()

数据表 (live.db):
  - orders: 生成的订单 (pending/filled/cancelled)
  - positions: 当前持仓 (实时估值)
  - trades: 成交记录
  - daily_pnl: 每日盈亏
"""

import json
import os
import sqlite3
from datetime import date, datetime
from typing import Optional

import pandas as pd

from backtest import compute_commission
from config.loader import get as cfg
from utils.logger import get_logger

logger = get_logger("execution.live_broker")

LIVE_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "live.db")

_conn: Optional[sqlite3.Connection] = None


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(LIVE_DB, check_same_thread=False)
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _conn.row_factory = sqlite3.Row
    return _conn


def init_live_db():
    conn = get_conn()
    # Migration: add peak_price column if not exists
    try:
        conn.execute("ALTER TABLE live_positions ADD COLUMN peak_price REAL NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            order_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT,
            side TEXT NOT NULL CHECK(side IN ('buy','sell')),
            shares INTEGER NOT NULL,
            signal_price REAL,
    reason TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','executed','cancelled','expired')),
            filled_price REAL,
            filled_shares INTEGER,
            filled_at TEXT,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS live_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT NOT NULL,
            name TEXT,
            shares INTEGER NOT NULL CHECK(shares > 0),
            cost_price REAL NOT NULL,
            total_cost REAL NOT NULL,
            buy_date TEXT NOT NULL,
            peak_price REAL NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
            close_date TEXT,
            close_price REAL,
            close_reason TEXT
        );

        CREATE TABLE IF NOT EXISTS live_trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT,
            side TEXT NOT NULL CHECK(side IN ('buy','sell')),
            shares INTEGER NOT NULL,
            price REAL NOT NULL,
            amount REAL NOT NULL,
            commission REAL NOT NULL,
            order_id INTEGER REFERENCES orders(id)
        );

        CREATE TABLE IF NOT EXISTS live_daily_pnl (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            cash REAL NOT NULL DEFAULT 5000,
            portfolio_value REAL NOT NULL,
            total_asset REAL NOT NULL,
            daily_return REAL,
            cumulative_return REAL,
            n_positions INTEGER,
            alerts TEXT
        );

        CREATE TABLE IF NOT EXISTS live_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    # 初始化初始资金
    conn.execute("""
        INSERT OR IGNORE INTO live_config (key, value)
        VALUES ('initial_capital', ?)
    """, (str(cfg("backtest.initial_capital", 5000)),))
    conn.commit()
    logger.info("live trading DB initialized")


def get_initial_capital() -> float:
    row = get_conn().execute(
        "SELECT value FROM live_config WHERE key='initial_capital'"
    ).fetchone()
    return float(row[0]) if row else 5000.0


def set_initial_capital(amount: float):
    get_conn().execute(
        "INSERT OR REPLACE INTO live_config (key, value) VALUES ('initial_capital', ?)",
        (str(amount),)
    )
    get_conn().commit()


def get_cash() -> float:
    """计算当前可用现金: 初始资金 - 已投入 + 已卖出回款"""
    initial = get_initial_capital()
    conn = get_conn()
    # 所有买入的总成本
    buy_total = conn.execute(
        "SELECT COALESCE(SUM(amount + commission), 0) FROM live_trades WHERE side='buy'"
    ).fetchone()[0]
    # 所有卖出的总收入(扣除费用后)
    sell_total = conn.execute(
        "SELECT COALESCE(SUM(amount - commission), 0) FROM live_trades WHERE side='sell'"
    ).fetchone()[0]
    return initial - buy_total + sell_total


def get_positions(store=None, use_live: bool = True) -> list[dict]:
    """获取当前持仓(含实时估值)。

    use_live=True 时优先使用新浪财经实时行情 (交易时段)，
    回退到 DB 日线收盘价 (非交易时段/网络异常)。
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM live_positions WHERE status='open' ORDER BY buy_date DESC"
    ).fetchall()

    if not rows:
        return []

    # Build base position list
    positions = []
    for r in rows:
        p = dict(r)
        p["latest_price"] = p["cost_price"]
        positions.append(p)

    # Try live quotes first (during market hours)
    live_quotes = {}
    if use_live:
        try:
            from execution.quote import fetch_quotes, is_trading_time
            if is_trading_time():
                symbols = [p["symbol"] for p in positions]
                live_quotes = fetch_quotes(symbols)
                if live_quotes:
                    logger.info(f"live quotes: {len(live_quotes)}/{len(symbols)} symbols")
        except Exception:
            pass  # fallback to DB prices below

    # Fallback to DB daily close for symbols without live quote
    need_db = [p["symbol"] for p in positions if p["symbol"] not in live_quotes]
    db_prices = {}
    if need_db and store:
        try:
            raw = store.get_daily(need_db)
            if not raw.empty and "close" in raw.columns:
                for sym in need_db:
                    try:
                        lp = float(raw["close"].iloc[-1][sym])
                        if lp and lp > 0:
                            db_prices[sym] = lp
                    except Exception:
                        pass
        except Exception:
            pass

    # Merge prices and compute PnL
    for p in positions:
        sym = p["symbol"]
        if sym in live_quotes:
            p["latest_price"] = round(live_quotes[sym]["price"], 2)
            p["change_pct"] = live_quotes[sym]["change_pct"]
            p["is_live"] = True
        elif sym in db_prices:
            p["latest_price"] = round(db_prices[sym], 2)
            p["is_live"] = False
        else:
            p["is_live"] = False

        current_value = p["shares"] * p["latest_price"]
        pnl = current_value - p["total_cost"]
        pnl_pct = (pnl / p["total_cost"] * 100) if p["total_cost"] > 0 else 0
        p["current_value"] = round(current_value, 2)
        p["pnl"] = round(pnl, 2)
        p["pnl_pct"] = round(pnl_pct, 2)

        # Track peak price for trailing stop
        stored_peak = p.get("peak_price", 0) or 0
        new_peak = max(stored_peak, p["latest_price"], p["cost_price"])
        p["peak_price"] = round(new_peak, 2)

    # Persist updated peak prices and timestamp
    from datetime import datetime
    now = datetime.now().isoformat()
    try:
        for p in positions:
            conn.execute("UPDATE live_positions SET peak_price=?, updated_at=? WHERE id=?",
                        (p["peak_price"], now, p["id"]))
        conn.commit()
    except Exception:
        pass

    return positions


def get_portfolio_summary(store=None) -> dict:
    """组合摘要"""
    positions = get_positions(store=store)
    total_cost = sum(p["total_cost"] for p in positions)
    total_value = sum(p.get("current_value", 0) for p in positions)
    total_pnl = total_value - total_cost
    cash = get_cash()
    total_asset = cash + total_value

    return {
        "total_invested": round(total_cost, 2),
        "market_value": round(total_value, 2),
        "total_pnl": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl / total_cost * 100, 2) if total_cost > 0 else 0,
        "cash": round(cash, 2),
        "total_asset": round(total_asset, 2),
        "total_return_pct": round((total_asset / get_initial_capital() - 1) * 100, 2),
        "n_positions": len(positions),
    }


def generate_orders(recommendations: list[dict], top_n: int = None) -> list[dict]:
    """根据推荐列表生成待执行订单。下单前过风控。

    风控接入:
      - 止损触发 → 自动生成卖出订单
      - 回撤熔断 → 禁止新买入, 只生成卖出订单
      - 集中度告警 → 附加到订单 reason

    返回: [order dict]，含 symbol/name/side/shares/signal_price/reason
    """
    if top_n is None:
        top_n = cfg("backtest.max_positions", 3)

    conn = get_conn()
    current = set(r[0] for r in conn.execute(
        "SELECT symbol FROM live_positions WHERE status='open'"
    ).fetchall())

    # ── 风控全量扫描 ──
    stop_loss_sells = []
    block_new = False
    risk_alerts = []
    try:
        from execution.risk_checker import RiskChecker
        positions = get_positions()
        cash = get_cash()
        risk = RiskChecker()
        scan = risk.full_scan(positions, cash)
        risk_alerts = scan.get("alerts", [])
        block_new = scan.get("block_new_positions", False)

        # 止损 → 自动生成卖单
        for action in scan.get("actions", []):
            if action.get("action") == "sell":
                stop_loss_sells.append(action)
                logger.warning(f"RISK: auto stop-loss {action['symbol']} — {action['reason']}")
            elif action.get("action") == "liquidate_all":
                # 清仓: 所有持仓生成卖单
                for p in positions:
                    if p["symbol"] not in [s["symbol"] for s in stop_loss_sells]:
                        stop_loss_sells.append({
                            "symbol": p["symbol"],
                            "action": "sell",
                            "reason": action["reason"],
                            "shares": p["shares"],
                        })
                block_new = True
                logger.critical(f"RISK: liquidation triggered — {action['reason']}")
    except Exception as e:
        logger.warning(f"risk scan failed, proceeding without: {e}")

    if block_new:
        logger.warning("RISK: new positions blocked (drawdown circuit breaker or liquidation)")

    # ── 卖出: 止损卖单优先 + 调仓卖出 ──
    target_symbols = [
        r["symbol"] for r in recommendations[:top_n]
        if r.get("last_price", 0) > 0 and r.get("last_price", 0) <= cfg("affordable.max_stock_price", 50)
    ]
    target_set = set(target_symbols)
    to_sell = [s for s in current if s not in target_set]

    # 止损卖单的去重: 避免重复生成已在调仓卖出列表中的股票
    existing_sell_syms = set(to_sell)
    orders = []

    # 1. 止损卖单 (优先级最高)
    for sl in stop_loss_sells:
        sym = sl["symbol"]
        if sym in existing_sell_syms:
            continue  # 已在调仓卖出中
        pos = conn.execute(
            "SELECT shares, name FROM live_positions WHERE symbol=? AND status='open'",
            (sym,)
        ).fetchone()
        if pos:
            orders.append({
                "symbol": sym,
                "name": pos[1] or sym,
                "side": "sell",
                "shares": sl.get("shares", pos[0]),
                "signal_price": sl.get("current_price", 0),
                "max_price": 0,
                "reason": f"🛑 风控止损: {sl['reason']}",
            })
            existing_sell_syms.add(sym)

    # 2. 调仓卖出
    for sym in to_sell:
        pos = conn.execute(
            "SELECT shares, cost_price, name FROM live_positions WHERE symbol=? AND status='open'",
            (sym,)
        ).fetchone()
        if pos:
            orders.append({
                "symbol": sym,
                "name": pos[2] or sym,
                "side": "sell",
                "shares": pos[0],
                "signal_price": next((r["last_price"] for r in recommendations if r["symbol"] == sym), 0),
                "max_price": 0,
                "reason": "调仓换股 — 不再推荐",
            })

    # 3. 买入新推荐 (如未被风控阻止)
    if block_new:
        logger.warning(f"RISK BLOCK: {len(target_symbols)} buy candidates suppressed")
        to_buy = []
    else:
        to_buy = [s for s in target_symbols if s not in current]

    cash = get_cash()
    if to_buy and cash > 0:
        remaining = cash
        capital_per = cash / len(to_buy)
        for sym in to_buy:
            rec = next(r for r in recommendations if r["symbol"] == sym)
            price = rec.get("last_price", 0)
            if price <= 0:
                continue
            max_shares = int(min(capital_per, remaining) / price / 100) * 100
            if max_shares < 100:
                max_shares = 100  # 最小1手
            cost = max_shares * price
            if cost > remaining:
                continue
            remaining -= cost
            orders.append({
                "symbol": sym,
                "name": rec.get("name") or sym,
                "side": "buy",
                "shares": max_shares,
                "signal_price": price,
                "max_price": price * 1.02,  # 买入上限: 信号价+2%
                "reason": f"信号推荐 rank={target_symbols.index(sym)+1} score={rec.get('score', 0):.4f}",
            })

    return orders


def save_orders(orders: list[dict]) -> int:
    """保存订单到数据库。返回订单数。用 order_date 去重避免重复插。"""
    conn = get_conn()
    today = date.today().isoformat()

    # 先删除今天的旧 pending 订单
    conn.execute(
        "DELETE FROM orders WHERE order_date=? AND status='pending'",
        (today,)
    )

    for o in orders:
        conn.execute("""
            INSERT INTO orders (created_at, order_date, symbol, name, side, shares,
                              signal_price, reason, status)
            VALUES (?,?,?,?,?,?,?,?,'pending')
        """, (
            datetime.now().isoformat(), today,
            o["symbol"], o.get("name", ""), o["side"],
            o["shares"], o.get("signal_price"),
            o.get("reason", ""),
        ))

    conn.commit()
    logger.info(f"saved {len(orders)} orders for {today}")
    return len(orders)


def get_pending_orders(date_str: str = None) -> list[dict]:
    if date_str is None:
        date_str = date.today().isoformat()
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM orders WHERE order_date=? AND status='pending' ORDER BY side, symbol",
        (date_str,)
    ).fetchall()
    return [dict(r) for r in rows]


def record_fill(order_id: int, filled_price: float, filled_shares: int = None,
                notes: str = "") -> dict:
    """记录成交。

    Args:
        order_id: 订单ID
        filled_price: 实际成交价
        filled_shares: 实际成交股数(默认=订单股数)
        notes: 备注

    Returns: {ok, msg}
    """
    conn = get_conn()
    order = conn.execute(
        "SELECT * FROM orders WHERE id=? AND status='pending'",
        (order_id,)
    ).fetchone()

    if not order:
        return {"ok": False, "msg": f"订单 #{order_id} 不存在或已执行"}

    if filled_shares is None:
        filled_shares = order[6]  # shares column

    if filled_price <= 0:
        return {"ok": False, "msg": "成交价必须大于0"}

    today = date.today().isoformat()
    now = datetime.now().isoformat()
    amount = filled_shares * filled_price
    fee, stamp, commission = compute_commission(amount, is_sell=(order[5] == "sell"))

    # 更新订单状态
    conn.execute("""
        UPDATE orders SET status='executed', filled_price=?, filled_shares=?,
        filled_at=?, notes=?
        WHERE id=?
    """, (filled_price, filled_shares, now, notes, order_id))

    # 记录交易
    conn.execute("""
        INSERT INTO live_trades (trade_date, symbol, name, side, shares, price, amount, commission, order_id)
        VALUES (?,?,?,?,?,?,?,?,?)
    """, (today, order[3], order[4], order[5], filled_shares, filled_price, amount, commission, order_id))

    # 更新持仓
    symbol = order[3]
    name = order[4]
    side = order[5]

    if side == "buy":
        # 检查是否已有持仓
        existing = conn.execute(
            "SELECT id, shares, total_cost FROM live_positions WHERE symbol=? AND status='open'",
            (symbol,)
        ).fetchone()

        if existing:
            new_shares = existing[1] + filled_shares
            new_cost = existing[2] + amount + commission
            new_avg = new_cost / new_shares
            conn.execute("""
                UPDATE live_positions SET shares=?, cost_price=?, total_cost=?
                WHERE id=?
            """, (new_shares, new_avg, new_cost, existing[0]))
        else:
            cost_px = (amount + commission) / filled_shares
            conn.execute("""
                INSERT INTO live_positions (symbol, name, shares, cost_price, total_cost, buy_date, peak_price, status)
                VALUES (?,?,?,?,?,?,?,'open')
            """, (symbol, name, filled_shares, cost_px, amount + commission, today, cost_px))

        logger.info(f"LIVE BUY: {symbol} {filled_shares}@{filled_price:.2f} cost={amount+commission:.1f}")

    elif side == "sell":
        pos = conn.execute(
            "SELECT id, shares, total_cost FROM live_positions WHERE symbol=? AND status='open'",
            (symbol,)
        ).fetchone()

        if pos:
            remaining = pos[1] - filled_shares
            if remaining <= 0:
                conn.execute(
                    "UPDATE live_positions SET status='closed', close_date=?, close_price=?, close_reason='已卖出' WHERE id=?",
                    (today, filled_price, pos[0])
                )
            else:
                # 部分卖出
                conn.execute(
                    "UPDATE live_positions SET shares=? WHERE id=?",
                    (remaining, pos[0])
                )
            logger.info(f"LIVE SELL: {symbol} {filled_shares}@{filled_price:.2f} proceeds={amount-commission:.1f}")
        else:
            logger.warning(f"attempted sell of unheld {symbol}")

    conn.commit()

    # ── 偏差监控: 记录成交滑点 ──
    try:
        from execution.monitor import init_monitor_db, log_trade
        init_monitor_db()
        signal_price = float(order[7]) if order[7] else filled_price  # signal_price column
        log_trade(today, symbol, side, signal_price, filled_price,
                  filled_shares, "filled", notes)
    except Exception:
        logger.exception("monitor logging failed")

    # 更新每日盈亏
    _update_daily_pnl()

    return {"ok": True, "msg": f"{'买入' if side == 'buy' else '卖出'} {symbol} {filled_shares}股 @¥{filled_price:.2f}"}


def cancel_order(order_id: int, reason: str = "") -> dict:
    """取消订单"""
    conn = get_conn()
    conn.execute(
        "UPDATE orders SET status='cancelled', notes=? WHERE id=? AND status='pending'",
        (reason or "手动取消", order_id)
    )
    conn.commit()
    return {"ok": True, "msg": f"订单 #{order_id} 已取消"}


def _update_daily_pnl(store=None):
    """更新今日盈亏记录"""
    today = date.today().isoformat()
    summary = get_portfolio_summary(store=store)

    # 当日收益: 对比昨天的 total_asset
    conn = get_conn()
    yesterday = conn.execute(
        "SELECT total_asset, cumulative_return FROM live_daily_pnl WHERE date<? ORDER BY date DESC LIMIT 1",
        (today,)
    ).fetchone()

    prev_asset = yesterday[0] if yesterday else summary["total_asset"]
    daily_ret = (summary["total_asset"] / prev_asset - 1) if prev_asset > 0 else 0
    initial = get_initial_capital()
    cum_ret = (summary["total_asset"] / initial - 1) if initial > 0 else 0

    conn.execute("""
        INSERT OR REPLACE INTO live_daily_pnl
        (date, cash, portfolio_value, total_asset, daily_return, cumulative_return, n_positions, alerts)
        VALUES (?,?,?,?,?,?,?,?)
    """, (
        today, summary["cash"], summary["market_value"],
        summary["total_asset"], round(daily_ret, 6), round(cum_ret, 6),
        summary["n_positions"], "[]"
    ))
    conn.commit()


def update_positions_market(store=None) -> dict:
    """用最新行情更新持仓估值。在每日收盘后调用。"""
    _update_daily_pnl(store=store)
    return get_portfolio_summary(store=store)


def get_trade_history(limit: int = 50) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM live_trades ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    trades = [dict(r) for r in rows]
    return trades


def get_order_history(date_str: str = None, limit: int = 50) -> list[dict]:
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    if date_str:
        rows = conn.execute(
            "SELECT * FROM orders WHERE order_date=? ORDER BY id DESC LIMIT ?",
            (date_str, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_daily_pnl_history(days: int = 90) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM live_daily_pnl ORDER BY date DESC LIMIT ?", (days,)
    ).fetchall()
    result = [dict(r) for r in rows]
    result.reverse()
    return result


def get_pnl_summary() -> dict:
    """盈亏摘要: 总投入、总市值、总盈亏、胜率"""
    conn = get_conn()
    conn.row_factory = sqlite3.Row

    # 所有 closed 持仓的盈亏
    closed = conn.execute("""
        SELECT symbol, shares, cost_price, total_cost, close_price
        FROM live_positions WHERE status='closed'
    """).fetchall()

    closed_pnl = sum(
        (r["close_price"] or 0) * r["shares"] - r["total_cost"]
        for r in closed
    )

    # 开放持仓的未实现盈亏
    portfolio = get_portfolio_summary()

    # 胜率: 从 daily_pnl 表
    win_days = conn.execute(
        "SELECT COUNT(*) FROM live_daily_pnl WHERE daily_return > 0"
    ).fetchone()[0]
    total_days = conn.execute(
        "SELECT COUNT(*) FROM live_daily_pnl"
    ).fetchone()[0]

    # 交易统计
    n_trades = conn.execute("SELECT COUNT(*) FROM live_trades").fetchone()[0]
    n_buys = conn.execute("SELECT COUNT(*) FROM live_trades WHERE side='buy'").fetchone()[0]
    n_sells = conn.execute("SELECT COUNT(*) FROM live_trades WHERE side='sell'").fetchone()[0]

    return {
        "total_asset": portfolio["total_asset"],
        "market_value": portfolio["market_value"],
        "cash": portfolio["cash"],
        "total_return_pct": portfolio["total_return_pct"],
        "realized_pnl": round(closed_pnl, 2),
        "unrealized_pnl": round(portfolio["total_pnl"], 2),
        "win_rate": round(win_days / max(total_days, 1) * 100, 1),
        "n_trading_days": total_days,
        "n_trades": n_trades,
        "n_buys": n_buys,
        "n_sells": n_sells,
        "initial_capital": get_initial_capital(),
    }


def end_of_day(store=None):
    """每日收盘处理: 更新估值、计算盈亏、标记过期订单。"""
    conn = get_conn()

    # 过期所有今日未执行的 pending 订单
    today = date.today().isoformat()
    conn.execute(
        "UPDATE orders SET status='expired', notes='当日未执行, 已过期' "
        "WHERE order_date=? AND status='pending'",
        (today,)
    )

    conn.commit()
    _update_daily_pnl(store=store)
    logger.info(f"end-of-day processing complete for {today}")


def close():
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None
