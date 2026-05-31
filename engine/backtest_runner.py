"""回测 — 5000元激进策略: 涨跌停约束 + 真实佣金 + 手数取整"""
import numpy as np
import pandas as pd
from backtest.metrics import compute_metrics
from config.loader import get as cfg
from utils.logger import get_logger

logger = get_logger("pipeline.backtest")


def _is_limit_up(close_price, prev_close):
    """检测涨停 (A股±10%, 创业板±20%)。用9.5%阈值容错。"""
    try:
        chg = float(close_price) / float(prev_close) - 1
    except (TypeError, ValueError, ZeroDivisionError):
        return False
    return chg > 0.095


def _is_limit_down(close_price, prev_close):
    """检测跌停"""
    try:
        chg = float(close_price) / float(prev_close) - 1
    except (TypeError, ValueError, ZeroDivisionError):
        return False
    return chg < -0.095


def _real_commission(trade_value: float) -> float:
    """真实佣金模型: 万三费率, 最低5元/笔, 卖出加千一印花税"""
    commission_rate = cfg("backtest.commission", 0.0003)
    min_commission = 5.0
    fee = max(min_commission, trade_value * commission_rate)
    return fee


def _affordable_filter(symbols, close_df, capital, top_n):
    """过滤: 只保留买得起至少1手的股票 (股价×100 ≤ 全部资金)"""
    if close_df.empty:
        return []
    max_price = capital / 100  # 5000/100 = ¥50
    latest = close_df.iloc[-1] if len(close_df) > 0 else pd.Series()
    affordable = [s for s in symbols if s in latest.index
                  and latest[s] > 0 and latest[s] <= max_price]
    return affordable


def run_backtest(store, factors_repo, all_stocks, close_df, passed, model,
                 all_dates, split_idx, test_dates_set, pred_series=None):
    """激进策略向量化回测: 涨跌停约束 + 资金约束 + 集中持仓。

    pred_series: 管线已算好的全量预测排名。若未传入则返回空。
    """
    if pred_series is None or pred_series.empty:
        logger.warning("no pred_series, backtest skipped")
        return {"metrics": {}, "equity_curve": pd.DataFrame(), "trades": pd.DataFrame()}

    top_n = cfg("backtest.max_positions", 3)
    initial_capital = cfg("backtest.initial_capital", 5_000)
    slippage = cfg("backtest.slippage", 0.003)

    test_close = close_df.loc[close_df.index.isin(all_dates[split_idx:])]
    if test_close.empty:
        return {"metrics": {}, "equity_curve": pd.DataFrame(), "trades": pd.DataFrame()}

    # 5000元可买性过滤: 排除买不起一手的股票
    affordable = _affordable_filter(pred_series.index.tolist(), test_close,
                                     initial_capital, top_n)
    if len(affordable) < 1:
        logger.warning("no affordable stocks for 5000 capital")
        return {"metrics": {}, "equity_curve": pd.DataFrame(), "trades": pd.DataFrame()}

    # 在买得起的股票中选top N
    affordable_pred = pred_series.loc[pred_series.index.isin(affordable)]
    top_stocks = affordable_pred.head(top_n).index.tolist()
    valid = [s for s in top_stocks if s in test_close.columns]

    # 涨跌停过滤: 排除当日涨停的股票 (买不到)
    prev_close = close_df.shift(1)
    entry_date = test_close.index[0]
    if entry_date in prev_close.index:
        prev = prev_close.loc[entry_date]
        entry_prices = test_close.loc[entry_date]
        valid = [s for s in valid if s in entry_prices.index and s in prev.index
                 and not _is_limit_up(entry_prices[s], prev[s])]

    if len(valid) < 1:
        logger.warning(f"no tradable stocks after price-limit filter")
        return {"metrics": {}, "equity_curve": pd.DataFrame(), "trades": pd.DataFrame()}

    # 等权持仓，手数取整
    capital_per_stock = initial_capital / len(valid)
    positions = {}
    total_commission = 0.0
    for sym in valid:
        price = test_close[sym].iloc[0] * (1 + slippage)  # 买入价含滑点
        shares = int(capital_per_stock / price / 100) * 100  # A股手数取整
        if shares >= 100:
            cost = shares * price
            total_commission += _real_commission(cost)
            positions[sym] = shares

    if not positions:
        return {"metrics": {}, "equity_curve": pd.DataFrame(), "trades": pd.DataFrame()}

    portfolio_prices = test_close[list(positions.keys())]
    # 按持仓股数加权 (而非等权)
    daily_value = pd.DataFrame(0.0, index=portfolio_prices.index, columns=["value"])
    for sym, shares in positions.items():
        daily_value["value"] += portfolio_prices[sym] * shares
    daily_value["value"] -= total_commission
    daily_return = daily_value["value"].pct_change().dropna()
    cumulative = (daily_value["value"] / daily_value["value"].iloc[0]) * initial_capital
    eq = pd.DataFrame({"value": cumulative, "return": daily_return})

    metrics = compute_metrics(daily_return, initial_capital=initial_capital)
    metrics["commission_paid"] = round(total_commission, 2)
    metrics["n_positions"] = len(positions)
    result = {"equity_curve": eq, "trades": pd.DataFrame(), "metrics": metrics}

    bench_code = cfg("backtest.benchmark", "000300")
    bench_returns = store.get_benchmark(bench_code)
    if not bench_returns.empty:
        aligned_bench = bench_returns.reindex(daily_return.index).dropna()
        common_idx = daily_return.index.intersection(aligned_bench.index)
        if len(common_idx) >= 5:
            daily_aligned = daily_return.loc[common_idx]
            bench_aligned = aligned_bench.loc[common_idx]
            excess = daily_aligned - bench_aligned
            excess_m = compute_metrics(excess, initial_capital=initial_capital)
            bench_m = compute_metrics(bench_aligned, initial_capital=initial_capital)
            result["benchmark"] = {
                "code": bench_code,
                "benchmark_return": round(float(bench_m.get("annual_return", 0)), 4),
                "benchmark_sharpe": round(float(bench_m.get("sharpe_ratio", 0)), 4),
                "alpha": round(float(excess_m.get("alpha", 0)), 4),
                "excess_sharpe": round(float(excess_m.get("sharpe_ratio", 0)), 4),
            }

    logger.info(f"backtest done: {len(positions)} stocks, {len(daily_return)} days, "
                f"sharpe={metrics.get('sharpe_ratio', 0):.3f}, "
                f"annual_return={metrics.get('annual_return', 0)*100:.1f}%, "
                f"commission={total_commission:.1f}")
    return result
