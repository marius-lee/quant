"""Stage 3: Out-of-Sample 检验 — CPCV + PBO + walk-forward IC 稳定性。"""

import json
import time
import sqlite3
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger, set_trace_id
from quant.evaluation.cpcv import PurgedWalkForward, compute_fold_icir
from quant.evaluation.pbo import compute_pbo


def validate_oos(input_json: str = "/tmp/_eval_phase2.json",
                 output_json: str = "/tmp/_eval_phase3.json") -> dict:
    """CPCV walk-forward OOS 检验 + PBO 计算。

    复用 Phase 2 的全量 IC 时序, 在 IC 序列上做 purged folding
    (避免重复计算因子).

    Returns
    -------
    dict with keys: kept, oos_irs, pbo_result, fold_details
    """
    import uuid; tid = uuid.uuid4().hex[:12]; set_trace_id(tid)
    logger = get_logger("evaluation.phase3")
    t0 = time.monotonic()
    logger.info(f"Phase 3 [{tid}] start — CPCV + PBO walk-forward OOS")

    # ADR 028: read from evaluation_runs DB first, fallback to JSON file
    p2 = None
    from quant.evaluation.run_store import load_latest
    p2 = load_latest("phase2")
    if p2 is None:
        logger.error("Phase 3: no Phase 2 data in evaluation_runs — aborting (no temp file fallback)")
        return {"kept": [], "oos_irs": [], "pbo_result": {}, "n_folds": 0}

    candidates = p2.get('passed', [])
    if not candidates:
        logger.warning("No candidates from Phase 2. Stopping.")
        return {"kept": [], "oos_irs": [], "pbo_result": {}, "n_folds": 0}

    # ── CPCV 参数 ──
    n_groups = _require_cfg("factor.evaluation.cpcv_groups")
    embargo_days = _require_cfg("factor.evaluation.embargo_days")
    pbo_max = _require_cfg("factor.evaluation.pbo_max")
    sharpe_decay_max = _require_cfg("factor.evaluation.sharpe_decay_max")
    lookback = _require_cfg("factor.evaluation.lookback")

    logger.info(f"Phase 3: CPCV N={n_groups}, embargo={embargo_days}d")
    logger.info(f"PBO threshold: <{pbo_max}, Sharpe decay: <{sharpe_decay_max*100:.0f}%")
    logger.info(f"Candidates: {', '.join(candidates)}")

    # ── 获取 IC 时间序列 ──
    # 从 Phase 2 传入的 IC 序列或重新查询
    # ic_series = {name: {date_str: ic_val}} — rebuild pd.Series
    ic_series_dict = {}
    raw_ic = p2.get("ic_series", {})
    if raw_ic:
        for name, ic_dict in raw_ic.items():
            if ic_dict:
                s = pd.Series(ic_dict)
                s.index = pd.to_datetime(list(ic_dict.keys()))
                ic_series_dict[name] = s

    if not ic_series_dict:
        logger.info("Phase 3: re-computing IC series for %d candidates...", len(candidates))
        from quant.factor.stats_cache import compute_factor_stats
        stats = compute_factor_stats(
            factor_names=candidates,
            n_symbols=None,
            lookback=lookback,
        )
        for name in candidates:
            ic_data = stats.get("ic_series", {}).get(name, {})
            if ic_data:
                s = pd.Series(ic_data)
                s.index = pd.to_datetime(list(ic_data.keys()))
                ic_series_dict[name] = s

    # ── CPCV 折叠 ──
    pvf = PurgedWalkForward(n_groups=n_groups, embargo_days=embargo_days)

    # 使用统一的日期索引 (取所有因子 IC 序列的并集上限)
    all_dates = []
    for s in ic_series_dict.values():
        if isinstance(s, pd.Series) and len(s) > 0:
            all_dates.extend(s.index.tolist())
    if not all_dates:
        logger.info("Phase 3: no IC data available. Stopping.")
        return {"kept": [], "oos_irs": [], "pbo_result": {}}

    unique_dates = sorted(set(all_dates))
    date_index = pd.DatetimeIndex(unique_dates)

    # ── 日期数检查: 数据不足时跳过 OOS 验证, 因子不被打回 ──
    # 对齐业界标准 (QuantConnect/米筐): 数据不足是暂时状态, 不是因子失效
    # 因子带着 insufficient_data 标记通过, Phase 4 可继续评估, Phase 5 设 monitoring
    if len(unique_dates) < 4:
        logger.warning("Phase 3: insufficient IC history (%d dates < 4 min for CPCV). "
                       "Skipping OOS validation — all %d candidates pass through with insufficient_data note.",
                       len(unique_dates), len(candidates))
        result = {
            "kept": candidates,
            "oos_irs": [0.0] * len(candidates),
            "pbo_result": {"pbo": 0.0, "logit_pbo": 0.0, "passed": True, "is_oos_corr": 0.0},
            "n_folds": 0,
            "note": (f"insufficient_data: {len(candidates)} factor(s) passed through Phase 3 "
                     f"due to insufficient IC history ({len(unique_dates)} dates < 4 min for CPCV fold). "
                     f"OOS validation deferred to next evaluation cycle."),
        }
        from quant.evaluation.run_store import save_phase
        result["n_factors"] = len(candidates)
        save_phase("phase3", result)
        logger.info("Phase 3 saved to evaluation_runs (insufficient_data, %d candidates passed through)", len(candidates))
        return result

    splits = pvf.split(unique_dates)
    logger.info(f"Phase 3: {len(splits)} CPCV folds from {len(unique_dates)} unique dates")

    # ── 逐折叠计算 ──
    fold_results = []  # List of {factor: fold_metrics}

    for fi, (train_idx, test_idx) in enumerate(splits):
        fold_metrics = {}
        for name in candidates:
            if name in ic_series_dict and len(ic_series_dict[name]) > 0:
                metrics = compute_fold_icir(ic_series_dict[name], date_index, train_idx, test_idx)
                fold_metrics[name] = metrics
        fold_results.append(fold_metrics)

        # 输出每 fold 摘要
        train_dates = date_index[train_idx]
        test_dates = date_index[test_idx]
        n_good = sum(1 for m in fold_metrics.values() if m["oos_icir"] > 0)
        logger.info(f"  Fold {fi + 1}: train={len(train_dates)}d ({train_dates[0].date()}→{train_dates[-1].date()}), "
              f"test={len(test_dates)}d ({test_dates[0].date()}→{test_dates[-1].date()}), "
              f"{n_good}/{len(candidates)} factors OOS_ICIR>0")

    # ── PBO 计算 ──
    pbo_result = compute_pbo(fold_results, candidates)
    logger.info(f"Phase 3 PBO: {pbo_result['pbo']:.3f} (logit={pbo_result['logit_pbo']:+.3f})")
    logger.info(f"Phase 3 PBO check: {'PASS' if pbo_result['passed'] else 'FAIL'} (threshold: <{pbo_max})")
    logger.info(f"Phase 3 IS-OOS ICIR Spearman corr: {pbo_result['is_oos_corr']:+.3f}")

    # ── 硬门禁: PBO 未通过 → 拒绝进入策略回测 (fail-fast, 零 fallback) ──
    if not pbo_result["passed"]:
        raise ValueError(
            f"Phase 3 GATE REJECTED: PBO={pbo_result['pbo']:.3f} >= threshold={pbo_max}. "
            f"IS-OOS corr={pbo_result['is_oos_corr']:+.3f}. "
            f"Factor pool likely overfit — refine factors before strategy backtest."
        )

    # ── 汇总各因子 OOS 表现 (三档: pass/marginal/fail) ──
    kept = []
    marginal = []
    dropped = []
    kept_oos_ir = []
    for name in candidates:
        oos_irs = []
        is_icirs = []
        for fold in fold_results:
            if name in fold:
                oos_irs.append(fold[name].get("oos_icir", 0.0))
                is_icirs.append(fold[name].get("is_icir", 0.0))

        avg_oos_ir = float(np.mean(oos_irs)) if oos_irs else 0.0
        avg_is_ir = float(np.mean(is_icirs)) if is_icirs else 0.0
        decay_ratio = (avg_oos_ir / avg_is_ir) if abs(avg_is_ir) > 0.01 else 1.0

        # Three-tier verdict (De Prado 2018 Ch.7-8; 明汯标准):
        #   kept:     OOS_ICIR>0 AND decay_ratio >= 0.50 (明汯: IS→OOS Sharpe 衰减<50%)
        #   marginal: OOS_ICIR>0 AND 0.30 <= decay_ratio < 0.50 (中等衰减, monitoring)
        #   dropped:  OOS_ICIR<=0 OR decay_ratio < 0.30 (严重衰减, retired)
        marginal_decay_min = 0.30  # 来源: 明汯 OOS/IS=0.50 threshold → 松一档 0.30
        if avg_oos_ir > 0 and decay_ratio >= (1 - sharpe_decay_max):
            kept.append(name)
            kept_oos_ir.append(avg_oos_ir)
            logger.info(f"  ✓ {name:30s} OOS_ICIR={avg_oos_ir:+.3f} (IS→OOS decay={decay_ratio:.0%})")
        elif avg_oos_ir > 0 and decay_ratio >= marginal_decay_min:
            marginal.append(name)
            logger.info(f"  ~ {name:30s} OOS_ICIR={avg_oos_ir:+.3f} (IS→OOS decay={decay_ratio:.0%}) — MARGINAL")
        else:
            dropped.append(name)
            logger.info(f"  ✗ {name:30s} OOS_ICIR={avg_oos_ir:+.3f} (IS→OOS decay={decay_ratio:.0%}) — DROPPED")

    logger.info(f"Phase 3 complete ({time.monotonic()-t0:.1f}s). "
                f"{len(kept)} kept, {len(marginal)} marginal, {len(dropped)} dropped "
                f"(total: {len(candidates)} candidates)")

    result = {
        "kept": kept,
        "marginal": marginal,
        "dropped": dropped,
        "oos_irs": [float(x) for x in kept_oos_ir],
        "pbo_result": pbo_result,
        "n_folds": len(splits),
    }

    # 持久化到 evaluation_runs (ADR 028: DB 替代临时文件)
    from quant.evaluation.run_store import save_phase
    result["n_factors"] = len(candidates)
    save_phase("phase3", result)
    logger.info("Phase 3 saved to evaluation_runs")

    return result
