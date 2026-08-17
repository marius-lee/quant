#!/usr/bin/env python3
"""立即重评全部 evaluating 因子 (v520b) — 不等周六自动评估.

用途: 用户拍板 "现在就开始写脚本重评" — 对刚复活的 97 + 原 4 个
      evaluating 因子 (合计 101, 含 probation 5 一并进入评估池)
      立即执行完整评估管线:
        1) 因子缓存物化 (评估窗口: 最近 ~378 自然日, 增量幂等)
        2) Phase 1-5: prepare_data → screen_factors → validate_oos
           → verify_costs → sync_factor_status (v519 单一裁决,
           p2+p3+p4+DSR 显著才 active; 未显著 → probation 半权)

用法:
    PYTHONPATH=. .venv/bin/python scripts/reevaluate_now.py [--skip-materialize] [--today 2026-08-16]

幂等性: 物化增量 (data_hash 校验, 已物化跳过); phase 结果 save_phase
        覆盖写入 evaluation_runs; 重复执行安全。
阶段打点: 每阶段输出 行数/耗时; 总量统计 {:.1f}s。

版本: v1.0 (2026-08-16)
"""
import sqlite3
import sys
import time

from quant.config.paths import MARKET_DB
from quant.config.constants import _require_cfg


def _t0():
    return time.time()


def _log_stage(stage: str, t0: float, extra: str = ""):
    print(f"[{time.strftime('%H:%M:%S')}] {stage}: 耗时 {time.time()-t0:.1f}s {extra}",
          flush=True)


def _prepare_dates() -> tuple[list[str], list[str], str]:
    """评估窗口日期列表 + 交易日列表 (与 Phase 1 effective_start 对齐)."""
    from datetime import datetime, timedelta
    import pandas as pd
    from quant.data.store import DataStore

    store = DataStore()
    conn = store._connect()
    db_max = conn.execute("SELECT MAX(date) FROM daily").fetchone()[0]
    lookback = _require_cfg("factor.evaluation.lookback")
    effective_start = (
        datetime.today() - timedelta(days=int(lookback * 1.5))
    ).strftime("%Y-%m-%d")
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM daily WHERE date >= ? AND date <= ? ORDER BY date",
        (effective_start, db_max)).fetchall()]
    store.close()
    return dates, db_max, effective_start


def _materialize_eval_window(dates: list[str]) -> dict:
    """物化评估窗口内全部 backtesting 池因子 (增量幂等, fork pool 并行)."""
    t = _t0()
    from quant.factor.store import FactorStore
    from quant.factor.compute import get_factor_names
    from quant.data.repos.universe_repo import UniverseRepo

    names = get_factor_names(status_filter="backtesting")
    symbols = UniverseRepo().get_symbols(exclude_market="BJ")
    print(f"[{time.strftime('%H:%M:%S')}] 物化: {len(names)} 因子 × {len(dates)} 交易日 × {len(symbols)} 股票",
          flush=True)
    fs = FactorStore()
    r = fs.materialize(date_range=dates, factor_names=names, symbols=symbols,
                       max_slice_days=25, workers=2)
    _log_stage("物化", t, f"rows={r.get('n_rows')} dates={r.get('n_dates')} factors={r.get('n_factors')}")
    return r


def _run_phases(today: str) -> dict:
    """Phase 1-5 顺序执行 (weekly.py 蓝本, 前一阶段失败跳过后续)."""
    results = {}
    t_all = _t0()

    def run(phase: str, fn):
        t = _t0()
        try:
            r = fn()
            results[phase] = "ok"
            _log_stage(phase, t)
            return True
        except Exception as e:
            import traceback
            traceback.print_exc()
            results[phase] = f"failed: {e}"
            _log_stage(phase, t, f"FAILED: {e}")
            return False

    p1 = run("phase1_prepare_data", lambda: (
        __import__('quant.evaluation.phase1_data', fromlist=['prepare_data']).prepare_data()))
    p2 = p1 and run("phase2_screen_factors", lambda: (
        __import__('quant.evaluation.phase2_single', fromlist=['screen_factors']).screen_factors()))
    p3 = p2 and run("phase3_cpcv_oos_pbo", lambda: (
        __import__('quant.evaluation.phase3_oos', fromlist=['validate_oos']).validate_oos()))
    p4 = p3 and run("phase4_costs", lambda: (
        __import__('quant.evaluation.phase4_costs', fromlist=['verify_costs']).verify_costs()))
    p5 = p2 and run("phase5_sync_status", lambda: (
        __import__('quant.evaluation.phase5_monitor', fromlist=['sync_factor_status']).sync_factor_status()))

    print(f"[{time.strftime('%H:%M:%S')}] 管线总计 {time.time()-t_all:.1f}s: "
          f"p1={'✓' if p1 else '✗'} p2={'✓' if p2 else '✗'} p3={'✓' if p3 else '✗'} "
          f"p4={'✓' if p4 else '✗'} p5={'✓' if p5 else '✗'}", flush=True)
    return results


def _report():
    conn = sqlite3.connect(MARKET_DB, timeout=10)
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM factor_registry GROUP BY status").fetchall()
    conn.close()
    print("\n=== 重评后状态分布 ===")
    for status, n in rows:
        print(f"  {status}: {n}")


def main() -> int:
    skip_mat = "--skip-materialize" in sys.argv
    today = sys.argv[sys.argv.index("--today") + 1] if "--today" in sys.argv else None
    if today is None:
        import datetime
        today = datetime.date.today().strftime("%Y-%m-%d")

    t_all = _t0()
    dates, db_max, eff_start = _prepare_dates()
    print(f"评估窗口: {eff_start} → {db_max} ({len(dates)} 交易日)")

    if not skip_mat:
        _materialize_eval_window(dates)
    else:
        print("跳过物化 (--skip-materialize)")

    _run_phases(today)
    _report()
    print(f"\n全部完成, 总耗时 {time.time()-t_all:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())