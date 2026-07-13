"""北向资金数据同步 — 沪深股通个股净买入。

数据源: akshare.stock_hsgt_individual_em (东方财富)
表: northbound_flow (date, symbol, net_buy, buy_amt, sell_amt, hold_shares, hold_ratio)
"""

import os
import sqlite3
from data.repos._base import DatabaseManager
import time
from config.constants import _require_cfg
from datetime import datetime, timedelta

import pandas as pd
from utils.logger import get_logger

logger = get_logger("data.northbound")

DB_PATH = os.path.join(os.path.dirname(__file__), "market.db")


def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS northbound_flow (
            date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            net_buy REAL,
            buy_amt REAL,
            sell_amt REAL,
            hold_shares REAL,
            hold_ratio REAL,
            PRIMARY KEY (date, symbol)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nb_date ON northbound_flow(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_nb_symbol ON northbound_flow(symbol)")
    conn.commit()


def sync_single_stock(symbol: str, conn=None) -> int:
    """同步单只股票的北向资金历史数据。返回新增行数。"""
    import akshare as ak
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close_conn = True
    
    _ensure_table(conn)
    
    n = 0
    df = ak.stock_hsgt_individual_em(symbol=symbol)
    if df is None or df.empty:
        return 0

    # Normalize columns
    col_map = {
        '持股日期': 'date', 'TRADE_DATE': 'date',
        '当日收盘价': 'close',
        '当日涨跌幅': 'change_pct',
        '持股数量': 'hold_shares', 'HOLD_SHARES': 'hold_shares',
        '持股市值': 'hold_value',
        '持股比例': 'hold_ratio', 'HOLD_RATIO': 'hold_ratio',
    }
    # Compute net_buy from change in hold_shares * close (approximate)
    # Ensure date column was mapped (skip if not found — e.g. 科创板)
    if "date" not in df.columns:
        return 0
    df = df.sort_values("date")
    if 'hold_shares' in df.columns and 'close' in df.columns:
        df['hold_change'] = df['hold_shares'].diff()
        df['net_buy'] = df['hold_change'] * df['close']

    # Filter to columns we have in table
    cols = ['date', 'symbol', 'net_buy', 'buy_amt', 'sell_amt', 'hold_shares', 'hold_ratio']
    df = df[[c for c in cols if c in df.columns]]

    n = 0
    for _, row in df.iterrows():
        conn.execute("""
            INSERT OR REPLACE INTO northbound_flow 
            (date, symbol, net_buy, buy_amt, sell_amt, hold_shares, hold_ratio)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (row.get('date'), symbol, row.get('net_buy'), row.get('buy_amt'),
              row.get('sell_amt'), row.get('hold_shares'), row.get('hold_ratio')))
        n += 1
    conn.commit()
    if n > 0:
        logger.info(f"northbound: {symbol} — {n} rows synced")
    
    return n


def sync_all(max_stocks: int = None, conn=None) -> int:
    """同步所有 A 股的北向资金数据。"""
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close_conn = True
    
    _ensure_table(conn)
    
    # Get all symbols from stocks table
    symbols = [r[0] for r in conn.execute(
        "SELECT symbol FROM stocks WHERE market IN ('SH','SZ') ORDER BY total_mv DESC"
    ).fetchall()]
    
    if max_stocks:
        symbols = symbols[:max_stocks]
    
    total = 0
    for i, sym in enumerate(symbols):
        n = sync_single_stock(sym, conn=conn)
        total += n
        if (i + 1) % 50 == 0:
            logger.info(f"northbound sync: {i+1}/{len(symbols)} stocks, {total} rows")
        time.sleep(_require_cfg("data.api_delay.northbound"))
    
    logger.info(f"northbound sync done: {total} rows for {len(symbols)} stocks")
    
    if close_conn:
        conn.close()
    return total


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    sync_all(max_stocks=n)
