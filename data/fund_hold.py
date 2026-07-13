"""基金持仓数据同步 — 季度基金重仓股。

数据源: akshare.stock_report_fund_hold (每季报披露后更新)
表: fund_hold (symbol, report_date, fund_count, hold_shares, hold_mv, change_type, change_ratio)
频率: 季度 (3/31, 6/30, 9/30, 12/31), 披露滞后~1月
"""

import os, sqlite3, time
from datetime import datetime

import pandas as pd
from config.constants import _require_cfg
from utils.logger import get_logger

logger = get_logger("data.fund_hold")
DB_PATH = os.path.join(os.path.dirname(__file__), "market.db")


def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fund_hold (
            symbol TEXT NOT NULL,
            report_date TEXT NOT NULL,
            fund_count INTEGER,
            hold_shares REAL,
            hold_mv REAL,
            change_type TEXT,
            change_ratio REAL,
            PRIMARY KEY (symbol, report_date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fh_date ON fund_hold(report_date)")
    conn.commit()


def sync_quarter(report_date: str, conn=None) -> int:
    """同步单个季度的基金持仓。返回新增行数。"""
    import akshare as ak
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close_conn = True

    _ensure_table(conn)

    df = ak.stock_report_fund_hold(date=report_date)
    if df is None or df.empty:
        return 0

    col_map = {
        '股票代码': 'symbol', '股票简称': 'name',
        '持有基金家数': 'fund_count', '持股总数': 'hold_shares',
        '持股市值': 'hold_mv', '持股变化': 'change_type',
        '持股变动数值': 'change_amount', '持股变动比例': 'change_ratio',
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    df['report_date'] = report_date
    if 'symbol' in df.columns:
        df['symbol'] = df['symbol'].astype(str).str.zfill(6)

    n = 0
    for _, row in df.iterrows():
        sym = str(row.get('symbol', '')).strip()
        if len(sym) < 6:
            continue
        conn.execute("""
            INSERT OR REPLACE INTO fund_hold
            (symbol, report_date, fund_count, hold_shares, hold_mv, change_type, change_ratio)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (sym, report_date,
              row.get('fund_count'), row.get('hold_shares'),
              row.get('hold_mv'), row.get('change_type'),
              row.get('change_ratio')))
        n += 1
    conn.commit()

    print(f"  {report_date}: {n} stocks")
    return n


def sync_recent(conn=None):
    """同步最近几个季度的数据。"""
    quarters = ['20241231', '20250331', '20250630', '20250930', '20251231']
    total = 0
    for q in quarters:
        n = sync_quarter(q, conn=conn)
        total += n
        time.sleep(_require_cfg("data.api_delay.fund_hold"))
    return total


if __name__ == "__main__":
    sync_recent()
