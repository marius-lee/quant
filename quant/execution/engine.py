"""执行引擎 — 模拟订单执行 + 交易记录持久化。

状态: trades.db (交易唯一真相源)
依赖: execution/cost.py 成本模型
"""

import os
from datetime import date as date_type
from typing import Optional
from dataclasses import dataclass
from quant.execution.cost import CostModel
from quant.data.store import market_conn  # P69: 统一连接层
from quant.data.repos import TradeRepo
from quant.config.paths import TRADE_DB, MARKET_DB


def _price_limit_pct(symbol: str) -> float:
    """B-16 fix: 板块差异化涨跌停幅度 (此前统一 10%, 创业板 20%/北交所 30% 被误判为除权).

    688 科创板 / 30x 创业板: ±20%; 4xx/8xx/92x 北交所: ±30%; 主板: ±10%.
    """
    if symbol.startswith(("68", "30")):
        return 0.20
    if symbol.startswith(("4", "8", "92")):
        return 0.30
    return 0.10


@dataclass
class Order:
    """模拟订单。"""
    symbol: str
    side: str     # buy | sell
    shares: int
    price: float
    cost: float = 0.0
    strategy: str = "quant"
    note: str = ""
    board_count: int = 100  # test-v307: default 100 股/手 (主板)
    """模拟执行引擎: 订单执行 → trades.db, 更新 capital_after。

    broker_adapter 注入 (ADR-036):
      传入 BrokerAdapter 实例后, execute() 调用 adapter 替代 SQLite 模拟写入。
      不传或传 None → 保持原有行为 (直接写 sim_trades 表)。
      这是为了支持 vnpy 券商对接, 同时不破坏回测路径。
    """

    def __init__(self, db_path: str = TRADE_DB, cost_model: CostModel | None = None,
                 broker_adapter=None):
        self.db_path = db_path
        self.cost_model = cost_model if cost_model is not None else CostModel.from_config()
        self.broker_adapter = broker_adapter  # ADR-036: 外部券商适配器 (None=模拟)
        # Schema auto-managed by TradeRepo.__init__()
        TradeRepo(db_path=self.db_path)

    def get_capital(self, strategy: str = "quant", prices: dict | None = None) -> float:
        """获取当前策略总资产 (现金 + 持仓价值) — 委托 TradeRepo。

        B-06 fix: 支持按市价计价 (MTM)。不传 prices 时保持原行为 (成本价),
        传入 {symbol: price} 后持仓按市价估值, 用于净值曲线/绩效/基准跟踪。
        """
        repo = TradeRepo(self.db_path)
        cash = repo.get_cash(strategy)
        positions = repo.get_positions(strategy)
        if prices:
            pos_value = sum(
                (float(prices.get(p['symbol']) or 0) or (p.get('price', 0) or 0))
                * (p.get('shares', 0) or 0)
                for p in positions)
        else:
            pos_value = sum((p.get('price', 0) or 0) * (p.get('shares', 0) or 0) for p in positions)
        return cash + pos_value

    def get_cash(self, strategy: str = "quant") -> float:
        """获取当前现金余额 — 委托 TradeRepo (sim_trades 实时计算)。"""
        return TradeRepo(self.db_path).get_cash(strategy)

    def is_initialized(self, strategy: str = "quant") -> bool:
        """策略是否已初始化 (防止亏完后重复种子)。"""
        return TradeRepo(self.db_path).is_initialized(strategy)

    def set_initial_capital(self, strategy: str, capital: float):
        """设置策略初始资金 — 委托 TradeRepo。"""
        TradeRepo(self.db_path).set_initial_capital(strategy, capital)

    def _check_ex_dividend(self, symbol: str, order_price: float, date: str) -> bool:
        """除权除息检测: 对比昨日收盘 vs 订单价格。

        A股涨跌停限制按板块区分 (B-16: 主板 ±10%, 创业板/科创板 ±20%, 北交所 ±30%)。
        若订单价格与前一交易日收盘价偏差超过该板块涨跌停幅度, 无法用正常交易解释,
        判定为除权除息事件, 跳过买入。

        Args:
            symbol: 股票代码
            order_price: 订单买入价格
            date: 交易日期 (YYYY-MM-DD)
        Returns:
            True: 检测到除权跳变, 应跳过买入
            False: 正常, 可执行
        """
        mc = market_conn("ro")
        row = mc.execute(
            "SELECT close FROM daily WHERE symbol=? AND date < ? ORDER BY date DESC LIMIT 1",
            (symbol, date)
        ).fetchone()
        if row and row[0]:
            prev_close = float(row[0])
            gap = abs(order_price / prev_close - 1)
            threshold = _price_limit_pct(symbol)
            if gap > threshold:
                from quant.utils.logger import get_logger
                get_logger("execution.engine").warning(
                    f"Ex-dividend detected: {symbol} order_price={order_price:.2f} "
                    f"prev_close={prev_close:.2f} gap={gap:.1%} > {threshold:.0%} — skipping buy"
                )
                mc.close()
                return True
        mc.close()
        return False

    def execute(
        self,
        orders: list,
        date: str,
        strategy: str = "quant",
    ) -> int:
        """执行模拟交易 — DB 操作委托 TradeRepo, 成本/PnL 计算留在引擎。

        orders: [Order, ...] 或 [(symbol, side, shares, price), ...]
        date: 交易日期 (YYYY-MM-DD)
        strategy: 策略标识

        返回: 执行的订单数。
        所有订单在同一事务中执行 — 部分失败时整体回滚。
        读操作（T+1、除权检测、PnL 计算）在事务外完成，只有 write 在事务内。
        """
        import traceback
        from quant.utils.logger import get_logger
        logger = get_logger("execution.engine")
        repo = TradeRepo(self.db_path)

        # ── Phase 1: 预计算 (纯读, 事务外) ──
        entries = []
        for o in orders:
            if isinstance(o, (list, tuple)):
                symbol, side, shares, price = o[0], o[1], o[2], o[3]
            else:
                symbol, side, shares, price = o.symbol, o.side, o.shares, o.price
            e = {
                "symbol": symbol, "side": side, "shares": shares, "price": price,
                "board_count": getattr(o, "board_count", 0),
            }
            if side == "buy" and self._check_ex_dividend(symbol, price, date):
                e["skip"] = True
                entries.append(e)
                continue
            if side == "buy":
                e["cost"] = round(self.cost_model.buy_cost(price, shares) - price * shares, 2)
                e["pnl"] = 0.0
                e["pnl_pct"] = 0.0
            else:
                if repo.check_t1(strategy, symbol, date):
                    logger.warning(
                        f"T+1 blocked: {symbol} bought today, cannot sell until next trading day"
                    )
                    e["t1_blocked"] = True
                    entries.append(e)
                    continue
                proceeds = self.cost_model.sell_proceeds(price, shares)
                e["cost"] = round(price * shares - proceeds, 2)
                avg_cost = repo.get_average_cost(strategy, symbol)  # FIFO (2026-07-21 audit H7)
                if avg_cost and avg_cost * shares > 0:
                    e["pnl"] = round(proceeds - avg_cost * shares, 2)
                    e["pnl_pct"] = round(proceeds / (avg_cost * shares) - 1, 2)
                else:
                    e["pnl"] = 0.0
                    e["pnl_pct"] = 0.0
            entries.append(e)

        # ── Phase 2: 写入 (事务内) ──
        conn = repo._conn()
        executed = 0
        conn.execute("BEGIN")
        for e in entries:
            if e.get("skip") or e.get("t1_blocked"):
                continue
            repo.record_trade(
                strategy, date, e["symbol"], e["side"],
                e["price"], e["shares"],
                pnl=e["pnl"], pnl_pct=e["pnl_pct"],
                board_count=e["board_count"],
                cost=e["cost"],
                conn=conn,
            )
            executed += 1
        conn.commit()
        logger.info(f"executed {executed} orders via TradeRepo")
        return executed

    def get_positions(self, strategy: str = "quant") -> list[dict]:
        """获取当前持仓列表 — 委托 TradeRepo。"""
        return TradeRepo(self.db_path).get_positions(strategy)

    def get_trades(self, strategy: str = "quant", limit: int = 50) -> list[dict]:
        """获取最近交易记录 — 委托 TradeRepo。"""
        return TradeRepo(self.db_path).get_trades(strategy, limit=limit)
