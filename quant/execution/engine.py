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


class ExecutionEngine:
    """模拟执行引擎: 订单执行 → trades.db, 更新 capital_after。

    broker_adapter 注入 (ADR-036):
      传入 BrokerAdapter 实例后, execute() 调用 adapter 替代 SQLite 模拟写入。
      不传或传 None → 保持原有行为 (直接写 sim_trades 表)。
      这是为了支持 vnpy 券商对接, 同时不破坏回测路径。
    """

    def __init__(self, db_path: str = None, cost_model: CostModel | None = None,
                 broker_adapter=None):
        # v555: db_path 默认参数改运行时取值 — 原 `db_path: str = TRADE_DB` 在
        # import 时绑定真实路径, 测试 monkeypatch trade_repo.TRADE_DB 常量无效,
        # 隔离失败的测试直写真实 trades.db (污染事故根因).
        # 运行时从 trade_repo 模块读: 测试 monkeypatch 该模块常量即可全链路隔离.
        import quant.data.repos.trade_repo as _tr_mod
        self.db_path = db_path or _tr_mod.TRADE_DB
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
        """除权除息检测: 对比昨日收盘 vs 订单价格 (单个查询版本，保留兼容性)。

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

    def _check_ex_dividend_batch(self, symbols: list[str], prices: dict[str, float], date: str) -> set[str]:
        """批量除权除息检测 — 精确事件查询.

        以 dividend 事件表为准 (ex_date == 交易日期): 当日有除权事件的股票跳过买入。
        不再使用价格 gap 启发式 — 虚构/模拟价格与真实昨收的偏差会造成误杀
        (ADR: 2026-08-12, 由 broker 模拟器测试暴露)。

        Args:
            symbols: 股票代码列表
            prices: {symbol: order_price} 订单价格字典 (保留签名兼容, 检测不再依赖价格)
            date: 交易日期 (YYYY-MM-DD)
        Returns:
            set: 当日有除权事件、需跳过买入的 symbol 集合
        """
        if not symbols:
            return set()

        mc = market_conn("ro")
        placeholders = ",".join(["?"] * len(symbols))
        rows = mc.execute(
            f"SELECT DISTINCT symbol FROM dividend "
            f"WHERE symbol IN ({placeholders}) AND ex_date = ?",
            symbols + [date],
        ).fetchall()
        mc.close()
        skip_symbols = {r[0] for r in rows}
        if skip_symbols:
            from quant.utils.logger import get_logger
            get_logger("execution.engine").warning(
                f"Ex-dividend on {date}: {sorted(skip_symbols)} — skipping buy"
            )

        return skip_symbols

    def execute(
        self,
        orders: list,
        date: str,
        strategy: str = "quant",
    ) -> int:
        """执行交易 — 模拟/实盘双路径 (v534, ADR-036 落实)。

        orders: [Order, ...] 或 [(symbol, side, shares, price), ...]
        date: 交易日期 (YYYY-MM-DD)
        strategy: 策略标识

        返回: 执行的订单数。

        双路径 (仅卖出单):
        - broker_adapter=None (回测/未注入): 纯模拟 — 直接写账本 (sim_trades)
        - broker_adapter 已连接: 先 adapter.sell 真实券商成交, 成功后才写账本
          (账本唯一真相源, 券商成交与账本原子同步 — 双账翻倍风险的根治)
        - broker_adapter 存在但未连接: RuntimeError 零 fallback — 宁可留仓
          也不模拟成交 (账本清、券商留 = 事后不可逆)

        买入单恒为纯账本写入: 实盘买入由 OrderManager 限价挂单流成交,
        此处仅做账本同步 (monitor/order_manager 自管券商买入)。

        所有订单在同一事务中执行 — 部分失败时整体回滚。
        读操作（T+1、除权检测、PnL 计算）在事务外完成，只有 write 在事务内。
        """
        import traceback
        from quant.utils.logger import get_logger
        logger = get_logger("execution.engine")
        repo = TradeRepo(self.db_path)

        # ── Phase 1: 预计算 (纯读, 事务外) ──
        # test-v458 P5: 批量除权检测，减少 DB round-trip
        buy_symbols = []
        buy_prices = {}
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
            if side == "buy":
                buy_symbols.append(symbol)
                buy_prices[symbol] = price
            entries.append(e)

        # v534: 卖出单双路径 — 券商先成交, 成功才进账本 (零 fallback).
        # simulated adapter 内部已写账本 (自管执行) → 排除, 防双重写入;
        # 真实券商 (vnpy/ctp/xtp) 注入且未连接 → RuntimeError 拒绝模拟。
        # 批量除权检测
        skip_symbols = self._check_ex_dividend_batch(buy_symbols, buy_prices, date)

        # v554 (P0-1): T+1 预检前置 — 原检查在券商卖出循环之后:
        # T+1 被阻断的卖单已在券商成交 (真实持仓已减) 但未记账 → 账本/券商分歧,
        # 次日重启后券商侧缺仓而账本显示持仓 (双账风险). 预检先标记, 券商循环跳过.
        for e in entries:
            symbol = e["symbol"]
            side = e["side"]
            shares = e["shares"]
            price = e["price"]
            if side == "buy":
                e["cost"] = round(self.cost_model.buy_cost(price, shares) - price * shares, 2)
                e["pnl"] = 0.0
                e["pnl_pct"] = 0.0
                if symbol in skip_symbols:
                    e["skip"] = True
            else:
                if repo.check_t1(strategy, symbol, date):
                    logger.warning(
                        f"T+1 blocked: {symbol} bought today, cannot sell until next trading day"
                    )
                    e["t1_blocked"] = True
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

        _adapter = getattr(self, "broker_adapter", None)
        if _adapter is not None and getattr(_adapter, "name", "") != "simulated":
            if not _adapter.is_connected():
                raise RuntimeError(
                    f"卖出无法执行: broker adapter {_adapter.name} 未连接 — 拒绝模拟成交 "
                    f"(v534 P0-1: 账本/券商双账风险). 请恢复券商连接后重试")
            for e in entries:
                if e["side"] == "sell" and e.get("t1_blocked"):
                    continue
                if e["side"] == "sell":
                    r = _adapter.sell(e["symbol"], e["price"], e["shares"],
                                      order_type="MARKET")
                    if not r.success:
                        raise RuntimeError(
                            f"券商卖出失败 {e['symbol']}: {r.error} — 拒绝写账本 (v534 P0-1)")
                    logger.info(f"[{date}] broker sell: {e['symbol']} {e['shares']}股 "
                                f"@¥{e['price']:.2f} status={r.status}")

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
