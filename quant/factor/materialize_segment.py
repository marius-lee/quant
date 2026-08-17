"""物化段进程 (v525 分片并行) — 独立 subprocess, 无共享内存。

主进程 (FactorStore.materialize) 把 todo 日期切成段, 每段:
    python -m quant.factor.materialize_segment --seg <json> --out <pickle>

段进程自载所需数据 (给段首 - eff_days 的 lookback 窗口), 复现
_worker_main 的计算循环 (全局数据对象在本进程内设置), 产出紧凑
results dict → pickle 落盘, 主进程收集后走既有 _consume_worker_result。

设计动机 (v525 事故链): fork+Pool+多线程+大 DataFrame COW 在 8GB M1
上反复 OOM/jetsam kill (309) 与 AfterFork 死循环 — 放弃共享内存并行。

版本: v1.0 (2026-08-17)
"""
import argparse
import json
import pickle
import sys
import traceback

import pandas as pd

from quant.factor.compute._preload import preload_aux_data_chunk
from quant.factor.compute._primitives import precompute_primitives
from quant.factor.compute.price._alternative import preload_ztd_cache
from quant.data.store import DataStore
from quant.factor import store as _store_mod
from quant.utils.logger import get_logger

_log = get_logger("factor.segment")


def _run(seg_path: str, out_path: str) -> int:
    seg = json.load(open(seg_path))
    start_idx, end_idx = seg["start_idx"], seg["end_idx"]
    date_list = seg["date_list"]
    factor_names = seg["factor_names"]
    symbols = seg["symbols"]
    eff_days = seg["eff_days"]
    source_hash = seg["source_hash"]
    cache_dir = seg["cache_dir"]

    dates = date_list[start_idx:end_idx]
    _log.info("segment %d..%d: %d dates (%s → %s), %d factors, %d symbols",
              start_idx, end_idx, len(dates), dates[0], dates[-1],
              len(factor_names), len(symbols))

    st = DataStore()
    fs = _store_mod.FactorStore(cache_dir=cache_dir)

    data_start = seg.get("data_start") or (
        pd.Timestamp(dates[0]) - pd.Timedelta(days=eff_days)).strftime("%Y-%m-%d")
    latest = dates[-1]

    data_full = st.get_daily(symbols, start=data_start, end=latest)
    prims = precompute_primitives(data_full, factor_names=factor_names,
                                  save_disk_cache=False)
    try:
        bm_ret = st.get_benchmark("000300", start=data_start)
        if not bm_ret.empty:
            prims["benchmark_ret"] = bm_ret
    except Exception as _e:
        _log.warning("segment: benchmark_ret unavailable (%s)", _e)

    preload_ztd_cache(dates, symbols)
    aux = preload_aux_data_chunk(symbols, dates[0], latest)
    fund = fs._build_fundamentals_panel(st, symbols, dates, data_full=data_full)

    global_map = seg.get("missing", {})
    _store_mod._DATA_FULL = data_full
    _store_mod._PRIMS = prims
    _store_mod._AUX_FULL = aux
    _store_mod._FUNDAMENTALS = fund
    _store_mod._SYMBOLS = symbols
    _store_mod._MISSING_MAP = {d: global_map.get(d, []) for d in dates}

    sym_map = fs._load_symbol_map()
    all_days = fs._load_trading_days()
    date_to_idx = {d: i for i, d in enumerate(all_days)}

    res = fs._worker_main(start_idx, end_idx, factor_names, date_list,
                          sym_map, date_to_idx, source_hash)
    with open(out_path, "wb") as f:
        pickle.dump(res, f, protocol=pickle.HIGHEST_PROTOCOL)
    _log.info("segment %d..%d done → %s", start_idx, end_idx, out_path)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seg", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    try:
        return _run(args.seg, args.out)
    except Exception:
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())