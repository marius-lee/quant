#!/usr/bin/env python3
"""资金流向历史数据回填 — 东方财富逐股.

回填全量股票资金流向 (主力净流入/超大单/大单/中单/小单).
依赖: akshare, 逐股串行, 每只 ~0.5s, 5000 只 ≈ 40min.
运行:
  PYTHONPATH=. .venv/bin/python3 scripts/backfill_fund_flow.py
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quant.data.fund_flow import sync_all
from quant.utils.logger import get_logger

logger = get_logger("backfill.fund_flow")


def main():
    logger.info("fund_flow backfill start (max 5000 stocks)")
    n = sync_all(max_stocks=5000)
    logger.info(f"fund_flow backfill done: {n} rows")


if __name__ == "__main__":
    main()
