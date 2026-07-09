"""归因分析调度器 — 每日 15:30."""
import time as _time, uuid as _uuid
from datetime import time
from monitor.metrics import metrics as _m
from utils.logger import get_logger
from quant.scheduler._base import _timed_loop

_log = get_logger("quant.scheduler.attribution")


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    _log.info(f"[{today}] 15:30 — attribution")
    t0 = _time.time()

    from monitor.attribution import brinson_attribution
    from execution.engine import ExecutionEngine
    engine = ExecutionEngine()
    positions = engine.get_positions(strategy="quant")

    if positions:
        attr = brinson_attribution(positions, date=today, benchmark="000300")
        _log.info(f"[{today}] attribution done: {attr.get('summary', 'N/A')}")
    else:
        _log.info(f"[{today}] no positions, skip attribution")

    elapsed = _time.time() - t0
    _log.info(f"[SCHEDULER] {today} | TASK=attribution | STATUS=OK | elapsed={elapsed:.1f}s")
    _m.inc("scheduler.attribution.ok")


def _loop():
    _timed_loop("attribution", time(15, 30), _run, skip_deadline=time(15, 45))
