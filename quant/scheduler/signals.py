"""信号生成调度器 — 每日 08:30."""
import time as _time, uuid as _uuid
from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish
from datetime import time
from quant.monitor.metrics import metrics as _m
from quant.utils.logger import get_logger, set_trace_id
from quant.scheduler._base import _timed_loop

_log = get_logger(__name__)


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    set_trace_id(tid)
    rid = _tk_start("signals", today)
    if rid is None:
        _log.info(f"[{today}] signals already running, skip duplicate trigger")
        return
    _log.info(f"[{today}] 08:30 — generating signals")
    t0 = _time.time()

    from quant.pipeline import generate_signals
    result = generate_signals(date_str=today, skip_pull=True)
    targets = result.get("target_positions", [])

    # signals already persisted by pipeline.generate_signals() → daily_signals table
    elapsed = _time.time() - t0
    _log.info(f"[{today}] signals done: {len(targets)} targets ({elapsed:.1f}s)")
    _tk_finish("signals", today, "ok", summary={"targets": len(targets), "elapsed": round(elapsed, 1)})
    _log.info(f"[SCHEDULER] {today} | TASK=signals | STATUS=OK | targets={len(targets)} | elapsed={elapsed:.1f}s")
    _m.inc("scheduler.signals.ok")


def _loop():
    _timed_loop("signals", time(8, 30), _run, has_multiprocess=True)
