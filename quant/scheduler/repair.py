"""早间补拉链 — 每日 08:00 (subprocess, manifest daily_repair).

v479: 晚间链完成后审计失败的表现在次日 08:00 重试 —
T+1 迟发数据 (margin/lhb/limit_up) 此时已发布, 修复在 signals (08:30) 之前完成.

额外兜底:
  - 回看最近 3 天 data_audit 的 fail 表 (周末覆盖周五晚间链缺口)
  - weekly_full 表 (dividend/stocks) 距上次全规则 ok 超 7 天 → 强制全量刷新
    (即使周六 weekly_eval 失败, 早间链也能兜底)

状态: 无失败 → ok (秒级); 修复成功 → ok; 仍失败 → failed (留 task_runs,
次日窗口再触发; 连续失败每晚 ERROR 告警由 daily_data 输出).
"""
import sqlite3
from datetime import datetime, date as _date, timedelta as _td

from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish
from quant.utils.logger import get_logger, set_trace_id
from quant.config.paths import MARKET_DB

_log = get_logger(__name__)


def _pending_tables(today: str) -> list[str]:
    """最近 3 天审计失败表 + 超过 7 天未 ok 的 weekly_full 表."""
    from quant.data.data_health import failed_tables_on, last_ok_check
    tables: set[str] = set()
    for i in range(1, 4):
        d = (_date.fromisoformat(today) - _td(days=i)).strftime("%Y-%m-%d")
        tables |= set(failed_tables_on(d))
    from quant.data.table_registry import weekly_full_specs
    for spec in weekly_full_specs():
        last_ok = last_ok_check(spec.table)
        if last_ok is None:
            tables.add(spec.table)
            _log.info(f"[{today}] {spec.table}: 从未 audit ok → 纳入本次全量刷新")
        else:
            lag = (datetime.now() - datetime.fromisoformat(last_ok)).days
            if lag > 7:
                tables.add(spec.table)
                _log.info(f"[{today}] {spec.table}: 上次 ok 距今 {lag} 天 > 7 → 强制刷新")
    return sorted(tables)


def _run(today: str):
    from quant.utils.logger import set_trace_id as _sid
    _sid(today[:8])
    rid = _tk_start("daily_repair", today, grace_seconds=1800)
    if rid is None:
        _log.info(f"[{today}] daily_repair already running, skip duplicate trigger")
        return

    tables = _pending_tables(today)
    if not tables:
        _log.info(f"[{today}] daily_repair: 无待修复表, done (0s)")
        _tk_finish("daily_repair", today, "ok", summary={"tables": [], "repaired": []})
        return

    _log.info(f"[{today}] daily_repair: 待处理 {tables}")
    from quant.data.data_health import repair_and_reaudit
    repaired, still = repair_and_reaudit(today, tables)
    for t in repaired:
        _log.info(f"[{today}] daily_repair fixed: {t}")
    for t in still:
        _log.error(f"[{today}] daily_repair STILL FAILED: {t}")

    _tk_finish("daily_repair", today, "ok" if not still else "failed",
               summary={"tables": tables, "repaired": repaired, "still": still})


if __name__ == "__main__":
    _run(datetime.now().strftime("%Y-%m-%d"))