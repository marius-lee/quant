from quant.config.paths import MARKET_DB
from quant.utils.date import to_compact
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
from config.constants import _require_cfg
from quant.utils.logger import get_logger

logger = get_logger("daily_sync")


def step1_ohlcv(date_str: str):
    """日线增量更新: 自动检测缺口, 只拉缺失的。"""
    from quant.data.store import DataStore
    store = DataStore()
    n = store.update_daily(start=date_str)
    logger.info(f"[1] daily: {n} new rows")
    return n


def step2_margin(date_str: str):
    """融资融券: SSE 直接JSON + SZSE akshare wrapper。

    P1-12 fix: 传递 YYYY-MM-DD 格式 (而非 to_compact YYYYMMDD), 内部函数自行转换 API 格式.
    数据库统一使用 ISO 格式 (YYYY-MM-DD), 与 daily 表一致.
    """
    from quant.data.margin import _sync_sse_raw, _sync_szse_wrapper
    import sqlite3
    conn = sqlite3.connect(MARKET_DB, timeout=_require_cfg("data.sqlite.timeout"))
    # 断言: date_str 必须是 ISO 格式 (YYYY-MM-DD), 防止 YYYYMMDD 混入
    assert len(date_str) == 10 and date_str[4] == '-' and date_str[7] == '-', \
        f"P1-12: date_str must be YYYY-MM-DD, got {date_str}"
    n_sse = _sync_sse_raw(date_str, conn)
    time.sleep(_require_cfg("sync.daily_interval"))
    n_szse = _sync_szse_wrapper(date_str, conn)
    conn.close()
    logger.info(f"[2] margin: SSE={n_sse}, SZSE={n_szse}")
    return n_sse + n_szse


def step3_limit_up(date_str: str):
    """涨停池: 单日同步。"""
    from quant.data.limit_up import sync_date
    n = sync_date(date_str)
    logger.info(f"[3] limit_up: {n} rows")
    return n


def step4_lhb(date_str: str):
    """龙虎榜: 单日同步。"""
    from quant.data.lhb import sync_date
    n = sync_date(date_str)
    logger.info(f"[4] lhb: {n} rows")
    return n


def step5_fundamentals(date_str: str):
    """基本面: 每周一更新 (PE/PB/市值)。非周一跳过。"""
    import pandas as pd
    if pd.Timestamp(date_str).weekday() != 0:
        logger.info("[5] fundamentals: skipped (not Monday)")
        return 0
    import sqlite3
    from quant.data.repos._base import DatabaseManager
    conn = sqlite3.connect(MARKET_DB, timeout=_require_cfg("data.sqlite.timeout"))
    from quant.data.fundamental import sync_all
    n = sync_all(conn, max_fetch=500)
    conn.close()
    logger.info(f"[5] fundamentals: {n} stocks updated")
    return n


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
    results["fundamentals"] = step5_fundamentals(date_str)

    elapsed = time.time() - t0
    logger.info(f"=== Daily Sync done in {elapsed:.0f}s: {results} ===")
    return results


if __name__ == "__main__":
    date = sys.argv[1] if len(sys.argv) > 1 else None
    run(date)
