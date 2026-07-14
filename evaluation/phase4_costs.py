"""Stage 4: 交易成本扣除后验证 — Grinold & Kahn (1999) Ch.8。

IC -> Sharpe: Sharpe = ICIR * sqrt(breadth)  (GK99 Eq.6.5)
breadth = N_positions * rebalances_per_year (monthly = *12)

扣费估算:
  - 单边佣金: config execution.commission (0.03%)
  - 印花税: 0.05% (卖出单向, 2025年起)
  - 冲击成本: config execution.impact_eta
  - 往返 = 2*佣金 + 印花税(卖) + 冲击 + 滑点

来源: Grinold & Kahn (1999) Ch.8; 国内券商因子研报惯例
"""

import json
import time
import math
from config.constants import _require_cfg
from utils.logger import get_logger, set_trace_id


def verify_costs(input_json: str = None) -> dict:
    """ICIR -> 估净 Sharpe, 应用 net_sharpe_min 阈值过滤。

    从 evaluation_runs 读取 Phase 3 结果, 过滤后写入 evaluation_runs。

    Returns dict: final_factors, dropped, sharpe_estimates, net_verdict
    """
    import uuid; tid = uuid.uuid4().hex[:12]; set_trace_id(tid)
    logger = get_logger("evaluation.phase4")
    t0 = time.monotonic()
    logger.info(f"Phase 4 [{tid}] start — cost verification (ICIR->net Sharpe)")

    # 从 evaluation_runs 读 Phase 3 结果 (ADR 028: DB)
    from evaluation.run_store import load_latest
    p3 = load_latest("phase3")
    if p3 is None:
        logger.error("Phase 4: no Phase 3 data in evaluation_runs — aborting")
        return {"final_factors": [], "dropped": [], "net_verdict": "no_input"}

    kept = p3.get("kept", [])
    oos_irs = p3.get("oos_irs", [])

    if not kept:
        logger.warning("No factors from Phase 3. Skipping Phase 4.")
        result = {"final_factors": [], "dropped": [], "net_verdict": "no_candidates"}
        from evaluation.run_store import save_phase
        save_phase("phase4", result)
        return result

    # 模型参数
    net_sharpe_min = _require_cfg("factor.evaluation.net_sharpe_min")
    n_positions = _require_cfg("alpha.sleeve.positions_per_factor")
    commission_rate = _require_cfg("execution.commission")
    impact_eta = _require_cfg("execution.impact_eta")

    # 印花税 (卖出单向 0.05%, 2025年起) + 滑点估算
    stamp_tax = 0.0005
    slippage_est = 0.001

    # 往返成本 (买入+卖出)
    round_trip_cost = 2 * commission_rate + stamp_tax + impact_eta + slippage_est

    # breadth: 独立下注次数/年 (GK99 Eq.6.5), 月度调仓
    rebalances_per_year = 12
    breadth = n_positions * rebalances_per_year

    # 年化波动率: A股 ~28% (沪深300 长期历史)
    annual_vol = 0.28

    logger.info(f"Phase 4 parameters: breadth={breadth}, round_trip_cost={round_trip_cost:.3%}, "
                f"annual_vol={annual_vol:.0%}, net_sharpe_min={net_sharpe_min}")

    # ICIR -> 净 Sharpe 估算 (GK99 Ch.8)
    final_factors = []
    dropped = []
    sharpe_estimates = {}

    for i, (name, oos_ir) in enumerate(zip(kept, oos_irs)):
        abs_ir = abs(oos_ir)

        # 毛 Sharpe = ICIR * sqrt(breadth) (GK99 Eq.6.5)
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
    }

    from evaluation.run_store import save_phase
    result["n_factors"] = len(kept)
    save_phase("phase4", result)
    logger.info("Phase 4 saved to evaluation_runs")

    logger.info(f"Phase 4 complete ({time.monotonic()-t0:.1f}s). "
                f"{len(final_factors)}/{len(kept)} factors net-of-cost viable.")
    return result
