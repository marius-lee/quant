"""龙虎榜数据同步。

数据源: akshare.stock_lhb_detail_em (东方财富)
表: lhb_detail (已存在, 需填充数据)
"""

import os
import sqlite3
import time
from datetime import datetime

import pandas as pd
from utils.logger import get_logger

logger = get_logger("data.lhb")

DB_PATH = os.path.join(os.path.dirname(__file__), "market.db")


def sync_lhb(start_date: str = "2025-01-01", end_date: str = None, conn=None) -> int:
    """同步龙虎榜明细数据。返回新增行数。"""
    import akshare as ak
    
    if end_date is None:
        end_date = datetime.today().strftime("%Y-%m-%d")
    
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close_conn = True
    
    try:
        df = ak.stock_lhb_detail_em(start_date=start_date, end_date=end_date)
        if df is None or df.empty:
            logger.info(f"LHB: no data for {start_date} → {end_date}")
            return 0
        
        # Normalize columns
        col_map = {
            '代码': 'symbol', 'SECURITY_CODE': 'symbol',
            '名称': 'name', 'SECURITY_NAME_ABBR': 'name',
            '上榜日期': 'trade_date', 'TRADE_DATE': 'trade_date',
            '收盘价': 'close', 'CLOSE_PRICE': 'close',
            '涨跌幅': 'change_pct', 'CHANGE_RATE': 'change_pct',
            '换手率': 'turnover_rate', 'TURNOVERRATE': 'turnover_rate',
            '龙虎榜净买额': 'net_buy', 'BILLBOARD_NET_AMT': 'net_buy',
            '龙虎榜买入额': 'buy_amt', 'BILLBOARD_BUY_AMT': 'buy_amt',
            '龙虎榜卖出额': 'sell_amt', 'BILLBOARD_SELL_AMT': 'sell_amt',
            '解读': 'reason', 'EXPLANATION': 'reason',
        }
        df = df.rename(columns=col_map)
        if 'trade_date' in df.columns:
            df['trade_date'] = pd.to_datetime(df['trade_date']).dt.strftime('%Y-%m-%d')
        
        n = 0
        for _, row in df.iterrows():
            try:
                sym = str(row.get('symbol', '')).zfill(6)
                conn.execute("""
                    INSERT OR REPLACE INTO lhb_detail 
                    (symbol, trade_date, close, change_pct, turnover_rate, net_buy, buy_amt, sell_amt, reason)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (sym, row.get('trade_date'), row.get('close'), row.get('change_pct'),
                      row.get('turnover_rate'), row.get('net_buy'), row.get('buy_amt'),
                      row.get('sell_amt'), row.get('reason')))
                n += 1
            except Exception:
                pass
        
        conn.commit()
        logger.info(f"LHB sync: {n} rows for {start_date} → {end_date}")
        
    except Exception as e:
        logger.warning(f"LHB sync failed: {e}")
    finally:
        if close_conn:
            conn.close()
    
    return n


if __name__ == "__main__":
    import sys
    s = sys.argv[1] if len(sys.argv) > 1 else "2025-01-01"
    e = sys.argv[2] if len(sys.argv) > 2 else None
    sync_lhb(start_date=s, end_date=e)
