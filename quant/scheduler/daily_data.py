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
    """每日 19:00 运行 - 晚间链第一阶段.

    V586 修复: 任何退出路径均保证 _tk_finish() 被调用, 避免
    task_runs 永卡 status='running'. 当任务在 audit/修复/DuckDB sync 阶段
    遇到异常且未被捕获时, PID 会自然死亡但 Dagster 环境无
    orchestrator 检测, 导致 UI 永远显示"运行中".

    修复手段: 在函数开头捕获 final_status/finished 标志,
    用整函数的 try/finally 保证 finish 被调用, 涵盖:
    - 正常完成路径
    - 主流程 Exception (已在内层 except 中 finish 并 re-raise)
    - BaseException (KeyboardInterrupt, SystemExit 等)
    - 任何未预期的退出
    """
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
    # V586: 标志变量 — 由 try/finally 兜底, 确保任何路径退出都 finish
    final_status = "failed"
    final_error = None
    final_summary = None
    finished = False  # 防重入标志 (嵌套 try 中已手动 finish 时跳过 finally)

    # V586: 完整函数包裹 try/finally, 保证 _tk_finish 被必然调用
    # 无此修复: 任务在 audit 阶段后 (如 22:27:40 之后的 DuckDB sync、
    # margin sync、consecutive_failures 检查) 遇到异常提前退出,
    # _tk_finish 永不调用 → task_runs 永卡 status='running'.
    # Dagster 模式下 orchestrator 进程未运行, 没人检测 PID 死亡 →
    # UI 恒显示"运行中" 直至人工干预或重启.
    try:
        # ── 主流程: daily 行情 (失败即 failed, 阻断链) ──
        try:
            from quant.data.store import DataStore
            store = DataStore()
            n = store.update_daily(target_date=today)
            store.close()
            elapsed = _time.time() - t0
            _log.info(f"[{today}] daily_data done: {n} new rows ({elapsed:.1f}s)")
        except Exception as e:
            final_error = f"主流程异常: {e}"
            _log.exception(f"[{today}] daily_data 主流程 crashed: {e}")
            _tk_finish("daily_data", today, "failed", error=final_error)
            finished = True
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

        # v560-fix (2026-08-19): DuckDB 同步必须放在 sync_main + audit 之后 —
        # 原位置在 rollback 表循环之前, 导致 daily_valuation 08-19 数据 (21:06
        # 由 em_valuation 写入 SQLite) 永不进 DuckDB: _sync_incremental 只追
        # date > DuckDB.MAX(date) (08-18), 之后无重同步 → 因子物化读 DuckDB 缺
        # 当日估值因子。实测: DuckDB daily_valuation 08-19 = 0 行 vs SQLite 5210.
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
                                ["symbol", "date"]
                            )
                        _log.info(f"[{today}] DuckDB {table} 全量重同步: {n} rows")
                    except Exception as _se:
                        _log.error(f"[{today}] DuckDB 全量重同步失败: {_tb.format_exc()}")
            # 4) 预聚合表刷新已删除 (v498: 零消费方, DROP 8 表 — 见 duckdb_sync_all.sh)
        except Exception:
            _log.warning(f"[{today}] DuckDB sync failed: {_tb.format_exc()}")

        elapsed = _time.time() - t0
        # v479: partial — 主流程 ok 但审计有残留失败 → 次日早间补拉链修复
        final_status = "ok" if not still else "partial"
        _log.info(f"[{today}] daily_data {final_status}: {elapsed:.1f}s, "
                  f"sync={sync_results}, repaired={repaired}, still_failed={still}")
        final_summary = {"elapsed": round(elapsed, 1), "synced": {k: str(v) for k, v in sync_results.items()},
                         "repaired": repaired, "still_failed": still}
        # 正常路径完成, 在 finally 中 finish (若未在 except 中已 finish)
    except BaseException as _be:
        # V586: 兜底 — 任何未捕获异常 (含 SystemExit/KeyboardInterrupt)
        # 都确保 _tk_finish 被调用, 避免 task_runs 永卡 running
        if not finished:
            final_error = f"{type(_be).__name__}: {_be}"
            _log.exception(f"[{today}] daily_data 未捕获异常 (finally 兜底): {_be}")
            _tk_finish("daily_data", today, "failed", error=final_error)
            finished = True
        raise
    finally:
        # V586: 正常退出时调用 finish (异常路径已在 except 中 finish, 跳过)
        if not finished:
            try:
                _tk_finish("daily_data", today, final_status,
                           error=final_error,
                           summary=final_summary)
            except Exception as _fe:
                _log.error(f"[{today}] daily_data finish 兜底失败: {_fe}")