"""周六数据维护 — weekly_full 表全量刷新 + 完整性审计 (v479).

weekly.eval 开头调用 (周六 06:00): dividend/stocks 等事件型低频表
全量幂等重拉, 随后运行完整审计, 与 daily_data 共享 data_audit 表.

失败语义: 单表异常记录并继续 (事件型表), 最终结果随 weekly_eval 汇总;
stocks 依赖外部 (tushare/baostock), 失败由 weekly_eval 的 failed 状态 + 
次日早间链 (repair._pending_tables weekly_full 7 天兜底) 双保险.
"""
import time as _time

from quant.utils.logger import get_logger

_log = get_logger("scheduler.maintenance")


def run_weekly_full(today: str) -> dict:
    """全量刷新 weekly_full 表 + 全表审计 + 失败表补拉. 返回 summary."""
    from quant.data.table_registry import weekly_full_specs
    from quant.data.data_health import audit_all, repair_and_reaudit

    results: dict[str, object] = {}
    t0 = _time.time()
    for spec in weekly_full_specs():
        if spec.sync_main is None:
            continue
        try:
            n = spec.sync_main()
            results[spec.table] = n
            _log.info(f"[{today}] maintenance {spec.table}: +{n} rows ({_time.time()-t0:.0f}s)")
        except Exception as e:
            results[spec.table] = f"FAIL: {str(e)[:120]}"
            _log.error(f"[{today}] maintenance {spec.table} failed: {str(e)[:160]}")

    # 全表审计 + 失败表补拉 (weekly_full 已试过, repair 用于 rollback 表兜底)
    audit = audit_all(today)
    failed = sorted(t for t, rules in audit.items()
                    if any(v == "fail" for v in rules.values()))
    repaired, still = [], []
    if failed:
        _log.warning(f"[{today}] weekly audit FAIL: {failed}")
        repaired, still = repair_and_reaudit(today, failed)
    _log.info(f"[{today}] maintenance audit done: repaired={repaired} still={still}")
    return {"synced": {k: str(v) for k, v in results.items()},
            "repaired": repaired, "still": still}