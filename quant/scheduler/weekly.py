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
    """复查所有 evaluating 状态的因子, 输出晋升预判报告 (v519: 不再直接改状态).

    v519 变更 (业界标准对齐, De Prado 2018 Ch.7-8 多阶段过滤):
      原实现用 stats_cache 即时计算 IC/ICIR 并直接 transition — 与完整评估管线
      (Phase 2-4 + phase5b 综合裁决) 并行且抢先, 导致轻量关卡先行晋升、
      phase5b 的 CPCV/PBO/成本验证结论永不作用于已升因子。
      现改为只读上一轮 Phase 2 快照做预判报告 (would-be verdict),
      状态变更统一收敛到 phase5b 单一裁决入口。

    Returns:
        dict with keys: evaluated, would_active, would_probation, would_archived,
        note (预判仅供参考, 实际裁决由 phase5b 执行)
    """
    from quant.data.repos import FactorRepo
    from quant.evaluation.run_store import load_latest
    from quant.config.constants import _require_cfg

    repo = FactorRepo()

    # Get all evaluating factors
    evaluating_factors = repo.get_all_by_status(("evaluating",))
    if not evaluating_factors:
        _log.info("reevaluate_evaluating_factors: no evaluating factors to re-evaluate")
        return {"evaluated": 0, "would_active": 0, "would_probation": 0, "would_archived": 0, "note": "no evaluating factors"}

    factor_names = [f["name"] for f in evaluating_factors]
    p2 = load_latest("phase2")
    if not p2:
        _log.warning("reevaluate_evaluating_factors: no Phase 2 snapshot yet — verdict deferred to phase5b")
        return {"evaluated": len(factor_names), "would_active": 0, "would_probation": 0,
                "would_archived": 0, "note": "no phase2 snapshot — verdict deferred to phase5b"}

    # Load evaluation config thresholds
    min_abs_ic = _require_cfg("factor.evaluation.ic_threshold")
    min_icir = _require_cfg("factor.evaluation.icir_threshold")
    min_half_life = _require_cfg("factor.evaluation.min_half_life")
    monitoring_min_ic = _require_cfg("factor.evaluation.monitoring_min_abs_ic")
    monitoring_min_icir = _require_cfg("factor.evaluation.monitoring_min_icir")

    meta = p2.get("meta", {}) if isinstance(p2.get("meta"), dict) else {}
    decay = p2.get("decay", {}) if isinstance(p2.get("decay"), dict) else {}

    would_active, would_probation, would_archived = [], [], []
    for name in factor_names:
        mean_ic = p2.get("ic_means", {}).get(name, 0.0) or 0.0
        mean_icir = p2.get("ic_irs", {}).get(name, 0.0) or 0.0
        abs_ic = abs(mean_ic)
        abs_icir = abs(mean_icir)
        reasons = []

        if abs_ic < min_abs_ic:
            reasons.append(f"|IC|={abs_ic:.4f}<{min_abs_ic}")
        if abs_icir < min_icir:
            reasons.append(f"ICIR={abs_icir:.2f}<{min_icir}")

        # half-life check (与原实现同一口径)
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

        if not reasons:
            would_active.append(name)
        elif abs_icir >= monitoring_min_icir and abs_ic >= monitoring_min_ic:
            would_probation.append(name)
        else:
            would_archived.append(name)

    _log.info("reevaluate_evaluating_factors (v519 report-only): phase2-snapshot verdicts → "
              f"active={sorted(would_active)}, probation={sorted(would_probation)}, "
              f"archived={sorted(would_archived)}")
    if would_active:
        _log.info("  → promoted to active by phase5b ONLY if Phase 2+3+4 all pass + DSR significant")
    for name in sorted(would_archived):
        _log.warning(f"  ✗ {name}: Phase 2 snapshot already below thresholds — likely archived this cycle")

    return {"evaluated": len(factor_names), "would_active": len(would_active),
            "would_probation": len(would_probation), "would_archived": len(would_archived),
            "note": "report-only; actual transitions executed by phase5b (p2+p3+p4+DSR)"}


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