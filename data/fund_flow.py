"""个股资金流向数据同步 — 主力/超大单/大单净流入。

数据源: akshare.stock_individual_fund_flow (东方财富)
API 限流敏感: 请求间隔需 >= 1.5s, 否则远端断连接
表: fund_flow (symbol, date, close, change_pct, main_net_inflow, main_net_ratio, ...)
"""

import os, sqlite3, time
from datetime import datetime

import pandas as pd
from utils.logger import get_logger

logger = get_logger("data.fund_flow")
DB_PATH = os.path.join(os.path.dirname(__file__), "market.db")


def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fund_flow (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            close REAL,
            change_pct REAL,
            main_net_inflow REAL,
            main_net_ratio REAL,
            super_large_net_inflow REAL,
            super_large_net_ratio REAL,
            large_net_inflow REAL,
            large_net_ratio REAL,
            mid_net_inflow REAL,
            mid_net_ratio REAL,
            small_net_inflow REAL,
            small_net_ratio REAL,
            PRIMARY KEY (symbol, date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ff_date ON fund_flow(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ff_symbol ON fund_flow(symbol)")
    conn.commit()


def sync_single_stock(symbol: str, market: str = 'sh', conn=None, max_retries: int = 3) -> int:
    """同步单只股票的资金流向历史数据。带重试逻辑。返回新增行数。"""
    import akshare as ak
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close_conn = True

    _ensure_table(conn)

    last_err = None
    for attempt in range(max_retries):
        try:
            df = ak.stock_individual_fund_flow(stock=symbol, market=market)
            if df is None or df.empty:
                if close_conn:
                    conn.close()
                return 0

            col_map = {
                '日期': 'date', '收盘价': 'close', '涨跌幅': 'change_pct',
                '主力净流入-净额': 'main_net_inflow', '主力净流入-净占比': 'main_net_ratio',
                '超大单净流入-净额': 'super_large_net_inflow', '超大单净流入-净占比': 'super_large_net_ratio',
                '大单净流入-净额': 'large_net_inflow', '大单净流入-净占比': 'large_net_ratio',
                '中单净流入-净额': 'mid_net_inflow', '中单净流入-净占比': 'mid_net_ratio',
                '小单净流入-净额': 'small_net_inflow', '小单净流入-净占比': 'small_net_ratio',
            }
            df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
            df['date'] = pd.to_datetime(df['date']).dt.strftime('%Y-%m-%d')
            df['symbol'] = symbol

            n = 0
            for _, row in df.iterrows():
                try:
                    conn.execute("""
                        INSERT OR REPLACE INTO fund_flow
                        (symbol, date, close, change_pct, main_net_inflow, main_net_ratio,
                         super_large_net_inflow, super_large_net_ratio, large_net_inflow, large_net_ratio,
                         mid_net_inflow, mid_net_ratio, small_net_inflow, small_net_ratio)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (symbol, row.get('date'), row.get('close'), row.get('change_pct'),
                          row.get('main_net_inflow'), row.get('main_net_ratio'),
                          row.get('super_large_net_inflow'), row.get('super_large_net_ratio'),
                          row.get('large_net_inflow'), row.get('large_net_ratio'),
                          row.get('mid_net_inflow'), row.get('mid_net_ratio'),
                          row.get('small_net_inflow'), row.get('small_net_ratio')))
                    n += 1
                except Exception:
                    pass

            conn.commit()
            if close_conn:
                conn.close()
            return n

        except Exception as e:
            last_err = e
            if attempt < max_retries - 1:
                wait = (attempt + 1) * 3  # 3s, 6s, 9s backoff
                time.sleep(wait)
            else:
                logger.warning(f"fund_flow sync failed for {symbol} after {max_retries} retries: {last_err}")
                if close_conn:
                    conn.close()
                return 0

    return 0


def sync_all(max_stocks: int = 500, conn=None):
    """同步市值最大的 N 只股票的资金流向数据。"""
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close_conn = True

    _ensure_table(conn)

    symbols = [r[0] for r in conn.execute(
        "SELECT symbol FROM stocks WHERE market IN ('SH','SZ') ORDER BY total_mv DESC"
    ).fetchall()]

    if max_stocks:
        symbols = symbols[:max_stocks]

    total = 0
    ok = 0
    fail = 0
    for i, sym in enumerate(symbols):
        mkt = 'sh' if sym.startswith(('6', '68')) else 'sz'
        n = sync_single_stock(sym, market=mkt, conn=conn)
        total += n
        if n > 0:
            ok += 1
        else:
            fail += 1
        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{len(symbols)}] ok={ok} fail={fail} total_rows={total}")
        # Rate limit: ~1.5s between requests (API 限流)
        time.sleep(5.0)

    logger.info(f"fund_flow sync done: {total} rows for {ok} stocks ({fail} failed)")
    print(f"Done: {total} rows, {ok} ok, {fail} failed")

    if close_conn:
        conn.close()
    return total


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    sync_all(max_stocks=n)
