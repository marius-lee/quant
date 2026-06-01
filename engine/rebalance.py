"""再平衡回测 — 周频重排名调仓，模拟5000元激进策略。

与静态回测（run_backtest）的区别:
  - 静态: 期初选top N，全程持有不动
  - 再平衡: 每周初重新排名选top N，计算换手率，扣交易成本
"""
import numpy as np
import pandas as pd
from backtest.metrics import compute_metrics
from backtest import compute_commission
from config.loader import get as cfg
from utils.logger import get_logger

logger = get_logger("pipeline.rebalance")


def run_backtest_with_rebalancing(
    store, factors_repo, all_stocks, close_df,
    passed, model, all_dates, split_idx,
    test_dates_set, pred_series=None,
    rebalance_freq: str = "W",
    top_n: int = None,
    initial_capital: float = None,
    commission: float = None,
    slippage: float = None,
) -> dict:
    """按周/月再平衡的回测。在每个调仓点重新排名，调仓至top N。

    注意: 当前版本使用 pipeline 启动时的一次性 pred_series 进行所有调仓日排名，
    而非每个调仓日重新从因子缓存计算最新预测。这意味着后期调仓可能使用过时的因子数据。
    TODO: 每次调仓时重新加载因子并预测。

    Args:
        rebalance_freq: "M"=月度, "W"=周度
        top_n: 持仓数量，默认从config读取
        initial_capital: 初始资金，默认从config读取
        commission: 手续费率（单边），默认从config读取
        slippage: 滑点（%），默认从config读取
    """
    if pred_series is None or pred_series.empty:
        logger.warning("rebalance: no pred_series, skipped")
        return {"metrics": {}, "equity_curve": pd.DataFrame(), "trades": pd.DataFrame()}

    top_n = top_n or cfg("backtest.max_positions", 3)
    initial_capital = initial_capital or cfg("backtest.initial_capital", 5_000)
    commission = commission if commission is not None else cfg("backtest.commission", 0.0003)
    slippage = slippage if slippage is not None else cfg("backtest.slippage", 0.003)

    test_dates = all_dates[split_idx:]
    test_close = close_df.loc[close_df.index.isin(test_dates)]

    if test_close.empty or len(test_dates) < 20:
        return {"metrics": {}, "equity_curve": pd.DataFrame(), "trades": pd.DataFrame()}

    # 按月分组: 取每月第一个交易日
    test_idx = pd.DatetimeIndex(test_dates)
    if rebalance_freq == "M":
        month_groups = test_idx.to_series().groupby(test_idx.to_period("M"))
        rebalance_dates = [g.iloc[0] for _, g in month_groups]
    elif rebalance_freq == "W":
        week_groups = test_idx.to_series().groupby(test_idx.to_period("W"))
        rebalance_dates = [g.iloc[0] for _, g in week_groups]
    else:
        rebalance_dates = test_dates[:1]  # 单次调仓

    logger.info(f"rebalance: {len(rebalance_dates)} rebalance points, "
                f"{len(test_dates)} test days, {top_n} positions")

    cash = initial_capital
    positions = {}  # {symbol: shares}
    equity_curve = []
    trades = []
    current_holdings = set()

    for i, rebal_date in enumerate(rebalance_dates):
        # 获取本期市场数据
        if rebal_date not in test_close.index:
            # 使用最近的交易日
            candidates = test_close.index[test_close.index >= rebal_date]
            if len(candidates) == 0:
                continue
            actual_date = candidates[0]
        else:
            actual_date = rebal_date

        period_end_idx = i + 1
        if period_end_idx < len(rebalance_dates):
            period_end = rebalance_dates[period_end_idx]
            period_mask = (test_close.index >= actual_date) & (test_close.index < period_end)
        else:
            period_mask = test_close.index >= actual_date

        period_close = test_close.loc[period_mask]
        if period_close.empty:
            continue

        # 获取当期收盘价（第一天的价格用于调仓）
        entry_prices = period_close.iloc[0]

        # 确定本期持仓: 排除涨停股（当日无法买入）
        # 用前周期最后一天的收盘价判断当日涨跌
        prev_close = close_df.shift(1)
        if actual_date in prev_close.index:
            prev_row = prev_close.loc[actual_date]
            limit_up_mask = (entry_prices / prev_row - 1) > 0.095
            blocked = set(prev_row[limit_up_mask].index)
        else:
            blocked = set()
        available_stocks = [s for s in pred_series.index
                          if s in entry_prices.index and entry_prices[s] > 0
                          and s not in blocked]
        if len(available_stocks) < top_n:
            target_stocks = available_stocks
        else:
            target_stocks = pred_series.loc[available_stocks].head(top_n).index.tolist()

        target_set = set(target_stocks)
        to_sell = current_holdings - target_set
        to_buy = target_set - current_holdings
        to_hold = current_holdings & target_set

        # 平仓卖出
        for sym in to_sell:
            if sym in positions and sym in entry_prices.index:
                price = entry_prices[sym] * (1 - slippage)
                proceeds = positions[sym] * price
                _, _, sell_cost = compute_commission(proceeds, is_sell=True)
                cash += proceeds - sell_cost
                trades.append({
                    "date": actual_date, "symbol": sym, "side": "sell",
                    "shares": positions[sym], "price": price,
                })
                del positions[sym]

        # 等权开仓买入：总资产 = 现金 + 已有持仓市值
        n_all = len(to_buy) + len(to_hold)
        if n_all == 0:
            continue

        hold_value = sum(positions[s] * entry_prices[s]
                         for s in to_hold if s in entry_prices.index and s in positions)
        total_equity = cash + hold_value
        target_value = total_equity / n_all
        for sym in to_buy:
            if sym in entry_prices.index:
                price = entry_prices[sym] * (1 + slippage)
                shares = int(target_value / price / 100) * 100  # A股手数取整
                if shares >= 100:
                    trade_value = shares * price
                    _, _, buy_cost = compute_commission(trade_value, is_sell=False)
                    total_cost = trade_value + buy_cost
                    if total_cost <= cash:
                        cash -= total_cost
                        positions[sym] = positions.get(sym, 0) + shares
                        trades.append({
                            "date": actual_date, "symbol": sym, "side": "buy",
                            "shares": shares, "price": price,
                        })

        # 对持有仓位按目标价值调整（等权）
        for sym in to_hold:
            if sym in entry_prices.index and sym in positions:
                price = entry_prices[sym]
                current_value = positions[sym] * price
                diff = target_value - current_value
                if abs(diff) > max(100, cash * 0.01):  # 最小交易阈值：100元或1%资金
                    if diff > 0:  # 加仓
                        shares = int(diff / price / 100) * 100
                        trade_value = shares * price * (1 + slippage)
                        _, _, buy_cost = compute_commission(trade_value, is_sell=False)
                        total_cost = trade_value + buy_cost
                        if shares >= 100 and total_cost <= cash:
                            cash -= total_cost
                            positions[sym] += shares
                            trades.append({
                                "date": actual_date, "symbol": sym, "side": "buy",
                                "shares": shares, "price": price * (1 + slippage),
                            })
                    else:  # 减仓
                        shares = min(int(-diff / price / 100) * 100, positions[sym] - 100)
                        if shares >= 100:
                            sell_price = price * (1 - slippage)
                            sell_proceeds = shares * sell_price
                            _, _, sell_cost = compute_commission(sell_proceeds, is_sell=True)
                            cash += sell_proceeds - sell_cost
                            positions[sym] -= shares
                            trades.append({
                                "date": actual_date, "symbol": sym, "side": "sell",
                                "shares": shares, "price": sell_price,
                            })

        current_holdings = set(positions.keys())

        # 按日计算持仓市值，记录净值曲线（期间不调仓）
        for day_idx in range(len(period_close)):
            day_date = period_close.index[day_idx]
            day_prices = period_close.iloc[day_idx]
            portfolio_value = cash
            for sym, shares in list(positions.items()):
                if sym in day_prices.index:
                    portfolio_value += shares * day_prices[sym]
            equity_curve.append({
                "date": day_date,
                "value": portfolio_value,
                "cash": cash,
                "positions": len(positions),
            })

    if not equity_curve:
        return {"metrics": {}, "equity_curve": pd.DataFrame(), "trades": pd.DataFrame()}

    eq = pd.DataFrame(equity_curve).set_index("date")
    eq["return"] = eq["value"].pct_change()

    daily_returns = eq["return"].dropna()
    metrics = compute_metrics(daily_returns, initial_capital=initial_capital)

    # 基础换手率统计
    if trades:
        n_rebalances = len(rebalance_dates)
        buy_count = sum(1 for t in trades if t["side"] == "buy")
        metrics["avg_turnover"] = buy_count / max(n_rebalances, 1) if n_rebalances > 0 else 0

    result = {
        "equity_curve": eq,
        "trades": pd.DataFrame(trades) if trades else pd.DataFrame(),
        "metrics": metrics,
        "rebalance_count": len(rebalance_dates),
    }

    # 基准对比
    from engine.backtest_runner import _compute_benchmark_result
    bench_result = _compute_benchmark_result(store, daily_returns, initial_capital)
    if bench_result:
        result["benchmark"] = bench_result

    logger.info(f"rebalance done: {len(daily_returns)} days, "
                f"sharpe={metrics.get('sharpe_ratio', 0):.3f}, "
                f"annual_return={metrics.get('annual_return', 0)*100:.1f}%, "
                f"turnover={len(trades)} trades")
    return result
