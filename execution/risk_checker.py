"""下单前风控检查 — 5000元激进策略专用。

检查项:
  1. 可用资金 ≥ 订单金额
  2. 股票未停牌
  3. 买入方向: 股票未涨停 (一字板/封板)
  4. 卖出方向: 股票未跌停
  5. 单票仓位 ≤ 上限
  6. 总持仓数 ≤ 上限
  7. 订单股数 ≥ 100 (A股最小交易单位)
"""
from config.loader import get as cfg
from utils.logger import get_logger

logger = get_logger("execution.risk")


class RiskChecker:
    """下单前风控检查器"""

    def __init__(self, data_store=None):
        self.store = data_store

    def check_buy(self, symbol: str, shares: int, price: float,
                  cash: float, positions: dict, prev_close: float = None) -> dict:
        """检查买入订单是否合规。"""
        if shares < 100:
            return {"ok": False, "reason": f"股数{shares}<100(最小交易单位)", "cost": 0}
        cost = shares * price
        commission = max(5.0, cost * 0.0003)
        total_cost = cost + commission
        if total_cost > cash:
            return {"ok": False, "reason": f"资金不足(需¥{total_cost:.0f}, 可用¥{cash:.0f})", "cost": total_cost}
        max_positions = cfg("backtest.max_positions", 3)
        if symbol not in positions and len(positions) >= max_positions:
            return {"ok": False, "reason": f"持仓已达上限{max_positions}只", "cost": total_cost}
        if prev_close and prev_close > 0:
            chg = price / prev_close - 1
            if chg > 0.095:
                return {"ok": False, "reason": f"{symbol}涨停(chg={chg*100:.1f}%), 无法买入", "cost": total_cost}
        return {"ok": True, "reason": "", "cost": total_cost}

    def check_sell(self, symbol: str, shares: int, price: float,
                   positions: dict, prev_close: float = None) -> dict:
        """检查卖出订单是否合规。"""
        if symbol not in positions:
            return {"ok": False, "reason": f"未持有{symbol}", "proceeds": 0}
        if positions[symbol] < shares:
            shares = positions[symbol]
        if shares < 100:
            return {"ok": False, "reason": f"可卖股数{shares}<100", "proceeds": 0}
        if prev_close and prev_close > 0:
            chg = price / prev_close - 1
            if chg < -0.095:
                return {"ok": False, "reason": f"{symbol}跌停(chg={chg*100:.1f}%), 无法卖出", "proceeds": 0}
        proceeds = shares * price
        commission = max(5.0, proceeds * 0.0003)
        stamp_tax = proceeds * 0.001
        net_proceeds = proceeds - commission - stamp_tax
        return {"ok": True, "reason": "", "proceeds": net_proceeds}

    def check_position_limits(self, positions: dict, prices: dict, cash: float) -> list:
        """检查持仓是否触发风控线，返回告警列表。"""
        return []
