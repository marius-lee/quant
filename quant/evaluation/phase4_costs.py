"""Stage 4: 交易成本扣除后验证 — Grinold & Kahn (1999) Ch.8。

IC -> Sharpe: Sharpe = ICIR * sqrt(breadth)  (GK99 Eq.6.5)
breadth = N_positions * rebalances_per_year (monthly = *12)

扣费估算 (v573 对齐回测执行层):
  - 往返成本统一复用 quant.execution.cost.CostModel.from_config().round_trip_cost_pct()
    = (佣金 + 滑点) × 2 + 印花税(卖) ≈ 0.31% (A股万三佣/千一滑/千五印花口径)
  - 与回测 loop.py:471 CostModel.from_config() 同口径, 避免筛选/执行成本背离

来源: Grinold & Kahn (1999) Ch.8; Almgren & Chriss (2001) 成本结构; 国内券商惯例
"""

import json
import time
import math
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger, set_trace_id


def verify_costs(input_json: str = None) -> dict:
    """ICIR -> 估净 Sharpe, 应用 net_sharpe_min 阈值过滤。

    result["dropped"]: Phase 3 通过但 Phase 4 净 Sharpe 不达标的因子列表。
    这不是 factor_registry.status 的新值。Phase 5 sync 会将这些因子
    标记为 rejected, reason="Phase 4: net-of-costs Sharpe too low"。

    从 evaluation_runs 读取 Phase 3 结果, 过滤后写入 evaluation_runs。

    Returns dict: final_factors, dropped, sharpe_estimates, net_verdict
    """
    import uuid; tid = uuid.uuid4().hex[:12]; set_trace_id(tid)
    logger = get_logger("evaluation.phase4")
    t0 = time.monotonic()
    logger.info(f"Phase 4 [{tid}] start — cost verification (ICIR->net Sharpe)")

    # 从 evaluation_runs 读 Phase 3 结果 (ADR 028: DB)
    from quant.evaluation.run_store import load_latest
    p3 = load_latest("phase3")
    if p3 is None:
        logger.error("Phase 4: no Phase 3 data in evaluation_runs — aborting")
        return {"final_factors": [], "dropped": [], "net_verdict": "no_input"}

    kept = p3.get("kept", [])
    oos_irs = p3.get("oos_irs", [])
    phase3_note = p3.get("note", "")

    # insufficient_data from Phase 3: OOS_ICIR=0.0 is a placeholder, not a real value
    # No data means no ICIR → cannot compute net Sharpe → pass through to Phase 5 as monitoring
    insufficient_data = bool(phase3_note and "insufficient_data" in phase3_note)

    if not kept:
        logger.warning("No factors from Phase 3. Skipping Phase 4.")
        result = {"final_factors": [], "dropped": [], "net_verdict": "no_candidates"}
        from quant.evaluation.run_store import save_phase
        save_phase("phase4", result)
        return result

    # 模型参数
    net_sharpe_min = _require_cfg("factor.evaluation.net_sharpe_min")
    n_positions = _require_cfg("alpha.sleeve.positions_per_factor")

    # v573 fix (2026-08-30): 往返成本统一复用执行层 CostModel, 与回测 loop.py 同口径
    # (Grinold & Kahn 1999 Ch.8 扣费口径). 原实现把 execution.impact_eta(=0.1, 实为
    # sqrt 冲击系数 10bp) 平加成 10% 往返成本 → annual_cost_pct≈122% (换手12×10.2%),
    # 任何因子 net_Sharpe 被砍至负 → Phase 4 实质全拒 (过严 ~33x vs 回测 CostModel≈0.31%).
    # 改读 CostModel.round_trip_cost_pct() ≈ 0.31%, 与回测执行成本对齐.
    from quant.execution.cost import CostModel
    round_trip_cost = CostModel.from_config().round_trip_cost_pct()

    # breadth: 独立下注次数/年 (GK99 Eq.6.5), 月度调仓
    rebalances_per_year = 12
    breadth = n_positions * rebalances_per_year

    # 年化波动率: A股 ~28% (沪深300 长期历史)
    annual_vol = 0.28

    logger.info(f"Phase 4 parameters: breadth={breadth}, round_trip_cost={round_trip_cost:.3%}, "
                f"annual_vol={annual_vol:.0%}, net_sharpe_min={net_sharpe_min}")

    # ICIR -> 净 Sharpe 估算 (GK99 Ch.8)
    final_factors = []
    marginal = []
    dropped = []
    sharpe_estimates = {}

    for i, (name, oos_ir) in enumerate(zip(kept, oos_irs)):
        abs_ir = abs(oos_ir)

        if insufficient_data and abs_ir < 1e-10:
            # Phase 3 skipped OOS; OOS_ICIR=0.0 is a placeholder
            # Skip net Sharpe calculation but keep factor for Phase 5 monitoring
            sharpe_estimates[name] = {
                "gross_sharpe": 0.0,
                "annual_cost_pct": 0.0,
                "net_sharpe": 0.0,
                "note": "OOS_ICIR not yet available — insufficient IC history for CPCV. Awaiting more data."
            }
            final_factors.append(name)
            dropped.append(name)  # Also in dropped so Phase 5 knows it needs monitoring
            logger.info(f"  ~ {name:30s} OOS_ICIR=placeholder (insufficient IC history) — pass through to monitoring")
            continue

        # 毛 Sharpe = ICIR * sqrt(breadth) (GK99 Eq.6.5)
        # v535 口径说明 (审计项7): oos_ir 为**日频** ICIR (phase3 未年化),
        # √breadth 同时承担 breadth 年化 (N持仓×12月调仓=240 次下注/年,
        # √240≈15.5) — 数值上 ≈ √annual_days(√244≈15.6) 巧合, 故 gross
        # 已≈完整 GK99 的 ICIR_annual. 严禁在此再乘 √annual_days —
        # 那才是"双重年化高估" (ICIR 年化 ×√244 又乘 breadth √240 ≈ ×242).
        # 年化常量统一引用 config (tear_sheet/stats_cache 同源).
        gross_sharpe = abs_ir * math.sqrt(breadth)

        # 年化换手率估算: 每月 100% -> 12 次/年
        annual_turnover = rebalances_per_year
        annual_cost_pct = annual_turnover * round_trip_cost

        # 净 Sharpe = 毛 Sharpe - 年化成本/年化波动
        net_sharpe_est = gross_sharpe - annual_cost_pct / annual_vol

        sharpe_estimates[name] = {
            "gross_sharpe": round(gross_sharpe, 3),
            "annual_cost_pct": round(annual_cost_pct * 100, 2),
            "net_sharpe": round(net_sharpe_est, 3),
        }

        if net_sharpe_est >= net_sharpe_min:
            final_factors.append(name)
            logger.info(f"  + {name:30s} OOS_ICIR={oos_ir:+.4f}  "
                       f"gross_SR={gross_sharpe:.2f}  net_SR={net_sharpe_est:.2f}")
        else:
            dropped.append(name)
            logger.info(f"  x {name:30s} OOS_ICIR={oos_ir:+.4f}  "
                       f"gross_SR={gross_sharpe:.2f}  net_SR={net_sharpe_est:.2f} < {net_sharpe_min} — DROPPED")

    result = {
        "final_factors": final_factors,
        "dropped": dropped,
        "sharpe_estimates": sharpe_estimates,
        "net_verdict": "ok" if final_factors else "empty",
        "insufficient_data": insufficient_data,
    }
    if phase3_note:
        result["note"] = phase3_note

    from quant.evaluation.run_store import save_phase
    result["n_factors"] = len(kept)
    save_phase("phase4", result)
    logger.info("Phase 4 saved to evaluation_runs")

    logger.info(f"Phase 4 complete ({time.monotonic()-t0:.1f}s). "
                f"{len(final_factors)}/{len(kept)} factors net-of-cost viable.")
    return result
