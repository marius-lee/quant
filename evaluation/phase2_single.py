"""Stage 2: 单因子检验 — IC / |t| / ICIR / half-life 四维过滤。"""

import json
import numpy as np
import pandas as pd
import sqlite3
from config.loader import get as cfg
from factor.stats_cache import compute_factor_stats


def screen_factors(input_json: str = "/tmp/_eval_phase1.json",
                   output_json: str = "/tmp/_eval_phase2.json") -> dict:
    """运行单因子检验, 返回通过/未通过因子列表。

    Returns
    -------
    dict with keys: passed, ic_means, ic_irs, decay, failed
    """
    with open(input_json) as f:
        p1 = json.load(f)

    # ── 阈值来源: config.yaml ──
    t_threshold = cfg("factor.evaluation.t_threshold", 2.0)
    min_abs_ic = cfg("factor.evaluation.min_abs_ic", 0.02)
    min_icir = cfg("factor.evaluation.min_icir", 0.5)
    min_half_life = cfg("factor.evaluation.min_half_life", 20)
    n_days = cfg("factor.evaluation.n_days", 120)

    print(f"Phase 2 thresholds: |IC|≥{min_abs_ic}, |t|≥{t_threshold}, "
          f"ICIR≥{min_icir}, half-life≥{min_half_life}d")

    # 获取 active 因子
    conn = sqlite3.connect("data/market.db")
    active_names = [r[0] for r in conn.execute(
        "SELECT name FROM factor_registry WHERE status='active'").fetchall()]
    conn.close()
    print(f"Active factors: {len(active_names)}")

    # 计算因子统计
    n_symbols = cfg("factor.evaluation.n_symbols", 0)
    lookback = cfg("factor.evaluation.lookback", 120)
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
        # decay = {display_name: [1d_val, 5d_val, 20d_val]}
        ic_1d = abs(ic_means.get(name, 0.0))
        display_name = meta.get(name, {}).get("display", name)
        decay_vals = decay.get(display_name, [0.0, 0.0, 0.0])
        ic_20d = abs(decay_vals[2]) if len(decay_vals) > 2 else 0.0
        half_life_est = 0
        if ic_1d > 0.001 and ic_20d > 0:
            ratio_20 = ic_20d / ic_1d
            if ratio_20 > 0:
                half_life_est = int(-20 / np.log(max(ratio_20, 0.01)))
        if half_life_est < min_half_life and ic_1d >= min_abs_ic:
            reasons.append(f"half-life={half_life_est}d<{min_half_life}")

        if not reasons:
            passed.append(name)
        else:
            failed[name] = reasons

    # ── 输出 ──
    print(f"\nPhase 2 results: {len(passed)} passed, {len(failed)} failed\n")

    print("=== PASSED ===")
    for name in sorted(passed, key=lambda n: abs(ic_means.get(n, 0.0)), reverse=True):
        ic = ic_means.get(name, 0.0)
        ir = ic_irs.get(name, 0.0)
        t = abs(ir) * np.sqrt(n_days)
        display_name = meta.get(name, {}).get("display", name)
        decay_vals = decay.get(display_name, [0.0, 0.0, 0.0])
        ic_20 = abs(decay_vals[2]) if len(decay_vals) > 2 else 0.0
        ratio = ic_20 / max(abs(ic), 0.001) if ic else 0
        hl = int(-20 / np.log(max(ratio, 0.01))) if ratio > 0 else 0
        print(f"  ✓ {name:30s} IC={ic:+.4f}  t={t:.1f}  IR={ir:+.2f}  HL≈{hl}d")

    if failed:
        print("\n=== FAILED ===")
        for name, reasons in sorted(failed.items()):
            print(f"  ✗ {name:30s} {'; '.join(reasons)}")

    result = {
        "passed": passed,
        "failed": {k: list(v) for k, v in failed.items()},
        "ic_means": {k: float(v) for k, v in ic_means.items()},
        "ic_irs": {k: float(v) for k, v in ic_irs.items()},
        "ic_series": stats.get("ic_series", {}),
    }

    with open(output_json, 'w') as f:
        json.dump(result, f, indent=2, default=str)

    print(f"\nPhase 2 complete. {len(passed)} factors advance to Phase 3.")
    return result
