"""盘中实时风控 — 每日 09:35-14:55 持续监控.

Grinold & Kahn 标准: 盘中独立风控 daemon, 与执行引擎解耦。
检查项: 总资产回撤 / 单只盈亏止盈止损 / 资本熔断。
"""
import time as _time
import os
from datetime import datetime, time
from config.constants import _require_cfg
from utils.logger import get_logger
from monitor.metrics import metrics as _m

_log = get_logger("quant.scheduler.monitor")

# ── 风控阈值 (config-driven, 硬编码为默认值) ──
MAX_DRAWDOWN_PCT = 5.0
CIRCUIT_BREAKER_PCT = 5.0
CHECK_INTERVAL_SEC = 30
QUOTE_THROTTLE_SEC = 5  # 行情 API 限频


def _run_continuous(today: str):
    """盘中持续风控循环 — 09:35 到 14:55 每 30s 检查一次."""
    from quant.scheduler.status import register, update
    from web.state_broker import broker
    from execution.calendar import is_market_open

    register("monitor", "09:35-14:55", has_multiprocess=False)

    _log.info(f"[{today}] monitor started — interval={CHECK_INTERVAL_SEC}s")

    triggered_stop = set()   # 当日已触发: {"600036:profit", "000001:loss"}
    last_quote_ts = 0.0

    while True:
        now = datetime.now()
        hhmm = time(now.hour, now.minute)

        if hhmm >= time(14, 55):
            update("monitor", status="idle (收市)")
            _log.info(f"[{today}] monitor stopped — market closing")
            break

        if hhmm < time(9, 35) or not is_market_open():
            update("monitor", status="waiting (未开盘)")
            _time.sleep(_require_cfg("quant.scheduler.poll_interval"))
            continue

        # ── 盘中检查 ──
        state = broker.get()
        total = state.get("total_asset", 0) or 0
        initial = (state.get("metrics") or {}).get("initial_capital", 5000)
        positions = state.get("positions") or []

        alerts = []

        # 1. 总资产回撤 / 熔断
        if initial > 0 and total > 0:
            dd_pct = round((1 - total / initial) * 100, 1)
            if dd_pct > MAX_DRAWDOWN_PCT:
                alerts.append(f"回撤 {dd_pct}% > {MAX_DRAWDOWN_PCT}%")
            if total < initial * (1 - CIRCUIT_BREAKER_PCT / 100):
                alerts.append(f"熔断! ¥{total:,.0f} < ¥{initial*0.95:,.0f}")
                broker.update({"circuit_breaker": True,
                               "cb_reason": f"总资产 {total:,.0f} < 95%初始资金"})

        # 2. 止盈止损扫描 — 每 QUOTE_THROTTLE_SEC 拉一次行情
        if positions:
            now_ts = _time.time()
            if now_ts - last_quote_ts >= QUOTE_THROTTLE_SEC:
                last_quote_ts = now_ts
                syms = [p["symbol"] for p in positions]
                quotes = {}
                from execution.quote import fetch_quotes
                quotes = fetch_quotes(syms) or {}
                from execution.stop_loss import RiskManager as _RM
                rm = _RM()
                signals = rm.check(positions, quotes, today)
                for sig in signals:
                    sym = sig["symbol"]
                    cur = sig["price"]
                    sell_shares = sig["shares"]
                    reason = sig["reason"]
                    pnl_pct = 0.0
                    for p2 in positions:
                        if p2["symbol"] == sym:
                            cost = p2.get("price", 0)
                            if cost > 0:
                                pnl_pct = (cur / cost - 1)
                            break

                    if "TP" in reason.upper():
                        tp_key = f"{sym}:profit"
                        if tp_key not in triggered_stop:
                            _execute_sell(today, sym, sell_shares, cur, "止盈", round(pnl_pct * 100, 1))
                            triggered_stop.add(tp_key)
                            alerts.append(f"{sym} 止盈 {pnl_pct*100:.0f}% ({reason})")
                            _m.inc("scheduler.monitor.stop_profit")
                    else:
                        sl_key = f"{sym}:loss"
                        if sl_key not in triggered_stop:
                            _execute_sell(today, sym, sell_shares, cur, "止损", round(pnl_pct * 100, 1))
                            triggered_stop.add(sl_key)
                            alerts.append(f"{sym} 止损 {pnl_pct*100:.0f}% ({reason})")
                            _m.inc("scheduler.monitor.stop_loss")

        status = "ok" if not alerts else "⚠ " + "; ".join(alerts)
        if alerts:
            _log.warning(f"[{today}] MONITOR: {status}")
            _m.inc("scheduler.monitor.alert")
        else:
            _m.inc("scheduler.monitor.ok")

        update("monitor", status=status, last_run=now.isoformat())

        _time.sleep(CHECK_INTERVAL_SEC)


def _execute_sell(today: str, symbol: str, shares: int, price: float,
                  reason: str, pnl_pct: float):
    """执行卖出订单 + 写入 trades DB."""
    from execution.engine import ExecutionEngine, Order
    engine = ExecutionEngine()
    engine.execute(
        [Order(symbol=symbol, side="sell", shares=shares,
               price=round(price, 2), cost=5.0)],
        today, strategy="quant"
    )
    _log.warning(f"[{today}] {reason}: {symbol} {shares}股 @¥{price:.2f} "
                 f"PnL={pnl_pct:+.1f}%")


def _outer_loop():
    """外层循环: 每天等待到 09:35 后启动 _run_continuous."""
    from execution.calendar import is_trading_day

    today = None
    started = False

    while True:
        now = datetime.now()
        current_day = now.strftime("%Y-%m-%d")

        if current_day != today:
            today = current_day
            started = False

        if not started and is_trading_day():
            hhmm = time(now.hour, now.minute)
            if hhmm >= time(9, 35):
                started = True
                _run_continuous(today)

        _time.sleep(_require_cfg("quant.scheduler.poll_interval"))


def _loop():
    """启动风控监控 daemon 线程."""
    import threading
    t = threading.Thread(target=_outer_loop, daemon=True, name="sch-monitor")
    t.start()
    _log.info("monitor scheduler launched (09:35-14:55)")
