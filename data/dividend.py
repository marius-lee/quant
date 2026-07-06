"""股息率数据同步 — Tushare dividend API.

数据源: tushare.pro_api().dividend()
表: dividend (symbol, div_year, cash_div, stk_div, record_date, ex_date)

因子: 过去12个月现金分红/股价 = dividend_yield. 高股息→正溢价 (A股震荡市).
"""
import os, sqlite3, time

import pandas as pd
from utils.logger import get_logger

logger = get_logger("data.dividend")
DB_PATH = os.path.join(os.path.dirname(__file__), "market.db")


def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dividend (
            symbol TEXT NOT NULL,
            end_date TEXT NOT NULL,
            div_year INTEGER,
            cash_div REAL,
            stk_div REAL,
            record_date TEXT,
            ex_date TEXT,
            PRIMARY KEY (symbol, end_date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dividend_year ON dividend(div_year)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dividend_symbol ON dividend(symbol)")
    conn.commit()


def sync_range(start_date: str = None, end_date: str = None, conn=None) -> int:
    """同步分红数据. 返回新增行数."""
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
        df = pro.dividend()
    except Exception as e:
        logger.warning(f"dividend failed: {e}")
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
                INSERT OR REPLACE INTO dividend
                (symbol, end_date, div_year, cash_div, stk_div, record_date, ex_date)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                sym,
                row.get('end_date', '')[:10] if row.get('end_date') else '',
                int(row.get('div_year', 0) or 0) if pd.notna(row.get('div_year')) else None,
                float(row.get('cash_div', 0) or 0),
                float(row.get('stk_div', 0) or 0),
                row.get('record_date', '')[:10] if row.get('record_date') else '',
                row.get('ex_date', '')[:10] if row.get('ex_date') else '',
            ))
            total += 1
        except Exception as e_row:
            logger.debug(f"dividend row skip {sym}: {e_row}")

    conn.commit()
    logger.info(f"dividend done: {total} rows")
    if close_conn:
        conn.close()
    return total


if __name__ == "__main__":
    sync_range()
