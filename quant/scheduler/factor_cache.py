"""增量因子物化调度器 — 每日 21:00."""
import time as _time, uuid as _uuid
from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish
from quant.utils.logger import get_logger, set_trace_id

_log = get_logger(__name__)


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    set_trace_id(tid)
    # grace 对齐 orchestrator._TIMEOUTS["factor_cache"]=5400 (test-v301:
    # 原默认 120s, 合法运行 2743s → cron+daemon 双触发 145s 即误 abort)
    rid = _tk_start("factor_cache", today, grace_seconds=5400)
    if rid is None:
        _log.info(f"[{today}] factor_cache already running, skip duplicate trigger")
        return
    _log.info(f"[{today}] 21:00 — incremental factor cache update")
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
        # 使用 today 和 2026-01-01 中较早的作为起始日期
        # (手动调用 _run('2025-08-01') 时需要回溯到更早日期)
        _cache_start = min('2026-01-01', today)
        # 结束日期取 daily 表最新日期（不是 today，手动回溯时 today 是过去日期）
        _latest = store._connect().execute(
            'SELECT MAX(date) FROM daily').fetchone()[0]
        _cache_end = max(today, _latest) if _latest else today
        dates = [r[0] for r in store._connect().execute(
            'SELECT DISTINCT date FROM daily WHERE date >= ? AND date <= ? ORDER BY date',
            (_cache_start, _cache_end)).fetchall()]
        symbols = UniverseRepo().get_symbols(exclude_market='BJ')
        # B-17 fix: 物化池 = backtesting ∪ using — 原只物化 backtesting 池
        # (candidate+monitoring+retired, 排除 active), 而实盘信号用 using 池
        # (active+monitoring) → 因子晋升 active 后因子值缓存缺失, 被静默丢弃
        factors = sorted(set(get_factor_names(status_filter='backtesting'))
                         | set(get_factor_names(status_filter='using')))
        store.close()

        # 数据可用性裁剪 (审计 P0-3): 源表超 SLO 的因子本轮不物化 —
        # 否则 is_materialized 永 False, 每晚全日期空算不可物化因子
        # (fund_flow_3m/short_interest 实证 2026-07-26)。源恢复后
        # 因子自动回池, 由 per-date missing 过滤补算。
        try:
            from quant.data.freshness import unavailable_factors
            _unavail = unavailable_factors(today)
            _drop = set(factors) & _unavail
            if _drop:
                factors = [f for f in factors if f not in _unavail]
                _log.warning(f"[{today}] factor_cache: pruned {len(_drop)} factors "
                             f"(source table stale): {sorted(_drop)}")
        except Exception as _fe:
            _log.warning(f"[{today}] factor_cache: freshness prune failed (non-fatal): {_fe}")

        fs = FactorStore()
        result = fs.materialize(dates, factors, symbols, force=False)

        elapsed = _time.time() - t0
        if result.get("skipped"):
            _log.info(f"[{today}] factor_cache: all dates already materialized, skipped")
        else:
            _log.info(f"[{today}] factor_cache done: {result['n_rows']} new rows ({elapsed:.1f}s)")
            # Trim old cache to max_days window
            try:
                from quant.config.constants import _require_cfg
                max_days = _require_cfg("backtest.factor_cache_max_days")
                trimmed = fs.trim_to_max_days(max_days)
                _log.info(f"[{today}] factor_cache: trimmed {trimmed} old rows ({max_days}d window)")
            except Exception as e:
                _log.warning(f"[{today}] factor_cache: trim failed (non-fatal): {e}")
            # Sync ic_mean to factor_registry
            try:
                from quant.data.repos import FactorRepo
                f_repo = FactorRepo()
                f_repo.sync_all_ic_means(f_repo.all_factor_names(), n_days=60)
                _log.info(f"[{today}] factor_cache: synced ic_mean to factor_registry")
            except Exception as e:
                _log.warning(f"[{today}] factor_cache: sync ic_mean failed: {e}")

        status = "ok"
        summary = {"rows": result.get("n_rows", 0), "elapsed": round(elapsed, 1)}
        _log.info(f"[SCHEDULER] {today} | TASK=factor_cache | STATUS=OK | "
                  f"rows={result.get('n_rows', 0)} | elapsed={elapsed:.1f}s")
    except Exception as e:
        error_msg = str(e)
        _log.exception(f"[{today}] factor_cache crashed: {e}")
        raise
    finally:
        _tk_finish("factor_cache", today, status, error=error_msg, summary=summary)
