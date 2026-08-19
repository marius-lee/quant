#!/usr/bin/env python3
"""逐笔信号复盘 — 实盘交易 vs 发出信号时的因子排名/得分。

用途: 区分"选股问题" vs "持仓管理问题"。对 sim_trades 中每笔买入,
回溯当日 daily_signals 的 targets (含 score/reason), 标注该股票在信号中的
排名与得分; 结合卖出的 pnl 判断: 高分买入后亏损 = 选股信号失效;
低分/未入选却买入 = 执行问题; 盈利后回吐 = 止盈/持仓管理问题。

版本: v1 (2026-08-19, v560)
用法: PYTHONPATH=. .venv/bin/python scripts/review_trades.py [strategy] [--start 2026-07-22]
幂等: 只读, 不写任何库。
"""
import sys
import json
import sqlite3
from datetime import datetime

sys.path.insert(0, "")

from quant.config.paths import TRADE_DB


def main(strategy: str = "quant", start: str = "2026-01-01"):
    conn = sqlite3.connect(str(TRADE_DB), timeout=10)
    conn.row_factory = sqlite3.Row

    trades = conn.execute(
        "SELECT * FROM sim_trades WHERE strategy=? AND date>=? ORDER BY date, id",
        (strategy, start),
    ).fetchall()
    print(f"共 {len(trades)} 笔交易 (strategy={strategy}, from {start})")

    signals_by_date = {}
    for r in conn.execute(
        "SELECT date, signals_json FROM daily_signals WHERE strategy=? ORDER BY date",
        (strategy,),
    ):
        try:
            data = json.loads(r["signals_json"])
            if isinstance(data, list):
                targets = data
            elif isinstance(data, dict):
                targets = data.get("targets", []) or []
            else:
                targets = []
            signals_by_date[r["date"]] = targets
        except Exception:
            signals_by_date[r["date"]] = []

    # 每笔买入回溯信号: 用买入当日信号 (盘前生成) 定位
    print("\n=== 逐笔买入信号对照 ===")
    stats = {"high_score_buy": 0, "high_score_loss": 0, "chase_add": 0, "no_signal": 0}
    rows_out = []
    for t in trades:
        if t["side"] != "buy":
            continue
        d, sym = t["date"], t["symbol"]
        targets = signals_by_date.get(d, [])
        idx = next((i for i, x in enumerate(targets) if x.get("symbol") == sym), None)
        if idx is None:
            stats["no_signal"] += 1
            rows_out.append((d, sym, t["price"], t["shares"], None, None, "无信号(执行/风控买入)"))
            continue
        score = targets[idx].get("score")
        reason = targets[idx].get("reason", "")
        rank = idx + 1
        # 该股票此前是否已持有 (加仓判定: 同一 symbol 之前有更早 buy)
        prev_buys = [x for x in trades
                     if x["symbol"] == sym and x["side"] == "buy" and x["id"] < t["id"]]
        is_add = len(prev_buys) > 0
        tag = f"加仓#{rank}" if is_add else f"新仓#{rank}"
        rows_out.append((d, sym, t["price"], t["shares"], rank, score, f"{tag} | {reason}"))
        if is_add:
            stats["chase_add"] += 1
        if score is not None and score >= 0:
            stats["high_score_buy"] += 1

    # 卖出配对盈亏 (FIFO 简化: 该 symbol 后续第一笔 sell 与之配对)
    print(f"{'日期':<12}{'代码':<8}{'买价':>8}{'股数':>6}  {'信号rank':>8}{'得分':>10}  说明")
    buy_seq = {}
    for t in trades:
        if t["side"] == "buy":
            buy_seq.setdefault(t["symbol"], []).append(t)
    for d, sym, px, sh, rank, score, note in rows_out:
        print(f"{d:<12}{sym:<8}{px:>8.2f}{sh:>6}  {str(rank or '-'):>8}{str(score if score is not None else '-'):>10}  {note}")

    # 亏损笔 (卖出 pnl<0) 与信号对照
    print("\n=== 亏损卖出 vs 买入信号 ===")
    print(f"{'卖出日':<12}{'代码':<8}{'卖价':>8}{'PnL':>8}  {'买入信号rank/得分'}")
    for t in trades:
        if t["side"] != "sell" or (t["pnl"] or 0) >= 0:
            continue
        # 该 symbol 最近一次买入
        buys = [x for x in trades if x["symbol"] == t["symbol"] and x["side"] == "buy" and x["id"] < t["id"]]
        if not buys:
            continue
        last_buy = buys[-1]
        sig_info = "?"
        targets = signals_by_date.get(last_buy["date"], [])
        idx = next((i for i, x in enumerate(targets) if x.get("symbol") == t["symbol"]), None)
        if idx is not None:
            sig_info = f"#{idx+1} score={targets[idx].get('score')} {targets[idx].get('reason','')[:40]}"
        print(f"{t['date']:<12}{t['symbol']:<8}{t['price']:>8.2f}{t['pnl']:>8.2f}  {sig_info}")
        if idx is not None and targets[idx].get("score", 0) >= 0:
            stats["high_score_loss"] += 1

    print("\n=== 汇总 ===")
    print(f"买入笔: {sum(1 for t in trades if t['side']=='buy')}")
    print(f"  其中加仓笔: {stats['chase_add']}")
    print(f"  有信号且得分≥0: {stats['high_score_buy']}")
    print(f"  无信号(执行/风控买入): {stats['no_signal']}")
    print(f"亏损卖出中 买入时得分≥0: {stats['high_score_loss']}")

    conn.close()
    return rows_out


if __name__ == "__main__":
    strat = sys.argv[1] if len(sys.argv) > 1 else "quant"
    start = "2026-07-22"
    if "--start" in sys.argv:
        i = sys.argv.index("--start")
        if i + 1 < len(sys.argv):
            start = sys.argv[i + 1]
    main(strat, start)