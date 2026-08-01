"""Stage 5: 因子状态裁决 (sync_factor_status) — 手工评估管线 (eval_standard.sh) 终段.

职责边界 (报告 §6.4, 2026-07-26 合并落地):
  scheduler/attribution (日频 20:00) = 实盘组合监控: Brinson/拥挤度/DSR/
      因子PnL/换手率/daily_equity — 本模块不再重复 (原 run_monitor 已删:
      crowding/turnover/capacity 与 attribution 重复, 且 _check_turnover
      拿 market.db 连接查 sim_trades 必崩).
  evaluation (周频/手工) = 因子生命周期: IC 衰减检查已活化到
      scheduler/weekly (stats["decay"] 零成本复用);
      本模块只做 phase1-4 结果的最终状态裁决.
"""

from quant.config.constants import _require_cfg
from quant.data.repos.factor_repo import FactorRepo


def sync_factor_status() -> dict:
    # ── 综合裁决逻辑 (De Prado 2018 Ch.7-8: 多阶段过滤, 每阶段三档输出) ──
    import sqlite3
    from quant.evaluation.run_store import load_latest
    from quant.utils.logger import get_logger
    _log = get_logger("evaluation.phase5")

    p2 = load_latest("phase2")
    p3 = load_latest("phase3")
    p4 = load_latest("phase4")

    if not p2:
        _log.warning("sync_factor_status: no Phase 2 data — skipping")
        return {"permanent": [], "weak": [], "marginal": [], "strong": [], "unchanged": 0}

    f_repo = FactorRepo()

    # ── 探伤断点: 全零 IC 守卫 ──
    p2_ic_vals_raw = p2.get("ic_means", {})
    p2_n_factors = p2.get("n_factors", 0)
    p2_n_passed = len(p2.get("passed", []))
    if (p2_n_factors > 4
            and p2_n_passed == 0
            and p2_ic_vals_raw
            and all(abs(v) < 1e-10 for v in p2_ic_vals_raw.values())):
        _log.critical(
            "sync_factor_status: CIRCUIT BREAKER — all %d factors have IC≈0.0000, "
            "Phase 2 IC computation likely broken. Refusing to sync. "
            "Fix Phase 2 and re-run evaluation.",
            p2_n_factors
        )
        return {"rejected": [], "retired": [], "monitoring": [], "active": [], "unchanged": 0, "circuit_breaker": True}

    from quant.data.repos._base import DatabaseManager
    conn = DatabaseManager.market()
    # 各 Phase 输出: passed=通过, marginal=踩线(观察), failed=不通过(退役)
    # Phase 5 聚合:
    #   pass+pass+pass → active
    #   any marginal → monitoring (信号偏弱, 等下一评估周期)
    #   any fail → retired (本轮无明显信号, retry_count++)
    #   retry_count ≥ max_retries → rejected (永久淘汰)
    max_retries = _require_cfg("factor.evaluation.max_retries")

    # 从 factor_registry 读取当前 retry_count
    retry_map = {}
    try:
        retry_rows = conn.execute(
            "SELECT name, retry_count FROM factor_registry"
        ).fetchall()
        retry_map = {r[0]: r[1] or 0 for r in retry_rows}
    except Exception as _e:
        # Q7-5 fix: factor_registry 查询失败必须可观测 (原裸 except: pass 吞错)
        _log.warning(f"phase5: retry_map query failed (defaults to empty): {_e}")

    # 加载 IC 数据用于 reason 生成
    p2_ic_vals = p2.get("ic_means", {})
    p2_ic_irs = p2.get("ic_irs", {})
    min_abs_ic = _require_cfg("factor.evaluation.min_abs_ic")
    min_icir = _require_cfg("factor.evaluation.min_icir")

    # Phase 2 输出
    p2_strong = set(p2.get("passed", []))
    p2_marginal = set(p2.get("monitoring", []))
    p2_weak = set(p2.get("failed", {}).keys()) if isinstance(p2.get("failed"), dict) else set(p2.get("failed", []))

    # Phase 3 输出 (三档)
    p3_kept = set(p3.get("kept", [])) if p3 else set()
    p3_marginal = set(p3.get("marginal", [])) if p3 else set()
    p3_dropped = set(p3.get("dropped", [])) if p3 else set()
    p3_note = p3.get("note", "") if p3 else ""

    # Phase 4 输出 (三档)
    p4_final = set(p4.get("final_factors", [])) if p4 else set()
    p4_marginal = set(p4.get("marginal", [])) if p4 else set()
    p4_dropped = set(p4.get("dropped", [])) if p4 else set()
    p4_insufficient = p4.get("insufficient_data", False) if p4 else False

    # ── 综合裁决 ──
    certified_active = set()
    marginal_factors = set()
    weak_factors = set()
    rejected_factors = set()

    # 数据不足: Phase 3 因数据太少跳过 OOS 验证的因子 → monitoring
    insufficient_data_factors = set()
    if (p3_note and "insufficient_data" in p3_note) or p4_insufficient:
        insufficient_data_factors = p2_strong  # 所有 Phase 2 pass 但因数据不足跳过后续的

    # 对每个 backtesting 池中的因子逐个裁决
    all_evaluated = p2_strong | p2_marginal | p2_weak
    reasons = {}  # factor_name → reason_str
    for fname in sorted(all_evaluated):
        # 跳过 diagnostics 排除的因子 (保留原状态)
        if fname not in all_evaluated:
            continue

        p2_status = ("pass" if fname in p2_strong
                     else "marginal" if fname in p2_marginal
                     else "fail")
        p3_status = ("pass" if fname in p3_kept
                     else "marginal" if fname in p3_marginal
                     else "skip" if fname in insufficient_data_factors and p3_note
                     else "fail" if fname in p3_dropped
                     else "skip")
        p4_status = ("pass" if fname in p4_final
                     else "marginal" if fname in p4_marginal
                     else "skip" if fname in insufficient_data_factors
                     else "fail" if fname in p4_dropped
                     else "skip")

        # 综合裁决表
        if fname in insufficient_data_factors:
            marginal_factors.add(fname)
            reasons[fname] = (
                f"[EVAL] Phase 3: OOS validation skipped — IC history too short for CPCV. "
                f"IC={p2_ic_vals.get(fname, 0):+.4f}. Awaiting more data for re-evaluation."
            )
        elif p2_status == "pass" and p3_status == "pass" and p4_status == "pass":
            certified_active.add(fname)
            reasons[fname] = "[EVAL] passed Phase 2+3+4 (full evaluation)"
        elif p2_status == "fail":
            weak_factors.add(fname)
            reasons[fname] = f"[EVAL] Phase 2: IC/ICIR below all thresholds"
        elif p3_status == "fail":
            weak_factors.add(fname)
            reasons[fname] = f"[EVAL] Phase 3: CPCV OOS_ICIR<0 or PBO>threshold"
        elif p4_status == "fail":
            weak_factors.add(fname)
            reasons[fname] = f"[EVAL] Phase 4: net-of-costs Sharpe too low"
        else:
            # any marginal → monitoring
            marginal_factors.add(fname)
            ic_val = p2_ic_vals.get(fname, 0.0)
            icir_val = p2_ic_irs.get(fname, 0.0)
            reasons[fname] = (
                f"[EVAL] Phase 2: IC={ic_val:+.4f} "
                f"|IC|<{min_abs_ic}" if abs(ic_val) < min_abs_ic else
                f"[EVAL] Phase 2: ICIR={abs(icir_val):.2f}<{min_icir}"
            ) + ", routed to monitoring"

    # ── retry_count 管理 ──
    # 对于本轮判定为 retired 的因子, retry_count += 1
    # 达到 max_retries → 升级为 rejected
    for fname in sorted(weak_factors):
        new_retry = retry_map.get(fname, 0) + 1
        retry_map[fname] = new_retry
        if new_retry >= max_retries:
            rejected_factors.add(fname)
            reasons[fname] = f"[EVAL] 累计 {new_retry} 次 retired (≥{max_retries}), 永久淘汰"
            _log.critical(f"[EVAL] {fname}: retired → rejected (retry_count={new_retry}≥{max_retries})")
        else:
            reasons[fname] = f"{reasons[fname]} (retry={new_retry}/{max_retries})"
            _log.info(f"[EVAL] {fname}: retired (retry_count={new_retry}/{max_retries})")

    # 对于本轮判定为 active/monitoring 的因子, retry_count 不递增
    # 但如果之前有 retired → 通过后续评估 → 重置 retry_count = 0
    for fname in sorted(certified_active | marginal_factors):
        if retry_map.get(fname, 0) > 0:
            retry_map[fname] = 0  # 恢复, 重置计数
            _log.info(f"[EVAL] {fname}: retry_count reset to 0 (recovered)")

    # ── 数据库写入 (通过 FactorStateManager 单一入口, ADR-026) ──
    from quant.factor.state_manager import FactorStateManager
    fsm = FactorStateManager()

    current_active = set(r[0] for r in conn.execute(
        "SELECT name FROM factor_registry WHERE status='active'"
    ).fetchall())

    # 不碰 active 因子 (实盘模块 management — attribution.py)
    active_to_update = certified_active - current_active
    marginal_to_update = marginal_factors - current_active
    weak_to_update = weak_factors - rejected_factors - current_active
    permanent_to_update = rejected_factors - current_active

    # ── 通过 StateManager 转换 (零 fallback: 非法转换→ValueError) ──
    active_ok = fsm.batch_transition(
        list(active_to_update), "EVAL_PASS",
        reason="[EVAL] passed Phase 2+3+4 (full evaluation)"
    )
    marginal_ok = 0
    for name in marginal_to_update:
        # EVAL_MARGINAL 只对 evaluating 状态有效
        cur = f_repo.get_factor_by_name(name)
        cs = cur["status"] if cur else "evaluating"
        if cs != "evaluating":
            _log.warning("sync_factor_status: %s EVAL_MARGINAL skipped (status=%s, 只允许 evaluating)", name, cs)
            continue
        try:
            fsm.transition(name, "EVAL_MARGINAL", reasons[name])
            marginal_ok += 1
        except Exception as e:
            _log.warning("sync_factor_status: %s EVAL_MARGINAL failed: %s", name, e)
    weak_ok = 0
    for name in weak_to_update:
        new_retry = retry_map.get(name, 0) + 1
        cur = f_repo.get_factor_by_name(name)
        cs = cur["status"] if cur else "evaluating"
        event = "EVAL_FAIL" if cs == "evaluating" else "IC_PERSISTENT"
        try:
            fsm.transition(name, event, reasons[name], retry_count=new_retry)
            weak_ok += 1
        except Exception as e:
            _log.warning("sync_factor_status: %s %s failed: %s", name, event, e)
    permanent_ok = 0
    for name in permanent_to_update:
        new_retry = retry_map.get(name, 0) + 1
        # EVAL_REJECT 不存在于状态机, 用 status_reason 标记永久淘汰
        f_repo.update(name, {
            "status_reason": f"[EVAL] 累计 {new_retry} 次 retired (≥{max_retries}), 永久淘汰",
            "retry_count": new_retry
        })
        _log.critical(f"sync_factor_status: {name} 永久淘汰 (retry={new_retry}≥{max_retries})")
        permanent_ok += 1

    _log.info(f"sync_factor_status: {active_ok}/{len(active_to_update)} active, "
              f"{marginal_ok}/{len(marginal_to_update)} monitoring, "
              f"{weak_ok}/{len(weak_to_update)} retired, "
              f"{permanent_ok}/{len(permanent_to_update)} rejected")
    for name in sorted(permanent_to_update):
        _log.critical(f"  [REJECTED] {name} — {reasons[name]}")
    for name in sorted(weak_to_update):
        _log.info(f"  [RETIRED]  {name} — {reasons[name]}")
    for name in sorted(marginal_to_update):
        _log.info(f"  [MONITOR]  {name} — {reasons[name]}")
    for name in sorted(active_to_update):
        _log.info(f"  [ACTIVE]   {name} — {reasons[name]}")

    conn.close()
    return {
        "rejected": sorted(permanent_to_update),
        "retired": sorted(weak_to_update),
        "monitoring": sorted(marginal_to_update),
        "active": sorted(active_to_update),
        "unchanged": len(current_active),
    }
