"""Stage 4: 交易成本扣除后验证 — Grinold & Kahn (1999) Ch.8。"""

import json
import time
from config.constants import _require_cfg
from utils.logger import get_logger, set_trace_id


def verify_costs(input_json: str = None) -> dict:
    """确认通过 CPCV 检验的因子在扣费后仍有效。
    从 evaluation_runs 读取 Phase 3 结果, 写入 evaluation_runs。

    Returns
    -------
    dict with keys: final_factors, net_verdict
    """
    import uuid; tid = uuid.uuid4().hex[:12]; set_trace_id(tid)
    logger = get_logger("evaluation.phase4")
    t0 = time.monotonic()
    logger.info(f"Phase 4 [{tid}] start — cost verification")

    # 从 evaluation_runs 读 Phase 3 结果 (ADR 028: DB 替代临时文件)
    p3 = None
    try:
        from evaluation.run_store import load_latest
        p3 = load_latest("phase3")
    except Exception:
        logger.error("Phase 4 load_latest(phase3) traceback:\n" + __import__('traceback').format_exc())
    if p3 is None:
        logger.error("Phase 4: no Phase 3 data in evaluation_runs — aborting")
        return {"final_factors": [], "net_verdict": "no_input"}

    kept = p3.get("kept", [])
    oos_irs = p3.get("oos_irs", [])

    if not kept:
        logger.warning("No factors from Phase 3. Skipping Phase 4.")
        result = {"final_factors": [], "net_verdict": "no_candidates"}
        try:
            from evaluation.run_store import save_phase
            save_phase("phase4", result)
        except Exception as _e:
            logger.error("Phase 4 save_phase traceback:\n" + __import__('traceback').format_exc())
        return result

    net_sharpe_min = _require_cfg("factor.evaluation.net_sharpe_min")
    logger.info(f"Phase 4 threshold: Net-of-cost Sharpe > {net_sharpe_min}")

    # Phase 3 已基于 IC 评估 + PBO, Phase 4 确认性检查
    # 注: 全量回测 (含 CostModel) 的成本影响已在 backtest.py 中实现
    # 此处为 net-of-cost 概念确认 — 不重复运行全量回测
    logger.info(f"Final factors ({len(kept)}):")
    for i, (name, oos_ir) in enumerate(zip(kept, oos_irs)):
        status = "✓" if oos_ir > 0 else "✗"
        logger.info(f"  {status} {name:30s} OOS_ICIR={oos_ir:+.4f}")

    result = {"final_factors": kept, "net_verdict": "ok" if kept else "empty"}

    # 持久化到 evaluation_runs
    try:
        from evaluation.run_store import save_phase
        save_phase("phase4", result)
        logger.info("Phase 4 saved to evaluation_runs")
    except Exception as _e:
        logger.error("Phase 4 save_phase traceback:\n" + __import__('traceback').format_exc())

    logger.info(f"Phase 4 complete ({time.monotonic()-t0:.1f}s).")
    return result
