"""Pipeline execute."""
import time
import uuid as _uuid
import pandas as pd
from quant.config.paths import TRADE_DB
from quant.config.constants import _require_cfg
from quant.execution.engine import ExecutionEngine
from quant.execution.cost import CostModel
from quant.utils.logger import get_logger
from quant.core.phase_tracker import PhaseTracker

logger = get_logger("pipeline")
LOT_SIZE = _require_cfg("backtest.lot_size")

def execute_signals(target_positions: list[dict], date_str: str, strategy: str = "quant",
                    prices: dict = None, db_path: str = TRADE_DB,
                    suppress_push: bool = False, ctx: "ExecutionContext | None" = None,
                    risk_only: bool = False, ohlc: dict = None) -> dict:
    """Pipeline 阶段二: 开盘执行 (Step 6)。

    prices: 预提供的开盘价dict (回测用); None则由fetch_quotes获取实时报价.
    db_path: 交易数据库路径 (回测用); None使用默认.
    suppress_push: True→不调用 broker.update (回测用).
    risk_only: True→只跑硬止损, 不再平衡 (weekly 非调仓日, rebalance_freq).
    ohlc: B8 (CODE-REVIEW) 回测用 {symbol: {open,high,low,prev_close}} —
          一字板涨跌停成交阻断; None → 不做限制 (live 有独立 quote 预检).
    """
    from quant.utils.logger import get_trace_id, set_trace_id as _set_tid
    tid = get_trace_id() or _uuid.uuid4().hex[:12]
    _set_tid(tid)
    from quant.monitor.metrics import metrics as _m
    _m.inc("pipeline.runs")
    # 统一上下文解包
    from quant.backtest.context import ExecutionContext
    if isinstance(ctx, ExecutionContext):
        db_path = ctx.db_path or db_path
        suppress_push = ctx.suppress_push
        engine = ctx.get_engine()
        cost_model = ctx.get_cost_model()
    elif ctx is not None:
        # 兼容旧 PipelineContext (dict-like)
        db_path = db_path or getattr(ctx, 'db_path', db_path)
        suppress_push = suppress_push or getattr(ctx, 'suppress_push', False)
        engine = ExecutionEngine(db_path=db_path)
        cost_model = CostModel.from_config()
    else:
        engine = ExecutionEngine(db_path=db_path)
        cost_model = CostModel.from_config()

    t0 = time.time()
    results = {"date": date_str, "steps": {}}
    tracker = PhaseTracker("generate_signals")
    import time as _time_ph
    _ph_t0 = _time_ph.time()
    _ph_start = _time_ph.time()
    logger.info(f"execute_signals started trace_id={tid} date={date_str} strategy={strategy}")

    # Get current positions
    current_positions = engine.get_positions(strategy)
    logger.info(f"execute: {len(current_positions)} current positions, {len(target_positions)} target")

    # Build current lots map
    current_lots = {}
    for p in current_positions:
        current_lots[p["symbol"]] = p["shares"] // LOT_SIZE

    # Build target lots map
    target_lots = {}
    for tp in target_positions:
        sym = tp["symbol"]
        target_lots[sym] = tp["shares"] // LOT_SIZE

    # Load prices — 直接用 Sina 实时开盘价, 不走 market.db 回退.
    if prices is not None:
        # Backtest mode: use provided open prices directly
        prices = pd.Series(prices)
    else:
        # Live mode: fetch from Sina
        # 拉不到报价 → 不执行 (用错价格比不交易危害大, 且永不 fallback 制造隐形 bug).
        from quant.execution.quote import fetch_quotes
        symbols = list(set(list(current_lots.keys()) + list(target_lots.keys())))
        quotes = fetch_quotes(symbols)
        if not quotes:
            logger.error(
                f"execute: fetch_quotes returned empty for {len(symbols)} symbols — "
                f"skipping execution to avoid trading at stale prices"
            )
            return results

        prices = {}
        for sym, q in quotes.items():
            open_px = q.get("open", 0)
            if open_px > 0:
                prices[sym] = open_px
        # P1-15 fix: 缺报价持仓不使用成本价 (阻断卖单, 宁可缺仓不可错价)
        _uncovered = [p["symbol"] for p in current_positions if p["symbol"] not in prices]
        if _uncovered:
            logger.warning(
                f"P1-15: {len(_uncovered)} positions without quotes, blocking sells: "
                f"{', '.join(_uncovered[:10])}"
            )
        # 报价未覆盖的目标 (极罕见) 使用 sina price 而非昨日 close
        for tp in target_positions:
            if tp["symbol"] not in prices:
                q = quotes.get(tp["symbol"], {})
                prices[tp["symbol"]] = q.get("price", 0) or q.get("open", 0)
    prices = pd.Series(prices)

    # ── 统一执行链 (报告 §1.2/§6.1, ExecutionModel 重构) ──
    # 回测/实盘共用: 冷却过滤 → 固定止损 → delta → validate+按alpha裁剪 → 成交.
    # 行为变化: validate 失败原"全部丢弃"(回测过于悲观) → 现与实盘一致按
    # alpha 边际成本公式裁剪 (B-13). 止损标的当日剔除 + stopped_out 写入由模型完成.
    # pipeline 语义 = 按给定价格立即成交 → BacktestExecutionModel
    # (限价挂单语义在 scheduler/execute 的 LiveExecutionModel).
    from quant.execution.execution_model import (
        BacktestExecutionModel, ExecutionContext,
    )
    _exec_ctx = ExecutionContext(
        engine=engine, strategy=strategy, today=date_str, prices=prices,
        cost_model=cost_model,
        ohlc=ohlc,  # B8: 一字板涨跌停阻断 (回测); None=live 无限制
    )
    _exec_res = BacktestExecutionModel().run(target_positions, _exec_ctx,
                                             risk_only=risk_only)
    orders = _exec_res.orders
    if _exec_res.stopped_out:
        # Q7-2 fix: stopped_out 必须写入 — loop.py 冷却依赖此字段 (原死代码)
        results["stopped_out"] = _exec_res.stopped_out

    results["steps"]["execution"] = {
        "orders": len(orders),
        "buys": sum(1 for o in orders if o.side == "buy"),
        "sells": sum(1 for o in orders if o.side == "sell"),
        "status": "ok",
    }
    logger.info(f"execute: {len(orders)} orders ({results['steps']['execution']['buys']} buys, {results['steps']['execution']['sells']} sells)")
    if not suppress_push:
        from quant.core.state_broker import broker
        broker.update({"status": "trades_executed", "progress": "6/7", "orders": len(orders), "trace_id": tid, "signals": target_positions})
    _m.inc("pipeline.trades", len(orders))

    # ── Step 7: Monitor (实盘 only, 回测 suppress_push=True 跳过) ──
    if not suppress_push:
        positions = engine.get_positions(strategy)
        trades = engine.get_trades(strategy, limit=50)
        total_wealth = engine.get_capital(
            strategy, prices={s: float(v) for s, v in prices.items() if v and v > 0})
        cash_balance = engine.get_cash(strategy)
        from quant.data.repos import TradeRepo
        seed = TradeRepo(db_path=db_path).get_initial_capital(strategy)
        from quant.monitor.report import generate_report, push_to_web
        report = generate_report(
            date_str, cash_balance, positions, trades,
            pnl_total=total_wealth - seed,
            initial_capital=seed,
        )
        push_to_web(report)
        cap = report["capital"]
        results["steps"]["monitor"] = {
            "cash": cap["cash"], "positions_value": cap["positions_value"],
            "total_wealth": cap["total_wealth"],
            "total_return": report["metrics"]["total_return_pct"], "status": "ok",
        }
        logger.info(f"execute monitor: wealth=Y{cap['total_wealth']:,.2f} return={report['metrics']['total_return_pct']}%")

    elapsed = time.time() - t0
    results["elapsed_sec"] = round(elapsed, 1)
    logger.info(f"execute_signals done trace_id={tid} elapsed={elapsed:.1f}s")
    return results





