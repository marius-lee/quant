#!/usr/bin/env python3
"""因子缓存物化性能基准 — 临时缓存目录, 不污染生产数据.

用法:
    cd /Users/mariusto/project/quant
    .venv/bin/python scripts/benchmark_factor_cache.py

输出:
    - 单 chunk (约 200 交易日) 物化耗时
    - 全量 9 chunks 估算耗时
    - 详细日志写入 logs/benchmark_factor_cache.log
"""

import os
import sys
import tempfile
import time
import json
from pathlib import Path

# 项目根目录
PROJ_ROOT = Path(__file__).resolve().parents[1]
os.chdir(PROJ_ROOT)

from quant.factor.store import FactorStore
from quant.factor.compute import get_factor_names
from quant.data.store import DataStore
from quant.data.repos.universe_repo import UniverseRepo
from quant.utils.logger import get_logger

_log = get_logger("benchmark.factor_cache")


def main():
    t0 = time.monotonic()

    # 临时缓存目录, 跑完自动删除
    tmpdir = tempfile.mkdtemp(prefix="factor_cache_bench_")
    _log.info("benchmark: temp cache dir=%s", tmpdir)

    store = DataStore()
    try:
        # 基准窗口 (历史 benchmark 用 2019-06-03 起 — 物化起点约定 v473:
        # 数据备齐 2019-01-01, 物化起点 2020-01-01; 本窗口仅测速, 用 2020)
        start = "2020-01-01"
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
            "benchmark: chunk 1 -> %d dates x %d factors x %d symbols",
            len(dates), len(factors), len(symbols),
        )
        print(
            f"[BENCH] chunk 1: {len(dates)} dates x {len(factors)} factors x {len(symbols)} symbols"
        )

        fs = FactorStore(db_path=os.path.join(tmpdir, "bench.db"))
        result = fs.materialize(dates, factors, symbols, force=True)

        chunk_sec = result.get("elapsed_sec", 0)
        n_chunks = 9  # 1739 交易日 / 200 ≈ 9 chunks
        est_full_low = chunk_sec * n_chunks
        est_full_high = chunk_sec * n_chunks * 1.3  # 尾部 chunk 可能更慢

        summary = {
            "chunk_dates": len(dates),
            "n_factors": len(factors),
            "n_symbols": len(symbols),
            "chunk_elapsed_sec": chunk_sec,
            "estimated_full_sec_low": est_full_low,
            "estimated_full_sec_high": est_full_high,
            "estimated_full_h_low": round(est_full_low / 3600, 1),
            "estimated_full_h_high": round(est_full_high / 3600, 1),
        }

        _log.info("benchmark result: %s", json.dumps(summary))
        print("[BENCH] result:")
        for k, v in summary.items():
            print(f"  {k}: {v}")
        print(f"[BENCH] full materialization estimate: {summary['estimated_full_h_low']}-{summary['estimated_full_h_high']}h")

    except Exception as e:
        _log.exception("benchmark failed: %s", e)
        print(f"[BENCH] failed: {e}", file=sys.stderr)
        raise
    finally:
        store.close()
        # 清理临时目录
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
        _log.info("benchmark: cleaned temp dir, total script time=%.1fs", time.monotonic() - t0)


if __name__ == "__main__":
    main()
