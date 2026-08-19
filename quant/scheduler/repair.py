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
    """最近 3 天审计失败表 + 超过 7 天未 ok 的 weekly_full 表.

    v492: 过滤 repair_eligible=False 的表 (财务表 sina 首轮 4-5h > 30min
    窗口, 早间链不兜底, 只由周六 data_maintenance 维护).
    """
    from quant.data.data_health import failed_tables_on, last_ok_check
    from quant.data.table_registry import REGISTRY, weekly_full_specs
    tables: set[str] = set()
    for i in range(1, 4):
        d = (_date.fromisoformat(today) - _td(days=i)).strftime("%Y-%m-%d")
        for t in failed_tables_on(d):
            if REGISTRY.get(t) is not None and REGISTRY[t].repair_eligible:
                tables.add(t)
    for spec in weekly_full_specs():
        if not spec.repair_eligible:
            continue
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


def _ensure_factor_cache(today: str) -> list[str]:
    """v532 (2026-08-18): 晚间链 factor_cache 失败兜底 — 08:00 增量物化缺口.

    晚间链 (19:00) 失败/中止 (子进程崩溃、质量门禁 error) 时 factor_cache
    未物化当日 → signals 08:30 用旧缓存。原 daily_repair 只修 REGISTRY 数据表,
    factor_cache 缺口无人接管 ("08:00 无因子物化兜底")。
    增量物化 _fc_start→today 幂等, 已物化日期跳过 (meta 记录), 正常补 1-2 日。
    返回: 本次物化的日期数 (0=已 ok 无需补)。
    """
    with sqlite3.connect(MARKET_DB) as conn:
        rows = conn.execute(
            "SELECT status FROM task_runs WHERE date=? AND task_name='factor_cache' "
            "ORDER BY id DESC LIMIT 1", (today,)).fetchall()
    if rows and rows[0][0] == "ok":
        _log.info(f"[{today}] factor_cache already ok (evening chain) — no repair needed")
        return []
    _log.info(f"[{today}] factor_cache 未物化 (晚间链失败/中止) — 08:00 增量物化兜底")
    from quant.config.constants import _require_cfg
    _fc_start = _require_cfg("backtest.factor_cache_start")
    from quant.scheduler.factor_cache import _run as _fc_run
    _fc_run(_fc_start, today)
    _log.info(f"[{today}] factor_cache repair done")
    return [today]


def _run(today: str):
    from quant.utils.logger import set_trace_id as _sid
    _sid(today[:8])
    from quant.scheduler.manifest import spec
    rid = _tk_start("daily_repair", today, grace_seconds=spec("daily_repair").grace_s)
    if rid is None:
        _log.info(f"[{today}] daily_repair already running, skip duplicate trigger")
        return

    tables = _pending_tables(today)
    repaired: list[str] = []
    still: list[str] = []
    if not tables:
        _log.info(f"[{today}] daily_repair: 无待修复表, done (0s)")
    else:
        _log.info(f"[{today}] daily_repair: 待处理 {tables}")
        from quant.data.data_health import repair_and_reaudit
        repaired, still = repair_and_reaudit(today, tables)
        for t in repaired:
            _log.info(f"[{today}] daily_repair fixed: {t}")
        for t in still:
            _log.error(f"[{today}] daily_repair STILL FAILED: {t}")

    # v532: factor_cache 兜底 — 晚间链失败时 08:00 增量物化 (signals 08:30 前)
    fc_dates = _ensure_factor_cache(today)

    _tk_finish("daily_repair", today, "ok" if not still else "failed",
               summary={"tables": tables, "repaired": repaired, "still": still,
                        "factor_cache_repaired": fc_dates})


if __name__ == "__main__":
    _run(datetime.now().strftime("%Y-%m-%d"))