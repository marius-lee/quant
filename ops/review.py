"""盘后复盘 — 模块F。每日收盘后运行，分析信号+交易数据。
用法: PYTHONPATH=. python3 ops/review.py
"""
import sqlite3, os, json
from datetime import date, datetime

TRADE_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trades.db")
MARKET_DB = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "market.db")


def generate_review(target_date: str = None) -> dict:
    """生成指定日期的复盘报告。默认今天。"""
    if target_date is None:
        target_date = date.today().isoformat()

    tc = sqlite3.connect(TRADE_DB)
    mc = sqlite3.connect(MARKET_DB)

    # ── 信号统计 ──
    signals = tc.execute(
        "SELECT mode, COUNT(*), AVG(score), AVG(board_count), SUM(is_bought) FROM signals WHERE date=? GROUP BY mode",
        (target_date,)
    ).fetchall()

    total_signals = sum(r[1] for r in signals)
    total_bought = sum(r[4] for r in signals)
    mode_stats = {}
    for mode, cnt, avg_score, avg_board, bought in signals:
        mode_stats[mode] = {
            "count": cnt, "avg_score": round(avg_score or 0, 3),
            "avg_board": round(avg_board or 0, 1), "bought": bought,
        }

    # ── 交易统计 ──
    buys = tc.execute(
        "SELECT symbol, price, shares, board_count FROM sim_trades WHERE side='buy' AND date=?",
        (target_date,)
    ).fetchall()

    sells = tc.execute(
        "SELECT symbol, price, shares, pnl, pnl_pct FROM sim_trades WHERE side='sell' AND date=?",
        (target_date,)
    ).fetchall()

    # 来源: 统一佣金模型 — 万三费率, 最低5元, 卖出加千一印花税
    def buy_cost(price, shares):
        val = price * shares
        return val + max(val * 0.0003, 5)
    def sell_proceeds(price, shares):
        val = price * shares
        return val - max(val * 0.0003, 5) - val * 0.001

    total_buy_cost = sum(buy_cost(r[1], r[2]) for r in buys)
    total_sell_cost = sum(r[1] * r[2] for r in sells)
    total_sell_pnl = sum(r[3] for r in sells if r[3])
    hold_symbols = [r[0] for r in buys if r[0] not in [s[0] for s in sells]]

    # ── 当前持仓估值 (依赖今日日线, 收盘同步后才准确) ──
    today_close_exists = mc.execute(
        "SELECT COUNT(*) FROM daily WHERE date=?", (target_date,)
    ).fetchone()[0] > 0

    positions_value = 0
    hold_details = []
    for sym in hold_symbols:
        buy_info = next((b for b in buys if b[0] == sym), None)
        if not buy_info:
            continue
        cost_val = buy_info[2] * buy_info[1]
        positions_value += cost_val
        detail = {
            "symbol": sym, "shares": buy_info[2], "cost": buy_info[1],
            "board_count": buy_info[3], "value": round(cost_val, 2),
        }
        if today_close_exists:
            row = mc.execute(
                "SELECT close FROM daily WHERE symbol=? AND date=?", (sym, target_date)
            ).fetchone()
            if row and row[0] > 0:
                detail["close"] = round(row[0], 2)
                detail["pnl_pct"] = round((row[0] / buy_info[1] - 1) * 100, 2)
                detail["value"] = round(buy_info[2] * row[0], 2)
        hold_details.append(detail)

    if today_close_exists:
        positions_value = sum(h.get("value", h["shares"] * h["cost"]) for h in hold_details)

    # ── 龙头信号 ──
    top_signals = tc.execute(
        "SELECT symbol, mode, score, board_count, gap_pct, reason FROM signals WHERE date=? AND is_bought=0 ORDER BY score DESC LIMIT 10",
        (target_date,)
    ).fetchall()

    tc.close()
    mc.close()

    # ── 汇总 ──
    from config.loader import get as cfg
    base_capital = float(cfg("backtest.initial_capital", 5000))
    available_cash = base_capital - total_buy_cost + sum(sell_proceeds(r[1], r[2]) for r in sells)
    total_asset = available_cash + positions_value

    return {
        "date": target_date,
        "generated_at": datetime.now().isoformat(),
        "signals": {
            "total": total_signals,
            "bought": total_bought,
            "by_mode": mode_stats,
        },
        "trades": {
            "buys": len(buys),
            "sells": len(sells),
            "total_cost": round(total_buy_cost, 2),
            "total_pnl": round(total_sell_pnl, 2),
        },
        "portfolio": {
            "available_cash": round(available_cash, 2),
            "positions_value": round(positions_value, 2),
            "total_asset": round(total_asset, 2),
            "holdings": hold_details,
        },
        "top_signals": [{
            "symbol": r[0], "mode": r[1], "score": r[2],
            "board_count": r[3], "gap_pct": r[4], "reason": r[5],
        } for r in top_signals],
    }


if __name__ == "__main__":
    report = generate_review()
    print(json.dumps(report, ensure_ascii=False, indent=2))
