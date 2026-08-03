#!/usr/bin/env python3
"""独立因子诊断模块 — 基于 IC 评估生成 Phase 2 预筛数据.

在不运行全量回测时, 直接从 factor_cache 计算 backtesting 池因子的 IC,
筛选 keep/boost 因子, 写入 evaluation_runs.diagnostics.

用法:
    cd /Users/mariusto/project/quant
    .venv/bin/python scripts/run_diagnostics.py
"""
import time, json
from quant.utils.logger import get_logger

_log = get_logger("diagnostics")


def main():
    t0 = time.monotonic()
    _log.info("run_diagnostics: start")

    from quant.factor.compute import get_factor_names
    from quant.factor.stats_cache import compute_factor_stats
    from quant.evaluation.run_store import save_phase
    from quant.config.constants import _require_cfg

    # 1. 获取 backtesting 池因子
    factor_names = get_factor_names(status_filter="backtesting")
    _log.info("run_diagnostics: %d backtesting factors", len(factor_names))

    # 2. 计算 IC 统计
    stats = compute_factor_stats(
        status_filter="backtesting",
        n_symbols=800,
        lookback=120,
    )
    ic_means = dict(zip(stats.get("factor_keys", []), stats.get("ic", [])))
    ic_irs = dict(zip(stats.get("factor_keys", []), stats.get("ic_ir", [])))

    # 3. 筛选 keep/boost (ICIR > diagnostics_min_icir)
    min_icir = _require_cfg("factor.evaluation.diagnostics_min_icir")
    passed = []
    factor_report = {}
    for name in factor_names:
        ic_mean = ic_means.get(name, 0.0)
        ic_ir = abs(ic_irs.get(name, 0.0))
        if ic_ir >= min_icir:
            passed.append(name)
            rec = "keep"
        elif ic_ir > 0.05:
            rec = "review"
        else:
            rec = "drop"
        factor_report[name] = {
            "recommendation": rec,
            "ic_mean": round(ic_mean, 4),
            "ic_ir": round(ic_ir, 2),
        }

    # 4. 写入 evaluation_runs
    save_phase("diagnostics", {
        "n_factors": len(factor_names),
        "passed": passed,
        "factor_report": factor_report,
        "summary": f"Diagnostics: {len(passed)}/{len(factor_names)} passed (ICIR>={min_icir})",
    })

    elapsed = time.monotonic() - t0
    _log.info("run_diagnostics: %d/%d passed in %.1fs", len(passed), len(factor_names), elapsed)
    print(f"Diagnostics: {len(passed)}/{len(factor_names)} passed in {elapsed:.0f}s")


if __name__ == "__main__":
    main()
