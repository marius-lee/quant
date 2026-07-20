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
    # 盘后换手率回填 — 安全网: tushare 已配置且为首选源(turnover_rate✅),
    # 正常情况 backfill_turnover_quotes 查询到 0 行待补 → 即时返回。
    # 仅在 tushare 某批失败、回退源(如 tickflow)接盘写入 turnover=0 时,
    # 才实际触发 tickflow quotes 回填 (约 60s/50只)。
    # 来源: 2026-07-21 全链路逻辑分析 (问题3: 冗余安全网)
    import traceback
    try:
        s = DataStore()
        tn = s.backfill_turnover_quotes(today)
        s.close()
        if tn > 0:
            _log.info(f"[{today}] turnover backfill: {tn} stocks updated (safety net triggered)")
        else:
            _log.debug(f"[{today}] turnover backfill: 0 stocks needed (tushare covered all)")
    except Exception:
        _log.warning(f"[{today}] turnover backfill failed: {traceback.format_exc()}")

    _tk_finish("daily_data", today, "ok",
               summary={"rows": n, "elapsed": round(elapsed, 1)})
    _log.info(f"[SCHEDULER] {today} | TASK=daily_data | STATUS=OK | "
              f"rows={n} | elapsed={elapsed:.1f}s")
