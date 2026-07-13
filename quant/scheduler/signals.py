"""信号生成调度器 — 每日 08:30."""
import time as _time, uuid as _uuid
from datetime import time
from monitor.metrics import metrics as _m
from utils.logger import get_logger
from quant.scheduler._base import _timed_loop

_log = get_logger("quant.scheduler.signals")


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    _log.info(f"[{today}] 08:30 — generating signals")
    t0 = _time.time()

    from pipeline import generate_signals
    from data.trade_repo import TradeRepo
    result = generate_signals(date_str=today)
    targets = result.get("target_positions", [])

    # ── 持久化: 信号写入 DB (execute 从此读取) ──
    if targets:
        cap = TradeRepo().get_cash("quant")
        TradeRepo().save_signals(today, targets, cap or 0.0)

    elapsed = _time.time() - t0
    _log.info(f"[{today}] signals done: {len(targets)} targets ({elapsed:.1f}s)")
    _log.info(f"[SCHEDULER] {today} | TASK=signals | STATUS=OK | targets={len(targets)} | elapsed={elapsed:.1f}s")
    _m.inc("scheduler.signals.ok")


def _loop():
    _timed_loop("signals", time(8, 30), _run, has_multiprocess=True)
