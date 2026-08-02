#!/usr/bin/env python3
"""正式跑 chunk 1 因子缓存物化.

行为:
  1. 删除 quant/data/factor_cache/ 下所有 .csv.gz 和 .manifest.json
  2. 只物化 chunk 1: 2019-06-03 -> 2020-03-26 (约 200 交易日)
  3. 打印耗时与全量估算

警告: 此脚本会清空已有因子缓存! 执行前请确认无需保留旧缓存。

用法:
    cd /Users/mariusto/project/quant
    .venv/bin/python scripts/run_factor_cache_chunk1.py
"""

import os
import sys
import time
import json
import glob
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parents[1]
os.chdir(PROJ_ROOT)

from quant.factor.store import FactorStore
from quant.factor.compute import get_factor_names
from quant.data.store import DataStore
from quant.data.repos.universe_repo import UniverseRepo
from quant.utils.logger import get_logger

_log = get_logger("factor_cache.chunk1")
CACHE_DIR = PROJ_ROOT / "quant" / "data" / "factor_cache"


def _clear_cache():
    """清空生产缓存目录。"""
    n = 0
    for pattern in ("*.csv.gz", "*.manifest.json"):
        for p in CACHE_DIR.glob(pattern):
            p.unlink()
            n += 1
    _log.info("cleared %d cache files from %s", n, CACHE_DIR)
    print(f"[CHUNK1] cleared {n} cache files")


def main():
    t0 = time.monotonic()

    # 1. 清空缓存
    _clear_cache()

    store = DataStore()
    try:
        # 2. 只取 chunk 1 日期
        start = "2019-06-03"
        end = "2020-03-26"
        dates = [
            r[0] for r in store._connect().execute(
                "SELECT DISTINCT date FROM daily WHERE date >= ? AND date <= ? ORDER BY date",
                (start, end),
            ).fetchall()
        ]
        symbols = UniverseRepo().get_symbols(exclude_market="BJ")
        factors = sorted(
            set(get_factor_names(status_filter="backtesting"))
            | set(get_factor_names(status_filter="using"))
        )

        _log.info(
            "chunk1: %d dates x %d factors x %d symbols",
            len(dates), len(factors), len(symbols),
        )
        print(
            f"[CHUNK1] running: {len(dates)} dates x {len(factors)} factors x {len(symbols)} symbols"
        )

        # 3. 正式跑 chunk 1, 写生产缓存
        fs = FactorStore()
        result = fs.materialize(dates, factors, symbols, force=True)

        chunk_sec = result.get("elapsed_sec", 0)
        n_chunks = 9
        est_low = chunk_sec * n_chunks
        est_high = chunk_sec * n_chunks * 1.3

        summary = {
            "chunk_dates": len(dates),
            "n_factors": len(factors),
            "n_symbols": len(symbols),
            "chunk_elapsed_sec": chunk_sec,
            "estimated_full_h_low": round(est_low / 3600, 1),
            "estimated_full_h_high": round(est_high / 3600, 1),
        }

        _log.info("chunk1 result: %s", json.dumps(summary))
        print("[CHUNK1] result:")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        print(
            f"[CHUNK1] full materialization estimate: "
            f"{summary['estimated_full_h_low']}-{summary['estimated_full_h_high']}h"
        )

    except Exception as e:
        _log.exception("chunk1 failed: %s", e)
        print(f"[CHUNK1] failed: {e}", file=sys.stderr)
        raise
    finally:
        store.close()
        _log.info("chunk1 total script time=%.1fs", time.monotonic() - t0)


if __name__ == "__main__":
    main()
