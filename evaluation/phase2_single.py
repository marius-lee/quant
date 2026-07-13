"""Stage 2: 单因子检验 — IC / |t| / ICIR / half-life 四维过滤。"""

import json
import time
import traceback
import numpy as np
import pandas as pd
import sqlite3
from config.constants import _require_cfg
from utils.logger import get_logger, set_trace_id
from factor.stats_cache import compute_factor_stats


def screen_factors(input_json: str = None, output_json: str = None,
                   prefilter_from_diagnostics: bool = True) -> dict:
    """运行单因子检验, 返回通过/未通过因子列表。

    Parameters
    ----------
    prefilter_from_diagnostics: 是否从 diagnostics 预筛因子 (两步架构).
        True → 只评估最近一次诊断的 keep/boost 因子.
        False → 评估全部 backtesting 因子.

    Returns
    -------
    dict with keys: passed, ic_means, ic_irs, decay, failed
    """
    import uuid; tid = uuid.uuid4().hex[:12]; set_trace_id(tid)
    logger = get_logger("evaluation.phase2")
    t0 = time.monotonic()
    logger.info(f"Phase 2 [{tid}] start — single-factor screening")

    # ── 数据健康检查: 读 Phase 1 ──
    from evaluation.run_store import load_latest
    p1 = load_latest("phase1")
    if p1 is None:
        logger.warning("Phase 2: no Phase 1 data in evaluation_runs — proceeding anyway")
    else:
        db_status = p1.get("db_status", "unknown")
        n_stocks = p1.get("n_stocks", 0)
        logger.info("Phase 2: DB status=%s, universe=%d stocks", db_status, n_stocks)
        if db_status == "degraded":
            logger.error("Phase 2: DB degraded — aborting. Check data/store.py sync.")
            return {"passed": [], "failed": {}, "ic_means": {}, "ic_irs": {}, "ic_series": {}, "n_factors": 0}

    # ── 阈值来源: config.yaml ──
    t_threshold = _require_cfg("factor.evaluation.t_threshold")
    min_abs_ic = _require_cfg("factor.evaluation.min_abs_ic")
    min_icir = _require_cfg("factor.evaluation.min_icir")
    min_half_life = _require_cfg("factor.evaluation.min_half_life")
    n_days = _require_cfg("factor.evaluation.n_days")

    logger.info(f"Phase 2 thresholds: |IC|≥{min_abs_ic}, |t|≥{t_threshold}, "
          f"ICIR≥{min_icir}, half-life≥{min_half_life}d")

    # 获取 backtesting 因子 (两步架构: 可选诊断预筛)
    from factor.compute import get_factor_names
    all_backtesting = get_factor_names(status_filter="backtesting")

    if prefilter_from_diagnostics:
        from evaluation.run_store import load_latest
        diag = load_latest("diagnostics")
        if diag and diag.get("passed"):
            diag_passed = set(diag["passed"])
            active_names = [n for n in all_backtesting if n in diag_passed]
            logger.info("Phase 2: diagnostics pre-filter %d -> %d factors",
                        len(all_backtesting), len(active_names))
        else:
            active_names = all_backtesting
            logger.info("Phase 2: no diagnostics data — using all %d backtesting factors",
                        len(active_names))
    else:
        active_names = all_backtesting
        logger.info(f"Phase 2: --all mode — evaluating all {len(active_names)} backtesting factors")

    # 计算因子统计
    n_symbols = _require_cfg("factor.evaluation.n_symbols")
    lookback = _require_cfg("factor.evaluation.lookback")
    stats = compute_factor_stats(
        n_symbols=n_symbols if n_symbols > 0 else None,
        lookback=lookback,
        factor_names=active_names,
    )

    factor_names = stats["factor_keys"]
    ic_means = dict(zip(factor_names, stats["ic"]))
    meta = stats.get("meta", {})
    ic_irs = dict(zip(factor_names, stats["ic_ir"]))
    decay = stats.get("decay", {})
    ic_series_dict = stats.get("ic_series", {})

    # ── Phase 2 评估 ──
    passed = []
    failed = {}

    for name in factor_names:
        ic = abs(ic_means.get(name, 0.0))
        ir = abs(ic_irs.get(name, 0.0))
        # t-stat: |IR| × √N (Lo 2002, Grinold & Kahn 1999)
        t_stat = ir * np.sqrt(n_days)
        reasons = []

        if ic < min_abs_ic:
            reasons.append(f"|IC|={ic:.4f}<{min_abs_ic}")
        if t_stat < t_threshold:
            reasons.append(f"|t|={t_stat:.1f}<{t_threshold}")
        if ir < min_icir:
            reasons.append(f"ICIR={ir:.2f}<{min_icir}")

        # IC half-life: days until IC drops to half
        # decay = {name: [1d_val, 5d_val, 20d_val]}
        ic_1d = abs(ic_means.get(name, 0.0))
        decay_vals = decay.get(meta.get(name, {}).get("display", name), [0.0, 0.0, 0.0])
        ic_20d = abs(decay_vals[2]) if len(decay_vals) > 2 else 0.0
        half_life_est = 0
        if ic_1d > 0.001 and ic_20d > 0:
            ratio_20 = ic_20d / ic_1d
            if ratio_20 >= 1.0:
                # IC 不衰减甚至增强 — 无半衰期问题，跳过检查
                half_life_est = 999
            elif ratio_20 > 0:
                half_life_est = int(-20 / np.log(max(ratio_20, 0.01)))
        if 0 < half_life_est < min_half_life and ic_1d >= min_abs_ic:
            reasons.append(f"half-life={half_life_est}d<{min_half_life}")

        if not reasons:
            passed.append(name)
        else:
            failed[name] = reasons

    # ── 输出 ──
    logger.info(f"Phase 2 results: {len(passed)} passed, {len(failed)} failed\n")

    logger.info("=== PASSED ===")
    for name in sorted(passed, key=lambda n: abs(ic_means.get(n, 0.0)), reverse=True):
        ic = ic_means.get(name, 0.0)
        ir = ic_irs.get(name, 0.0)
        t = abs(ir) * np.sqrt(n_days)
        decay_vals = decay.get(meta.get(name, {}).get("display", name), [0.0, 0.0, 0.0])
        ic_20 = abs(decay_vals[2]) if len(decay_vals) > 2 else 0.0
        ratio = ic_20 / max(abs(ic), 0.001) if ic else 0
        hl = int(-20 / np.log(max(ratio, 0.01))) if ratio > 0 else 0
        logger.info(f"  ✓ {name:30s} IC={ic:+.4f}  t={t:.1f}  IR={ir:+.2f}  HL≈{hl}d")

    if failed:
        logger.info("=== FAILED ===")
        for name, reasons in sorted(failed.items()):
            logger.info(f"  ✗ {name:30s} {'; '.join(reasons)}")

    result = {
        "passed": passed,
        "failed": {k: list(v) for k, v in failed.items()},
        "ic_means": {k: float(v) for k, v in ic_means.items()},
        "ic_irs": {k: float(v) for k, v in ic_irs.items()},
        "decay": {k: [float(x) for x in v] for k, v in decay.items()} if decay else {},
    }

    # 持久化到 evaluation_runs (纯 DB, 无临时文件, 不减 ic_series)

    # 持久化到 evaluation_runs (精简: 不含 ic_series — Phase 3 自行重算)
    from evaluation.run_store import save_phase
    slim = dict(result)
    slim.pop("ic_series", None)
    slim.pop("ic_series_dict", None)
    slim["n_factors"] = len(active_names)
    save_phase("phase2", slim)
    logger.info("Phase 2 saved to evaluation_runs (%d factors, ~%d bytes)",
                 slim["n_factors"], len(json.dumps(slim, default=str)))

    logger.info(f"Phase 2 complete ({time.monotonic()-t0:.1f}s). {len(passed)} factors advance to Phase 3.")
    return result
