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
        n = store.update_daily()
        store.close()

        elapsed = _time.time() - t0
        _log.info(f"[{today}] daily_data done: {n} new rows ({elapsed:.1f}s)")
    # 盘后换手率回填 — 安全网: tushare 已配置且为首选源(turnover_rate✅),
    # 正常情况 backfill_turnover 查询到 0 行待补 → 即时返回。
    # 仅在 tushare 某批失败、回退源(无 turnover)接盘写入 turnover=0 时,
    # 才实际触发 baostock 回填 (baostock 0.3s/只, 来源: 2026-07-21 实测)。
    # 来源: 2026-07-21 全链路逻辑分析 (问题3: 冗余安全网)
        import traceback
        try:
            s = DataStore()
            tn = s.backfill_turnover(today)
            s.close()
            if tn > 0:
                _log.info(f"[{today}] turnover backfill: {tn} stocks updated (safety net triggered)")
            else:
                _log.debug(f"[{today}] turnover backfill: 0 stocks needed (tushare covered all)")
        except Exception:
            _log.warning(f"[{today}] turnover backfill failed: {traceback.format_exc()}")

        # ── fund_flow / margin 增量同步 (审计 P0-2, 2026-07-26) ──
        # 两源此前无自动同步 (孤儿脚本), fund_flow 停滞 5 个月、margin 17 天
        # 无人知 → 因子在池空算。接入晚间链, 失败不阻塞主链 (因子端按缺失处理)。
        try:
            from quant.data.fund_flow import sync_all as _ff_sync
            n_ff = _ff_sync()  # top-500 市值, 同历史口径
            _log.info(f"[{today}] fund_flow sync: {n_ff} rows")
        except Exception:
            _log.warning(f"[{today}] fund_flow sync failed: {traceback.format_exc()}")
        try:
            from quant.data import margin as _margin
            from datetime import datetime as _dt, timedelta as _td
            _mg_start = (_dt.strptime(today, "%Y-%m-%d") - _td(days=30)).strftime("%Y-%m-%d")
            n_mg = _margin.sync_range(_mg_start, today)
            _log.info(f"[{today}] margin sync: {n_mg} rows")
        except Exception:
            _log.warning(f"[{today}] margin sync failed: {traceback.format_exc()}")

        # ── daily_valuation 估值增量同步 (审计 P0-4) ──
        # 严格 PIT 改造后估值因子只认本表; 曾停滞 2026-07-03 → 缺口期因子 NaN。
        # 增量 14 天窗口 (已同步日期自动跳过), 失败不阻塞主链。
        try:
            from quant.data.jq_valuation import sync_range as _jv_sync
            from datetime import datetime as _dt2, timedelta as _td2
            _jv_start = (_dt2.strptime(today, "%Y-%m-%d") - _td2(days=14)).strftime("%Y-%m-%d")
            _jv_sync(start=_jv_start, end=today)
            _log.info(f"[{today}] daily_valuation sync done ({_jv_start}..{today})")
        except Exception:
            _log.warning(f"[{today}] daily_valuation sync failed: {traceback.format_exc()}")

        # ── 数据新鲜度 watchdog (审计 P0-2) ──
        # 每表 MAX(date) 超 SLO → ERROR + CRITICAL 告警 (telegram/wechat/本地日志)
        try:
            from quant.data.freshness import check_freshness
            fresh = check_freshness(today)
            stale = [r for r in fresh if r["stale"]]
            for r in stale:
                _log.error(f"[{today}] DATA STALE: {r['table']} max_date={r['max_date']} "
                           f"lag={r['lag_days']}d > SLO {r['slo']}d")
            if stale:
                from quant.monitor.notify import send_alert
                send_alert({
                    "level": "CRITICAL",
                    "title": f"数据停滞: {', '.join(r['table'] for r in stale)}",
                    "body": "; ".join(
                        f"{r['table']} max={r['max_date']} lag={r['lag_days']}d (SLO {r['slo']}d)"
                        for r in stale),
                })
        except Exception:
            _log.warning(f"[{today}] freshness check failed: {traceback.format_exc()}")

        status = "ok"
        summary = {"rows": n, "elapsed": round(elapsed, 1)}
        _log.info(f"[SCHEDULER] {today} | TASK=daily_data | STATUS=OK | "
                  f"rows={n} | elapsed={elapsed:.1f}s")
    except Exception as e:
        error_msg = str(e)
        _log.exception(f"[{today}] daily_data crashed: {e}")
        raise
    finally:
        _tk_finish("daily_data", today, status, error=error_msg, summary=summary)
