"""统一执行模型 (报告 §6.1 / §1.2) — 回测/实盘共用执行链, 差异只在成交语义。

修复前的语义分裂:
  回测 pipeline.execute_signals: 市价单 100% 成交, validate 失败全部丢弃
  实盘 scheduler/execute: 卖市价+买限价(OrderManager), validate 失败按 alpha 裁剪

本模块 (Template Method):
  共用链 — 冷却过滤 → 固定止损(RiskManager) → delta(compute_trades)
          → validate + 按 alpha 裁剪 (B-13 边际成本公式, 两侧统一)
  子类差异 — BacktestExecutionModel: 买卖均市价立即成交
             LiveExecutionModel: 卖市价立即成交, 买挂限价单(OrderManager)
             熔断检查只在 Live (回测无熔断概念)
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

import pandas as pd

from quant.config.constants import _require_cfg
from quant.execution.cost import CostModel
from quant.execution.engine import ExecutionEngine, Order
from quant.execution.stop_loss import RiskManager
from quant.optimizer.rebalance import compute_trades, validate_orders
from quant.utils.logger import get_logger

_log = get_logger("execution.model")

LOT_SIZE = _require_cfg("backtest.lot_size")


@dataclass
class ExecutionContext:
    """执行上下文 — 两个模型的输入完全一致。"""
    engine: ExecutionEngine
    strategy: str
    today: str
    prices: dict                 # {symbol: float} 开盘价(回测) / 实时价(实盘)
    cost_model: CostModel
    repo: object = None          # TradeRepo — Live 熔断/冷却需要; 回测可 None
    risk_manager: RiskManager = None  # 注入以共享 cooloff store; None 则按模式自建
    ohlc: dict = None            # B8: 回测 {symbol: {open,high,low,prev_close}} — 一字板涨跌停判定


@dataclass
class ExecutionResult:
    """执行结果 — 字段对两侧一致, buys_mode 区分成交语义。"""
    stopped_out: list = field(default_factory=list)   # 触发止损的 symbol
    sells: int = 0
    buys: int = 0
    buys_mode: str = ""          # "filled" | "limit_placed" | "blocked_circuit_breaker" | "none"
    orders: list = field(default_factory=list)        # 最终订单 (裁剪后)


def trim_orders_by_alpha(orders: list, cash: float, cost_model,
                         target_scores: dict, log=None) -> list:
    """资金不足时按 alpha 得分降序裁剪 — B-13 边际成本公式, 回测/实盘统一。

    原只在实盘 execute.py; 回测 validate 失败全部丢弃 (过于悲观且语义分裂)。
    返回: sell_orders + 可负担的 buy_orders (股数按整手重算)。
    """
    log = log or _log
    buy_orders = [o for o in orders if o.side == "buy"]
    sell_orders = [o for o in orders if o.side == "sell"]
    buy_orders.sort(key=lambda o: target_scores.get(o.symbol, 0), reverse=True)
    available = cash
    for o in sell_orders:
        available += o.price * o.shares - o.cost
    feasible = []
    for o in buy_orders:
        px = o.price
        if px <= 0:
            continue
        # B-13: 边际成本 while 递减 (原双重计数公式已废)
        # B7 (2026-08-18): 上限必须 min(原目标股数) — 原 max_shares 仅按可用资金,
        # 资金不足裁剪时首单被放大到耗尽全部资金, 超出目标股数, 组合超额配置.
        max_shares = min(o.shares, int(available // (px * LOT_SIZE)) * LOT_SIZE)
        while max_shares >= LOT_SIZE and cost_model.buy_cost(px, max_shares) > available:
            max_shares -= LOT_SIZE
        if max_shares >= LOT_SIZE:
            o.shares = max_shares
            o.cost = cost_model.buy_cost(px, max_shares)
            available -= o.cost
            feasible.append(o)
            log.info(f"  trim kept {o.symbol}: {o.shares}股 @¥{px:.2f} "
                     f"(score={target_scores.get(o.symbol, 0):.2f})")
        else:
            log.info(f"  trim dropped {o.symbol}: unaffordable "
                     f"(score={target_scores.get(o.symbol, 0):.2f})")
    return sell_orders + feasible


class ExecutionModel(ABC):
    """执行模型基类 — run() 为共用链, 成交语义由子类实现。"""

    #: compute_trades 是否跳过资金可行性检查 (实盘 pipeline 已分配完毕 → True)
    skip_cash_feasibility: bool = False
    #: cooloff 存储: None=DB(实盘), dict=内存(回测) — risk_manager 未注入时生效
    cooloff_store = None

    def _risk_manager(self, ctx: ExecutionContext) -> RiskManager:
        if ctx.risk_manager is not None:
            return ctx.risk_manager
        return RiskManager(strategy=ctx.strategy, cooloff_store=self.cooloff_store)

    def _execute_stop_orders(self, orders: list, ctx: ExecutionContext) -> None:
        """v532 (P0-1, 实盘资金安全): 止损卖单经 broker_adapter.

        原 4 处止损直接 ctx.engine.execute → 只写 sim_trades — 账本已清、
        券商实际仍持仓 → 持仓翻倍风险 (ADR-036 止损路径从未实现)。

        - adapter=None (回测/未注入): engine.execute 模拟 (回测语义)
        - adapter 已连接 (simulated 等价写账本 / 真实券商): adapter.sell
        - adapter 存在但未连接 (vnpy 掉线): RuntimeError — 零 fallback,
          宁可留仓也不双账 (账本清、券商留 = 事后不可逆)
        """
        adapter = getattr(ctx.engine, 'broker_adapter', None)
        if adapter is None:
            ctx.engine.execute(orders, ctx.today, ctx.strategy)
            return
        if not adapter.is_connected():
            raise RuntimeError(
                f"止损无法执行: broker adapter {adapter.name} 未连接 — 拒绝模拟成交 "
                f"(P0-1: 账本/券商双账风险). 请恢复券商连接后重试")
        for o in orders:
            r = adapter.sell(o.symbol, o.price, o.shares, order_type="MARKET")
            if not r.success:
                raise RuntimeError(
                    f"止损券商卖出失败 {o.symbol}: {r.error} — 拒绝模拟成交 (P0-1)")
            _log.info(f"[{ctx.today}] stop-loss via broker: {o.symbol} "
                      f"{o.shares}股 @¥{o.price:.2f} status={r.status}")

    def run(self, targets: list, ctx: ExecutionContext,
            risk_only: bool = False) -> ExecutionResult:
        """共用执行链: 冷却 → 止损 → delta → 校验裁剪 → 分单成交。

        risk_only=True (rebalance_freq=weekly 的非调仓日):
        只执行硬止损 + 冷却登记, 跳过 delta/validate/分单 — 组合不再平衡,
        业界标准周频语义 (风控每日不断, 建仓集中在调仓日)。
        """
        rm = self._risk_manager(ctx)
        result = ExecutionResult()

        if risk_only:
            positions = ctx.engine.get_positions(ctx.strategy)
            # 硬止损
            stops = rm.check_hard_stop(positions, ctx.prices)
            for st in stops:
                _log.warning(f"[{ctx.today}] stop-loss (risk_only): {st['symbol']} "
                             f"drop={st['drop']:.1%}, selling {st['shares']}")
                self._execute_stop_orders(
                    [Order(symbol=st["symbol"], side="sell", shares=st["shares"],
                           price=st["price"], cost=0)], ctx)
                rm.set_cooloff(st["symbol"], ctx.today)
                result.stopped_out.append(st["symbol"])
            # v410: ATR 动态止盈止损 (回测↔实盘一致)
            _quotes = {s: {"price": p} for s, p in ctx.prices.items() if p > 0}
            atr_stops = rm.check(positions, _quotes, ctx.today,
                                 atr_panel=getattr(ctx, "atr_panel", None))
            for _as in atr_stops:
                self._execute_stop_orders(
                    [Order(symbol=_as["symbol"], side="sell", shares=_as["shares"],
                           price=_as["price"], cost=0)], ctx)
                result.stopped_out.append(_as["symbol"])
            result.sells = len(result.stopped_out)
            result.buys_mode = "none"
            return result

        # ── 1. 冷却过滤 (含历史止损) ──
        cooling = rm.get_cooloff_symbols(ctx.today)
        if cooling:
            targets = [tp for tp in targets if tp["symbol"] not in cooling]
            _log.info(f"[{ctx.today}] cooloff filter: {sorted(cooling)} excluded")
        target_lots = {tp["symbol"]: tp["shares"] // LOT_SIZE for tp in targets}

        # ── 2. 固定止损 + ATR 动态止盈止损 (v410: 回测↔实盘一致) ──
        positions = ctx.engine.get_positions(ctx.strategy)
        current_lots = {p["symbol"]: p["shares"] // LOT_SIZE for p in positions}
        # 硬止损
        stops = rm.check_hard_stop(positions, ctx.prices)
        for st in stops:
            _log.warning(f"[{ctx.today}] stop-loss: {st['symbol']} "
                         f"drop={st['drop']:.1%}, selling {st['shares']}")
            self._execute_stop_orders(
                [Order(symbol=st["symbol"], side="sell", shares=st["shares"],
                       price=st["price"], cost=0)], ctx)
            rm.set_cooloff(st["symbol"], ctx.today)
            result.stopped_out.append(st["symbol"])
            positions = ctx.engine.get_positions(ctx.strategy)
            current_lots = {p["symbol"]: p["shares"] // LOT_SIZE for p in positions}

        # v410: ATR 动态止盈止损 (回测↔实盘一致)
        # 构建 quotes dict (回测用日线价格)
        _quotes = {s: {"price": p} for s, p in ctx.prices.items() if p > 0}
        atr_stops = rm.check(positions, _quotes, ctx.today,
                             atr_panel=getattr(ctx, "atr_panel", None))
        for _as in atr_stops:
            _log.warning(f"[{ctx.today}] ATR stop: {_as['symbol']} {_as['reason']}")
            self._execute_stop_orders(
                [Order(symbol=_as["symbol"], side="sell", shares=_as["shares"],
                       price=_as["price"], cost=0)], ctx)
            rm.set_cooloff(_as["symbol"], ctx.today)
            result.stopped_out.append(_as["symbol"])
            result.sells += 1
            # B3 (2026-08-18): ATR 止损卖出后必须刷新持仓 —
            # 原硬止损刷新了而 ATR 分支没有, 且 target 过滤在 ATR 检查之前,
            # delta 对已清仓 symbol 生成卖出单 → 二次卖出/负持仓/券商拒单.
            positions = ctx.engine.get_positions(ctx.strategy)
            current_lots = {p["symbol"]: p["shares"] // LOT_SIZE for p in positions}

        # B3: 止损清仓 symbol 统一从目标中移除 (硬止损+ATR, 移至 ATR 之后)
        if result.stopped_out:
            target_lots = {s: l for s, l in target_lots.items()
                           if s not in result.stopped_out}
            targets = [tp for tp in targets if tp["symbol"] not in result.stopped_out]

        # ── 3. delta 计算 ──
        cash = ctx.engine.get_cash(ctx.strategy)
        position_value = 0.0
        for p in positions:
            px = ctx.prices.get(p["symbol"])
            if px is None or pd.isna(px) or px <= 0:
                # P1-15 fix: 缺报价 → 阻断交易, 估值视为 0 (不使用成本价)
                logger = get_logger("execution.model")
                logger.warning(f"P1-15: no market price for {p['symbol']}, excluding from valuation")
                continue
            position_value += p["shares"] * float(px)
        total_capital = round(cash + position_value, 2)

        orders = compute_trades(
            pd.Series(target_lots, dtype=int),
            pd.Series(current_lots, dtype=int),
            pd.Series(ctx.prices) if not isinstance(ctx.prices, pd.Series) else ctx.prices,
            ctx.cost_model,
            capital=total_capital, cash=cash,
            skip_cash_feasibility=self.skip_cash_feasibility,
        ) if (target_lots or current_lots) else []

        # ── 4. 校验 + 按 alpha 裁剪 (两侧统一, 原回测失败全丢) ──
        if orders:
            is_valid, msg = validate_orders(orders, ctx.engine.get_cash(ctx.strategy))
            if not is_valid:
                _log.warning(f"[{ctx.today}] validate_orders failed: {msg}, "
                             f"trimming by alpha...")
                scores = {tp["symbol"]: tp.get("score", 0) for tp in targets}
                orders = trim_orders_by_alpha(
                    orders, ctx.engine.get_cash(ctx.strategy),
                    ctx.cost_model, scores)

        # ── 5. 分单成交 (子类语义) ──
        sell_orders = [o for o in orders if o.side == "sell"]
        buy_orders = [o for o in orders if o.side == "buy"]
        self.execute_sells(sell_orders, ctx)
        result.buys_mode = self.execute_buys(buy_orders, ctx) if buy_orders else "none"
        result.sells = len(sell_orders)
        result.buys = len(buy_orders)
        result.orders = orders
        return result

    @abstractmethod
    def execute_sells(self, orders: list, ctx: ExecutionContext):
        """卖单成交语义。"""

    @abstractmethod
    def execute_buys(self, orders: list, ctx: ExecutionContext) -> str:
        """买单成交语义, 返回 buys_mode。"""


class BacktestExecutionModel(ExecutionModel):
    """回测: 买卖均按给定价格 (开盘价) 立即成交。
    P2b: 接入 Almgren-Chriss 成本模型, 模拟滑点和市场冲击。
    B8: 一字板涨跌停无法成交 (open==high==low==涨/跌停价) — 阻断对应订单。
    """

    skip_cash_feasibility = False

    def __init__(self):
        self.cooloff_store = {}  # 回测热路径: 内存冷却, 零 DB 写

# ── B8: 一字板涨跌停判定 ──
    def _sealed_orders(self, orders, ctx):
        """按一字板涨跌停阻断订单: 返回 (allowed, blocked) 两个列表.

        A股制度: 主板±10% / 创业板·科创板±20% / 北交所±30% (B-16)。
        判定: open==high==low 触及涨/跌停价 → 全天无成交 → 该方向订单不可能成交。
        数据缺失 (ohlc 未提供 / 字段缺) → 不阻断, 保持旧行为。
        """
        from quant.execution.engine import _price_limit_pct
        if not orders or not ctx.ohlc:
            return list(orders), []  # 数据缺失: 不阻断 (保持旧行为)
        allowed, blocked = [], []
        for o in orders:
            d = ctx.ohlc.get(o.symbol) or {}
            op, hi, lo, pc = d.get("open"), d.get("high"), d.get("low"), d.get("prev_close")
            if not (op and hi and lo and pc):
                allowed.append(o)
                continue
            limit_pct = _price_limit_pct(o.symbol)
            sealed = False
            if o.side == "buy":
                # 开盘即涨停且全天未开板: 买入无法成交
                limit_up = round(pc * (1 + limit_pct), 2)
                if hi == lo == op and abs(op - limit_up) <= 0.02:
                    sealed = True
            else:
                # 开盘即跌停且全天未开板: 卖出无法成交
                limit_down = round(pc * (1 - limit_pct), 2)
                if hi == lo == op and abs(op - limit_down) <= 0.02:
                    sealed = True
            if sealed:
                _log.info(f"[{ctx.today}] B8 sealed {o.side} blocked: {o.symbol} "
                          f"open={op} (limit {'up' if o.side == 'buy' else 'down'}), skip")
                blocked.append(o)
            else:
                allowed.append(o)
        return allowed, blocked

    def _apply_cost(self, orders: list, ctx: ExecutionContext, side: str):
        """P2b: 对订单应用市场冲击成本。

        买入: fill_price = signal_price * (1 + impact_bps / 10000)
        卖出: fill_price = signal_price * (1 - impact_bps / 10000)
        impact_bps 来自 CostModel.estimate_market_impact()。
        """
        if ctx.cost_model is None:
            return
        impact_bps = 0.0  # B6 (CODE-REVIEW): 循环外初始化, 原空订单列表时 UnboundLocalError
        try:
            for o in orders:
                impact_bps = ctx.cost_model.estimate_market_impact(
                    o.symbol, o.shares, side
                )
                if impact_bps and impact_bps > 0:
                    if side == "buy":
                        o.price = round(o.price * (1 + impact_bps / 10000), 2)
                    else:
                        o.price = round(o.price * (1 - impact_bps / 10000), 2)
                    # v406: 价格调整后更新 cost, 防止成交现金为负
                    o.cost = round(o.cost + o.price * o.shares * (impact_bps / 10000), 2)
            _log.debug(f"[{ctx.today}] cost model: {len(orders)} {side} orders, "
                       f"impact_bps={impact_bps:.1f}")
        except Exception as e:
            _log.debug(f"[{ctx.today}] cost model apply failed (non-fatal): {e}")

    def execute_sells(self, orders, ctx):
        # B8: 一字跌停阻断 (无法卖出)
        sellable, _blocked = self._sealed_orders(orders, ctx)
        self._apply_cost(sellable, ctx, "sell")
        if sellable:
            ctx.engine.execute(sellable, ctx.today, ctx.strategy)

    def execute_buys(self, orders, ctx):
        # B8: 一字涨停阻断 (无法买入)
        buyable, _blocked = self._sealed_orders(orders, ctx)
        self._apply_cost(buyable, ctx, "buy")
        ctx.engine.execute(buyable, ctx.today, ctx.strategy)
        return "filled"


class LiveExecutionModel(ExecutionModel):
    """实盘: 卖单市价立即成交, 买单挂限价单 (ADR 033, OrderManager)。

    ADR-036: 卖单通过 BrokerAdapter.sell() 执行, 买单通过 OrderManager 挂限价单。
    当 broker_adapter 注入 ExecutionContext.engine 时: 卖单走真实券商;
    当 broker_adapter 为 None 时: 走原有 engine.execute() 模拟写入。

    熔断检查只在本模型 (B-14): monitor 盘中触发熔断 → 阻断新买单。
    """

    skip_cash_feasibility = True
    cooloff_store = None  # DB 持久化冷却

    def execute_sells(self, orders, ctx):
        if not orders:
            return
        # ADR-036: 卖单通过 broker_adapter (如果可用) 或 engine.execute (回退)
        adapter = getattr(ctx.engine, 'broker_adapter', None)
        if adapter is not None and adapter.is_connected():
            for o in orders:
                result = adapter.sell(o.symbol, o.price, o.shares, order_type="MARKET")
                if not result.success:
                    _log.error(f"[{ctx.today}] broker sell failed: {o.symbol} "
                               f"{o.shares}股 @¥{o.price:.2f}: {result.error}")
                else:
                    _log.info(f"[{ctx.today}] broker sell: {o.symbol} "
                              f"{o.shares}股 @¥{o.price:.2f} status={result.status}")
        elif adapter is not None:
            # v532 (P0-1): adapter 存在但未连接 → 拒绝模拟 — 账本已清、券商
            # 仍持仓 = 持仓翻倍 (原直接回退 engine.execute 是双账根源).
            raise RuntimeError(
                f"卖单无法执行: broker adapter {adapter.name} 未连接 — 拒绝模拟成交 "
                f"(P0-1: 账本/券商双账风险). 请恢复券商连接后重试")
        else:
            ctx.engine.execute(orders, ctx.today, ctx.strategy)
        _log.info(f"[{ctx.today}] executed {len(orders)} sell orders")

    def execute_buys(self, orders, ctx) -> str:
        # B-14: 熔断检查 (live only)
        if ctx.repo is not None:
            cb_date = ctx.repo.get_flag("circuit_breaker")
            if cb_date:
                cb_reason = ctx.repo.get_flag("circuit_breaker_reason") or ""
                _log.warning(f"[{ctx.today}] CIRCUIT BREAKER active "
                             f"(triggered {cb_date}: {cb_reason}) "
                             f"— skipping {len(orders)} buy orders")
                for o in orders:
                    ctx.repo.update_signal_exec_note(ctx.today, o.symbol,
                                                     "blocked_circuit_breaker")
                return "blocked_circuit_breaker"
        from quant.scheduler.order_manager import OrderManager
        om = OrderManager()
        om.cancel_all(ctx.today, ctx.strategy)  # 先清旧挂单, 防重启重复
        for o in orders:
            ref_price = ctx.prices.get(o.symbol, o.price)
            om.place(ctx.today, ctx.strategy, o.symbol, o.shares, ref_price)
        _log.info(f"[{ctx.today}] placed {len(orders)} limit buy orders")
        return "limit_placed"
