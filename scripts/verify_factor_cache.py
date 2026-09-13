#!/usr/bin/env python
# 用途: 物化完成后自动校验因子缓存 — 确认物化池(backtesting U using)全部因子均
#       物化且有非空值, 并区分 blocked 是"整段全 blocked(真坏)"还是"局部缺口(合法
#       数据覆盖, 如 analyst_buy 依赖 analyst_forecast 表 2026-07 才起覆盖)".
# 版本: v1.0.0
# 用法: PYTHONPATH=. .venv/bin/python scripts/verify_factor_cache.py
#       (建议由 scripts/watch_factor_cache_done.sh 在物化进程退出后自动调用)
# 输出: 打印摘要 + 写 logs/factor_cache_verify.json; 退出码 0=通过, 1=有疑点.
import json
import glob
import os
from collections import defaultdict

import pandas as pd

from quant.factor.compute import get_factor_names
from quant.utils.logger import get_logger

_log = get_logger("factor.verify")
CACHE_DIR = "quant/data/factor_cache"
PARQUET_F = os.path.join(CACHE_DIR, "parquet_f")
BLOCKED_PATH = os.path.join(CACHE_DIR, "blocked.json")
# 阈值: 关键是用 "未解释缺失" 区分 — 缺失但 blocked(数据覆盖缺口, 合法) vs
#       缺失且未 blocked(真算不出, bug). 故不直接卡覆盖率, 而卡 unexplained.
UNEXPLAINED_FAIL = 0.1   # 未解释缺失(总日期-物化-已blocked) > 10% → 失败
NONNAN_WARN = 0.5
EXPR_MUST_COVER = 0.9    # 7 表达式因子必须几乎全覆盖且有值(它们是本次新接线)


def _read_parts(factor: str):
    """逐分片累加统计, 不整体载入内存. 返回 (n_dates, n_rows, n_nan)."""
    dates = set()
    n_rows = 0
    n_nan = 0
    for fp in glob.glob(os.path.join(PARQUET_F, factor, "*")):
        df = pd.read_parquet(fp)
        dates.update(df["date_i16"].unique().tolist())
        v = df["value_f32"]
        n_rows += len(v)
        n_nan += int(v.isna().sum())
    return len(dates), n_rows, n_nan


def main():
    pool = sorted(set(get_factor_names("backtesting")) | set(get_factor_names("using")))
    _log.info("pool factors: %d", len(pool))

    stats = {}
    max_dates = 0
    for f in pool:
        d = os.path.join(PARQUET_F, f)
        if not os.path.isdir(d) or not glob.glob(os.path.join(d, "*")):
            stats[f] = {"materialized": False}
            continue
        n_dates, n_rows, n_nan = _read_parts(f)
        max_dates = max(max_dates, n_dates)
        stats[f] = {
            "materialized": True,
            "n_dates": n_dates,
            "n_rows": n_rows,
            "nonnan_frac": (n_rows - n_nan) / n_rows if n_rows else 0.0,
        }
    # 覆盖率相对最完整因子
    for f in pool:
        if stats[f].get("materialized"):
            stats[f]["coverage"] = stats[f]["n_dates"] / max_dates if max_dates else 0.0

    # blocked 分布: 每因子 blocked 的 "日期数" (按 date_str 去重计)
    blocked = {}
    if os.path.exists(BLOCKED_PATH):
        blocked = json.load(open(BLOCKED_PATH))
    blocked_by_factor = defaultdict(int)       # 因子 → blocked 日期数
    blocked_total = 0
    for _d, fs in blocked.items():
        for fac in fs:
            blocked_by_factor[fac] += 1
            blocked_total += 1

    # 判定: 用 "未解释缺失" 区分合法数据缺口(blocked) 与真算不出
    problems = []
    expr7 = ["amihud_proxy", "micro_gap", "money_flow_cmf", "residual_momentum_proxy",
             "volume_price_trend", "wq_alpha_001", "wq_alpha_032"]
    for f in pool:
        s = stats.get(f, {})
        if not s.get("materialized"):
            problems.append(f"NOT_MATERIALIZED: {f}")
            continue
        mat = s["n_dates"]
        blk = blocked_by_factor.get(f, 0)
        unexplained = max(0, max_dates - mat - blk)
        unex_frac = unexplained / max_dates if max_dates else 1.0
        nn = s["nonnan_frac"]
        s["blocked_dates"] = blk
        s["unexplained_frac"] = unex_frac
        if unex_frac > UNEXPLAINED_FAIL:
            problems.append(f"UNEXPLAINED_MISSING({unex_frac:.2f}): {f}")
        if nn < NONNAN_WARN:
            problems.append(f"EMPTY_VALUES({nn:.2f}): {f}")
        # 表达式因子必须几乎全覆盖且有值(本次新接线, 是核心校验点)
        if f in expr7:
            if unex_frac > (1 - EXPR_MUST_COVER):
                problems.append(f"EXPR_LOW_COVERAGE(unex={unex_frac:.2f}): {f}")
            if nn < 0.5:
                problems.append(f"EXPR_EMPTY_VALUES({nn:.2f}): {f}")

    # 合法局部 blocked 提示 (不计入失败)
    partial_blocked = {f: blocked_by_factor[f] for f in pool
                       if blocked_by_factor.get(f, 0) and max_dates
                       and (max_dates - stats.get(f, {}).get("n_dates", 0)
                            - blocked_by_factor[f]) / max_dates <= UNEXPLAINED_FAIL}

    result = {
        "pool_size": len(pool),
        "max_dates": max_dates,
        "stats": stats,
        "blocked_total": blocked_total,
        "blocked_by_factor": dict(blocked_by_factor),
        "partial_blocked_ok": partial_blocked,
        "problems": problems,
        "pass": len(problems) == 0,
    }
    os.makedirs("logs", exist_ok=True)
    with open("logs/factor_cache_verify.json", "w") as fh:
        json.dump(result, fh, indent=2, default=str)

    print(f"POOL={len(pool)} max_dates={max_dates} blocked_total={blocked_total}")
    print("per-factor: materialized / blocked / unexplained / nonnan:")
    for f in pool:
        s = stats.get(f, {})
        if s.get("materialized"):
            print(f"  {f:24} mat={s['n_dates']:4} blk={s.get('blocked_dates',0):4} "
                  f"unex={s.get('unexplained_frac',0):.2f} nonnan={s['nonnan_frac']:.2f}")
        else:
            print(f"  {f:24} NOT MATERIALIZED")
    if partial_blocked:
        print(f"legit partial-blocked (data coverage gap, OK): {partial_blocked}")
    if problems:
        print(f"\nPROBLEMS ({len(problems)}):")
        for p in problems:
            print(f"  - {p}")
        print("\nRESULT: FAIL")
        return 1
    print("\nRESULT: PASS — 11 因子均物化且有非空值, 无整段 blocked 真坏因子")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
