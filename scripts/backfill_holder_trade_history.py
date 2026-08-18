#!/usr/bin/env python3
"""回填 holder_trade (股东增减持) 历史 — 2020-01-01 起逐只拉全史 (v527).

背景: v520 复活 97 因子后物化池 104 因子, insider_increase 因子依赖
holder_trade 90 天窗口 (覆盖原为 2025-01-01 起) → 2020-2023 日期被
blocked。本脚本调 holder_trade.sync_range 按日期过滤回填 (幂等
INSERT OR REPLACE, akshare 同花顺源逐只拉取, 耗时长 ~1.5-3h)。

用法:
    PYTHONPATH=. .venv/bin/python scripts/backfill_holder_trade_history.py

版本: v1.0 (2026-08-17)
"""
import time

from quant.data.holder_trade import sync_range
from quant.utils.logger import get_logger

_log = get_logger("backfill.holder_trade")


def main() -> int:
    t0 = time.time()
    _log.info("holder_trade backfill: 2019-10-01 → 今天 (逐只全量, 预计 1.5-3h)")
    n = sync_range("2019-10-01", "")
    _log.info("holder_trade backfill done: %d rows, %.1fs", n, time.time() - t0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())