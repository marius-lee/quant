"""调仓计算 — 目标 vs 当前持仓 → 买卖订单列表。"""

from typing import Optional
import pandas as pd
import numpy as np
from execution.engine import Order


LOT_SIZE = 100


def compute_trades(
    target_lots: pd.Series,
    current_lots: pd.Series,
    prices: pd.Series,
    cost_model,
    max_turnover_ratio: float = 0.0,
    capital: float = 0.0,
    cash: float = 0.0,
) -> list[Order]:
    """计算调仓订单。

    target_lots: index=symbol, values=目标手数
    current_lots: index=symbol, values=当前手数
    prices: index=symbol, 最新价格
    cost_model: CostModel 实例 (execution/cost.py)
    max_turnover_ratio: 最大换手率 (总资产占比), 超过则拒绝
    capital: 当前总资产

    返回: [Order, ...] 按 side 排序 (先卖后买，释放资金)
    """
    # 合并所有符号
    all_syms = target_lots.index.union(current_lots.index)
    tgt = target_lots.reindex(all_syms, fill_value=0)
    cur = current_lots.reindex(all_syms, fill_value=0)
    diff = tgt - cur

    orders = []

    # 计算换手金额
    turnover_value = 0.0
    for sym in diff.index:
        if diff[sym] != 0:
            turnover_value += abs(diff[sym]) * prices.get(sym, 0) * LOT_SIZE

    if capital > 0 and max_turnover_ratio > 0:
        ratio = turnover_value / capital
        if ratio > max_turnover_ratio:
            from utils.logger import get_logger
            get_logger("optimizer.rebalance").warning(
                f"turnover {ratio:.1%} exceeds limit {max_turnover_ratio:.1%}, scaling down"
            )
            scale = max_turnover_ratio / ratio
            # Use ceil for |diff|≥1 to preserve non-zero orders
            scaled = diff * scale
            # Round away from zero: ±0.5 threshold, but never kill |diff|≥1
            result = pd.Series(0, index=diff.index, dtype=int)
            for i in range(len(diff)):
                d = scaled.iloc[i]
                if abs(d) >= 0.5:
                    result.iloc[i] = int(np.ceil(d) if d > 0 else np.floor(d))
                elif abs(diff.iloc[i]) >= 1:
                    # Original diff was at least 1 lot — keep direction
                    result.iloc[i] = 1 if diff.iloc[i] > 0 else -1
            diff = result

    # 卖出订单 (diff < 0 → 卖出)
    for sym in diff[diff < 0].index:
        shares = abs(int(diff[sym])) * LOT_SIZE
        if shares > 0 and sym in prices.index:
            price = prices[sym]
            orders.append(Order(
                symbol=sym,
                side="sell",
                shares=shares,
                price=price,
                cost=cost_model.sell_cost(price, shares),
            ))

    # 买入订单 (diff > 0 → 买入)
    for sym in diff[diff > 0].index:
        shares = int(diff[sym]) * LOT_SIZE
        if shares > 0 and sym in prices.index:
            price = prices[sym]
            orders.append(Order(
                symbol=sym,
                side="buy",
                shares=shares,
                price=price,
                cost=cost_model.buy_cost(price, shares),
            ))

    # ── 换手缩放后的 cash feasibility 检查 ──
    # 换手率限制可能使卖单缩水但买单保留, 造成资金缺口。
    # 此时优先执行所有卖单, 再按买入成本从低到高依次纳入买单。
    available_cash = cash if cash > 0 else capital
    if available_cash > 0 and orders:
        sell_orders = [o for o in orders if o.side == "sell"]
        buy_orders = [o for o in orders if o.side == "buy"]
        sell_inflow = sum(o.price * o.shares - o.cost for o in sell_orders)
        available = available_cash + sell_inflow
        feasible = []
        for o in buy_orders:
            if available >= o.cost:
                feasible.append(o)
                available -= o.cost
        if len(feasible) < len(buy_orders):
            from utils.logger import get_logger
            get_logger("optimizer.rebalance").warning(
                f"cash feasibility: {len(buy_orders) - len(feasible)} buy(s) trimmed (insufficient funds)"
            )
            orders = sell_orders + feasible

    return orders


def validate_orders(orders: list[Order], capital: float) -> tuple[bool, str]:
    """验证订单的可行性 (资金充足、无负持仓)。

    返回: (is_valid, message)
    """
    if not orders:
        return True, "no orders"

    # 检查 shares 是整手
    for o in orders:
        if o.shares % LOT_SIZE != 0:
            return False, f"{o.symbol}: shares {o.shares} not multiple of {LOT_SIZE}"

    # 先卖后买，累计资金变化
    # Order.cost: sell→fees(佣金+印花税+滑点), buy→成交额+佣金+滑点
    cash = capital
    for o in orders:
        if o.side == "sell":
            cash += o.price * o.shares - o.cost   # proceeds = price*shares - fees
        else:
            cash -= o.cost  # buy_cost already = price*shares + fees, don't double-count

    if cash < -1:  # 允许 1 元容差
        return False, f"insufficient funds: need {-cash:.2f} more"

    return True, "OK"


def order_summary(orders: list[Order]) -> str:
    """订单摘要，用于日志输出。"""
    if not orders:
        return "no orders"
    buys = [o for o in orders if o.side == "buy"]
    sells = [o for o in orders if o.side == "sell"]
    buy_value = sum(o.price * o.shares for o in buys)
    sell_value = sum(o.price * o.shares for o in sells)
    total_cost = sum(o.cost for o in orders)
    return (
        f"{len(sells)} sells (¥{sell_value:,.0f}) + "
        f"{len(buys)} buys (¥{buy_value:,.0f}), "
        f"cost ¥{total_cost:,.2f}"
    )
