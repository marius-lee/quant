"""每日数据拉取调度器 — 每日 19:00."""
import time as _time, uuid as _uuid
from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish
from quant.utils.logger import get_logger, set_trace_id

_log = get_logger(__name__)


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    set_trace_id(tid)
    # grace 对齐 orchestrator._TIMEOUTS["daily_data"]=7200 (test-v301:
    # 原 1800s, 合法运行曾达 6251s → 19:30 第二触发误 abort 活任务,
    # 僵尸持锁 → 下一次 daily_data "database is locked")
    rid = _tk_start("daily_data", today, grace_seconds=7200)
    if rid is None:
        _log.info(f"[{today}] daily_data already running, skip duplicate trigger")
        return
    _log.info(f"[{today}] 19:00 — pulling daily data")
    t0 = _time.time()
    status = "failed"
    error_msg = None
    summary = {}

    try:
        from quant.data.store import DataStore
        store = DataStore()
        n = store.update_daily(target_date=today)
        store.close()
        elapsed = _time.time() - t0
        _log.info(f"[{today}] daily_data done: {n} new rows ({elapsed:.1f}s)")
    except Exception as e:
        error_msg = str(e)
        _log.exception(f"[{today}] daily_data crashed: {e}")
        _tk_finish("daily_data", today, "failed", error=error_msg)
        raise

    # v382: 后续步骤各自独立 try/except, 单步失败不阻断整体
    import traceback

    # ── 换手率回填 ──
    try:
        s = DataStore()
        tn = s.backfill_turnover(date=today)
        s.close()
        if tn > 0:
            _log.info(f"[{today}] turnover backfill: {tn} stocks updated")
    except Exception:
        _log.warning(f"[{today}] turnover backfill failed: {traceback.format_exc()}")

    # ── fund_flow ──
    try:
        from quant.data.fund_flow import sync_all as _ff_sync
        n_ff = _ff_sync()
        _log.info(f"[{today}] fund_flow sync: {n_ff} rows")
    except Exception:
        _log.warning(f"[{today}] fund_flow sync failed: {traceback.format_exc()}")

    # ── margin ──
    try:
        from quant.data import margin as _margin
        from datetime import datetime as _dt, timedelta as _td
        _mg_start = (_dt.strptime(today, "%Y-%m-%d") - _td(days=30)).strftime("%Y-%m-%d")
        n_mg = _margin.sync_range(_mg_start, today)
        _log.info(f"[{today}] margin sync: {n_mg} rows")
    except Exception:
        _log.warning(f"[{today}] margin sync failed: {traceback.format_exc()}")

    # ── daily_valuation ──
    try:
        from quant.data.em_valuation import sync_range as _em_sync
        from datetime import datetime as _dt2, timedelta as _td2
        _em_start = (_dt2.strptime(today, "%Y-%m-%d") - _td2(days=14)).strftime("%Y-%m-%d")
        _em_sync(start=_em_start, end=today)
        _log.info(f"[{today}] daily_valuation sync done ({_em_start}..{today})")
    except Exception:
        _log.warning(f"[{today}] daily_valuation sync failed: {traceback.format_exc()}")

    # ── 数据新鲜度 ──
    try:
        from quant.data.freshness import check_freshness
        fresh = check_freshness(today)
        stale = [r for r in fresh if r["stale"]]
        for r in stale:
            _log.error(f"[{today}] DATA STALE: {r['table']} max_date={r['max_date']} "
                       f"lag={r['lag_days']}d > SLO {r['slo']}d")
    except Exception:
        _log.warning(f"[{today}] freshness check failed: {traceback.format_exc()}")

    status = "ok"
    summary = {"rows": n, "elapsed": round(elapsed, 1)}
    _log.info(f"[SCHEDULER] {today} | TASK=daily_data | STATUS=OK | "
              f"rows={n} | elapsed={elapsed:.1f}s")
    _tk_finish("daily_data", today, status, summary=summary)
