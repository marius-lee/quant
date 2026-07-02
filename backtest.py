"""多日回测 — 在历史数据上批量运行 pipeline，追踪 PnL 曲线。

用法:
  PYTHONPATH=. python3 backtest.py                    # 最近 60 个交易日
  PYTHONPATH=. python3 backtest.py 2026-01-01 2026-06-30 5000  # 指定区间+本金
"""

import sys, os, time, json, sqlite3
from datetime import datetime, timedelta
from collections import defaultdict

import pandas as pd
import numpy as np

from data.store import DataStore
from execution.engine import ExecutionEngine
from execution.cost import CostModel
from utils.logger import get_logger

logger = get_logger("backtest")
TRADE_DB = os.path.join(os.path.dirname(__file__), "data", "trades.db")
LOT_SIZE = 100


def run_backtest(start_date="2026-01-01", end_date="2026-06-30", capital=5000):
    """在 [start_date, end_date] 区间内逐日运行 pipeline。

    返回: DataFrame(index=date, columns=[cash, positions_value, total_wealth, return])
    """
    import pipeline

    # 清理旧交易记录
    if os.path.exists(TRADE_DB):
        os.remove(TRADE_DB)

    store = DataStore()
    engine = ExecutionEngine()
    engine.set_initial_capital("quant", capital)

    # 获取区间内所有交易日
    all_dates = [r[0] for r in store._connect().execute(
        "SELECT DISTINCT date FROM daily WHERE date >= ? AND date <= ? ORDER BY date",
        (start_date, end_date)
    ).fetchall()]

    # 至少需要 60 天数据做因子计算 (lookback window)
    if len(all_dates) < 65:
        logger.warning(f"Only {len(all_dates)} trading days, need >= 65 for factor lookback")
        all_dates = all_dates  # still try

    # 每周调仓一次 (减少交易成本)
    rebalance_dates = all_dates[::5]  # 每5个交易日一次
    logger.info(f"Backtest: {len(all_dates)} trading days, {len(rebalance_dates)} rebalance dates")

    results = []
    initial_cap = capital
    for i, date_str in enumerate(rebalance_dates):
        t0 = time.time()
        try:
            engine = ExecutionEngine()  # fresh engine to read current DB
            cap_before = engine.get_capital("quant")
            positions_before = engine.get_positions("quant")

            result = pipeline.run(date_str=date_str, capital=capital, strategy="quant")

            # 计算当日总资产
            # get_capital() now returns total wealth (cash + positions_value)
            total_wealth = engine.get_capital("quant")
            positions_after = engine.get_positions("quant")
            pos_value = sum(p["price"] * p["shares"] for p in positions_after)

            day_return = (total_wealth - initial_cap) / initial_cap if initial_cap > 0 else 0
            # Track cumulative capital
            initial_cap = total_wealth  # for next day comparison

            optimizer_info = result["steps"].get("optimizer", {})
            exec_info = result["steps"].get("execution", {})

            results.append({
                "date": date_str,
                "cash": round(total_wealth - pos_value, 2),
                "positions_value": round(pos_value, 2),
                "total_wealth": round(total_wealth, 2),
                "return": round(day_return, 6),
                "positions": optimizer_info.get("positions", 0),
                "orders": exec_info.get("orders", 0),
                "elapsed": round(time.time() - t0, 1),
            })

            logger.info(
                f"[{i+1}/{len(rebalance_dates)}] {date_str}: "
                f"wealth=¥{total_wealth:,.2f}, return={day_return*100:+.2f}%, "
                f"{optimizer_info.get('positions',0)} pos, {result['elapsed_sec']}s"
            )

        except Exception as e:
            logger.error(f"Backtest failed on {date_str}: {e}")
            results.append({"date": date_str, "error": str(e)})
            continue

    store.close()

    df = pd.DataFrame(results)
    if not df.empty and "total_wealth" in df.columns:
        # 计算统计量
        daily_rets = df.set_index("date")["total_wealth"].pct_change().dropna()
        if len(daily_rets) > 1:
            sharpe = (daily_rets.mean() * 252) / (daily_rets.std() * np.sqrt(252)) if daily_rets.std() > 0 else 0
            cum_return = (df["total_wealth"].iloc[-1] / capital - 1) if capital > 0 else 0
            logger.info(
                f"\n=== Backtest Summary ==="
                f"\n  Period: {start_date} → {end_date}"
                f"\n  Rebalance days: {len(rebalance_dates)}"
                f"\n  Final wealth: ¥{df['total_wealth'].iloc[-1]:,.2f}"
                f"\n  Cumulative return: {cum_return*100:+.1f}%"
                f"\n  Sharpe (est): {sharpe:.3f}"
            )

    return df


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else "2026-01-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-30"
    cap = float(sys.argv[3]) if len(sys.argv) > 3 else 5000

    df = run_backtest(start, end, cap)
    print(f"\nResults saved ({len(df)} rows)")
    print(df.to_string())
