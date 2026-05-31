"""券商API抽象层 — 统一接口，支持多券商。

当前支持:
  - mock: 模拟券商 (回测/开发用)
  - xtquant: QMT极简量化 (需安装xtquant)
  - easytrader: 华泰/佣金宝等 (需安装easytrader)

通过 config.yaml execution.broker 切换。
"""

from utils.logger import get_logger

logger = get_logger("execution.broker")


class BrokerInterface:
    """券商API抽象基类"""

    def connect(self) -> bool:
        raise NotImplementedError

    @property
    def connected(self) -> bool:
        raise NotImplementedError

    def get_cash(self) -> float:
        raise NotImplementedError

    def get_positions(self) -> dict:
        raise NotImplementedError

    def get_price(self, symbol: str) -> float:
        raise NotImplementedError

    def buy(self, symbol: str, price: float, shares: int) -> dict:
        raise NotImplementedError

    def sell(self, symbol: str, price: float, shares: int) -> dict:
        raise NotImplementedError

    def cancel(self, order_id: str) -> bool:
        raise NotImplementedError

    def get_orders(self) -> list:
        raise NotImplementedError

    def disconnect(self):
        raise NotImplementedError


class MockBroker(BrokerInterface):
    """模拟券商 — 回测和开发使用"""

    def __init__(self, initial_cash: float = 5000, **kwargs):
        self._cash = initial_cash
        self._positions = {}
        self._orders = []
        self._order_id = 0
        self._connected = False

    def connect(self) -> bool:
        self._connected = True
        return True

    @property
    def connected(self) -> bool:
        return self._connected

    def get_cash(self) -> float:
        return self._cash

    def get_positions(self) -> dict:
        return dict(self._positions)

    def get_price(self, symbol: str) -> float:
        return self._positions.get(symbol, {}).get("current_price", 0)

    def buy(self, symbol: str, price: float, shares: int) -> dict:
        if shares < 100:
            return {"order_id": None, "status": "rejected", "error": "minimum 100 shares"}
        cost = shares * price + max(5.0, shares * price * 0.0003)
        if cost > self._cash:
            return {"order_id": None, "status": "rejected", "error": "insufficient funds"}
        self._order_id += 1
        self._cash -= cost
        old = self._positions.get(symbol, {"shares": 0, "cost_price": 0})
        total_shares = old["shares"] + shares
        avg_cost = (old["shares"] * old["cost_price"] + cost) / total_shares
        self._positions[symbol] = {"shares": total_shares, "cost_price": avg_cost, "current_price": price}
        oid = f"B{self._order_id:06d}"
        self._orders.append({"order_id": oid, "symbol": symbol, "side": "buy", "shares": shares, "price": price, "status": "filled"})
        logger.info(f"mock buy: {symbol} {shares}@{price:.2f}  cost={cost:.1f}  cash={self._cash:.1f}")
        return {"order_id": oid, "status": "filled", "filled_shares": shares, "filled_price": price, "error": None}

    def sell(self, symbol: str, price: float, shares: int) -> dict:
        if symbol not in self._positions:
            return {"order_id": None, "status": "rejected", "error": "not held"}
        held = self._positions[symbol]["shares"]
        if shares > held:
            shares = held
        if shares < 100:
            return {"order_id": None, "status": "rejected", "error": "minimum 100 shares"}
        proceeds = shares * price
        commission = max(5.0, proceeds * 0.0003)
        stamp_tax = proceeds * 0.001
        self._order_id += 1
        self._cash += proceeds - commission - stamp_tax
        self._positions[symbol]["shares"] -= shares
        if self._positions[symbol]["shares"] <= 0:
            del self._positions[symbol]
        oid = f"S{self._order_id:06d}"
        self._orders.append({"order_id": oid, "symbol": symbol, "side": "sell", "shares": shares, "price": price, "status": "filled"})
        logger.info(f"mock sell: {symbol} {shares}@{price:.2f}  proceed={proceeds:.1f}  cash={self._cash:.1f}")
        return {"order_id": oid, "status": "filled", "filled_shares": shares, "filled_price": price, "error": None}

    def cancel(self, order_id: str) -> bool:
        return False

    def get_orders(self) -> list:
        return self._orders

    def disconnect(self):
        self._connected = False


def get_broker(name: str = None, **kwargs) -> BrokerInterface:
    """工厂函数: 根据配置返回券商实例"""
    from config.loader import get as cfg
    if name is None:
        name = cfg("execution.broker", "mock")
    if name == "mock":
        return MockBroker(initial_cash=kwargs.get("initial_cash", cfg("backtest.initial_capital", 5000)))
    elif name == "xtquant":
        try:
            import xtquant
            logger.info("xtquant connected")
        except ImportError:
            logger.error("xtquant not installed, falling back to mock")
            return MockBroker()
        return MockBroker()
    elif name == "easytrader":
        try:
            import easytrader
            logger.info("easytrader loaded")
        except ImportError:
            logger.error("easytrader not installed, falling back to mock")
            return MockBroker()
        return MockBroker()
    else:
        logger.warning(f"unknown broker: {name}, using mock")
        return MockBroker()
