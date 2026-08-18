#!/usr/bin/env python3
"""回填 fund_hold (基金重仓) 历史季度 — 2020Q1 起补齐 (v527).

背景: v520 复活 97 因子后物化池 104 因子, ihn 因子依赖 fund_hold
(覆盖原为 2024-12-31 起) → 2020-2023 日期被 blocked。本脚本补齐
2020Q1-2024Q3 + 2026Q1/Q2 全部季度 (幂等 INSERT OR REPLACE)。

用法:
    PYTHONPATH=. .venv/bin/python scripts/backfill_fund_hold_history.py

版本: v1.0 (2026-08-17)
"""
import time
import sqlite3

from quant.data.fund_hold import sync_quarter, _ensure_table
from quant.config.paths import MARKET_DB
from quant.utils.logger import get_logger

_log = get_logger("backfill.fund_hold")

QUARTERS = [
    "20200331", "20200630", "20200930", "20201231",
    "20210331", "20210630", "20210930", "20211231",
    "20220331", "20220630", "20220930", "20221231",
    "20230331", "20230630", "20230930", "20231231",
    "20240331", "20240630", "20240930",
    "20260331", "20260630",
]


def main() -> int:
    t0 = time.time()
    conn = sqlite3.connect(MARKET_DB, timeout=30)
    _ensure_table(conn)
    total = 0
    for i, q in enumerate(QUARTERS, 1):
        n = sync_quarter(q, conn=conn)
        total += n
        _log.info("fund_hold [%d/%d] %s: %d rows (累计 %d)", i, len(QUARTERS), q, n, total)
        time.sleep(0.5)
    conn.close()
    _log.info("fund_hold backfill done: %d rows, %.1fs", total, time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())