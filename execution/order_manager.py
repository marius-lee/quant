"""订单管理 — 将推荐信号转为实际订单。

流程:
  信号(pipeline) → 目标持仓列表 → 对比当前持仓 → 买卖清单 → 风控检查 → 下单
"""
from execution.risk_checker import RiskChecker
from execution.broker import BrokerInterface, get_broker
from utils.logger import get_logger

logger = get_logger("execution.order")


class OrderManager:
    """信号→订单转换器"""

    def __init__(self, broker: BrokerInterface = None, initial_cash: float = 5000):
        self.broker = broker or get_broker()
        self.risk = RiskChecker()
        self.cash = initial_cash

    def connect(self) -> bool:
        ok = self.broker.connect()
        if ok:
            self.cash = self.broker.get_cash()
        return ok

    def generate_orders(self, recommendations: list, top_n: int = 3) -> dict:
        """根据推荐列表生成买卖订单。"""
        positions = self.broker.get_positions()
        current_symbols = set(positions.keys())
        cash = self.broker.get_cash()

        target_symbols = set(r["symbol"] for r in recommendations[:top_n])
        target_prices = {r["symbol"]: r["last_price"] for r in recommendations if r["symbol"] in target_symbols}

        to_buy = target_symbols - current_symbols
        to_sell = current_symbols - target_symbols
        to_hold = target_symbols & current_symbols

        orders = []
        alerts = []

        for sym in to_sell:
            price = target_prices.get(sym, self.broker.get_price(sym))
            if price <= 0:
                alerts.append(f"无法获取{sym}价格, 跳过卖出")
                continue
            shares = positions[sym].get("shares", 0)
            check = self.risk.check_sell(sym, shares, price, positions)
            if check["ok"]:
                result = self.broker.sell(sym, price, shares)
                orders.append({"side": "sell", "symbol": sym, "shares": shares, "price": price, "result": result})
                if result.get("status") != "filled":
                    alerts.append(f"卖出{sym}未成交: {result.get('error')}")
            else:
                alerts.append(f"卖出{sym}被风控拦截: {check['reason']}")

        # 卖出后重新获取现金余额（卖出已增加 cash），再计算买入预算
        cash = self.broker.get_cash()
        n_new = len(to_buy)
        if n_new > 0:
            capital_per = cash / n_new
            for sym in to_buy:
                price = target_prices.get(sym, 0)
                if price <= 0:
                    alerts.append(f"无法获取{sym}价格, 跳过买入")
                    continue
                max_shares = int(capital_per / price / 100) * 100
                if max_shares < 100:
                    alerts.append(f"{sym} ¥{price} — 资金不足买1手(需¥{price*100:.0f})")
                    continue
                check = self.risk.check_buy(sym, max_shares, price, cash, positions)
                if check["ok"]:
                    result = self.broker.buy(sym, price, max_shares)
                    orders.append({"side": "buy", "symbol": sym, "shares": max_shares, "price": price, "result": result})
                    cash = self.broker.get_cash()
                    if result.get("status") == "filled":
                        positions[sym] = {"shares": max_shares, "cost_price": price, "current_price": price}
                        # 更新本地副本使后续check_buy的max_positions检查生效
                else:
                    alerts.append(f"买入{sym}被风控拦截: {check['reason']}")

        return {
            "orders": orders,
            "alerts": alerts,
            "target_positions": [{"symbol": s, "price": target_prices.get(s, 0), "action": "buy" if s in to_buy else "hold"} for s in target_symbols],
            "cash_remaining": cash,
            "n_positions": len(target_symbols),
        }


def simulate_order(recommendations: list, cash: float = 5000, top_n: int = 3) -> dict:
    """模拟下单: 用于回测验证，不连真实券商。"""
    broker = get_broker("mock", initial_cash=cash)
    broker.connect()
    om = OrderManager(broker=broker, initial_cash=cash)
    return om.generate_orders(recommendations, top_n=top_n)
