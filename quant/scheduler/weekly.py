"""因子评估调度器 — 每周六 06:00 全自动五阶段评估 + IC 权重刷新.

业界标准 (WorldQuant/AQR/Grinold & Kahn):
  周度批量评估 → 因子状态更新 → 下周信号使用新因子池.
  eval_standard.sh 的 Phase 1-5 全部接入, 不再需要手动运行 (test-v300).
"""
import time as _time, uuid as _uuid, traceback
from datetime import time
from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish
from quant.monitor.metrics import metrics as _m
from quant.utils.logger import get_logger
from quant.scheduler._base import _weekly_loop

_log = get_logger(__name__)


def _run_phase(name: str, today: str, fn, grace: int = 3600) -> bool:
    """运行单个评估阶段, 写 task_runs. 返回 True=成功."""
    _tk_start(name, today, grace_seconds=grace)
    t0 = _time.time()
    try:
        fn()
        elapsed = _time.time() - t0
        _log.info(f"[{today}] {name} done ({elapsed:.1f}s)")
        _tk_finish(name, today, "ok", summary={"elapsed": round(elapsed, 1)})
        return True
    except Exception as e:
        _log.error(f"[{today}] {name} failed: {e}")
        _log.error(traceback.format_exc())
        _tk_finish(name, today, "failed", error=str(e))
        return False


def _run(today: str):
    tid = _uuid.uuid4().hex[:12]
    from quant.utils.logger import set_trace_id
    set_trace_id(tid)
    from quant.utils.logger import offline_mode, cleanup_old_logs
    cleanup_old_logs(keep_days=14)  # test-v321: 周度评估时清理14天前旧日志

    rid = _tk_start("weekly_eval", today, grace_seconds=7200)
    if rid is None:
        _log.info(f"[{today}] weekly_eval already running, skip duplicate trigger")
        return
    _log.info(f"[{today}] weekly factor evaluation (Phase 1-5 full pipeline)")
    t0 = _time.time()

    # ═══════════════════════════════════════════
    # Step 0: 因子策展 — 先收录新因子 (test-v265)
    # ═══════════════════════════════════════════
    curation_ok = True
    try:
        _tk_start("factor_curation", today, grace_seconds=3600)
        from quant.factor.factor_curator import FactorCurator
        curator = FactorCurator()
        curated = curator.curate(n_symbols=500, n_dates=120, auto_register=True)
        _log.info(f"[{today}] factor curation: {curated['n_evaluated']} new, "
                  f"{curated['n_registered']} registered")
        if curated.get('results'):
            for r in curated['results'][:3]:
                _log.info(f"  {r['name']}: IC={r['mean_ic']:.4f} → {r['verdict']} ({r['source'][:30]})")
        _tk_finish("factor_curation", today, "ok",
                   summary={"evaluated": curated['n_evaluated'], "registered": curated['n_registered']})
    except Exception as e:
        _log.warning(f"[{today}] factor curation failed (non-fatal): {e}")
        _tk_finish("factor_curation", today, "failed", error=str(e))
        curation_ok = False

    # ═══════════════════════════════════════════
    # Phase 1-5: 完整评估管线 (原 eval_standard.sh)
    # 设计: 各阶段独立 task_runs, 前一阶段失败时后续跳过
    # 结果写入 evaluation_runs 表, Phase 5b 读取后同步到 factor_registry
    # ═══════════════════════════════════════════
    with offline_mode():
        # Phase 1: 数据准备
        p1_ok = _run_phase("eval_phase1", today, lambda: (
            __import__('quant.evaluation.phase1_data', fromlist=['prepare_data']).prepare_data()
        ))

        # Phase 2: 单因子检验 (依赖 Phase 1)
        p2_ok = False
        if p1_ok:
            p2_ok = _run_phase("eval_phase2", today, lambda: (
                __import__('quant.evaluation.phase2_single', fromlist=['screen_factors'])
                .screen_factors()
            ))

        # Phase 3: CPCV OOS + PBO (依赖 Phase 2)
        p3_ok = False
        if p2_ok:
            p3_ok = _run_phase("eval_phase3", today, lambda: (
                __import__('quant.evaluation.phase3_oos', fromlist=['validate_oos']).validate_oos()
            ))

        # Phase 4: 交易成本验证 (依赖 Phase 3)
        p4_ok = False
        if p3_ok:
            p4_ok = _run_phase("eval_phase4", today, lambda: (
                __import__('quant.evaluation.phase4_costs', fromlist=['verify_costs']).verify_costs()
            ))

        # Phase 5b: 状态同步 → factor_registry (依赖 Phase 2-4)
        if p2_ok:
            _run_phase("eval_phase5", today, lambda: (
                __import__('quant.evaluation.phase5_monitor', fromlist=['sync_factor_status'])
                .sync_factor_status()
            ))

    # ── IC 权重刷新 + 衰减检查 ──
    from quant.factor.stats_cache import force_refresh_cache
    stats = force_refresh_cache()
    n_factors = len(stats.get("factors", []))

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
    phases_ok = sum([curation_ok, p1_ok, p2_ok, p3_ok, p4_ok])
    _log.info(f"[{today}] weekly evaluation done: {phases_ok}/6 phases OK, {n_factors} factors, "
              f"{len(decaying)} decay alerts ({elapsed:.1f}s)")
    _tk_finish("weekly_eval", today, "ok" if phases_ok >= 5 else "failed",
               summary={"phases_ok": phases_ok, "factors": n_factors,
                        "decay_alerts": len(decaying), "elapsed": round(elapsed, 1)})
    _log.info(f"[SCHEDULER] {today} | TASK=weekly_eval | STATUS={'OK' if phases_ok >= 5 else 'FAILED'} | "
              f"phases={phases_ok}/6 factors={n_factors} | elapsed={elapsed:.1f}s")
    _m.inc("scheduler.weekly.ok")


def _loop():
    # 周六 06:00 (UTC+8), weekday=5
    _weekly_loop("weekly_eval", target_weekday=5, target_time=time(6, 0), run_fn=_run)
