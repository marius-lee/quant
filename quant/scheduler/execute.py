"""交易执行调度器 — 每日 09:30.
ADR 033: 买入改为限价挂单, 由 monitor 盘中被动管理成交.
"""
import time as _time, uuid as _uuid
import pandas as pd
from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish
from datetime import time, datetime
from quant.monitor.metrics import metrics as _m
from quant.utils.logger import get_logger, set_trace_id
from quant.config.constants import _require_cfg
from quant.execution.engine import ExecutionEngine, Order
from quant.execution.cost import CostModel
from quant.optimizer.rebalance import compute_trades, validate_orders
from quant.scheduler._base import _timed_loop

_log = get_logger(__name__)


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    set_trace_id(tid)
    rid = _tk_start("execute", today)
    if rid is None:
        _log.info(f"[{today}] execute already running, skip duplicate trigger")
        return
    _log.info(f"[{today}] 09:30 — executing trades")
    t0 = _time.time()

    from quant.data.trade_repo import TradeRepo
    LOT_SIZE = _require_cfg("backtest.lot_size")
    strategy = "quant"

    # ── Step 1: 读取信号 + 持仓 ──
    sig = TradeRepo().get_latest_signals()
    targets = sig["targets"] if sig and sig["date"] == today else []
    signals_date = sig["date"] if sig else "未知"
    _log.info(f"[{today}] read {len(targets)} targets from daily_signals "
              f"(generated {signals_date})")

    if not targets:
        _log.error(f"[{today}] 今日无信号，拒绝执行 (no fallback)")
        _tk_finish("execute", today, "failed", error="no signals")
        _m.inc("scheduler.execute.no_targets")
        return

    engine = ExecutionEngine()
    cost_model = CostModel()
    current_positions = engine.get_positions(strategy)
    _log.info(f"[{today}] {len(current_positions)} current positions")

    # ── Step 2: 构建 lots map ──
    current_lots = {}
    for p in current_positions:
        current_lots[p["symbol"]] = p["shares"] // LOT_SIZE

    target_lots = {}
    for tp in targets:
        sym = tp["symbol"]
        target_lots[sym] = tp["shares"] // LOT_SIZE

    # ── Step 3: 获取实时报价 ──
    from quant.execution.quote import fetch_quotes
    all_syms = list(set(list(current_lots.keys()) + list(target_lots.keys())))
    quotes = fetch_quotes(all_syms)
    if not quotes:
        _log.error(f"[{today}] 无实时报价, 拒绝执行 (no fallback)")
        _tk_finish("execute", today, "failed", error="no quotes")
        _m.inc("scheduler.execute.no_quotes")
        return

    prices = {}
    for sym, q in quotes.items():
        open_px = q.get("open", 0)
        if open_px > 0:
            prices[sym] = open_px
    # 未覆盖持仓用成本价
    for p in current_positions:
        if p["symbol"] not in prices:
            prices[p["symbol"]] = p.get("price", 0)
    for tp in targets:
        if tp["symbol"] not in prices:
            q = quotes.get(tp["symbol"], {})
            prices[tp["symbol"]] = q.get("price", 0) or q.get("open", 0)
    prices = pd.Series(prices)

    # ── Step 4: 止损检查 ──
    cash = engine.get_cash(strategy)
    position_value = 0.0
    for p in current_positions:
        px = prices.get(p["symbol"], p.get("price", 0))
        if pd.isna(px) or px <= 0:
            px = p.get("price", 0)
        position_value += p["shares"] * float(px)
    total_capital = round(cash + position_value, 2)

    sl_pct = _require_cfg("risk.stop_loss_pct")
    for p in current_positions:
        cost_basis = p.get("price", 0)
        current_px = prices.get(p["symbol"], None)
        if current_px is None or current_px <= 0 or cost_basis <= 0 or pd.isna(current_px):
            continue
        drop = (float(current_px) - cost_basis) / cost_basis
        if drop <= -sl_pct:
            shares = int(p["shares"])
            if shares > 0:
                _log.warning(f"[{today}] SL execute stop-loss: {p['symbol']} "
                             f"drop={drop:.1%}, selling {shares}")
                engine.execute(
                    [Order(symbol=p["symbol"], side="sell", shares=shares,
                           price=float(current_px), cost=0)],
                    today, strategy)
        current_positions = engine.get_positions(strategy)
        current_lots = {p2["symbol"]: p2["shares"] // LOT_SIZE
                        for p2 in current_positions}

    # ── Step 5: 计算 delta ──
    current_lots_series = pd.Series(current_lots, dtype=int)
    target_lots_series = pd.Series(target_lots, dtype=int)
    orders = compute_trades(
        target_lots_series, current_lots_series, prices, cost_model,
        capital=total_capital, cash=engine.get_cash(strategy),
    )
    if orders:
        is_valid, msg = validate_orders(orders, engine.get_cash(strategy))
        if not is_valid:
            _log.warning(f"[{today}] validate_orders failed: {msg}, skipping")
            orders = []

    # ── Step 6: 卖单立即执行, 买单挂限价 (ADR 033) ──
    sell_orders = [o for o in orders if o.side == "sell"]
    buy_orders = [o for o in orders if o.side == "buy"]

    if sell_orders:
        engine.execute(sell_orders, today, strategy)
        _log.info(f"[{today}] executed {len(sell_orders)} sell orders")

    if buy_orders:
        from quant.scheduler.order_manager import OrderManager
        om = OrderManager()
        om.cancel_all(today, strategy)  # 先清旧挂单, 防重启重复
        for o in buy_orders:
            ref_price = prices.get(o.symbol, o.price)
            om.place(today, strategy, o.symbol, o.shares, ref_price)
        _log.info(f"[{today}] placed {len(buy_orders)} limit buy orders")

    buys_done = len(buy_orders)
    sells_done = len(sell_orders)
    elapsed = _time.time() - t0
    _log.info(f"[{today}] execute done: {sells_done} sells, "
              f"{buys_done} limit buys placed — elapsed={elapsed:.1f}s")
    _log.info(f"[SCHEDULER] {today} | TASK=execute | STATUS=OK | "
              f"sells={sells_done} limit_buys={buys_done} | elapsed={elapsed:.1f}s")
    _tk_finish("execute", today, "ok", summary={"sells": sells_done, "limit_buys": buys_done, "elapsed": round(elapsed, 1)})
    _m.inc("scheduler.execute.ok")


def _loop():
    _timed_loop("execute", time(9, 30), _run, has_multiprocess=True)
