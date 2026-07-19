"""涨停板数据同步 — A股每日涨停/跌停池。

数据源: akshare.stock_zt_pool_em (东方财富)
表: limit_up_pool (新)

涨停因子是最强A股动量预测器:
- 涨停连板概率 ~30-40% (A股独有现象)
- 首板次日的正收益概率 ~60%
- IC 显著高于传统价格因子
"""

import os
import sqlite3
from quant.data.repos._base import DatabaseManager
import time
from quant.config.constants import _require_cfg
from datetime import datetime, timedelta

import pandas as pd
from quant.utils.logger import get_logger
from quant.utils.date import validate_date_format

logger = get_logger("data.limit_up")

DB_PATH = os.path.join(os.path.dirname(__file__), "market.db")


def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS limit_up_pool (
            date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT,
            change_pct REAL,
            close REAL,
            amount REAL,
            circ_mv REAL,
            total_mv REAL,
            turnover_rate REAL,
            lock_capital REAL,
            first_time TEXT,
            last_time TEXT,
            open_times INTEGER,
            zt_stat TEXT,
            limit_up_times INTEGER,
            industry TEXT,
            PRIMARY KEY (date, symbol)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_zt_date ON limit_up_pool(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_zt_symbol ON limit_up_pool(symbol)")
    conn.commit()


def sync_date(date_str: str, conn=None) -> int:
    """同步单日涨停池。返回新增行数。"""
    import akshare as ak
    
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close_conn = True
    
    _ensure_table(conn)
    
    df = ak.stock_zt_pool_em(date=date_str.replace('-', ''))
    if df.empty:
        return 0

    # Normalize
    col_map = {
        '代码': 'symbol',
        '名称': 'name',
        '涨跌幅': 'change_pct',
        '最新价': 'close',
        '成交额': 'amount',
        '流通市值': 'circ_mv',
        '总市值': 'total_mv',
        '换手率': 'turnover_rate',
        '封板资金': 'lock_capital',
        '首次封板时间': 'first_time',
        '最后封板时间': 'last_time',
        '炸板次数': 'open_times',
        '涨停统计': 'zt_stat',
        '连板数': 'limit_up_times',
        '所属行业': 'industry',
    }
    df = df.rename(columns=col_map)
    df['date'] = date_str
    df['symbol'] = df['symbol'].astype(str).str.zfill(6)

    n = 0
    for _, row in df.iterrows():
        conn.execute("""
            INSERT OR REPLACE INTO limit_up_pool 
            (date, symbol, name, change_pct, close, amount, circ_mv, total_mv,
             turnover_rate, lock_capital, first_time, last_time, open_times, 
             zt_stat, limit_up_times, industry)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (date_str, row['symbol'], row.get('name'),
              row.get('change_pct'), row.get('close'), row.get('amount'),
              row.get('circ_mv'), row.get('total_mv'), row.get('turnover_rate'),
              row.get('lock_capital'), row.get('first_time'), row.get('last_time'),
              row.get('open_times'), row.get('zt_stat'), row.get('limit_up_times'),
              row.get('industry')))
        n += 1
    conn.commit()
    logger.info(f"limit_up: {date_str} — {n} stocks")
    
    return n


def sync_range(start_date: str, end_date: str = None, conn=None) -> int:
    """同步区间内每个交易日的涨停池。"""
    if end_date is None:
        end_date = datetime.today().strftime("%Y-%m-%d")
    
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close_conn = True
    
    # Get trading days from daily table
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM daily WHERE date >= ? AND date <= ? ORDER BY date",
        (start_date, end_date)
    ).fetchall()]
    
    total = 0
    for i, d in enumerate(dates):
        n = sync_date(d, conn=conn)
        total += n
        if (i + 1) % 10 == 0:
            logger.info(f"limit_up sync: {i+1}/{len(dates)} dates, {total} stocks")
        time.sleep(_require_cfg("data.api_delay.limit_up"))
    
    logger.info(f"limit_up sync done: {total} rows for {len(dates)} dates")
    
    if close_conn:
        conn.close()
    return total


if __name__ == "__main__":
    import sys
    s = sys.argv[1] if len(sys.argv) > 1 else "2026-01-01"
    e = sys.argv[2] if len(sys.argv) > 2 else None
    sync_range(start_date=s, end_date=e)
