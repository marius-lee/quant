"""券商适配器 — 统一执行层抽象接口。

Adapter 模式: 执行链底部通过抽象接口调用券商，实现可替换:
  - SimulatedAdapter: 封装现有 ExecutionEngine/SQLite，行为完全不变（默认）
  - VnpyCtpAdapter:    CTP 柜台真实下单 (需 vnpy 环境)
  - VnpyXtpAdapter:    XTP 柜台真实下单 (需 vnpy 环境)

设计原则:
  1. 最小接口 — 只暴露执行层需要的操作
  2. 零配置默认 — 未安装 vnpy 自动回退 SimulatedAdapter
  3. 统一返回类型 — OrderResult / AccountInfo 屏蔽底层差异

ADR: docs/adr/ADR-036-vnpy-execution-integration.md
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional
from quant.utils.logger import get_logger
from quant.config.loader import get as _cfg_get
from quant.config.constants import _require_cfg

_log = get_logger("execution.broker_adapter")


# ═══════════════════════════════════════════════════════════
# 统一返回类型
# ═══════════════════════════════════════════════════════════

@dataclass
class OrderResult:
    """下单返回 — 屏蔽券商/模拟差异。"""
    success: bool
    order_id: str = ""
    symbol: str = ""
    side: str = ""          # buy | sell
    shares: int = 0
    price: float = 0.0
    filled_shares: int = 0
    filled_price: float = 0.0
    status: str = ""        # filled | pending | rejected | cancelled | simulated
    error: str = ""         # 失败原因
    is_simulated: bool = False


@dataclass
class AccountInfo:
    """账户信息 — 屏蔽券商/模拟差异。"""
    total_asset: float = 0.0     # 总资产 (含持仓市值)
    available_cash: float = 0.0  # 可用资金
    frozen_cash: float = 0.0     # 冻结资金 (挂单占用)
    positions: list[dict] = field(default_factory=list)
    # [{symbol, shares, avg_cost, current_price, market_value, pnl, pnl_pct}]


# ═══════════════════════════════════════════════════════════
# 抽象基类
# ═══════════════════════════════════════════════════════════

class BrokerAdapter(ABC):
    """券商适配器抽象基类。

    所有执行层下单操作通过此接口，具体实现可替换为：
      - SimulatedAdapter  (SQLite, 默认, 零依赖)
      - VnpyCtpAdapter    (CTP, 需 vnpy)
      - VnpyXtpAdapter    (XTP, 需 vnpy)
    """

    @abstractmethod
    def connect(self) -> bool:
        """连接券商网关。返回是否成功。"""

    @abstractmethod
    def disconnect(self):
        """断开连接。"""

    @abstractmethod
    def buy(self, symbol: str, price: float, shares: int,
            order_type: str = "LIMIT") -> OrderResult:
        """提交买单。

        Args:
            symbol: 股票代码 (纯数字, 如 '600036')
            price: 委托价格
            shares: 委托股数 (必须是整手=100的倍数)
            order_type: LIMIT=限价单, MARKET=市价单
        """

    @abstractmethod
    def sell(self, symbol: str, price: float, shares: int,
             order_type: str = "MARKET") -> OrderResult:
        """提交卖单。"""

    @abstractmethod
    def cancel(self, order_id: str) -> bool:
        """撤单。返回是否成功。"""

    @abstractmethod
    def get_positions(self) -> list[dict]:
        """获取当前持仓。
        Returns: [{symbol, shares, avg_cost, current_price, ...}]
        """

    @abstractmethod
    def get_account(self) -> AccountInfo:
        """获取账户信息。"""

    @abstractmethod
    def get_orders(self, status: str = None) -> list[dict]:
        """获取订单列表。
        Args:
            status: None=全部, 'pending'=未成交, 'filled'=已成交
        """

    @abstractmethod
    def is_connected(self) -> bool:
        """连接状态。"""

    @property
    @abstractmethod
    def name(self) -> str:
        """适配器名称。"""

    def __repr__(self) -> str:
        return f"<BrokerAdapter:{self.name} connected={self.is_connected()}>"


# ═══════════════════════════════════════════════════════════
# SimulatedAdapter — 封装现有 ExecutionEngine
# ═══════════════════════════════════════════════════════════

class SimulatedAdapter(BrokerAdapter):
    """模拟券商适配器 — 封装现有 ExecutionEngine/SQLite，行为完全不变。

    这是默认适配器。所有操作走现有 engine.execute() → sim_trades 表。
    """

    name = "simulated"

    def __init__(self, db_path: str = None, strategy: str = "quant"):
        from quant.config.paths import TRADE_DB
        self._db_path = db_path or TRADE_DB
        self._strategy = strategy
        self._connected = False
        self._engine = None

    def connect(self) -> bool:
        from quant.execution.engine import ExecutionEngine
        self._engine = ExecutionEngine(db_path=self._db_path)
        self._connected = True
        _log.info("simulated adapter: connected (db=%s)", self._db_path)
        return True

    def disconnect(self):
        self._connected = False
        self._engine = None

    def is_connected(self) -> bool:
        return self._connected and self._engine is not None

    def _get_engine(self):
        if not self._connected:
            self.connect()
        return self._engine

    def buy(self, symbol: str, price: float, shares: int,
            order_type: str = "LIMIT") -> OrderResult:
        """模拟买单 — 立即以给定价格成交。"""
        from quant.execution.engine import Order
        from datetime import date as _date
        engine = self._get_engine()
        today = _date.today().strftime("%Y-%m-%d")

        # 资金检查
        from quant.execution.cost import CostModel
        cm = CostModel.from_config()
        cost = cm.buy_cost(price, shares)
        cash = engine.get_cash(self._strategy)
        if cash < cost:
            return OrderResult(
                success=False, symbol=symbol, side="buy",
                shares=shares, price=price,
                error=f"insufficient cash: need ¥{cost:.2f}, have ¥{cash:.2f}",
                is_simulated=True,
            )

        try:
            executed = engine.execute(
                [Order(symbol=symbol, side="buy", shares=shares,
                       price=round(price, 2), cost=cm.commission(price * shares))],
                today, strategy=self._strategy,
            )
            if executed > 0:
                _log.info("sim buy: %s %s股 @¥%.2f", symbol, shares, price)
                return OrderResult(
                    success=True, symbol=symbol, side="buy",
                    shares=shares, price=price,
                    filled_shares=shares, filled_price=price,
                    status="filled", is_simulated=True,
                )
            else:
                return OrderResult(
                    success=False, symbol=symbol, side="buy",
                    shares=shares, price=price,
                    error="engine.execute returned 0 (ex-dividend skip?)",
                    is_simulated=True,
                )
        except Exception as e:
            _log.error("sim buy failed: %s %s: %s", symbol, shares, e)
            return OrderResult(
                success=False, symbol=symbol, side="buy",
                shares=shares, price=price,
                error=str(e), is_simulated=True,
            )

    def sell(self, symbol: str, price: float, shares: int,
             order_type: str = "MARKET") -> OrderResult:
        """模拟卖单 — 立即以给定价格成交。"""
        from quant.execution.engine import Order
        from datetime import date as _date
        engine = self._get_engine()
        today = _date.today().strftime("%Y-%m-%d")

        try:
            executed = engine.execute(
                [Order(symbol=symbol, side="sell", shares=shares,
                       price=round(price, 2), cost=5.0)],
                today, strategy=self._strategy,
            )
            if executed > 0:
                _log.info("sim sell: %s %s股 @¥%.2f", symbol, shares, price)
                return OrderResult(
                    success=True, symbol=symbol, side="sell",
                    shares=shares, price=price,
                    filled_shares=shares, filled_price=price,
                    status="filled", is_simulated=True,
                )
            else:
                return OrderResult(
                    success=False, symbol=symbol, side="sell",
                    shares=shares, price=price,
                    error="engine.execute returned 0",
                    is_simulated=True,
                )
        except Exception as e:
            _log.error("sim sell failed: %s %s: %s", symbol, shares, e)
            return OrderResult(
                success=False, symbol=symbol, side="sell",
                shares=shares, price=price,
                error=str(e), is_simulated=True,
            )

    def cancel(self, order_id: str) -> bool:
        """模拟撤单 — 直接返回成功 (没有待处理的挂单队列)。"""
        _log.debug("sim cancel: %s (no-op in simulated mode)", order_id)
        return True

    def get_positions(self) -> list[dict]:
        engine = self._get_engine()
        return engine.get_positions(self._strategy)

    def get_account(self) -> AccountInfo:
        engine = self._get_engine()
        cash = engine.get_cash(self._strategy)
        positions = engine.get_positions(self._strategy)
        pos_value = sum(
            (p.get("price", 0) or 0) * (p.get("shares", 0) or 0)
            for p in positions
        )
        # 补充持仓市值字段
        for p in positions:
            p["market_value"] = (p.get("price", 0) or 0) * (p.get("shares", 0) or 0)
        return AccountInfo(
            total_asset=cash + pos_value,
            available_cash=cash,
            positions=positions,
        )

    def get_orders(self, status: str = None) -> list[dict]:
        engine = self._get_engine()
        return engine.get_trades(self._strategy, limit=200)


# ═══════════════════════════════════════════════════════════
# VnpyAdapter — vnpy 骨架 (需 vnpy 环境)
# ═══════════════════════════════════════════════════════════

class VnpyAdapter(BrokerAdapter):
    """vnpy 券商适配器 — CTP/XTP 真实下单。

    需要 vnpy 4.4+ 环境。自动检测 vnpy 是否可用，不可用时降级。
    子类化以支持不同网关: VnpyCtpAdapter / VnpyXtpAdapter。

    配置:
      execution.broker.vnpy.gateway: "CtpGateway" | "XtpGateway"
      execution.broker.vnpy.settings: {username, password, broker_id, ...}
    """

    name = "vnpy"

    # P0-10 fix: 白名单 — 禁止非法 adapter 名称
    _VALID_ADAPTERS = frozenset({"simulated", "vnpy"})

    def __init__(self, gateway_name: str = None, settings: dict = None,
                 strategy: str = "quant"):
        self._gateway_name = gateway_name or _cfg_get("execution.broker.vnpy.gateway")
        self._settings = settings or _cfg_get("execution.broker.vnpy.settings") or {}
        self._strategy = strategy
        self._connected = False
        self._vnpy_available = False
        self._gateway = None
        self._event_engine = None
        self._main_engine = None
        self._pending_orders = {}  # order_id → 状态 dict (P0-10 fix)

        # 检测 vnpy 可用性
        self._vnpy_available = _check_vnpy()
        if not self._vnpy_available:
            _log.warning(
                "vnpy not installed — VnpyAdapter will raise on connect(). "
                "Install: pip install vnpy vnpy_ctp vnpy_xtp"
            )

    def connect(self) -> bool:
        # P0-10 fix: 白名单校验 — 只有配置为 vnpy 时才允许连接
        configured = _cfg_get("execution.broker.adapter")
        if configured not in self._VALID_ADAPTERS:
            raise ValueError(
                f"execution.broker.adapter={configured!r} 不在白名单 {self._VALID_ADAPTERS} — "
                f"VnpyAdapter 拒绝连接"
            )
        if not self._vnpy_available:
            raise RuntimeError(
                "vnpy not installed. Install: pip install vnpy vnpy_ctp vnpy_xtp. "
                "Use SimulatedAdapter for paper trading."
            )
        _log.info("vnpy adapter connect: whitelist OK (adapter=%s)", configured)
        try:
            self._init_vnpy_engine()
            self._connected = True
            _log.info("vnpy adapter: connected (gateway=%s)", self._gateway_name)
            return True
        except Exception as e:
            _log.error("vnpy connect failed: %s", e)
            self._connected = False
            return False

    def disconnect(self):
        if self._gateway:
            try:
                self._gateway.close()
            except Exception as _e:
                _log.debug("vnpy gateway close failed (non-fatal): %s", _e)
        self._connected = False
        self._gateway = None

    def is_connected(self) -> bool:
        return self._connected

    # ── vnpy 初始化 (子类可覆盖) ──

    def _init_vnpy_engine(self):
        """初始化 vnpy 事件引擎 + 主引擎 + 网关。"""
        from vnpy.event import EventEngine
        from vnpy.trader.engine import MainEngine
        from vnpy.trader.setting import SETTINGS

        self._event_engine = EventEngine()
        self._event_engine.start()
        self._main_engine = MainEngine(self._event_engine)

        # 设置全局配置
        SETTINGS["log.active"] = True
        SETTINGS["log.level"] = 20  # INFO

        # 添加网关
        self._main_engine.add_gateway(self._gateway_name)

        # 连接
        gateway_settings = self._settings.copy()
        self._main_engine.connect(gateway_settings, self._gateway_name)

        # 注册事件回调
        from vnpy.trader.event import EVENT_ORDER, EVENT_TRADE, EVENT_POSITION
        self._event_engine.register(EVENT_ORDER, self._on_order)
        self._event_engine.register(EVENT_TRADE, self._on_trade)
        self._event_engine.register(EVENT_POSITION, self._on_position)

        _log.info("vnpy engine initialized (gateway=%s)", self._gateway_name)

    def _on_order(self, event):
        """订单回报回调 — 同步订单状态到内存表 (P0-10 fix)。"""
        t0 = __import__('time').monotonic()
        try:
            order = event.dict.get("order") if hasattr(event, "dict") else None
            if order is None:
                return
            self._pending_orders[order.orderid] = {
                "symbol": order.symbol,
                "price": float(order.price),
                "volume": int(order.volume),
                "traded": int(order.traded),
                "status": order.status.name,
            }
            _log.info("vnpy order update: %s status=%s traded=%d/%d", order.orderid, order.status.name, order.traded, order.volume)
        except Exception as e:
            _log.error("vnpy _on_order failed: %s", e)
            raise
        finally:
            _log.debug("vnpy _on_order took %.3fs", __import__('time').monotonic() - t0)

    def _on_trade(self, event):
        """成交回报回调 — 记录到 sim_trades 表 (P0-10 fix)。

        与 SimulatedAdapter 同架构: 通过 TradeRepo.record_trade 写入 sim_trades(mode='live')
        — 此前空实现导致实盘成交永远不入库, 持仓/现金/T+1/止损全基于陈旧 SQLite。
        """
        t0 = __import__('time').monotonic()
        try:
            trade = event.dict.get("trade") if hasattr(event, "dict") else None
            if trade is None:
                return
            from quant.data.repos import TradeRepo
            from datetime import datetime as _dt
            today = trade.datetime.strftime("%Y-%m-%d") if trade.datetime else _dt.now().strftime("%Y-%m-%d")
            symbol = trade.symbol
            side = "sell" if trade.direction.name == "SHORT" else "buy"
            shares = int(trade.volume)
            price = float(trade.price)
            commission_rate = _require_cfg("execution.commission")
            TradeRepo().record_trade(
                strategy=self._strategy, date=today, symbol=symbol,
                side=side, price=price, shares=shares,
                cost=price * shares * commission_rate,
                mode="live",
            )
            _log.info("vnpy trade recorded: %s %s %d股 @\xa5%.2f (strategy=%s)", today, symbol, shares, price, self._strategy)
        except Exception as e:
            _log.error("vnpy _on_trade failed: %s", e)
            raise
        finally:
            _log.debug("vnpy _on_trade took %.3fs", __import__('time').monotonic() - t0)

    def _on_position(self, event):
        """持仓回报回调 — 记录到日志用于对账 (P0-10 fix)。"""
        t0 = __import__('time').monotonic()
        try:
            pos = event.dict.get("position") if hasattr(event, "dict") else None
            if pos is None:
                return
            _log.info("vnpy position update: %s direction=%s vol=%d", pos.symbol, pos.direction.name, pos.volume)
        except Exception as e:
            _log.error("vnpy _on_position failed: %s", e)
            raise
        finally:
            _log.debug("vnpy _on_position took %.3fs", __import__('time').monotonic() - t0)

    # ── 下单方法 ──

    def _symbol_to_vnpy(self, symbol: str) -> str:
        """转换股票代码为 vnpy 格式。

        vnpy 格式: 交易所.代码 (如 SSE.600036, SZSE.000001)
        """
        if symbol.startswith(("4", "8", "92")):
            return f"BSE.{symbol}"
        if symbol.startswith(("6", "9", "68")):
            return f"SSE.{symbol}"
        return f"SZSE.{symbol}"

    def _symbol_from_vnpy(self, vt_symbol: str) -> str:
        """从 vnpy vt_symbol 提取纯数字代码。"""
        return vt_symbol.split(".")[-1] if "." in vt_symbol else vt_symbol

    def buy(self, symbol: str, price: float, shares: int,
            order_type: str = "LIMIT") -> OrderResult:
        if not self._connected:
            return OrderResult(success=False, symbol=symbol, side="buy",
                               shares=shares, price=price,
                               error="not connected")

        try:
            from vnpy.trader.constant import Direction, Offset, OrderType
            vt_symbol = self._symbol_to_vnpy(symbol)
            direction = Direction.LONG
            offset = Offset.OPEN
            vnpy_order_type = OrderType.LIMIT if order_type.upper() == "LIMIT" else OrderType.MARKET

            vt_orderids = self._main_engine.send_order(
                symbol=vt_symbol,
                exchange=self._main_engine.get_exchange(vt_symbol),
                direction=direction,
                offset=offset,
                price=price,
                volume=shares,
                order_type=vnpy_order_type,
            )

            if vt_orderids:
                vt_orderid = vt_orderids[0] if isinstance(vt_orderids, list) else vt_orderids
                _log.info("vnpy buy: %s %s股 @¥%.2f order_id=%s",
                          symbol, shares, price, vt_orderid)
                return OrderResult(
                    success=True, symbol=symbol, side="buy",
                    shares=shares, price=price,
                    order_id=vt_orderid, status="pending",
                )
            else:
                return OrderResult(success=False, symbol=symbol, side="buy",
                                   shares=shares, price=price,
                                   error="send_order returned empty")
        except Exception as e:
            _log.error("vnpy buy failed: %s: %s", symbol, e)
            return OrderResult(success=False, symbol=symbol, side="buy",
                               shares=shares, price=price, error=str(e))

    def sell(self, symbol: str, price: float, shares: int,
             order_type: str = "MARKET") -> OrderResult:
        if not self._connected:
            return OrderResult(success=False, symbol=symbol, side="sell",
                               shares=shares, price=price,
                               error="not connected")

        try:
            from vnpy.trader.constant import Direction, Offset, OrderType
            vt_symbol = self._symbol_to_vnpy(symbol)
            direction = Direction.SHORT
            offset = Offset.CLOSE
            vnpy_order_type = OrderType.LIMIT if order_type.upper() == "LIMIT" else OrderType.MARKET

            vt_orderids = self._main_engine.send_order(
                symbol=vt_symbol,
                exchange=self._main_engine.get_exchange(vt_symbol),
                direction=direction,
                offset=offset,
                price=price,
                volume=shares,
                order_type=vnpy_order_type,
            )

            if vt_orderids:
                vt_orderid = vt_orderids[0] if isinstance(vt_orderids, list) else vt_orderids
                _log.info("vnpy sell: %s %s股 @¥%.2f order_id=%s",
                          symbol, shares, price, vt_orderid)
                return OrderResult(
                    success=True, symbol=symbol, side="sell",
                    shares=shares, price=price,
                    order_id=vt_orderid, status="pending",
                )
            else:
                return OrderResult(success=False, symbol=symbol, side="sell",
                                   shares=shares, price=price,
                                   error="send_order returned empty")
        except Exception as e:
            _log.error("vnpy sell failed: %s: %s", symbol, e)
            return OrderResult(success=False, symbol=symbol, side="sell",
                               shares=shares, price=price, error=str(e))

    def cancel(self, order_id: str) -> bool:
        if not self._connected or not self._main_engine:
            return False
        try:
            self._main_engine.cancel_order(order_id)
            _log.info("vnpy cancel: %s", order_id)
            return True
        except Exception as e:
            _log.error("vnpy cancel failed: %s: %s", order_id, e)
            return False

    def get_positions(self) -> list[dict]:
        if not self._main_engine:
            return []
        try:
            from vnpy.trader.constant import Direction
            raw = self._main_engine.get_all_positions()
            result = []
            for pos in raw:
                result.append({
                    "symbol": self._symbol_from_vnpy(pos.vt_symbol),
                    "shares": pos.volume if pos.direction == Direction.LONG else -pos.volume,
                    "avg_cost": pos.price,
                    "pnl": pos.pnl,
                })
            return result
        except Exception as e:
            _log.error("vnpy get_positions failed: %s", e)
            return []

    def get_account(self) -> AccountInfo:
        if not self._main_engine:
            return AccountInfo()
        try:
            raw = self._main_engine.get_all_accounts()
            if raw:
                acct = raw[0]
                return AccountInfo(
                    total_asset=acct.balance + acct.position_profit,
                    available_cash=acct.available,
                    frozen_cash=acct.frozen,
                )
            return AccountInfo()
        except Exception as e:
            _log.error("vnpy get_account failed: %s", e)
            return AccountInfo()

    def get_orders(self, status: str = None) -> list[dict]:
        if not self._main_engine:
            return []
        try:
            raw = self._main_engine.get_all_orders()
            result = []
            for o in raw:
                if status and o.status.value != status:
                    continue
                result.append({
                    "order_id": o.vt_orderid,
                    "symbol": self._symbol_from_vnpy(o.vt_symbol),
                    "side": o.direction.value,
                    "shares": o.volume,
                    "price": o.price,
                    "status": o.status.value,
                })
            return result
        except Exception as e:
            _log.error("vnpy get_orders failed: %s", e)
            return []


class VnpyCtpAdapter(VnpyAdapter):
    """CTP 柜台适配器 (期货/股票) — 最广泛使用的 A 股券商接口。"""
    name = "vnpy_ctp"

    def __init__(self, settings: dict = None, strategy: str = "quant"):
        super().__init__(
            gateway_name="CtpGateway",
            settings=settings,
            strategy=strategy,
        )


class VnpyXtpAdapter(VnpyAdapter):
    """XTP 柜台适配器 (中泰证券等) — 股票专用极速柜台。"""
    name = "vnpy_xtp"

    def __init__(self, settings: dict = None, strategy: str = "quant"):
        super().__init__(
            gateway_name="XtpGateway",
            settings=settings,
            strategy=strategy,
        )


# ═══════════════════════════════════════════════════════════
# Adapter 工厂
# ═══════════════════════════════════════════════════════════

def _check_vnpy() -> bool:
    """检测 vnpy 是否可用。"""
    try:
        import vnpy  # noqa: F401
        return True
    except ImportError:
        return False


# 全局单例 — 进程内复用同一连接
_adapter_instance: Optional[BrokerAdapter] = None


def get_broker_adapter(name: str = None, **kwargs) -> BrokerAdapter:
    """获取券商适配器实例。

    按优先级:
      1. 显式传入 name → 创建对应适配器
      2. config execution.broker.adapter → 从配置读取
      3. 默认 SimulatedAdapter (零依赖, 行为不变)

    进程级单例 — 同一进程内复用连接。
    """
    global _adapter_instance
    if _adapter_instance is not None and _adapter_instance.is_connected():
        return _adapter_instance

    if name is None:
        name = _cfg_get("execution.broker.adapter") or "simulated"

    name = name.lower()
    _log.info("creating broker adapter: %s", name)

    if name == "simulated" or name == "sim":
        _adapter_instance = SimulatedAdapter(**kwargs)
        _adapter_instance.connect()
    elif name == "vnpy_ctp" or name == "ctp":
        adapter_kwargs = {k: v for k, v in kwargs.items() if k in ("settings", "strategy")}
        _adapter_instance = VnpyCtpAdapter(**adapter_kwargs)
        if _adapter_instance._vnpy_available:
            _adapter_instance.connect()
    elif name == "vnpy_xtp" or name == "xtp":
        adapter_kwargs = {k: v for k, v in kwargs.items() if k in ("settings", "strategy")}
        _adapter_instance = VnpyXtpAdapter(**adapter_kwargs)
        if _adapter_instance._vnpy_available:
            _adapter_instance.connect()
    elif name == "vnpy":
        adapter_kwargs = {k: v for k, v in kwargs.items() if k in ("gateway_name", "settings", "strategy")}
        _adapter_instance = VnpyAdapter(**adapter_kwargs)
        if _adapter_instance._vnpy_available:
            _adapter_instance.connect()
    else:
        _log.warning("unknown adapter '%s', falling back to simulated", name)
        _adapter_instance = SimulatedAdapter(**kwargs)
        _adapter_instance.connect()

    return _adapter_instance


def reset_adapter():
    """重置全局适配器（测试/配置切换用）。"""
    global _adapter_instance
    if _adapter_instance:
        try:
            _adapter_instance.disconnect()
        except Exception as _e:
            _log.debug("adapter disconnect failed (non-fatal): %s", _e)
    _adapter_instance = None
