"""盘中实时风控 — 每日 09:35-14:55 持续监控.

Grinold & Kahn 标准: 盘中独立风控 daemon, 与执行引擎解耦。
检查项: 总资产回撤 / 单只持仓亏损 / 资本熔断。
"""
import time as _time
from datetime import datetime, time
from utils.logger import get_logger
from monitor.metrics import metrics as _m

_log = get_logger("quant.scheduler.monitor")

# ── 风控阈值 ──
MAX_DRAWDOWN_PCT = 5.0
MAX_SINGLE_LOSS_PCT = 8.0
CIRCUIT_BREAKER_PCT = 5.0
CHECK_INTERVAL_SEC = 30


def _run_continuous(today: str):
    """盘中持续风控循环 — 09:35 到 14:55 每 30s 检查一次."""
    from quant.scheduler.status import register, update
    from web.state_broker import broker

    register("monitor", "09:35-14:55", has_multiprocess=False)

    _log.info(f"[{today}] monitor started — interval={CHECK_INTERVAL_SEC}s")

    while True:
        now = datetime.now()
        hhmm = time(now.hour, now.minute)

        if hhmm >= time(14, 55):
            update("monitor", status="idle (收市)")
            _log.info(f"[{today}] monitor stopped — market closing")
            break

        if hhmm < time(9, 35):
            update("monitor", status="waiting (未开盘)")
            _time.sleep(30)
            continue

        try:
            state = broker.get()
            total = state.get("total_asset", 0) or 0
            initial = (state.get("metrics") or {}).get("initial_capital", 5000)
            positions = state.get("positions") or []

            alerts = []

            if initial > 0 and total > 0:
                drawdown_pct = round((1 - total / initial) * 100, 1)
                if drawdown_pct > MAX_DRAWDOWN_PCT:
                    alerts.append(f"日内回撤 {drawdown_pct}% > {MAX_DRAWDOWN_PCT}%")
                if total < initial * (1 - CIRCUIT_BREAKER_PCT / 100):
                    alerts.append(f"熔断! ¥{total:,.0f} < ¥{initial*0.95:,.0f}")

            for p in positions:
                pnl = p.get("pnl_pct")
                if pnl is not None and abs(pnl) > MAX_SINGLE_LOSS_PCT:
                    alerts.append(f"{p['symbol']} {pnl:+.1f}% > {MAX_SINGLE_LOSS_PCT}%")

            status = "ok" if not alerts else "⚠ " + "; ".join(alerts)
            if alerts:
                _log.warning(f"[{today}] MONITOR ALERT: {status}")
                _m.inc("scheduler.monitor.alert")
            else:
                _m.inc("scheduler.monitor.ok")

            update("monitor", status=status, last_run=now.isoformat())

        except Exception as e:
            update("monitor", status="error", last_error=str(e))
            _log.warning(f"[{today}] monitor error: {e}")
            _m.inc("scheduler.monitor.error")

        _time.sleep(CHECK_INTERVAL_SEC)


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
                try:
                    _run_continuous(today)
                except Exception as e:
                    _log.error(f"[{today}] monitor outer exception: {e}")

        _time.sleep(30)


def _loop():
    """启动风控监控 daemon 线程."""
    import threading
    t = threading.Thread(target=_outer_loop, daemon=True, name="sch-monitor")
    t.start()
    _log.info("monitor scheduler launched (09:35-14:55)")
