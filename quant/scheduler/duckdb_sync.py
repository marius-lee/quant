"""DuckDB 增量同步调度器 — 每日 daily_data 后运行.

v562f: 新增阶段, 解决 "factor_cache: DuckDB daily 落后" 拦截物化.
daily_data 完成后立即同步 SQLite→DuckDB (增量 upsert + 预聚合), 耗时 ~30-60s,
保证 factor_cache 读取最新 DuckDB.
"""
import time as _time
import uuid as _uuid
from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish
from quant.utils.logger import get_logger, set_trace_id

_log = get_logger(__name__)


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    set_trace_id(tid)
    from quant.scheduler.manifest import spec
    rid = _tk_start("duckdb_sync", today, grace_seconds=spec("duckdb_sync").grace_s)
    if rid is None:
        _log.info(f"[{today}] duckdb_sync already running, skip duplicate trigger")
        return
    _log.info(f"[{today}] duckdb_sync: starting SQLite→DuckDB incremental sync")
    t0 = _time.time()
    status = "failed"
    error_msg = None

    try:
        from quant.data.duckdb_store import DuckDBManager
        dm = DuckDBManager()
        dm._sync_incremental()
        elapsed = _time.time() - t0
        _log.info(f"[{today}] duckdb_sync done: {elapsed:.1f}s")
        status = "ok"
    except Exception as e:
        error_msg = str(e)
        _log.exception(f"[{today}] duckdb_sync crashed: {e}")
        raise
    finally:
        _tk_finish("duckdb_sync", today, status, error=error_msg)


if __name__ == "__main__":
    from datetime import datetime
    _run(datetime.now().strftime("%Y-%m-%d"))
