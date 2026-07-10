"""分红数据同步 — akshare 东方财富分红送配 API。

数据源: akshare.stock_history_dividend_detail(symbol=..., indicator='分红') (免费, 无需积分)
表: dividend (symbol, end_date, div_year, cash_div, stk_div, record_date, ex_date)

因子: 过去12月现金分红/股价 = dividend_yield. 高股息→正溢价 (A股震荡市).
"""
import os, sqlite3, time

import pandas as pd
import akshare as ak
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


def _get_stock_pool(conn) -> list:
    """从 stocks 表获取股票池 code 列表."""
    try:
        rows = conn.execute("SELECT DISTINCT symbol FROM stocks").fetchall()
        return [r[0] for r in rows if r[0]]
    except Exception:
        rows = conn.execute("SELECT DISTINCT symbol FROM daily").fetchall()
        return [r[0] for r in rows if r[0]]


def sync_range(start_date: str = None, end_date: str = None, conn=None) -> int:
    """同步分红数据 (akshare 逐只拉取, 免费无积分限制). 返回新增行数."""
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=_require_cfg("data.sqlite.timeout"))
        conn.execute(f"PRAGMA busy_timeout = {_require_cfg('data.sqlite.busy_timeout')}")
        close_conn = True

    _ensure_table(conn)

    symbols = _get_stock_pool(conn)
    if not symbols:
        logger.warning("dividend: no stock pool found")
        if close_conn:
            conn.close()
        return 0

    total = 0
    for i, sym in enumerate(symbols):
        try:
            df = ak.stock_history_dividend_detail(symbol=sym, indicator='分红', date='')
        except Exception as e:
            logger.debug(f"dividend {sym} fetch failed: {e}")
            continue

        if df is None or df.empty:
            continue

        df['除权除息日'] = pd.to_datetime(df['除权除息日'], errors='coerce')

        # 按 date 范围过滤 (如果有)
        if start_date:
            df = df[df['除权除息日'] >= pd.Timestamp(start_date)]
        if end_date:
            df = df[df['除权除息日'] <= pd.Timestamp(end_date)]

        if df.empty:
            continue

        for _, row in df.iterrows():
            try:
                ex_date = row['除权除息日']
                if pd.isna(ex_date):
                    continue

                # 派息 = 每10股派息金额, 转为每股
                cash_div_raw = float(row.get('派息', 0) or 0)
                cash_div = cash_div_raw / 10.0

                # 送股 + 转增 = 每10股送转数
                song_gu = float(row.get('送股', 0) or 0)
                zhuan_zeng = float(row.get('转增', 0) or 0)
                stk_div = (song_gu + zhuan_zeng) / 10.0  # 每股送转数

                # 股权登记日
                record_date = row.get('股权登记日')
                if pd.isna(record_date):
                    record_date = ''

                conn.execute("""
                    INSERT OR REPLACE INTO dividend
                    (symbol, end_date, div_year, cash_div, stk_div, record_date, ex_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (
                    sym,
                    str(ex_date)[:10],
                    int(ex_date.year),
                    cash_div,
                    stk_div,
                    str(record_date)[:10] if record_date else '',
                    str(ex_date)[:10],
                ))
                total += 1
            except Exception as e_row:
                logger.debug(f"dividend row skip {sym}: {e_row}")

        # 进度
        if (i + 1) % 100 == 0:
            conn.commit()
            logger.info(f"dividend [{i+1}/{len(symbols)}] {total} rows")
            time.sleep(_require_cfg("data.api_delay.dividend"))

    conn.commit()
    logger.info(f"dividend done: {total} rows from {len(symbols)} stocks")
    if close_conn:
        conn.close()
    return total


if __name__ == "__main__":
    sync_range()
