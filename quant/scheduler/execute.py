"""交易执行调度器 — 每日 09:30."""
import time as _time, uuid as _uuid
from datetime import time, datetime
from monitor.metrics import metrics as _m
from utils.logger import get_logger
from quant.scheduler._base import _timed_loop

_log = get_logger("quant.scheduler.execute")


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    _log.info(f"[{today}] 09:30 — executing trades")
    t0 = _time.time()

    from pipeline import execute_signals, generate_signals
    # 不重算 — 直接读 08:30 产出 (Grinold & Kahn: 盘前 batch → 盘中执行)
    from web.state_broker import broker
    state = broker.get()
    targets = state.get("signals", [])
    signals_time = state.get("when", {}).get("signals_generated", "未知")
    _log.info(f"[{today}] read {len(targets)} targets from 08:30 signals (generated {signals_time})")

    if not targets:
        _log.error(f"[{today}] 08:30 未产出信号，拒绝执行 (no fallback)")
        _m.inc("scheduler.execute.no_targets")
        return

    result = execute_signals(targets, date_str=today)
    elapsed = _time.time() - t0

    orders = result.get("steps", {}).get("execution", {})
    _log.info(f"[{today}] execute done: {orders.get('orders', 0)} orders ({orders.get('buys', 0)} buys, {orders.get('sells', 0)} sells)")
    _log.info(f"[SCHEDULER] {today} | TASK=execute | STATUS=OK | elapsed={elapsed:.1f}s")
    _m.inc("scheduler.execute.ok")


def _loop():
    _timed_loop("execute", time(9, 30), _run, has_multiprocess=True)
