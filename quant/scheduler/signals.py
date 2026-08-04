"""信号生成调度器 — 每日 08:30."""
import time as _time, uuid as _uuid
from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish
from datetime import time
from quant.monitor.metrics import metrics as _m
from quant.utils.logger import get_logger, set_trace_id
from quant.scheduler._base import _timed_loop

_log = get_logger(__name__)


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    set_trace_id(tid)
    # grace 对齐 orchestrator._TIMEOUTS["signals"]=1800 (test-v301: 原 900s
    # 小于合法运行时长, 双调度第二触发误 abort 活任务)
    rid = _tk_start("signals", today, grace_seconds=1800)
    if rid is None:
        _log.info(f"[{today}] signals already running, skip duplicate trigger")
        return
    _log.info(f"[{today}] 08:30 — generating signals")
    t0 = _time.time()
    status = "failed"
    error_msg = None
    summary = {}

    try:

        from quant.pipeline import generate_signals
        from quant.factor.store import FactorStore
        from quant.config.paths import FACTOR_CACHE_DB
        from quant.backtest.context import LiveContext

        # test-v398: 实盘复用 LiveContext — engine/cost_model/constructor 只建一次
        _live_ctx = LiveContext()

        # ADR-037: 冷却期过滤提前到信号生成阶段
        # 此前冷却过滤只在 ExecutionModel.run() 执行阶段，冷却标的仍出现在
        # daily_signals 和 Web UI 候选池中。现改为信号阶段即过滤，使候选池更干净。
        from quant.execution.stop_loss import RiskManager
        rm = RiskManager(strategy="quant")
        cooloff = list(rm.get_cooloff_symbols(today))
        if cooloff:
            _log.info(f"[{today}] cooling-off filter: {len(cooloff)} symbols excluded from signals")

        fs = FactorStore(db_path=FACTOR_CACHE_DB)
        result = generate_signals(
            date_str=today, skip_pull=True, factor_store=fs,
            exclude_symbols=cooloff,
            ctx=_live_ctx.to_backtest_context(),
        )
        fs.close()
        targets = result.get("target_positions", [])

        # signals already persisted by pipeline.generate_signals() → daily_signals table
        elapsed = _time.time() - t0
        _log.info(f"[{today}] signals done: {len(targets)} targets ({elapsed:.1f}s)")
        status = "ok"
        summary = {"targets": len(targets), "elapsed": round(elapsed, 1)}
        _log.info(f"[SCHEDULER] {today} | TASK=signals | STATUS=OK | targets={len(targets)} | elapsed={elapsed:.1f}s")
        _m.inc("scheduler.signals.ok")

    except Exception as e:
        error_msg = str(e)
        _log.exception(f"[{today}] signals crashed: {e}")
        raise
    finally:
        _tk_finish("signals", today, status, error=error_msg, summary=summary)

def _loop():
    _timed_loop("signals", time(8, 30), _run, has_multiprocess=True)
