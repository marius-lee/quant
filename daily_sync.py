"""每日数据同步 — 一个命令更新所有数据。

功能:
  1. OHLCV 日线增量 (tencent+pytdx+akshare)
  2. 融资融券 (SSE+SZSE, 今天)
  3. 涨停池 (limit_up_pool)
  4. 龙虎榜 (lhb_detail)
  5. 基本面 (PE/PB/市值, 每周一更新)

不可用: 北向资金(API截止2024-08), 资金流向(IP封), 大宗交易(API坏)

用法:
  PYTHONPATH=. .venv/bin/python3 daily_sync.py            # 更新今天的数据
  PYTHONPATH=. .venv/bin/python3 daily_sync.py 2026-07-03 # 指定日期
"""

import sys, os, time
from datetime import datetime, timedelta
from utils.logger import get_logger

logger = get_logger("daily_sync")


def step1_ohlcv(date_str: str):
    """日线增量更新: 自动检测缺口, 只拉缺失的。"""
    from data.store import DataStore
    store = DataStore()
    try:
        n = store.update_daily(start="2020-01-01")
        logger.info(f"[1] daily: {n} new rows")
        return n
    except Exception as e:
        logger.warning(f"[1] daily failed: {e}")
        return 0
    finally:
        store.close()


def step2_margin(date_str: str):
    """融资融券: SSE 直接JSON + SZSE akshare wrapper。"""
    from data.margin import _sync_sse_raw, _sync_szse_wrapper
    import sqlite3
    conn = sqlite3.connect(os.path.join(os.path.dirname(__file__), "data", "market.db"))
    n_sse = _sync_sse_raw(date_str.replace("-", ""), conn)
    time.sleep(3)
    n_szse = _sync_szse_wrapper(date_str.replace("-", ""), conn)
    conn.close()
    logger.info(f"[2] margin: SSE={n_sse}, SZSE={n_szse}")
    return n_sse + n_szse


def step3_limit_up(date_str: str):
    """涨停池: 单日同步。"""
    try:
        from data.limit_up import sync_date
        n = sync_date(date_str)
        logger.info(f"[3] limit_up: {n} rows")
        return n
    except Exception as e:
        logger.warning(f"[3] limit_up failed: {e}")
        return 0


def step4_lhb(date_str: str):
    """龙虎榜: 单日同步。"""
    try:
        from data.lhb import sync_date
        n = sync_date(date_str)
        logger.info(f"[4] lhb: {n} rows")
        return n
    except Exception as e:
        logger.warning(f"[4] lhb failed: {e}")
        return 0


def step5_fundamentals():
    """基本面: 每周一更新 (PE/PB/市值)。非周一跳过。"""
    if datetime.now().weekday() != 0:
        logger.info("[5] fundamentals: skipped (not Monday)")
        return 0
    try:
        import sqlite3, os
        conn = sqlite3.connect(os.path.join(os.path.dirname(__file__), "data", "market.db"))
        from data.fundamental import sync_all
        n = sync_all(conn, max_fetch=500)
        conn.close()
        logger.info(f"[5] fundamentals: {n} stocks updated")
        return n
    except Exception as e:
        logger.warning(f"[5] fundamentals failed: {e}")
        return 0


def run(date_str: str = None):
    if date_str is None:
        date_str = datetime.now().strftime("%Y-%m-%d")

    t0 = time.time()
    logger.info(f"=== Daily Sync: {date_str} ===")

    results = {}

    # 1. OHLCV (必须最先, 其他依赖daily表)
    results["daily"] = step1_ohlcv(date_str)

    # 2. 融资融券
    results["margin"] = step2_margin(date_str)

    # 3. 涨停池
    results["limit_up"] = step3_limit_up(date_str)

    # 4. 龙虎榜
    results["lhb"] = step4_lhb(date_str)

    # 5. 基本面 (周一)
    results["fundamentals"] = step5_fundamentals()

    elapsed = time.time() - t0
    logger.info(f"=== Daily Sync done in {elapsed:.0f}s: {results} ===")
    return results


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else None
    run(date)
