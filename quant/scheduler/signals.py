"""信号生成调度器 — 每日 08:30.

v565: Dagster + Legacy 双模式统一监控 — 调度模块恢复写入 task_runs。
"""
import time as _time, uuid as _uuid
from datetime import time
from quant.utils.date import today_str
from quant.monitor.metrics import metrics as _m
from quant.utils.logger import get_logger, set_trace_id
from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish

_log = get_logger(__name__)


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    set_trace_id(tid)
    # v565: Dagster 双模式统一写入 task_runs
    rid = _tk_start("signals", today, grace_seconds=1800)
    if rid is None:
        _log.info(f"[{today}] signals already running, skip duplicate trigger")
        return {"targets": 0, "elapsed": 0.0}
    _log.info(f"[{today}] 08:30 — generating signals")
    t0 = _time.time()
    _status = "ok"
    _error = None
    _targets = 0
    try:
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
        _targets = len(result.get("target_positions", []))
    except Exception as e:
        _status = "failed"
        _error = str(e)
        _log.exception(f"[{today}] signals crashed: {e}")
        raise
    finally:
        # v577 (V586 defensive pattern): guarantee task_runs row is finished on EVERY
        # exit path — a crash must not leave status='running' (cf. the 2026-09-08
        # signals stuck row, pid=51902 web/app.py, HANDOFF v609/v625).
        elapsed = _time.time() - t0
        _tk_finish("signals", today, _status, error=_error,
                   summary={"targets": _targets, "elapsed": round(elapsed, 1)})
        if _status == "ok":
            _log.info(f"[{today}] signals done: {_targets} targets ({elapsed:.1f}s)")
            _m.inc("scheduler.signals.ok")
        else:
            _m.inc("scheduler.signals.failed")
        _log.info(f"[SCHEDULER] {today} | TASK=signals | STATUS={_status.upper()} | "
                  f"targets={_targets} | elapsed={elapsed:.1f}s")
    return {"targets": _targets, "elapsed": round(_time.time() - t0, 1)}


if __name__ == "__main__":
    import sys
    _run(sys.argv[1] if len(sys.argv) > 1 else today_str())