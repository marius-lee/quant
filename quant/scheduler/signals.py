"""信号生成调度器 — 每日 08:30.

注意: task_log 由 Runner 统一管理，任务模块不再调用 _tk_start/_tk_finish。
"""
import time as _time, uuid as _uuid
from datetime import time
from quant.utils.date import today_str
from quant.monitor.metrics import metrics as _m
from quant.utils.logger import get_logger, set_trace_id

_log = get_logger(__name__)


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    set_trace_id(tid)
    _log.info(f"[{today}] 08:30 — generating signals")
    t0 = _time.time()

    from quant.pipeline import generate_signals
    from quant.factor.store import FactorStore
    from quant.config.paths import FACTOR_CACHE_DB
    from quant.backtest.context import ExecutionContext

    # 实盘复用 ExecutionContext — engine/cost_model/constructor 只建一次
    _ctx = ExecutionContext(suppress_push=False)

    # ADR-037: 冷却期过滤提前到信号生成阶段
    from quant.execution.stop_loss import RiskManager
    rm = RiskManager(strategy="quant")
    cooloff = list(rm.get_cooloff_symbols(today))
    if cooloff:
        _log.info(f"[{today}] cooling-off filter: {len(cooloff)} symbols excluded from signals")

    fs = FactorStore(db_path=FACTOR_CACHE_DB)
    result = generate_signals(
        date_str=today, skip_pull=True, factor_store=fs,
        exclude_symbols=cooloff,
        ctx=_ctx,
    )
    fs.close()
    targets = result.get("target_positions", [])

    # signals already persisted by pipeline.generate_signals() → daily_signals table
    elapsed = _time.time() - t0
    _log.info(f"[{today}] signals done: {len(targets)} targets ({elapsed:.1f}s)")
    _log.info(f"[SCHEDULER] {today} | TASK=signals | STATUS=OK | targets={len(targets)} | elapsed={elapsed:.1f}s")
    _m.inc("scheduler.signals.ok")
    return {"targets": len(targets), "elapsed": round(elapsed, 1)}


if __name__ == "__main__":
    import sys
    _run(sys.argv[1] if len(sys.argv) > 1 else today_str())