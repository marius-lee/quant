"""监控报告 — 日/周绩效报告生成 + Web 状态推送。"""
from quant.utils.logger import get_logger
logger = get_logger("monitor.report")

import json
from typing import Optional
from datetime import date, datetime
from quant.monitor.attribution import compute_sharpe, compute_max_drawdown, compute_win_rate


def generate_report(
    report_date: str,
    cash_balance: float,
    positions: list[dict],
    trades: list[dict],
    initial_capital: float,
    pnl_total: float = 0.0,
) -> dict:
    """生成日报。

    返回结构:
    {
      "date": str,
      "capital": float,
      "pnl": {"total": float, "realized": float, "unrealized": float},
      "positions": list[dict],
      "metrics": {
        "total_return": float,
        "sharpe_rolling_20d": float,
        "max_drawdown": float,
        "win_rate": float,
        "trade_count": int,
      },
      "trades": list[dict],
    }
    """
    import pandas as pd

    # Sharpe/MDD 优先用 daily_equity (每日估值权益曲线, 更准确)
    # 回退到逐笔交易估算 (2026-07-22: audit "Sharpe计算" 项)
    ret_series = None
    try:
        from quant.data.trade_repo import TradeRepo
        repo = TradeRepo()
        eq_rows = repo.get_connection().execute(
            "SELECT date, total_equity FROM daily_equity ORDER BY date"
        ).fetchall()
        if len(eq_rows) >= 20:
            eq_series = pd.Series(
                [r[1] for r in eq_rows],
                index=[r[0] for r in eq_rows]
            )
            ret_series = eq_series.pct_change().dropna()
            logger.debug(f"[report] Sharpe from daily_equity: {len(ret_series)} daily returns")
    except Exception:
        logger.debug("[report] daily_equity unavailable, fallback to trade-based returns")

    if ret_series is None or len(ret_series) < 5:
        # Fall back: 从逐笔交易 capital_after 估算
        daily_returns = []
        if trades:
            prev_cap = initial_capital
            for t in sorted(trades, key=lambda x: x.get("date", "")):
                cap_after = t.get("capital_after", prev_cap)
                if prev_cap > 0:
                    daily_returns.append(cap_after / prev_cap - 1)
                prev_cap = cap_after
        if daily_returns:
            ret_series = pd.Series(daily_returns)
            logger.debug(f"[report] Sharpe from trades: {len(ret_series)} returns")
        else:
            ret_series = pd.Series(dtype=float)

    if len(ret_series) >= 5:
        sharpe = compute_sharpe(ret_series)
        mdd = compute_max_drawdown(ret_series)
    else:
        sharpe = 0.0
        mdd = 0.0

    win_rate = compute_win_rate(trades)

    # 计算未实现盈亏: 当前持仓(close - buy_price) * shares
    unrealized = 0.0
    realized = 0.0
    for t in trades:
        if t.get("pnl", 0):
            realized += t.get("pnl", 0)
    # 从日线取最新收盘价估算未实现盈亏 (2026-07-21 audit M6)
    if positions:
        from quant.data.store import DataStore
        try:
            ds = DataStore()
            syms = [p.get("symbol") for p in positions if p.get("symbol")]
            if syms:
                last_date = ds._connect().execute("SELECT MAX(date) FROM daily").fetchone()[0]
                if last_date:
                    closes = {r[0]: r[1] for r in ds._connect().execute(
                        f"SELECT symbol, close FROM daily WHERE date='{last_date}' AND symbol IN ({','.join('?'*len(syms))})",
                        syms
                    )}
                    for p in positions:
                        sym = p.get("symbol", "")
                        cur = closes.get(sym, p.get("price", 0))
                        unrealized += (cur - p.get("price", 0)) * p.get("shares", 0)
            ds.close()
        except Exception:
            pass  # 日线不可用时不阻塞报告生成

    # 计算持仓市值
    positions_value = sum(
        p.get("price", 0) * p.get("shares", 0) for p in positions
    )
    total_wealth = cash_balance + positions_value
    total_return = (total_wealth - initial_capital) / initial_capital
    logger.info(f"[report] {report_date}: cash=¥{cash_balance:.0f} pos=¥{positions_value:.0f} total=¥{total_wealth:.0f} return={total_return*100:.1f}%")

    report = {
        "date": report_date,
        "capital": {
            "cash": round(cash_balance, 2),
            "positions_value": round(positions_value, 2),
            "total_wealth": round(total_wealth, 2),
        },
        "pnl": {
            "total": round(pnl_total, 2),
            "realized": round(realized, 2),
            "unrealized": round(unrealized, 2),
        },
        "positions": positions,
        "metrics": {
            "total_return": round(total_return, 4),
            "total_return_pct": round(total_return * 100, 1),
            "sharpe_rolling_20d": round(sharpe, 2),
            "max_drawdown": round(mdd, 4),
            "max_drawdown_pct": round(mdd * 100, 1),
            "win_rate": round(win_rate, 2),
            "trade_count": len(trades),
        },
        "trades": trades[-20:],
    }

    return report


def push_to_web(report: dict):
    logger.debug(f"[web-push] pushing report to shared state")
    """推送报告到 Web 前端 (通过 web/shared.py 内存共享)。"""
    from web.shared import update_state
    update_state(report)
