"""统一成本模型 — 佣金 + 印花税 + 滑点估计。
来源: ② A股标准费率; ② 券商普遍收费结构
"""

from quant.utils.logger import get_logger
from quant.execution.impact import estimate_impact_pct
from quant.config.constants import _require_cfg

def market_impact(order_shares: float, daily_amount: float, eta: float = 0.1) -> float:
    """Non-linear market impact: sqrt(order_size / daily_volume) model.

    slippage = eta * sqrt(order_shares * avg_price / daily_amount)
    Source: Almgren & Chriss (2001), Kissell & Glantz (2003)

    Args:
        order_shares: number of shares to trade
        daily_amount: daily turnover amount (元)
        eta: impact coefficient (calibrated per market, default 0.1 = 10bp base)

    Returns: estimated slippage as fraction (e.g., 0.0015 = 15bp)
    """
    import numpy as np
    if daily_amount <= 0 or order_shares <= 0:
        return 0.0
    participation = abs(order_shares * 10) / max(daily_amount, 1)  # rough: assume avg_price ~10
    return float(eta * np.sqrt(min(participation, 1.0)))

logger = get_logger("execution.cost")

from dataclasses import dataclass


@dataclass
# NOTE: buy_cost/sell_proceeds 会直接影响现金余额 (由 get_cash() 从 sim_trades 计算)。
# cost_gap = commission + slippage
class CostModel:
    """A股交易成本模型。

    佣金: 万三 (0.03%), 最低 5 元/笔
    印花税: 千一 (0.1%), 仅卖出
    滑点: 千一 (0.1%), 买卖双向
    """
    commission_rate: float = 0.0003   # 万三
    min_commission: float = 5.0       # 最低 5 元
    stamp_tax_rate: float = 0.001     # 千一 (仅卖出)
    slippage_rate: float = 0.001      # 千一买卖双向

    @classmethod
    def from_config(cls) -> "CostModel":
        return cls(
            commission_rate=_require_cfg("execution.commission"),
            min_commission=_require_cfg("execution.min_commission"),
            stamp_tax_rate=_require_cfg("execution.stamp_tax"),
            slippage_rate=_require_cfg("execution.slippage"),
        )

    def commission(self, trade_value: float) -> float:
        """佣金 = max(成交额 × 佣金率, 最低佣金)。"""
        return max(trade_value * self.commission_rate, self.min_commission)

    def stamp_tax(self, trade_value: float, side: str = "sell") -> float:
        """印花税 = 成交额 × 千一, 仅卖出。"""
        if side == "sell":
            return trade_value * self.stamp_tax_rate
        return 0.0

    def slippage(self, trade_value: float) -> float:
        """滑点 = 成交额 × 滑点率。"""
        return trade_value * self.slippage_rate


    def slippage_with_impact(
        self, trade_value: float, shares: int, daily_volume: float = None,
        daily_volatility: float = None,
    ) -> float:
        """动态滑点: 有成交量数据时用 Almgren-Chriss 模型, 否则回退固定值.

        Args:
            trade_value: 成交额 (price × shares)
            shares: 委托股数
            daily_volume: 近 20 日均成交量, None → 用固定滑点
            daily_volatility: 日波动率, None → 用 config 默认值
        """
        if daily_volume and daily_volume > 0:
            impact_pct = estimate_impact_pct(shares, daily_volume, daily_volatility)
            return trade_value * max(impact_pct, 0.0001)  # 最低 0.01% (比固定低一个量级)
        return self.slippage(trade_value)

    def buy_cost(self, price: float, shares: int, daily_volume: float = None, daily_vol: float = None) -> float:
        """买入总成本 = 成交额 + 佣金 + 滑点 (支持动态冲击)。"""
        value = price * shares
        impact = self.slippage_with_impact(value, shares, daily_volume, daily_vol)
        logger.debug(f"[cost] buy {shares}@{price:.2f} impact={impact:.2f}")
        return value + self.commission(value) + impact

    def sell_proceeds(self, price: float, shares: int, daily_volume: float = None, daily_vol: float = None) -> float:
        """卖出净收入 = 成交额 - 佣金 - 印花税 - 滑点 (支持动态冲击)。"""
        value = price * shares
        impact = self.slippage_with_impact(value, shares, daily_volume, daily_vol)
        return value - self.commission(value) - self.stamp_tax(value, "sell") - impact

    def sell_cost(self, price: float, shares: int, daily_volume: float = None, daily_vol: float = None) -> float:
        """卖出总成本 (佣金+印花税+滑点, 支持动态冲击)。"""
        value = price * shares
        impact = self.slippage_with_impact(value, shares, daily_volume, daily_vol)
        return self.commission(value) + self.stamp_tax(value, "sell") + impact

    def round_trip_cost_pct(self) -> float:
        """往返成本百分比 (买+卖)。"""
        return (self.commission_rate + self.slippage_rate) * 2 + self.stamp_tax_rate
