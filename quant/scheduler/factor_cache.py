"""增量因子物化调度器 — 每日 21:00."""
import time as _time, uuid as _uuid
from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish
from quant.utils.logger import get_logger, set_trace_id

_log = get_logger(__name__)


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    set_trace_id(tid)
    _tk_start("factor_cache", today)
    _log.info(f"[{today}] 21:00 — incremental factor cache update")
    t0 = _time.time()

    from quant.factor.store import FactorStore
    from quant.factor.compute import get_factor_names
    from quant.data.repos.universe_repo import UniverseRepo
    from quant.data.store import DataStore

    store = DataStore()
    dates = [r[0] for r in store._connect().execute(
        'SELECT DISTINCT date FROM daily WHERE date >= ? AND date <= ? ORDER BY date',
        ('2026-01-01', today)).fetchall()]
    symbols = UniverseRepo().get_symbols(exclude_market='BJ')
    factors = get_factor_names(status_filter='backtesting')
    store.close()

    fs = FactorStore()
    result = fs.materialize(dates, factors, symbols, force=False)

    elapsed = _time.time() - t0
    if result.get("skipped"):
        _log.info(f"[{today}] factor_cache: all dates already materialized, skipped")
    else:
        _log.info(f"[{today}] factor_cache done: {result['n_rows']} new rows ({elapsed:.1f}s)")

    _tk_finish("factor_cache", today, "ok",
               summary={"rows": result.get("n_rows", 0), "elapsed": round(elapsed, 1)})
    _log.info(f"[SCHEDULER] {today} | TASK=factor_cache | STATUS=OK | "
              f"rows={result.get('n_rows', 0)} | elapsed={elapsed:.1f}s")
