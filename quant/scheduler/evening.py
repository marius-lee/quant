"""晚间链式调度 — daily_data → factor_cache → attribution 严格依赖串行 (test-v302).

替代原 cron 19:00/20:00/21:00 三个固定时刻条目。固定时刻的结构性缺陷:
daily_data 合法运行 10min~2h (实测 6251s), 而 attribution 固定 20:00、
factor_cache 固定 21:00 — attribution 必然跑在当日因子物化之前,
G1 (oos_verify 需当日缓存) 与 G4 (factor PnL 需当日缓存) 每个交易日必崩
(test-v301 断点 3/4 的排程根因)。

链式语义:
- 阶段按依赖顺序串行, 前阶段 task_runs 最新状态 = ok 才启动下一阶段
- 阶段已 ok (如人工提前跑过) → 跳过, 不重复执行
- 阶段失败/中断 → 链立即中止 (fail-loud), evening_chain 标 failed
- 各阶段仍各自记录 task_runs 行, 调度页面可见每阶段状态
- cron 与 orchestrator daemon 并存时, 各阶段 _tk_start grace 去重, 不会双跑
"""
import importlib
import time as _time
import uuid as _uuid
from datetime import datetime

from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish, query_date as _tk_query
from quant.utils.logger import get_logger, set_trace_id

_log = get_logger(__name__)

# 依赖顺序即执行顺序; attribution 必须在 factor_cache 之后 (G1/G4 需当日因子缓存)
_CHAIN = [
    ("daily_data", "quant.scheduler.daily_data"),
    ("adj_factor", None),                          # test-v317: 内联执行 (DataStore.sync_adj_factor)
    ("factor_cache", "quant.scheduler.factor_cache"),
    ("attribution", "quant.scheduler.attribution"),
    ("lgb_train", "quant.scheduler.lgb_train"),    # 仅周一/周四
]


def _load_stage(module_path: str):
    """按模块路径加载阶段模块 (单独函数便于测试 monkeypatch)."""
    return importlib.import_module(module_path)


def _stage_status(today: str, name: str):
    """task_runs 中该任务今日最新状态; 无记录 → None."""
    rows = [r for r in _tk_query(today) if r["task_name"] == name]
    return rows[0]["status"] if rows else None  # query_date 按 id DESC, 首行即最新


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    set_trace_id(tid)
    from quant.scheduler.orchestrator import _TIMEOUTS
    rid = _tk_start("evening_chain", today, grace_seconds=_TIMEOUTS["evening_chain"])
    if rid is None:
        _log.info(f"[{today}] evening_chain already running, skip duplicate trigger")
        return
    _log.info(f"[{today}] evening chain: daily_data → factor_cache → attribution")
    t0 = _time.time()
    status = "ok"
    error_msg = None

    try:
        for name, module_path in _CHAIN:
            # lgb_train: 仅周一/周四执行
            if name == "lgb_train":
                wd = datetime.now().weekday()
                if wd not in (0, 3):  # 0=周一, 3=周四
                    _log.info(f"[{today}] evening chain: {name} skipped (not Mon/Thu, wd={wd})")
                    continue
            # adj_factor: 内联执行 (test-v317: 无需单独模块, 1 批/天不超限流)
            if name == "adj_factor":
                if _stage_status(today, name) == "ok":
                    _log.info(f"[{today}] evening chain: {name} already ok, skip")
                    continue
                _log.info(f"[{today}] evening chain: starting {name}")
                stage_t0 = _time.time()
                from quant.scheduler.task_log import start as _s, finish as _f
                _s(name, today)
                try:
                    from quant.data.store import DataStore
                    result = DataStore().sync_adj_factor(max_batches=1)
                    _f(name, today, "ok", str(result.get("rows", 0)) if isinstance(result, dict) else str(result))
                except Exception as _adj_e:
                    _f(name, today, "failed", str(_adj_e))
                    error_msg = f"{name} failed: {_adj_e}, chain aborted"
                    _log.error(f"[{today}] evening chain: {error_msg}")
                    status = "failed"
                    break
                _log.info(f"[{today}] evening chain: {name} ok ({_time.time() - stage_t0:.1f}s)")
                continue
            # 人工提前跑过且成功 → 跳过
            if _stage_status(today, name) == "ok":
                _log.info(f"[{today}] evening chain: {name} already ok, skip")
                continue
            _log.info(f"[{today}] evening chain: starting {name}")
            stage = _load_stage(module_path)
            stage_t0 = _time.time()
            try:
                stage._run(today)
            except Exception as e:
                _log.error(f"[{today}] evening chain: {name} raised: {e}")
            st = _stage_status(today, name)
            if st != "ok":
                error_msg = f"{name} status={st or 'no-record'}, chain aborted (后续阶段跳过)"
                _log.error(f"[{today}] evening chain: {error_msg}")
                status = "failed"
                break
            _log.info(f"[{today}] evening chain: {name} ok ({_time.time() - stage_t0:.1f}s)")
        elapsed = _time.time() - t0
        _log.info(f"[SCHEDULER] {today} | TASK=evening_chain | STATUS={status.upper()} | elapsed={elapsed:.1f}s")
    finally:
        _tk_finish("evening_chain", today, status, error=error_msg)
