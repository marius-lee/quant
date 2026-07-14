"""股权质押数据同步 — akshare 东方财富质押比例 API。

数据源: akshare.stock_gpzy_pledge_ratio_em() (免费, 无需积分)
表: pledge_stat (symbol, end_date, pledge_times, pledge_shares, pledge_amount, total_shares)

因子: 质押股数/总股本 = pledge_ratio. 高质押→崩盘风险→负溢价.
"""
import os, sqlite3, time

import pandas as pd
import akshare as ak
from quant.utils.logger import get_logger

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
    """同步股权质押统计 (akshare 批拉全市场). 返回新增行数."""
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close_conn = True

    _ensure_table(conn)

    total = 0
    df = ak.stock_gpzy_pledge_ratio_em()

    if df is None or df.empty:
        if close_conn:
            conn.close()
        return 0

    # 列映射: 股票代码→symbol, 交易日期→end_date, 质押笔数→pledge_times,
    #          质押股数→pledge_shares, 质押市值→pledge_amount
    for _, row in df.iterrows():
        code = str(row.get('股票代码', ''))
        if not code or len(code) != 6:
            continue
        # 统一为 6 位数字
        sym = code.zfill(6)
        # 从质押比例(%)反推总股本: total_shares = 质押股数 / (质押比例%/100)
        pledge_shares_val = float(row.get('质押股数', 0) or 0)
        pledge_ratio_pct = float(row.get('质押比例', 0) or 0)
        if pledge_ratio_pct > 0 and pledge_shares_val > 0:
            total_shares_val = round(pledge_shares_val / (pledge_ratio_pct / 100.0), 2)
        else:
            total_shares_val = None
        conn.execute("""
            INSERT OR REPLACE INTO pledge_stat
            (symbol, end_date, pledge_times, pledge_shares, pledge_amount, total_shares)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            sym,
            str(row.get('交易日期', ''))[:10] if pd.notna(row.get('交易日期')) else '',
            int(row.get('质押笔数', 0) or 0),
            pledge_shares_val,
            float(row.get('质押市值', 0) or 0),
            total_shares_val,
        ))
        total += 1

    conn.commit()
    logger.info(f"pledge_stat done: {total} rows")
    if close_conn:
        conn.close()
    return total


if __name__ == "__main__":
    sync_range()
