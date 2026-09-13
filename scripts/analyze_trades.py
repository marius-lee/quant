"""Analyze backtest trades to identify top-performing stocks and patterns."""
from quant.utils.logger import offline_mode
from quant.utils.excepthook import setup
setup()

with offline_mode():
    from quant.backtest.loop import run_backtest

    r = run_backtest(
        start_date="2025-08-29",
        end_date="2026-09-08",
        capital=5000,
        strategy="nano_analysis_v644",
        factor_status_filter="active",
        combine_mode="equal_weight",
        universe_size=500,
        retrain_freq=5,
    )

    trades = r["trades"]
    print(f"Total trades: {len(trades)}")
    symbols = sorted(set(t["symbol"] for t in trades))
    print(f"Unique symbols: {symbols}")

    # Top 10 best-performing trades
    sorted_trades = sorted(trades, key=lambda t: t.get("pnl_pct", 0), reverse=True)[:10]
    print("\n=== Top 10 Best Trades ===")
    for t in sorted_trades:
        sym = t["symbol"]
        pnl = t.get("pnl_pct", 0)
        entry = t.get("entry_price", "?")
        exit_p = t.get("exit_price", "?")
        print(f"  {sym:10s} entry={entry:>8} exit={exit_p:>8} pnl={pnl:+.1f}%")

    # Analyze which stocks contributed most to the 20x
    print("\n=== P&L by Symbol ===")
    from collections import defaultdict
    sym_pnl = defaultdict(float)
    sym_count = defaultdict(int)
    for t in trades:
        sym_pnl[t["symbol"]] += t.get("pnl_pct", 0)
        sym_count[t["symbol"]] += 1
    for sym in sorted(sym_pnl, key=lambda s: sym_pnl[s], reverse=True)[:10]:
        print(f"  {sym:10s} total_pnl={sym_pnl[sym]:+.1f}% trades={sym_count[sym]}")
