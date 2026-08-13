"""因子评估调度器 — 每周六 06:00 全自动五阶段评估 + IC 权重刷新.

业界标准 (WorldQuant/AQR/Grinold & Kahn):
  周度批量评估 → 因子状态更新 → 下周信号使用新因子池.
  eval_standard.sh 的 Phase 1-5 全部接入, 不再需要手动运行 (test-v300).
"""
import time as _time, uuid as _uuid, traceback
import numpy as np
import pandas as pd
from quant.scheduler.task_log import start as _tk_start, finish as _tk_finish
from quant.monitor.metrics import metrics as _m
from quant.utils.logger import get_logger
from quant.utils.logger import offline_mode

_log = get_logger(__name__)


# ════════════════════════════════════════════════════════════
# Phase 0: 重评估 evaluating 因子并晋升/归档
# ════════════════════════════════════════════════════════════

def reevaluate_evaluating_factors(today: str) -> dict:
    """重新评估所有 evaluating 状态的因子, 根据 IC/ICIR 触发状态晋升/归档.

    Returns:
        dict with keys: reevaluated, promoted_active, promoted_probation, archived, errors
    """
    from quant.data.repos import FactorRepo
    from quant.factor.state_machine import FactorStateMachine
    from quant.factor.stats_cache import compute_factor_stats
    from quant.factor.store import FactorStore
    from quant.config.constants import _require_cfg

    repo = FactorRepo()
    fsm = FactorStateMachine()

    # Get all evaluating factors
    evaluating_factors = repo.get_all_by_status(("evaluating",))
    if not evaluating_factors:
        _log.info("reevaluate_evaluating_factors: no evaluating factors to re-evaluate")
        return {"reevaluated": 0, "promoted_active": 0, "promoted_probation": 0, "archived": 0, "errors": 0}

    factor_names = [f["name"] for f in evaluating_factors]
    _log.info(f"reevaluate_evaluating_factors: re-evaluating {len(factor_names)} evaluating factors")

    # Load evaluation config
    min_abs_ic = _require_cfg("factor.evaluation.ic_threshold")
    min_icir = _require_cfg("factor.evaluation.icir_threshold")
    min_half_life = _require_cfg("factor.evaluation.min_half_life")
    monitoring_min_ic = _require_cfg("factor.evaluation.monitoring_min_abs_ic")
    monitoring_min_icir = _require_cfg("factor.evaluation.monitoring_min_icir")
    n_days = _require_cfg("factor.evaluation.n_days")
    lookback = _require_cfg("factor.evaluation.lookback")
    n_symbols = _require_cfg("factor.evaluation.n_symbols")

    # Materialize factor cache for evaluating factors first
    _log.info("reevaluate_evaluating_factors: materializing factor cache for evaluating factors...")
    fs = FactorStore()
    from quant.data.repos.universe_repo import UniverseRepo
    from quant.data.store import DataStore
    from quant.factor.compute import get_factor_names

    store = DataStore()
    _latest = store._connect().execute(
        'SELECT MAX(date) FROM daily').fetchone()[0]
    # Need lookback + n_days days of factor data for IC computation
    total_lookback = lookback + n_days
    actual_end = min(today, _latest) if _latest else today
    start_date = (pd.Timestamp(actual_end) - pd.Timedelta(days=total_lookback)).strftime('%Y-%m-%d')
    dates = [r[0] for r in store._connect().execute(
        'SELECT DISTINCT date FROM daily WHERE date >= ? AND date <= ? ORDER BY date',
        (start_date, actual_end)).fetchall()]
    symbols = UniverseRepo().get_symbols(exclude_market='BJ')
    store.close()

    # Materialize factor cache for evaluating factors
    fs.materialize(
        date_range=dates,
        factor_names=factor_names,
        symbols=symbols,
        force=True,
    )

    # Compute factor stats for evaluating factors
    stats = compute_factor_stats(
        n_symbols=n_symbols if n_symbols > 0 else None,
        lookback=lookback,
        factor_names=factor_names,
    )

    factor_names = stats["factor_keys"]
    ic_means = dict(zip(factor_names, stats["ic"]))
    ic_irs = dict(zip(factor_names, stats["ic_ir"]))
    decay = stats.get("decay", {})
    meta = stats.get("meta", {})

    promoted_active = 0
    promoted_probation = 0
    archived = 0
    errors = 0

    for name in factor_names:
        try:
            mean_ic = ic_means.get(name, 0.0)
            mean_icir = ic_irs.get(name, 0.0)
            abs_ic = abs(mean_ic)
            abs_icir = abs(mean_icir)
            reasons = []

            # Evaluate based on thresholds
            if abs_ic < min_abs_ic:
                reasons.append(f"|IC|={abs_ic:.4f}<{min_abs_ic}")
            if abs_icir < min_icir:
                reasons.append(f"ICIR={abs_icir:.2f}<{min_icir}")

            # Check half-life
            decay_vals = decay.get(meta.get(name, {}).get("display", name), [0.0, 0.0, 0.0])
            ic_1d = abs(decay_vals[0]) if len(decay_vals) > 0 else 0.0
            ic_20d = abs(decay_vals[2]) if len(decay_vals) > 2 else 0.0
            half_life_est = None
            if ic_1d > 0.001 and ic_20d > 0:
                ratio_20 = ic_20d / ic_1d
                if 0 < ratio_20 < 1.0:
                    half_life_est = int(-19 * np.log(2) / np.log(ratio_20))
            if half_life_est is not None and half_life_est < min_half_life and ic_1d >= min_abs_ic:
                reasons.append(f"half-life={half_life_est}d<{min_half_life}")

            # Determine verdict and transition
            if not reasons:
                # EVAL_OK -> active
                fsm.transition(name, "EVAL_OK", reason=f"reeval: IC={abs_ic:.4f} IR={abs_icir:.2f}")
                promoted_active += 1
            elif abs_icir >= monitoring_min_icir and abs_ic >= monitoring_min_ic:
                # EVAL_MARGINAL -> probation
                fsm.transition(name, "EVAL_MARGINAL", reason=f"reeval: IC={abs_ic:.4f} IR={abs_icir:.2f} marginal")
                promoted_probation += 1
            else:
                # EVAL_FAIL -> archived
                fsm.transition(name, "EVAL_FAIL", reason=f"reeval: IC={abs_ic:.4f} IR={abs_icir:.2f} failed; {'; '.join(reasons)}")
                archived += 1

        except Exception as e:
            _log.error(f"reevaluate {name} failed: {e}")
            errors += 1

    _log.info(f"reevaluate_evaluating_factors done: promoted_active={promoted_active}, "
              f"promoted_probation={promoted_probation}, archived={archived}, errors={errors}")
    return {
        "reevaluated": len(factor_names),
        "promoted_active": promoted_active,
        "promoted_probation": promoted_probation,
        "archived": archived,
        "errors": errors,
    }


# ═════════════════════════════════════════════════════════════
# Phase 1-5: 完整评估管线 (原 eval_standard.sh)
# ═════════════════════════════════════════════════════════════

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

    rid = _tk_start("weekly_eval", today, grace_seconds=43200)
    if rid is None:
        _log.info(f"[{today}] weekly_eval already running, skip duplicate trigger")
        return
    _log.info(f"[{today}] weekly factor evaluation (Phase 1-5 full pipeline)")
    t0 = _time.time()

    # ═══════════════════════════════════════════
    # v479: 数据维护 — weekly_full 表全量刷新 (dividend/stocks) + 全表审计
    # (失败不阻断评估, 由早间补拉链 weekly_full 7 天兜底)
    # ═══════════════════════════════════════════
    from quant.scheduler.data_maintenance import run_weekly_full
    try:
        mnt = run_weekly_full(today)
        _log.info(f"[{today}] data maintenance: {mnt}")
    except Exception as _mnt_e:
        _log.error(f"[{today}] data maintenance failed: {_mnt_e}")

    # ═══════════════════════════════════════════
    # Phase 0: 重评估 evaluating 因子并晋升/归档
    # ═══════════════════════════════════════════
    p0_ok = _run_phase("eval_reevaluate", today, lambda: reevaluate_evaluating_factors(today))

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
        p5_ok = False
        if p2_ok:
            p5_ok = _run_phase("eval_phase5", today, lambda: (
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
    # v420: 收紧 ok 判定 — 任一阶段失败不得标 ok.
    # 重启补跑门控 (manifest weekly 窗口 + _should_run) 仅 'ok' 不重跑,
    # 失败必须留 failed 才能让当天重跑补救 (如 phase5 状态裁决失败 → 重启后自动重试).
    phases_ok = sum([p0_ok, curation_ok, p1_ok, p2_ok, p3_ok, p4_ok, p5_ok])
    ok = phases_ok == 7
    _log.info(f"[{today}] weekly evaluation done: {phases_ok}/7 phases OK, {n_factors} factors, "
              f"{len(decaying)} decay alerts ({elapsed:.1f}s)")
    _tk_finish("weekly_eval", today, "ok" if ok else "failed",
               summary={"phases_ok": phases_ok, "factors": n_factors,
                        "decay_alerts": len(decaying), "elapsed": round(elapsed, 1)})
    _log.info(f"[SCHEDULER] {today} | TASK=weekly_eval | STATUS={'OK' if ok else 'FAILED'} | "
              f"phases={phases_ok}/7 factors={n_factors} | elapsed={elapsed:.1f}s")
    _m.inc("scheduler.weekly.ok")


if __name__ == "__main__":
    _run(_uuid.uuid4().hex[:12])