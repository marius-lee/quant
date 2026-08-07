#!/usr/bin/env python3
"""融资融券历史数据回填 — 自动重试版.
运行: PYTHONPATH=. .venv/bin/python3 scripts/backfill_margin.py
"""
import os, sys, sqlite3, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from quant.data.margin import sync_range
from quant.utils.logger import get_logger

logger = get_logger("backfill.margin")
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "quant", "data", "market.db")


def main():
    conn = sqlite3.connect(DB, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")

    for attempt in range(20):
        try:
            logger.info(f"margin backfill attempt {attempt+1}/20: 2019-01-01 → 2025-12-31")
            n = sync_range("2019-01-01", "2025-12-31", conn=conn)
            logger.info(f"margin backfill done: {n} rows")
            conn.close()
            return
        except Exception as e:
            logger.warning(f"margin backfill failed (attempt {attempt+1}): {e}")
            if attempt < 19:
                wait = min(60, 15 * (attempt + 1))
                logger.info(f"retrying in {wait}s...")
                time.sleep(wait)

    conn.close()
    logger.error("margin backfill exhausted retries")


if __name__ == "__main__":
    main()
