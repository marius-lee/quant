#!/usr/bin/env python3
# DEPRECATED (2026-07-05): baostock service 不可用 (Socket error)。
# daily_basic 表未被 pipeline 使用 — store.get_fundamentals() 实际走 daily_valuation (JQData)。
# 此模块保留供参考，若 baostock 恢复可复用。
"""Sync daily PE_TTM/PB from baostock to market.db. (DEPRECATED — see above)"""
import os, time, sqlite3, logging
from quant.config.constants import _require_cfg
import pandas as pd
import baostock as bs
from quant.utils.date import validate_date_format

logger = logging.getLogger("quant.data.daily_basic")

DB = os.path.join(os.path.dirname(__file__), "market.db")

def _get_symbols(conn):
    """Get all non-BJ stock codes, mapped to baostock format."""
    rows = conn.execute(
        "SELECT symbol, market FROM stocks WHERE symbol NOT LIKE '8%' AND market IN ('SH','SZ')"
    ).fetchall()
    # baostock format: sh.600519, sz.000001
    return [f"{r[1].lower()}.{r[0]}" for r in rows]

def sync_date(date_str, conn=None):
    """Sync PE_TTM/PB for one date. date_str: YYYY-MM-DD."""
    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(DB)
    
    bs.login()
    symbols = _get_symbols(conn)
    logger.info(f"daily_basic {date_str}: fetching {len(symbols)} stocks...")
    
    # Batch: query 10 stocks at a time
    batch_size = 10
    inserted = 0
    for i in range(0, len(symbols), batch_size):
        batch = symbols[i:i+batch_size]
        for bs_code in batch:
            rs = bs.query_history_k_data_plus(
                bs_code,
                "date,code,close,peTTM,pbMRQ",
                start_date=date_str.replace('-', '-'), end_date=date_str.replace('-', '-'),
                frequency="d", adjustflag="2")
            rows = []
            while rs.next():
                rows.append(rs.get_row_data())
            if rows:
                r = rows[0]
                sym = bs_code.split('.')[1]
                conn.execute(
                    """INSERT OR REPLACE INTO daily_basic (symbol, date, close, pe_ttm, pb)
                       VALUES (?, ?, ?, ?, ?)""",
                    (sym, r[0], float(r[2]), float(r[3]), float(r[4]))
                )
                inserted += 1
        if (i // batch_size) % 50 == 0 and i > 0:
            logger.info(f"daily_basic {date_str}: {i}/{len(symbols)} fetched, {inserted} inserted")
        time.sleep(_require_cfg("data.api_delay.daily_basic"))
    
    conn.commit()
    bs.logout()
    if own_conn:
        conn.close()
    logger.info(f"daily_basic {date_str}: {inserted}/{len(symbols)} stocks")
    return inserted

def sync_range(start_date, end_date, conn=None):
    """Sync a range of dates."""
    own_conn = conn is None
    if own_conn:
        conn = sqlite3.connect(DB)
    
    # Get trading days from daily table
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM daily WHERE date >= ? AND date <= ? ORDER BY date",
        (start_date, end_date)
    ).fetchall()]
    
    logger.info(f"daily_basic sync: {len(dates)} dates from {start_date} to {end_date}")
    total = 0
    for d in dates:
        n = sync_date(d, conn=conn)
        total += n
    
    if own_conn:
        conn.close()
    return total
