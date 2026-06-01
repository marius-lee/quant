"""回测 — 5000元激进策略: 涨跌停约束 + 真实佣金 + 手数取整"""
import numpy as np
import pandas as pd
from backtest.metrics import compute_metrics
from backtest import compute_commission as _real_commission
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


def affordable_filter(symbols, close_df, capital):
    """过滤: 只保留买得起至少1手的股票 (股价×100 ≤ 全部资金)。
    供 backtest_runner 和 builder 共用。"""
    if close_df.empty:
        return []
    max_price = capital / 100  # 5000/100 = ¥50
    latest = close_df.iloc[-1] if len(close_df) > 0 else pd.Series()
    return [s for s in symbols if s in latest.index and latest[s] > 0 and latest[s] <= max_price]


def _compute_benchmark_result(store, daily_returns, initial_capital):
    """基准对比计算 — 供 backtest_runner 和 rebalance 共用。
    返回 benchmark dict 或 None。"""
    bench_code = cfg("backtest.benchmark", "000300")
    bench_returns = store.get_benchmark(bench_code)
    if bench_returns.empty:
        return None
    aligned = bench_returns.reindex(daily_returns.index).dropna()
    common_idx = daily_returns.index.intersection(aligned.index)
    if len(common_idx) < 5:
        return None
    excess = daily_returns.loc[common_idx] - aligned.loc[common_idx]
    bench_m = compute_metrics(aligned.loc[common_idx], initial_capital=initial_capital)
    excess_m = compute_metrics(excess, initial_capital=initial_capital)
    return {
        "code": bench_code,
        "benchmark_return": round(float(bench_m.get("annual_return", 0)), 4),
        "benchmark_sharpe": round(float(bench_m.get("sharpe_ratio", 0)), 4),
        "alpha": round(float(excess_m.get("alpha", 0)), 4),
        "excess_sharpe": round(float(excess_m.get("sharpe_ratio", 0)), 4),
    }


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
    affordable = affordable_filter(pred_series.index.tolist(), test_close,
                                     initial_capital)
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
            fee, _, _ = _real_commission(cost, is_sell=False)
            total_commission += fee
            positions[sym] = shares

    if not positions:
        return {"metrics": {}, "equity_curve": pd.DataFrame(), "trades": pd.DataFrame()}

    portfolio_prices = test_close[list(positions.keys())]
    daily_value = pd.DataFrame(0.0, index=portfolio_prices.index, columns=["value"])
    for sym, shares in positions.items():
        daily_value["value"] += portfolio_prices[sym] * shares
    # 从初始净值中扣除佣金（建仓一次性成本），而非每天重复扣除
    net_capital = initial_capital - total_commission
    cumulative = (daily_value["value"] / daily_value["value"].iloc[0]) * net_capital
    daily_return = cumulative.pct_change().dropna()
    eq = pd.DataFrame({"value": cumulative, "return": daily_return})

    metrics = compute_metrics(daily_return, initial_capital=initial_capital)
    metrics["commission_paid"] = round(total_commission, 2)
    metrics["n_positions"] = len(positions)
    result = {"equity_curve": eq, "trades": pd.DataFrame(), "metrics": metrics}

    bench_result = _compute_benchmark_result(store, daily_return, initial_capital)
    if bench_result:
        result["benchmark"] = bench_result

    logger.info(f"backtest done: {len(positions)} stocks, {len(daily_return)} days, "
                f"sharpe={metrics.get('sharpe_ratio', 0):.3f}, "
                f"annual_return={metrics.get('annual_return', 0)*100:.1f}%, "
                f"commission={total_commission:.1f}")
    return result
