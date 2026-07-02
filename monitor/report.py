"""监控报告 — 日/周绩效报告生成 + Web 状态推送。"""
from utils.logger import get_logger
logger = get_logger("monitor.report")

import json
from typing import Optional
from datetime import date, datetime
from monitor.attribution import compute_sharpe, compute_max_drawdown, compute_win_rate


def generate_report(
    report_date: str,
    cash_balance: float,
    positions: list[dict],
    trades: list[dict],
    pnl_total: float = 0.0,
    initial_capital: float = 5000.0,
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
    # 从交易历史估算收益率序列 (简化: 用每日 capital_after 推算)
    daily_returns = []
    if trades:
        prev_cap = initial_capital
        for t in sorted(trades, key=lambda x: x.get("date", "")):
            cap_after = t.get("capital_after", prev_cap)
            if prev_cap > 0:
                daily_returns.append(cap_after / prev_cap - 1)
            prev_cap = cap_after

    import pandas as pd
    if daily_returns:
        ret_series = pd.Series(daily_returns)
        sharpe = compute_sharpe(ret_series)
        mdd = compute_max_drawdown(ret_series)
    else:
        sharpe = 0.0
        mdd = 0.0

    win_rate = compute_win_rate(trades)

    # 计算未实现盈亏 (简化: 当前持仓市值 vs 成本)
    unrealized = 0.0
    realized = 0.0
    for t in trades:
        if t.get("pnl", 0):
            realized += t.get("pnl", 0)

    # 计算持仓市值
    positions_value = sum(
        p.get("price", 0) * p.get("shares", 0) for p in positions
    )
    total_wealth = cash_balance + positions_value
    logger.info(f"[report] {report_date}: cash=¥{cash_balance:.0f} pos=¥{positions_value:.0f} total=¥{total_wealth:.0f} return={total_return*100:.1f}%")
    total_return = (total_wealth - initial_capital) / initial_capital

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
    try:
        from web.shared import update_state
        update_state(report)
    except Exception:
        pass
