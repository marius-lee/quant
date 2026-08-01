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
        return {"permanent": [], "archived": [], "probation": [], "active": [], "unchanged": 0}

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
        return {"permanent": [], "archived": [], "probation": [], "active": [], "unchanged": 0, "circuit_breaker": True}

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
    p2_active = set(p2.get("active", []))
    p2_probation = set(p2.get("probation", []))
    p2_archived = set(p2.get("archived", {}).keys()) if isinstance(p2.get("archived"), dict) else set(p2.get("archived", []))

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

    # ── 综合裁决 — 与状态机四态对齐 ──
    certified_active = set()
    factors_to_probation = set()
    factors_to_archived = set()
    factors_permanent = set()

    # 数据不足: Phase 3 因数据太少跳过 OOS 验证的因子 → probation
    insufficient_data_factors = set()
    if (p3_note and "insufficient_data" in p3_note) or p4_insufficient:
        insufficient_data_factors = p2_active

    # 对每个 backtesting 池中的因子逐个裁决
    all_evaluated = p2_active | p2_probation | p2_archived
    reasons = {}  # factor_name → reason_str
    for fname in sorted(all_evaluated):
        # 跳过 diagnostics 排除的因子 (保留原状态)
        if fname not in all_evaluated:
            continue

        p2_status = ("pass" if fname in p2_active
                     else "marginal" if fname in p2_probation
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
            factors_to_probation.add(fname)
            reasons[fname] = (
                f"[EVAL] Phase 3: OOS validation skipped — IC history too short for CPCV. "
                f"IC={p2_ic_vals.get(fname, 0):+.4f}. Awaiting more data for re-evaluation."
            )
        elif p2_status == "pass" and p3_status == "pass" and p4_status == "pass":
            certified_active.add(fname)
            reasons[fname] = "[EVAL] passed Phase 2+3+4 (full evaluation)"
        elif p2_status == "fail":
            factors_to_archived.add(fname)
            reasons[fname] = f"[EVAL] Phase 2: IC/ICIR below all thresholds"
        elif p3_status == "fail":
            factors_to_archived.add(fname)
            reasons[fname] = f"[EVAL] Phase 3: CPCV OOS_ICIR<0 or PBO>threshold"
        elif p4_status == "fail":
            factors_to_archived.add(fname)
            reasons[fname] = f"[EVAL] Phase 4: net-of-costs Sharpe too low"
        else:
            # any marginal → monitoring
            factors_to_probation.add(fname)
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
    for fname in sorted(factors_to_archived):
        new_retry = retry_map.get(fname, 0) + 1
        retry_map[fname] = new_retry
        if new_retry >= max_retries:
            factors_permanent.add(fname)
            reasons[fname] = f"[EVAL] 累计 {new_retry} 次 retired (≥{max_retries}), 永久淘汰"
            _log.critical(f"[EVAL] {fname}: retired → rejected (retry_count={new_retry}≥{max_retries})")
        else:
            reasons[fname] = f"{reasons[fname]} (retry={new_retry}/{max_retries})"
            _log.info(f"[EVAL] {fname}: retired (retry_count={new_retry}/{max_retries})")

    # 对于本轮判定为 active/monitoring 的因子, retry_count 不递增
    # 但如果之前有 retired → 通过后续评估 → 重置 retry_count = 0
    for fname in sorted(certified_active | factors_to_probation):
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
    monitoring_to_update = factors_to_probation - current_active
    retired_to_update = factors_to_archived - factors_permanent - current_active
    rejected_to_update = factors_permanent - current_active

    # ── 通过 StateManager 转换 (零 fallback: 非法转换→ValueError) ──
    active_ok = fsm.batch_transition(
        list(active_to_update), "EVAL_PASS",
        reason="[EVAL] passed Phase 2+3+4 (full evaluation)"
    )
    monitoring_ok = 0
    for name in monitoring_to_update:
        try:
            fsm.transition(name, "EVAL_MARGINAL", reasons[name])
            monitoring_ok += 1
        except Exception as e:
            _log.warning("sync_factor_status: %s EVAL_MARGINAL failed: %s", name, e)
    retired_ok = 0
    for name in retired_to_update:
        new_retry = retry_map.get(name, 0) + 1
        # test-v306: probation 因子不能走 EVAL_FAIL (状态机只允许 evaluating→EVAL_FAIL).
        # 已在实盘的 probation 因子应走 IC_PERSISTENT 归档.
        current = f_repo.get_factor_by_name(name)
        current_status = current["status"] if current else "evaluating"
        event = "EVAL_FAIL" if current_status == "evaluating" else "IC_PERSISTENT"
        try:
            fsm.transition(name, event, reasons[name], retry_count=new_retry)
            retired_ok += 1
        except Exception as e:
            _log.warning("sync_factor_status: %s %s failed: %s", name, event, e)
    rejected_ok = 0
    for name in rejected_to_update:
        new_retry = retry_map.get(name, 0) + 1
        try:
            fsm.transition(name, "EVAL_REJECT", reasons[name], retry_count=new_retry)
            rejected_ok += 1
        except Exception as e:
            _log.warning("sync_factor_status: %s EVAL_REJECT failed: %s", name, e)

    _log.info(f"sync_factor_status: {active_ok}/{len(active_to_update)} active, "
              f"{monitoring_ok}/{len(monitoring_to_update)} monitoring, "
              f"{retired_ok}/{len(retired_to_update)} retired, "
              f"{rejected_ok}/{len(rejected_to_update)} rejected")
    for name in sorted(rejected_to_update):
        _log.critical(f"  [REJECTED] {name} — {reasons[name]}")
    for name in sorted(retired_to_update):
        _log.info(f"  [RETIRED]  {name} — {reasons[name]}")
    for name in sorted(monitoring_to_update):
        _log.info(f"  [MONITOR]  {name} — {reasons[name]}")
    for name in sorted(active_to_update):
        _log.info(f"  [ACTIVE]   {name} — {reasons[name]}")

    conn.close()
    return {
        "rejected": sorted(rejected_to_update),
        "retired": sorted(retired_to_update),
        "monitoring": sorted(monitoring_to_update),
        "active": sorted(active_to_update),
        "unchanged": len(current_active),
    }
