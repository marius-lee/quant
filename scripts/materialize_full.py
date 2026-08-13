"""全因子分批物化 — 一次性回填 2020-01-01 → daily MAX(date)。

池 = 90 全期因子 (价量/基本面/lhb/margin, 数据全期覆盖) + 4 macro (v470 注册)。
排除: 北向 (northbound 表不存在) / intraday×3 (快照 2026-08 起) /
analyst×4 / fund_hold×3 / holder_trade×3 / pledge_ratio (数据不足)。

起点约定 (2026-08-13 复核): 因子物化从 2020-01-01 起, 数据准备到 2019-01-01。
勿改回 2019-01-01 — 2018 年 daily 仅 ~354 只股票子集 (2019 起才全量 ~3,500+ 只),
1978-12 起按 2019 起点需要 2018 lookback, 绝大多数股票早期全 NaN,
且短窗口因子会把 2019 日期误标已物化。

用法:
    PYTHONPATH=. .venv/bin/python scripts/materialize_full.py           # 全批顺序跑
    PYTHONPATH=. .venv/bin/python scripts/materialize_full.py --batch 2 # 只跑批 2
    PYTHONPATH=. .venv/bin/python scripts/materialize_full.py --dry     # 只打印每批待算日期数

批次间幂等: 每批 force=False, 已物化日期/因子跳过 (manifest 判定);
批内断点续传: store 的 _checkpoint.json 自动续跑。
"""
import argparse
import sys
import time as _time

BATCHES = {
    1: ["alpha002_vol_div", "alpha055_pos_vol", "dt_streak", "smart_money_20d", "wq_alpha_006"],
    2: ["alpha012_vol_dir", "alpha033_gap", "alpha035_range_mom", "alpha041_geo_vwap",
        "alpha042_vwap_div", "amihud_20d", "amihud_250d", "ctr_20d", "day_night",
        "gap_5d", "hl_volume_20d", "ideal_amplitude", "idio_vol_126d", "idio_vol_60d",
        "limit_touch_no_seal", "liquidity_shock", "ma_alignment_20d", "max_ret_20d",
        "momentum_126d", "momentum_252d", "momentum_63d", "money_flow_5d", "net_limit_ratio"],
    3: ["overnight_gap_5d", "overnight_gap_ratio", "price_channel_position", "qlib_vema",
        "range_20d", "residual_momentum_126d", "reversal_5d", "rsi_rev_14d", "seal_time",
        "seal_turnover_ratio", "seasonality_12m_1m", "skewness_60d", "tail_risk", "trcf",
        "trend_strength", "turnover_accel", "turnover_adj_amihud_20d", "turnover_anomaly",
        "turnover_rev_5d", "uret_20d", "vol_price_corr_10d", "vol_price_sync_20d", "volatility_126d"],
    4: ["vp_divergence", "zt_streak", "ztd", "abn_turnover", "abn_turnover_resid", "str",
        "limit_up_prox_5d", "bp_ratio", "ep_ratio", "epa", "epd", "epds", "financial_anomaly",
        "high52w_dist", "roe_ratio", "roe_trimmed", "size", "accruals", "debt_ratio",
        "gp_ta", "roa", "roe_reported"],
    5: ["ocfp", "asset_growth", "earnings_growth_yoy", "revenue_growth_yoy", "sue",
        "piotroski_fscore", "gross_margin_diff", "dividend_yield",
        "lhb_freq_60d", "lhb_intensity_5d", "lhb_net_buy_20d", "lhb_post_quality",
        "lhb_reversal_5d", "margin_balance_chg", "margin_buy_ratio", "margin_buy_ratio_5d",
        "short_interest", "macro_cpi_yoy", "macro_m2_yoy", "macro_pmi_diff", "macro_rate_10y"],
}

START = "2020-01-01"

ALL_FACTORS = sorted({f for fs in BATCHES.values() for f in fs})


def _dates_until(end: str) -> list[str]:
    from quant.data.store import DataStore
    store = DataStore()
    conn = store._connect()
    try:
        latest = conn.execute("SELECT MAX(date) FROM daily").fetchone()[0]
        actual_end = min(end, latest) if latest else end
        dates = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM daily WHERE date >= ? AND date <= ? ORDER BY date",
            (START, actual_end)).fetchall()]
    finally:
        conn.close()
    store.close()
    return dates


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=None, help="只跑指定批号 (默认全部 94 因子单次物化)")
    ap.add_argument("--dry", action="store_true", help="只打印计划")
    ap.add_argument("--workers", type=int, default=None,
                    help="并行 worker 数 (默认 config factor.compute.materialize_max_workers)")
    args = ap.parse_args()

    from quant.factor.store import FactorStore
    from quant.data.repos.universe_repo import UniverseRepo

    if args.workers is None:
        from quant.config.constants import _require_cfg
        args.workers = _require_cfg("factor.compute.materialize_max_workers")

    if args.batch is not None:
        batch_nos = [args.batch]
    else:
        batch_nos = None  # 单次直跑全因子

    dates = _dates_until("2099-01-01")
    fs = FactorStore()
    symbols = None
    t0 = None

    if args.dry:
        from quant.factor.store import FactorStore as _FS
        fs = _FS()
        if batch_nos is not None:
            plan = [(b, BATCHES[b]) for b in batch_nos]
        else:
            plan = [("ALL", ALL_FACTORS)]
        for label, factors in plan:
            missing = sum(1 for d in dates if fs._date_missing_factors(d, factors))
            print(f"[{label}] {len(factors)} factors × {len(dates)} dates "
                  f"({dates[0]}..{dates[-1]}) → ~{missing}/{len(dates)} dates pending",
                  flush=True)
        return

    symbols = UniverseRepo().get_symbols(exclude_market='BJ')
    _label = f"batch{args.batch}" if batch_nos is not None else "ALL"
    factors = [BATCHES[b] for b in batch_nos] if batch_nos is not None else [ALL_FACTORS]
    for fidx, fgroup in enumerate(factors):
        print(f"\n[{_label}] {len(fgroup)} factors × {len(dates)} dates "
              f"({dates[0]}..{dates[-1]}), workers={args.workers}", flush=True)
        t0 = _time.time()
        result = fs.materialize(dates, fgroup, symbols, force=False,
                                workers=args.workers)
        el = _time.time() - t0
        print(f"[{_label}] done: rows={result.get('n_rows')} skipped={result.get('skipped')} "
              f"failed_dates={result.get('failed_dates')} elapsed={el/60:.1f}min", flush=True)
        if result.get("failed_dates"):
            print(f"[{_label}] FAILED dates: {result['failed_dates'][:10]}", flush=True)
            sys.exit(1)

    print("\nAll done.", flush=True)


if __name__ == "__main__":
    main()
