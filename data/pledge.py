"""股权质押数据同步 — Tushare pledge_stat API.

数据源: tushare.pro_api().pledge_stat()
表: pledge_stat (symbol, end_date, pledge_times, pledge_shares, pledge_amount, total_shares)

因子: 质押股数/总股本 = pledge_ratio. 高质押→崩盘风险→负溢价.
"""
import os, sqlite3, time

import pandas as pd
from utils.logger import get_logger

logger = get_logger("data.pledge")
DB_PATH = os.path.join(os.path.dirname(__file__), "market.db")


def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pledge_stat (
            symbol TEXT NOT NULL,
            end_date TEXT NOT NULL,
            pledge_times INTEGER,
            pledge_shares REAL,
            pledge_amount REAL,
            total_shares REAL,
            PRIMARY KEY (symbol, end_date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pledge_date ON pledge_stat(end_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pledge_symbol ON pledge_stat(symbol)")
    conn.commit()


def sync_range(start_date: str = None, end_date: str = None, conn=None) -> int:
    """同步股权质押统计. 返回新增行数."""
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
    try:
        df = pro.pledge_stat()
    except Exception as e:
        logger.warning(f"pledge_stat failed: {e}")
        if close_conn:
            conn.close()
        return 0

    if df is None or df.empty:
        if close_conn:
            conn.close()
        return 0

    for _, row in df.iterrows():
        ts_code = row.get('ts_code', '')
        if '.' not in str(ts_code):
            continue
        sym = ts_code.split('.')[0]
        try:
            conn.execute("""
                INSERT OR REPLACE INTO pledge_stat
                (symbol, end_date, pledge_times, pledge_shares, pledge_amount, total_shares)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                sym,
                row.get('end_date', '')[:10] if row.get('end_date') else '',
                int(row.get('pledge_times', 0) or 0),
                float(row.get('pledge_shares', 0) or 0),
                float(row.get('pledge_amount', 0) or 0),
                float(row.get('total_shares', 0) or 0),
            ))
            total += 1
        except Exception as e_row:
            logger.debug(f"pledge row skip {sym}: {e_row}")

    conn.commit()
    logger.info(f"pledge_stat done: {total} rows")
    if close_conn:
        conn.close()
    return total


if __name__ == "__main__":
    sync_range()
