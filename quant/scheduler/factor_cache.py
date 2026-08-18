"""增量因子物化调度器 — 每日 21:00."""
import time as _time, uuid as _uuid
from quant.config.constants import _require_cfg
from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish
from quant.utils.logger import get_logger, set_trace_id

_log = get_logger(__name__)


def _run(start_date: str, end_date: str):
    """物化因子缓存: 从 start_date 到 end_date (含两端)。

    Args:
        start_date: 物化起点 (YYYY-MM-DD)
        end_date:   物化终点 (YYYY-MM-DD)

    用法:
        # 手动全量/回填: 终点取数据最新日 (SQLite daily MAX(date)), 勿写死
        # _run('2019-01-01', today_str())
        _run(today, today)                 # 每日增量
    """
    tid = _uuid.uuid4().hex[:12]
    set_trace_id(tid)
    rid = _tk_start("factor_cache", end_date, grace_seconds=21600)
    if rid is None:
        _log.info(f"[{end_date}] factor_cache already running, skip duplicate trigger")
        return
    _log.info(f"[{end_date}] factor_cache: {start_date} → {end_date}")
    t0 = _time.time()
    status = "failed"
    error_msg = None
    summary = {}

    try:
        from quant.factor.store import FactorStore
        from quant.factor.compute import get_factor_names
        from quant.data.repos.universe_repo import UniverseRepo
        from quant.data.store import DataStore

        store = DataStore()
        _conn = store._connect()
        try:
            _latest = _conn.execute(
                'SELECT MAX(date) FROM daily').fetchone()[0]
            actual_end = min(end_date, _latest) if _latest else end_date
            dates = [r[0] for r in _conn.execute(
                'SELECT DISTINCT date FROM daily WHERE date >= ? AND date <= ? ORDER BY date',
                (start_date, actual_end)).fetchall()]
        finally:
            _conn.close()
        symbols = UniverseRepo().get_symbols(exclude_market='BJ')
        # v542: materialize_exclude 排除恒空结果因子 (fund_change/financial_anomaly),
        # 与 materialize_full.sh 物化池一致 — 否则每晚空算 429 因子日 + blocked 刷屏
        _excl = _require_cfg('factor.materialize_exclude')
        factors = sorted(set(get_factor_names(status_filter='backtesting', exclude=_excl))
                         | set(get_factor_names(status_filter='using', exclude=_excl)))
        store.close()

        # 数据可用性裁剪 (审计 P0-3): 源表超 SLO 的因子本轮不物化 —
        # 否则 is_materialized 永 False, 每晚全日期空算不可物化因子
        # (fund_flow_3m/short_interest 实证 2026-07-26)。源恢复后
        # 因子自动回池, 由 per-date missing 过滤补算。
        try:
            from quant.data.freshness import unavailable_factors
            _unavail = unavailable_factors(end_date)
            _drop = set(factors) & _unavail
            if _drop:
                factors = [f for f in factors if f not in _unavail]
                _log.warning(f"[{end_date}] factor_cache: pruned {len(_drop)} factors "
                             f"(source table stale): {sorted(_drop)}")
        except Exception as _fe:
            _log.warning(f"[{end_date}] factor_cache: freshness prune failed (non-fatal): {_fe}")

        fs = FactorStore()
        result = fs.materialize(dates, factors, symbols, force=False)

        elapsed = _time.time() - t0
        if result.get("skipped"):
            _log.info(f"[{end_date}] factor_cache: all dates already materialized, skipped")
        else:
            _log.info(f"[{end_date}] factor_cache done: {result['n_rows']} new rows ({elapsed:.1f}s)")
            # Trim old cache to max_days window (test-v466: 失败不再降级为 warning)
            try:
                max_days = _require_cfg("backtest.factor_cache_max_days")
                trimmed = fs.trim_to_max_days(max_days)
                _log.info(f"[{end_date}] factor_cache: trimmed {trimmed} old rows ({max_days}d window)")
            except Exception as e:
                _log.error(f"[{end_date}] factor_cache: trim failed: {e}")
                raise
            # Sync ic_mean to factor_registry
            try:
                from quant.data.repos import FactorRepo
                f_repo = FactorRepo()
                f_repo.sync_all_ic_means(f_repo.all_factor_names(), n_days=60)
                _log.info(f"[{end_date}] factor_cache: synced ic_mean to factor_registry")
            except Exception as e:
                _log.warning(f"[{end_date}] factor_cache: sync ic_mean failed: {e}")

        # test-v466 (MC-6): 失败日期上报 — 不再静默吞掉, 任务标记 failed
        _failed = result.get("failed_dates") or []
        if _failed:
            status = "failed"
            error_msg = f"{len(_failed)} dates failed: {','.join(sorted(_failed)[:20])}"
            _log.error(f"[{end_date}] factor_cache: {error_msg}")
        else:
            status = "ok"
        summary = {"rows": result.get("n_rows", 0), "elapsed": round(elapsed, 1),
                   "failed_dates": _failed}
        _log.info(f"[SCHEDULER] {end_date} | TASK=factor_cache | STATUS=OK | "
                  f"rows={result.get('n_rows', 0)} | elapsed={elapsed:.1f}s")
    except Exception as e:
        error_msg = str(e)
        _log.exception(f"[{end_date}] factor_cache crashed: {e}")
        raise
    finally:
        _tk_finish("factor_cache", end_date, status, error=error_msg, summary=summary)
