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

    # v449: Sync SQLite -> DuckDB (DuckDB 仅用于读查询分流, 写入仍走 SQLite)
    # v448: DuckDB 后台同步线程从未启动, 导致 DuckDB daily 表落后 SQLite 数月
    #   - materialize() 走 DuckDB 优先, 获取不到新增日期数据 -> cache 短 fewer
    #   - 手动补数: 同步缺失日期 2025-06-01..2026-08-10 (1575017 行)
    # v453: 增加历史回填 + 同步验证 (backfill -> incremental -> verify)
    # v456: 增加预聚合表刷新 (daily_ma, daily_ret, daily_std 等)
    try:
        from quant.data.duckdb_store import get_duckdb_proxy
        proxy = get_duckdb_proxy()
        # 对 daily 和 daily_valuation 两张带日期的表执行 3 步同步
        for table in ("daily", "daily_valuation"):
            # 1) 历史回填: 仅补最近 504 天内的缺失日期
            proxy._duckdb._sync_backfill_missing_dates(table=table, max_backfill_days=504)
            # 2) 增量同步: 追赶新增/更新行
            proxy._duckdb._sync_incremental()
            # 3) 验证一致性
            res = proxy._duckdb.verify_sync(table=table)
            if res["match"]:
                _log.info(f"[{today}] DuckDB sync OK: {table} fully synced ({res['duckdb_dates']} dates, {res['duckdb_rows']} rows)")
            else:
                _log.warning(f"[{today}] DuckDB sync mismatch: {table} sqlite={res['sqlite_rows']} duckdb={res['duckdb_rows']} rows")
        # 4) 刷新预聚合表 (最近 60 个交易日)
        proxy._duckdb.refresh_preaggregates()
    except Exception:
        _log.warning(f"[{today}] DuckDB sync failed: {traceback.format_exc()}")

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
        n_ff = _ff_sync(days=100)  # v430: 增量窗口, 覆盖 3m 因子; 全量回填走 backfill
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

    # ── limit_up_pool (涨停池) ──
    # test-v404: 此前未纳入晚间链, 依赖手动 daily_sync.py, 7/9-7/16 永久缺失
    try:
        from quant.data.limit_up import sync_date as _lu_sync
        _lu_n = _lu_sync(today)
        _log.info(f"[{today}] limit_up sync: {_lu_n} rows")
    except Exception:
        _log.warning(f"[{today}] limit_up sync failed: {traceback.format_exc()}")

    # ── limit_down_pool (跌停池) ──
    # v430: 此前空表 — net_limit_ratio 读 df_down 恒空, 情绪因子失真.
    # 同 akshare 东财源 (stock_zt_pool_dtgc_em), 失败不阻断 (fallback 由 retry 层)
    try:
        from quant.data.limit_up import sync_down_date as _ld_sync
        _ld_n = _ld_sync(today)
        _log.info(f"[{today}] limit_down sync: {_ld_n} rows")
    except Exception:
        _log.warning(f"[{today}] limit_down sync failed: {traceback.format_exc()}")

    # ── lhb_detail (龙虎榜) ──
    # test-v404: 此前未纳入晚间链, lhb_reversal_5d 因子因 post_5d 缺失而静默失败
    try:
        from quant.data.lhb import sync_date as _lhb_sync_date
        _lhb_n = _lhb_sync_date(today)
        _log.info(f"[{today}] lhb sync: {_lhb_n} rows")
    except Exception:
        _log.warning(f"[{today}] lhb sync failed: {traceback.format_exc()}")

    # ── news_sentiment (个股新闻情绪) ──
    # v430 判定: 不接入 — akshare stock_news_em 在本环境 (pyarrow 14) 必然崩溃
    # (Invalid regular expression: \\u), 接入只会每晚打 warning; 待数据源修复再启

    status = "ok"
    summary = {"rows": n, "elapsed": round(elapsed, 1)}
    _log.info(f"[SCHEDULER] {today} | TASK=daily_data | STATUS=OK | "
              f"rows={n} | elapsed={elapsed:.1f}s")
    _tk_finish("daily_data", today, status, summary=summary)
