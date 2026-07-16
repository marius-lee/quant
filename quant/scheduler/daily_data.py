"""每日数据拉取调度器 — 每日 19:00."""
import time as _time, uuid as _uuid
from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish
from quant.utils.logger import get_logger, set_trace_id

_log = get_logger(__name__)


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    set_trace_id(tid)
    _tk_start("daily_data", today)
    _log.info(f"[{today}] 19:00 — pulling daily data")
    t0 = _time.time()

    from quant.data.store import DataStore
    store = DataStore()
    n = store.update_daily()
    store.close()

    elapsed = _time.time() - t0
    _log.info(f"[{today}] daily_data done: {n} new rows ({elapsed:.1f}s)")
    _tk_finish("daily_data", today, "ok",
               summary={"rows": n, "elapsed": round(elapsed, 1)})
    _log.info(f"[SCHEDULER] {today} | TASK=daily_data | STATUS=OK | "
              f"rows={n} | elapsed={elapsed:.1f}s")
