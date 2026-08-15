"""每日数据拉取调度器 — 每日 19:00 (晚间链 stage 1).

v479 改造: 子同步从硬编码 try/except 泳道改为按 table_registry 的 rollback
循环 (自带 T+1 迟发补偿窗口); 末尾跑完整性审计 (data_health.audit_all) +
失败表自动补拉 (repair_and_reaudit); 仍有失败 → 任务状态 partial
(次日 08:00 早间补拉链 daily_repair 再试), 连续失败 ≥3 天 → ERROR 告警.

状态语义:
  ok      — 主流程 + 全部子同步成功且审计全绿
  partial — 主流程成功, 但存在审计失败表 (已尝试补拉仍败) → 早间链修复
  failed  — 主流程 (update_daily) 异常 → 晚间链崩溃语义 (下游 stage 跳过)
"""
import time as _time, uuid as _uuid, traceback as _tb
from datetime import datetime as _dt, timedelta as _td
from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish
from quant.utils.logger import get_logger, set_trace_id

_log = get_logger(__name__)


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    set_trace_id(tid)
    # grace 对齐 manifest._EVENING_STAGE_GRACE["daily_data"]=21600 (v474:
    # v428 后 _check_timeouts fallback 300s 每晚误杀 5-12min 的回归;
    # 实测合法运行最长 4.4h; 原 7200 在 1.7h 后 dedup 失效也有双跑风险)
    rid = _tk_start("daily_data", today, grace_seconds=21600)
    if rid is None:
        _log.info(f"[{today}] daily_data already running, skip duplicate trigger")
        return
    _log.info(f"[{today}] 19:00 — pulling daily data")
    t0 = _time.time()
    status = "failed"
    error_msg = None

    # ── 主流程: daily 行情 (失败即 failed, 阻断链) ──
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

    # ── 换手率回填 (v491: 必须在 DuckDB 同步之前 — 否则新行 turnover=0 先进
    # DuckDB, 之后 SQLite 补 turnover 但 DuckDB 增量只追新日期, 永不同步回补
    # 的历史行 → 因子物化读 DuckDB 恒读到 0) ──
    try:
        s = DataStore()
        tn = s.backfill_turnover(date=today)
        s.close()
        if tn > 0:
            _log.info(f"[{today}] turnover backfill: {tn} stocks updated")
    except Exception:
        _log.warning(f"[{today}] turnover backfill failed: {_tb.format_exc()}")

    # v382: 后续步骤各自独立 try/except, 单步失败不阻断整体 (v479 改由
    # 审计/补拉闭环兜底, 任务状态 partial 而非 ok)

    # v449: Sync SQLite -> DuckDB (DuckDB 仅用于读查询分流, 写入仍走 SQLite)
    # v448: DuckDB 后台同步线程从未启动, 导致 DuckDB daily 表落后 SQLite 数月
    #   - materialize() 走 DuckDB 优先, 获取不到新增日期数据 -> cache 短 fewer
    #   - 手动补数: 同步缺失日期 2025-06-01..2026-08-10 (1575017 行)
    # v453: 增加历史回填 + 同步验证 (backfill -> incremental -> verify)
    # v498: 预聚合表刷新已删 (零消费方, DROP 8 表 — 见 scripts/duckdb_sync_all.sh)
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
                # v491: verify_sync 值级校验 (turnover/amount 非零行数) 不一致
                # → 历史行 UPDATE (回填) 未进 DuckDB, 物化会读旧值 → 全量重同步
                _log.warning(f"[{today}] DuckDB sync mismatch: {table} sqlite={res['sqlite_rows']} duckdb={res['duckdb_rows']} rows — 触发全量重同步")
                try:
                    # v492: daily_valuation 也走通用全量 UPSERT — v491 的
                    # _sync_table 是增量 (date > MAX), 历史行 UPDATE 依然
                    # 不进 DuckDB (半成品, 本版补全)
                    if table == "daily":
                        n = proxy._duckdb.sync_daily_full()
                    else:
                        n = proxy._duckdb.sync_table_full(
                            "daily_valuation",
                            ["symbol", "date", "pe_ttm", "pb", "ps_ttm", "pcf_ttm", "market_cap", "turnover_rate", "source"],
                            ["symbol", "date"])
                    _log.info(f"[{today}] DuckDB {table} 全量重同步: {n} rows")
                except Exception as _se:
                    _log.error(f"[{today}] DuckDB 全量重同步失败: {_tb.format_exc()}")
        # 4) 预聚合表刷新已删除 (v498: 零消费方, DROP 8 表 — 见 duckdb_sync_all.sh)
    except Exception:
        _log.warning(f"[{today}] DuckDB sync failed: {_tb.format_exc()}")

    # ── v479: 子同步按注册表循环 (rollback 模式, 自带 T+1 迟发补偿窗口) ──
    from quant.data.table_registry import rollback_specs
    sync_results: dict[str, object] = {}
    for spec in rollback_specs():
        if spec.sync_main is None:
            continue
        start_ = (_dt.strptime(today, "%Y-%m-%d") - _td(days=spec.window_days)).strftime("%Y-%m-%d")
        try:
            n = spec.sync_main(start_, today)
            sync_results[spec.table] = n
            _log.info(f"[{today}] sync {spec.table}: +{n} rows ({start_}..{today})")
        except Exception as e:
            sync_results[spec.table] = f"FAIL: {str(e)[:120]}"
            _log.warning(f"[{today}] sync {spec.table} failed: {str(e)[:160]}")

    # ── v479: 完整性审计 + 自动补拉修复 (sync → audit → repair → re-audit) ──
    from quant.data.data_health import audit_all, repair_and_reaudit, consecutive_failures
    audit = audit_all(today)
    failed = sorted(t for t, rules in audit.items()
                    if any(v == "fail" for v in rules.values()))
    repaired: list[str] = []
    still: list[str] = []
    if failed:
        _log.warning(f"[{today}] audit FAIL tables: {failed}")
        repaired, still = repair_and_reaudit(today, failed)
        for t in repaired:
            _log.info(f"[{today}] audit repaired: {t}")
        for t in still:
            _log.error(f"[{today}] audit STILL FAILED after repair: {t}")

    # 连续失败告警升级 (≥3 天同一表 fail → ERROR, 需人工排查数据源)
    for t, rules in audit.items():
        if any(v == "fail" for v in rules.values()) and consecutive_failures(t, days=5) >= 3:
            _log.error(f"[{today}] DATA HEALTH: {t} 连续失败 ≥3 天 — 数据源需人工排查/换源")

    elapsed = _time.time() - t0
    # v479: partial — 主流程 ok 但审计有残留失败 → 次日早间补拉链修复
    status = "ok" if not still else "partial"
    _log.info(f"[{today}] daily_data {status}: {elapsed:.1f}s, "
              f"sync={sync_results}, repaired={repaired}, still_failed={still}")
    _tk_finish("daily_data", today, status,
               summary={"elapsed": round(elapsed, 1), "synced": {k: str(v) for k, v in sync_results.items()},
                        "repaired": repaired, "still_failed": still})