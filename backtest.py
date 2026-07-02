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
from data.benchmark import sync_benchmark
from utils.logger import get_logger

logger = get_logger("backtest")
TRADE_DB = os.path.join(os.path.dirname(__file__), "data", "trades.db")
LOT_SIZE = 100


def run_backtest(start_date="2026-01-01", end_date="2026-06-30", capital=5000):
    """在 [start_date, end_date] 区间内逐日运行 pipeline。

    返回: DataFrame(index=date, columns=[cash, positions_value, total_wealth, return])
    """
    import pipeline
    from optimizer.rebalance import Order as PipelineOrder  # P0-2: for daily stop-loss

    # 清理旧交易记录
    if os.path.exists(TRADE_DB):
        os.remove(TRADE_DB)

    store = DataStore()
    # 同步基准指数数据
    sync_benchmark("000300")
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

    # 每2周调仓一次 (减少交易成本)
    rebalance_dates = all_dates[::10]  # 每5个交易日一次
    logger.info(f"Backtest: {len(all_dates)} trading days, {len(rebalance_dates)} rebalance dates")

    results = []
    original_capital = capital  # 初始本金，全程不变，用于计算累计收益率
    prev_total_wealth = capital  # 首个交易日前一日总资产=初始本金，避免 NameError
    prev_wealth_for_daily = capital  # P0-2: 用于日频止损间的 wealth 追踪
    
    sl_checks = 0  # P0-2: 止损触发计数
    for day_idx, date_str in enumerate(all_dates):
        t0 = time.time()
        try:
            # P0-2: On every trading day, check stop-loss first
            current_positions = engine.get_positions("quant")
            if current_positions:
                stop_loss_pct = pipeline.cfg("risk.stop_loss_pct", 0.15)
                prices_for_sl = {}
                try:
                    # Get today's close prices for all held symbols
                    syms = [p["symbol"] for p in current_positions]
                    daily_sl = store.get_daily(syms, start=date_str, end=date_str)
                    if not daily_sl.empty:
                        close_sl = daily_sl["close"]
                        if date_str in close_sl.index:
                            prices_for_sl = close_sl.loc[date_str].to_dict()
                except Exception:
                    pass
                for p in current_positions:
                    cost_basis = p.get("price", 0)
                    current_px = prices_for_sl.get(p["symbol"], None)
                    if current_px is None or current_px <= 0 or cost_basis <= 0:
                        continue
                    drop = (current_px - cost_basis) / cost_basis
                    if drop <= -stop_loss_pct:
                        shares = int(p["shares"])
                        if shares > 0:
                            logger.warning(f"[SL-DAILY] {date_str}: {p['symbol']} drop={drop:.1%}, selling {shares} shares")
                            engine.execute([PipelineOrder(symbol=p["symbol"], side="sell", shares=shares, price=current_px, cost=0)], date_str, "quant")
                            sl_checks += 1
            
            # Only run full pipeline on rebalance dates
            if date_str not in rebalance_dates:
                # Non-rebalance day: just record wealth  
                total_wealth = engine.get_capital("quant")
                daily_return = (total_wealth - prev_wealth_for_daily) / prev_wealth_for_daily if prev_wealth_for_daily > 0 else 0
                prev_wealth_for_daily = total_wealth
                results.append({
                    "date": date_str,
                    "wealth": round(total_wealth, 2),
                    "daily_return": round(daily_return, 6),
                    "type": "daily_check",
                })
                continue

            # pipeline.run() needs capital for generate_report's initial_capital reference
            result = pipeline.run(date_str=date_str, capital=capital, strategy="quant", skip_pull=True)

            # 计算当日总资产
            # get_capital() now returns total wealth (cash + positions_value)
            total_wealth = engine.get_capital("quant")
            positions_after = engine.get_positions("quant")
            pos_value = sum(p["price"] * p["shares"] for p in positions_after)

            # 累计收益率 (全期, 基于初始本金)
            cumulative_return = (total_wealth - original_capital) / original_capital if original_capital > 0 else 0
            daily_return = (total_wealth - prev_total_wealth) / prev_total_wealth if prev_total_wealth > 0 else 0
            prev_total_wealth = total_wealth

            optimizer_info = result["steps"].get("optimizer", {})
            exec_info = result["steps"].get("execution", {})

            results.append({
                "date": date_str,
                "cash": round(total_wealth - pos_value, 2),
                "positions_value": round(pos_value, 2),
                "total_wealth": round(total_wealth, 2),
                "cumulative_return": round(cumulative_return, 6),
                "daily_return": round(daily_return, 6),
                "positions": optimizer_info.get("positions", 0),
                "orders": exec_info.get("orders", 0),
                "elapsed": round(time.time() - t0, 1),
            })

            logger.info(
                f"[{rebalance_dates.index(date_str)+1}/{len(rebalance_dates)}] {date_str}: "
                f"wealth=¥{total_wealth:,.2f}, return={cumulative_return*100:+.2f}%, "
                f"{optimizer_info.get('positions',0)} pos, {result['elapsed_sec']}s"
            )

        except Exception as e:
            logger.error(f"Backtest failed on {date_str}: {e}")
            results.append({"date": date_str, "error": str(e)})
            continue

    store.close()

    df = pd.DataFrame(results)
    cum_return = 0.0  # 初始化，确保 bench 对比段可安全引用
    if not df.empty and "wealth" in df.columns:
        # P0-2: daily results use "wealth" column; rebalance results use "total_wealth"
        if "total_wealth" not in df.columns:
            df["total_wealth"] = df["wealth"]
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

    # ── Benchmark comparison ──
    try:
        bench_code = "000300"  # 沪深300
        bench_returns = store.get_benchmark(bench_code, start=start_date)
        if not bench_returns.empty:
            # Align benchmark returns to backtest dates
            bench_dates = [r["date"] for r in results if "date" in r]
            bench_aligned = bench_returns.reindex(pd.to_datetime(bench_dates)).dropna()
            if len(bench_aligned) > 1:
                bench_cum = (1 + bench_aligned).prod() - 1
                # Excess return (alpha)
                if "cumulative_return" in df.columns and not df.empty:
                    strategy_cum = df["cumulative_return"].dropna().iloc[-1] if not df["cumulative_return"].dropna().empty else 0
                else:
                    strategy_cum = cum_return  # 已在循环前初始化为 0.0
                excess = strategy_cum - bench_cum
                # Tracking error (annualized)
                strategy_daily = df.set_index("date")["total_wealth"].pct_change().dropna()
                common_idx = strategy_daily.index.intersection(bench_aligned.index)
                te_daily = strategy_daily.loc[common_idx] - bench_aligned.loc[common_idx]
                tracking_err = float(te_daily.std() * np.sqrt(252)) if len(te_daily) > 1 else 0.0
                # Information ratio
                ir = float(te_daily.mean() / te_daily.std() * np.sqrt(252)) if te_daily.std() > 0 else 0.0

                logger.info(
                    f"\n=== Benchmark ({bench_code}) ==="
                    f"\n  Benchmark return: {bench_cum*100:+.1f}%"
                    f"\n  Excess return (α): {excess*100:+.1f}%"
                    f"\n  Tracking error (ann): {tracking_err*100:.1f}%"
                    f"\n  Information ratio: {ir:.3f}"
                )
    except Exception as e:
        logger.warning(f"Benchmark comparison failed: {e}")

    return df


if __name__ == "__main__":
    start = sys.argv[1] if len(sys.argv) > 1 else "2026-01-01"
    end = sys.argv[2] if len(sys.argv) > 2 else "2026-06-30"
    cap = float(sys.argv[3]) if len(sys.argv) > 3 else 5000

    df = run_backtest(start, end, cap)
    print(f"\nResults saved ({len(df)} rows)")
    print(df.to_string())
