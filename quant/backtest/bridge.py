"""评估→回测桥接 — 从 evaluation_runs 读取评估结果, 输出回测所需参数。

设计原则:
  - 评估结果已入库 (evaluation_runs), 桥接模块只读不写
  - 零 fallback: 评估未完成时直接 raise, 不静默降级
  - 不依赖临时文件, 所有数据走 DB

对标: Quantopian Research → Backtesting 工作流
"""

from quant.utils.logger import get_logger
_log = get_logger("backtest.bridge")


def evaluation_to_backtest() -> tuple[list[str], dict]:
    """从 evaluation_runs 读取最新评估结果, 返回 (factor_names, ic_map)。

    优先级: Phase 3 kept > Phase 2 passed (Phase 3 是 Phase 2 的超集筛选)
    若评估管线未运行, 直接 raise ValueError (fail-fast, 零 fallback)。

    Returns:
        factor_names: 通过评估的因子名列表
        ic_map: {factor_name: {ic_mean, ic_ir, weight}} IC 权重映射
    """
    from quant.evaluation.run_store import load_latest

    p2 = load_latest("phase2")
    p3 = load_latest("phase3")

    # ── 因子名: Phase 3 优先 ──
    factor_names = None
    if p3 and p3.get("kept"):
        factor_names = p3["kept"]
        _log.info("bridge: using Phase 3 kept (%d factors)", len(factor_names))
    elif p2 and p2.get("passed"):
        factor_names = p2["passed"]
        _log.info("bridge: using Phase 2 passed (%d factors, Phase 3 not run)", len(factor_names))

    if not factor_names:
        raise ValueError(
            "bridge: no evaluation results found in evaluation_runs. "
            "Run eval_standard.sh first to populate Phase 2/3 results."
        )

    # ── IC 权重: 从 Phase 2 提取 ──
    ic_map = {}
    if p2:
        ic_means = p2.get("ic_means", {})
        ic_irs = p2.get("ic_irs", {})
        for name in factor_names:
            if name in ic_means:
                ic_mean = ic_means[name]
                ic_map[name] = {
                    "ic_mean": ic_mean,
                    "ic_ir": ic_irs.get(name, 0),
                    "weight": abs(ic_mean),
                }
    if not ic_map:
        raise ValueError(
            "bridge: Phase 2 has no IC weights for the selected factors. "
            "Re-run Phase 2 evaluation."
        )

    _log.info("bridge: resolved %d factors with IC weights", len(ic_map))
    return factor_names, ic_map
