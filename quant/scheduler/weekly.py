"""因子评估调度器 — 每周六 06:00 自动刷新 IC 权重。

业界标准 (Grinold & Kahn / AQR): 因子 IC 权重每周/每月更新一次。
日更会引入噪声，增大换手率，吃掉收益。
"""
import time as _time, uuid as _uuid
from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish
from datetime import time
from quant.monitor.metrics import metrics as _m
from quant.utils.logger import get_logger
from quant.scheduler._base import _weekly_loop

_log = get_logger(__name__)


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    rid = _tk_start("weekly_eval", today, grace_seconds=7200)  # 对齐 _TIMEOUTS (test-v301)
    if rid is None:
        _log.info(f"[{today}] weekly_eval already running, skip duplicate trigger")
        return
    _log.info(f"[{today}] weekly factor evaluation starting")
    t0 = _time.time()

    from quant.factor.stats_cache import force_refresh_cache
    stats = force_refresh_cache()
    n_factors = len(stats.get("factors", []))

    # ── IC 衰减检查 (报告 §6.4: 原 phase5_monitor._check_ic_decay 死代码活化) ──
    # 复用刚刷新的 stats["decay"] (零额外计算). retention=|IC_20d/IC_1d| < 0.3
    # → 长期 IC 不到短期 1/3, 信号快衰减, 周频评审告警 (因子生命周期域).
    decay = stats.get("decay", {})
    decaying = []
    for name, d in decay.items():
        try:
            ic_1d, ic_20d = (d[0], d[2]) if isinstance(d, (list, tuple)) and len(d) >= 3 \
                else (d.get("1d", 0), d.get("20d", 0))
            if ic_1d and abs(ic_1d) > 1e-6:
                retention = abs(ic_20d / ic_1d)
                if retention < 0.3:
                    decaying.append((name, ic_1d, ic_20d, retention))
        except (TypeError, IndexError, AttributeError):
            continue
    if decaying:
        for name, ic_1d, ic_20d, ret in decaying:
            _log.warning(f"[{today}] IC decay alert: {name} IC_1d={ic_1d:+.4f} "
                         f"IC_20d={ic_20d:+.4f} retention={ret:.0%} (<30%)")
        _m.inc("scheduler.weekly.ic_decay_alerts", len(decaying))
    else:
        _log.info(f"[{today}] IC decay check: {len(decay)} factors OK (retention >= 30%)")

    elapsed = _time.time() - t0
    _log.info(f"[{today}] weekly factor evaluation done: {n_factors} factors, "
              f"{len(decaying)} decay alerts ({elapsed:.1f}s)")
    _tk_finish("weekly_eval", today, "ok",
               summary={"factors": n_factors, "decay_alerts": len(decaying),
                        "elapsed": round(elapsed, 1)})
    _log.info(f"[SCHEDULER] {today} | TASK=weekly_eval | STATUS=OK | "
              f"factors={n_factors} decay_alerts={len(decaying)} | elapsed={elapsed:.1f}s")
    _m.inc("scheduler.weekly.ok")


def _loop():
    # 周六 06:00 (UTC+8), weekday=5
    _weekly_loop("weekly_eval", target_weekday=5, target_time=time(6, 0), run_fn=_run)
