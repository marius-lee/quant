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

    # ── 交易统计 (按 strategy 分组, 5track竞技场独立核算) ──
    buys = tc.execute(
        "SELECT symbol, price, shares, board_count, strategy FROM sim_trades WHERE side='buy' AND date=?",
        (target_date,)
    ).fetchall()

    sells = tc.execute(
        "SELECT symbol, price, shares, pnl, pnl_pct, strategy FROM sim_trades WHERE side='sell' AND date=?",
        (target_date,)
    ).fetchall()

    # 来源: 统一佣金模型 — 万三费率, 最低5元, 卖出加千一印花税
    def buy_cost(price, shares):
        val = price * shares
        return val + max(val * 0.0003, 5)
    def sell_proceeds(price, shares):
        val = price * shares
        return val - max(val * 0.0003, 5) - val * 0.001

    # ── 按策略分组核算 (来源: 5 track 仓位竞技场, 每track ¥5,000独立) ──
    from config.loader import get as cfg
    CHEN_TRACKS = {"chen"}

    strats = set()
    for b in buys: strats.add(b[4])
    for s in sells: strats.add(s[5])

    strat_capital = {}
    strat_buys = {s: [] for s in strats}
    strat_sells = {s: [] for s in strats}

    for b in buys:
        strat_buys[b[4]].append(b)
    for s in sells:
        strat_sells[s[5]].append(s)

    # 计算每个策略的独立资金
    for st in strats:
        st_buys_sum = sum(buy_cost(b[1], b[2]) for b in strat_buys.get(st, []))
        st_sells_sum = sum(sell_proceeds(s[1], s[2]) for s in strat_sells.get(st, []))
        # 查找该策略在今日之前的 capital_after (用于推断起始资金)
        row = tc.execute(
            "SELECT capital_after FROM sim_trades WHERE strategy=? AND capital_after IS NOT NULL AND date < ? ORDER BY id DESC LIMIT 1",
            (st, target_date)
        ).fetchone()
        if row:
            init_cap = row[0]
        else:
            init_cap = float(cfg("backtest.initial_capital", 5000))
        strat_capital[st] = init_cap - st_buys_sum + st_sells_sum

    # 汇总 (buys/sells 保持原结构供后续使用)
    buys_nostrat = [(b[0], b[1], b[2], b[3]) for b in buys]
    sells_nostrat = [(s[0], s[1], s[2], s[3], s[4]) for s in sells]

    total_buy_cost = sum(buy_cost(b[1], b[2]) for b in buys if b[4] in CHEN_TRACKS)
    total_sell_cost = sum(s[1] * s[2] for s in sells if s[5] in CHEN_TRACKS)
    total_sell_pnl = sum(s[3] for s in sells if s[5] in CHEN_TRACKS and s[3])
    available_cash = sum(v for st, v in strat_capital.items() if st in CHEN_TRACKS)
    hold_symbols = [r[0] for r in buys if r[4] in CHEN_TRACKS and r[0] not in [s[0] for s in sells if s[5] in CHEN_TRACKS]]

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

    # ── 持仓暴露监控 (来源: Narang 4章/10章 — 暴露监控是风险模型的核心) ──
    exposure = {"sectors": {}, "board_dist": {}, "note": "Narang 10章: 每日监控防止意外集中暴露"}
    for h in hold_details:
        sym = h["symbol"]
        value = h.get("value", h["shares"] * h["cost"])
        sec_row = mc.execute("SELECT industry FROM stocks WHERE symbol=?", (sym,)).fetchone()
        sector = sec_row[0] if sec_row and sec_row[0] else "未知"
        exposure["sectors"][sector] = exposure["sectors"].get(sector, 0) + value
        board = h.get("board_count", 0)
        label = f"{board}连板" if board > 0 else "非连板"
        exposure["board_dist"][label] = exposure["board_dist"].get(label, 0) + value

    mc.close()

    # ── 汇总 ──
    chen_available = sum(strat_capital.get(st, 0) for st in CHEN_TRACKS)
    chen_total = chen_available + positions_value
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
            "buys_by_strategy": {st: len(bs) for st, bs in strat_buys.items() if bs},
        },
        "portfolio": {
            "available_cash": round(chen_available, 2),
            "all_tracks_cash": round(available_cash, 2),
            "positions_value": round(positions_value, 2),
            "total_asset": round(total_asset, 2),
            "chen_total": round(chen_total, 2),
            "track_breakdown": {st: round(v, 2) for st, v in strat_capital.items()},
            "exposure": exposure,
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
