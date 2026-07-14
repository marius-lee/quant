"""因子评估调度器 — 每周六 06:00 自动刷新 IC 权重。

业界标准 (Grinold & Kahn / AQR): 因子 IC 权重每周/每月更新一次。
日更会引入噪声，增大换手率，吃掉收益。
"""
import time as _time, uuid as _uuid
from datetime import time
from quant.monitor.metrics import metrics as _m
from quant.utils.logger import get_logger
from quant.scheduler._base import _weekly_loop

_log = get_logger("quant.scheduler.weekly")


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    _log.info(f"[{today}] weekly factor evaluation starting")
    t0 = _time.time()

    from quant.factor.stats_cache import force_refresh_cache
    stats = force_refresh_cache()
    n_factors = len(stats.get("factors", []))

    elapsed = _time.time() - t0
    _log.info(f"[{today}] weekly factor evaluation done: {n_factors} factors ({elapsed:.1f}s)")
    _log.info(f"[SCHEDULER] {today} | TASK=weekly_eval | STATUS=OK | factors={n_factors} | elapsed={elapsed:.1f}s")
    _m.inc("scheduler.weekly.ok")


def _loop():
    # 周六 06:00 (UTC+8), weekday=5
    _weekly_loop("weekly_eval", target_weekday=5, target_time=time(6, 0), run_fn=_run)
