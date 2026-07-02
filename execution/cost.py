"""统一成本模型 — 佣金 + 印花税 + 滑点估计。
来源: ② A股标准费率; ② 券商普遍收费结构
"""

from dataclasses import dataclass


@dataclass
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
        from config.loader import get as cfg
        return cls(
            commission_rate=cfg("execution.commission", 0.0003),
            min_commission=cfg("execution.min_commission", 5.0),
            stamp_tax_rate=cfg("execution.stamp_tax", 0.001),
            slippage_rate=cfg("execution.slippage", 0.001),
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

    def buy_cost(self, price: float, shares: int) -> float:
        """买入总成本 = 成交额 + 佣金 + 滑点。"""
        value = price * shares
        return value + self.commission(value) + self.slippage(value)

    def sell_proceeds(self, price: float, shares: int) -> float:
        """卖出净收入 = 成交额 - 佣金 - 印花税 - 滑点。"""
        value = price * shares
        return value - self.commission(value) - self.stamp_tax(value, "sell") - self.slippage(value)

    def sell_cost(self, price: float, shares: int) -> float:
        """卖出总成本 (佣金+印花税+滑点)。"""
        value = price * shares
        return self.commission(value) + self.stamp_tax(value, "sell") + self.slippage(value)

    def round_trip_cost_pct(self) -> float:
        """往返成本百分比 (买+卖)。"""
        return (self.commission_rate + self.slippage_rate) * 2 + self.stamp_tax_rate
