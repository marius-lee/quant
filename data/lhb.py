"""龙虎榜数据同步 — 多 API 回退。

数据源优先级:
  1. stock_lhb_detail_em (东方财富 — 当前不可用, result=None)
  2. stock_lhb_stock_detail_em (东方财富 — 按股票查, 需循环)
  3. stock_lhb_ggtj_sina (新浪 — 个股龙虎榜统计)

表: lhb_detail (date, symbol, close, change_pct, turnover_rate, net_buy, buy_amt, sell_amt, reason)
"""

import os
import sqlite3
import time
from datetime import datetime, timedelta

import pandas as pd
from utils.logger import get_logger

logger = get_logger("data.lhb")

DB_PATH = os.path.join(os.path.dirname(__file__), "market.db")


def sync_lhb_stock(symbol: str, conn=None) -> int:
    """同步单只股票的龙虎榜历史 (东方财富 API)."""
    try:
        import akshare as ak
        df = ak.stock_lhb_stock_detail_em(symbol=symbol)
        if df is None or df.empty:
            return 0
        
        close_conn = False
        if conn is None:
            conn = sqlite3.connect(DB_PATH)
            close_conn = True
        
        col_map = {
            '上榜日期': 'trade_date', 'TRADE_DATE': 'trade_date',
            '收盘价': 'close', 'CLOSE_PRICE': 'close',
            '涨跌幅': 'change_pct', 'CHANGE_RATE': 'change_pct',
            '换手率': 'turnover_rate', 'TURNOVERRATE': 'turnover_rate',
            '龙虎榜净买入': 'net_buy', '龙虎榜买入额': 'buy_amt',
            '龙虎榜卖出额': 'sell_amt', '解读': 'reason',
        }
        df = df.rename(columns=col_map)
        df['symbol'] = str(symbol).zfill(6)
        
        if 'trade_date' in df.columns:
            df['trade_date'] = pd.to_datetime(df['trade_date']).dt.strftime('%Y-%m-%d')
        
        n = 0
        for _, row in df.iterrows():
            try:
                conn.execute("""
                    INSERT OR REPLACE INTO lhb_detail 
                    (symbol, trade_date, close, change_pct, turnover_rate, net_buy, buy_amt, sell_amt, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (row['symbol'], row.get('trade_date'), row.get('close'),
                      row.get('change_pct'), row.get('turnover_rate'),
                      row.get('net_buy'), row.get('buy_amt'), row.get('sell_amt'),
                      row.get('reason')))
                n += 1
            except Exception:
                pass
        
        conn.commit()
        if close_conn:
            conn.close()
        return n
    except Exception as e:
        logger.debug(f"LHB {symbol}: {e}")
        return 0


def sync_lhb_range(start_date: str = "2025-01-01", end_date: str = None,
                   max_stocks: int = 500, conn=None) -> int:
    """同步龙虎榜 (遍历 top N 股票)."""
    if end_date is None:
        end_date = datetime.today().strftime("%Y-%m-%d")
    
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close_conn = True
    
    # Get top N stocks by market cap (most likely to appear on LHB)
    symbols = [r[0] for r in conn.execute(
        "SELECT symbol FROM stocks WHERE market IN ('SH','SZ') ORDER BY total_mv DESC LIMIT ?",
        (max_stocks,)
    ).fetchall()]
    
    total = 0
    for i, sym in enumerate(symbols):
        n = sync_lhb_stock(sym, conn=conn)
        total += n
        if (i + 1) % 100 == 0:
            logger.info(f"LHB sync: {i+1}/{len(symbols)} stocks, {total} rows")
        time.sleep(0.2)  # Rate limit
    
    logger.info(f"LHB sync done: {total} rows from {len(symbols)} stocks")
    
    if close_conn:
        conn.close()
    return total


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    sync_lhb_range(max_stocks=n)
