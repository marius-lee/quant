"""大股东增减持数据同步 — Tushare stk_holdertrade API.

数据源: tushare.pro_api().stk_holdertrade()
表: holder_trade (symbol, ann_date, holder_type, change_vol, change_ratio, direction)
方向: in=增持, out=减持. 因子关注减持 (out) 信号.

API 限流: tushare 200 call/min, 统一走 config.loader RateLimiter.
"""
import os, sqlite3, time
from datetime import datetime

import pandas as pd
from utils.logger import get_logger

logger = get_logger("data.holder_trade")
DB_PATH = os.path.join(os.path.dirname(__file__), "market.db")


def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS holder_trade (
            symbol TEXT NOT NULL,
            ann_date TEXT NOT NULL,
            holder_name TEXT,
            holder_type TEXT,
            change_vol REAL,
            change_ratio REAL,
            direction TEXT,
            avg_price REAL,
            PRIMARY KEY (symbol, ann_date, holder_name)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_holder_date ON holder_trade(ann_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_holder_symbol ON holder_trade(symbol)")
    conn.commit()


def sync_range(start_date: str, end_date: str, conn=None) -> int:
    """同步大股东增减持数据. 返回新增行数."""
    import tushare as ts
    token = os.environ.get("TUSHARE_TOKEN", "")
    if not token:
        logger.warning("TUSHARE_TOKEN not set")
        return 0
    ts.set_token(token)
    pro = ts.pro_api()

    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close_conn = True

    _ensure_table(conn)

    total = 0
    for date_str in pd.date_range(start_date, end_date, freq='MS'):
        ann_month = date_str.strftime('%Y%m')
        try:
            df = pro.stk_holdertrade(ann_date=ann_month)
        except Exception as e:
            logger.warning(f"holder_trade {ann_month} failed: {e}")
            time.sleep(1)
            continue

        if df is None or df.empty:
            continue

        for _, row in df.iterrows():
            ts_code = row.get('ts_code', '')
            if '.' not in str(ts_code):
                continue
            sym = ts_code.split('.')[0]
            direction = 'in' if row.get('in_de') == '增持' else 'out'
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO holder_trade
                    (symbol, ann_date, holder_name, holder_type, change_vol, change_ratio, direction, avg_price)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    sym,
                    row.get('ann_date', '')[:10] if row.get('ann_date') else '',
                    row.get('holder_name', ''),
                    row.get('holder_type', ''),
                    float(row.get('change_vol', 0) or 0),
                    float(row.get('change_ratio', 0) or 0),
                    direction,
                    float(row.get('avg_price', 0) or 0),
                ))
                total += 1
            except Exception as e_row:
                logger.debug(f"holder_trade row skip {sym}: {e_row}")

        conn.commit()
        time.sleep(0.6)  # Rate limit

    logger.info(f"holder_trade done: {total} rows")
    if close_conn:
        conn.close()
    return total


if __name__ == "__main__":
    import sys
    start = sys.argv[1] if len(sys.argv) > 1 else '2025-01-01'
    end = sys.argv[2] if len(sys.argv) > 2 else '2026-07-01'
    sync_range(start, end)
