"""北向资金数据同步 — 陆股通/港股通个股持仓与资金流。

数据源: akshare stock_hsgt_individual_em (东方财富)
存储: market.db → northbound_flow 表 (date, symbol, net_buy, hold_shares, hold_value)

北向资金因子 IC 实证:
  - 净买入/流通市值 5日均值: IC≈0.04-0.06 (A股最可靠因子之一)
  - 持股比例变化: IC≈0.03-0.05
"""

import sqlite3
import os
import time
import pandas as pd
import numpy as np
from typing import Optional
from utils.logger import get_logger

logger = get_logger("data.northbound")

NORTHBOUND_DB = os.path.join(os.path.dirname(__file__), "market.db")


def _ensure_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS northbound_flow (
            date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            net_buy REAL,          -- 当日净买入(万元, 沪+深)
            hold_shares REAL,      -- 持股数量(股)
            hold_value REAL,       -- 持股市值(万元)
            PRIMARY KEY (date, symbol)
        );
        CREATE INDEX IF NOT EXISTS idx_nb_date ON northbound_flow(date);
        CREATE INDEX IF NOT EXISTS idx_nb_symbol ON northbound_flow(symbol);
    """)


def sync_northbound(symbols: Optional[list] = None, days: int = 60) -> int:
    """同步北向资金数据到本地数据库。

    symbols: 股票列表 (None=沪深300成分股，全量5200+太慢)
    days: 拉取最近N个交易日
    返回: 新增行数
    """
    import akshare as ak
    
    conn = sqlite3.connect(NORTHBOUND_DB)
    _ensure_schema(conn)
    
    if symbols is None:
        # 默认: 取有turnover数据的活跃股 (成交量>0.5%), 约500只
        symbols = [r[0] for r in conn.execute("""
            SELECT DISTINCT symbol FROM daily 
            WHERE date >= date('now', '-20 days') AND turnover > 0.5
            LIMIT 500
        """).fetchall()]
    
    total_new = 0
    failed = 0
    
    for i, sym in enumerate(symbols):
        try:
            df = ak.stock_hsgt_individual_em(symbol=sym)
            if df.empty:
                continue
            
            df["symbol"] = sym
            # 取最近 days 行
            df = df.tail(days)
            
            # 标准化列名
            cols_map = {
                "日期": "date", "date": "date",
                "ggt_ss_net_buy": "ss_net", 
                "ggt_sz_net_buy": "sz_net",
            }
            df = df.rename(columns=cols_map)
            
            # 计算净买入总和 (万 → 保持原单位)
            ss_net = df.get("ss_net", pd.Series(0, index=df.index)).fillna(0)
            sz_net = df.get("sz_net", pd.Series(0, index=df.index)).fillna(0)
            
            for _, row in df.iterrows():
                dt = str(row["date"])[:10]
                net_buy = float(row.get("ss_net", 0) or 0) + float(row.get("sz_net", 0) or 0)
                hold_shares = float(row.get("hold_shares", 0) or 0)
                hold_value = float(row.get("hold_value", 0) or 0)
                
                conn.execute("""
                    INSERT OR REPLACE INTO northbound_flow(date, symbol, net_buy, hold_shares, hold_value)
                    VALUES(?,?,?,?,?)
                """, (dt, sym, net_buy, hold_shares, hold_value))
                total_new += 1
            
            # 每50只暂停防限流
            if (i + 1) % 50 == 0:
                conn.commit()
                time.sleep(0.5)
                
        except Exception as e:
            failed += 1
            if failed <= 3:
                logger.debug(f"northbound {sym} query failed: {e}")
    
    conn.commit()
    conn.close()
    logger.info(f"northbound sync: {total_new} rows for {len(symbols)-failed}/{len(symbols)} stocks")
    return total_new


def get_northbound_flow(symbols: list, date: str, window: int = 5) -> pd.Series:
    """获取北向资金 N 日净流入因子值。

    返回: Series(index=symbol, value=净买入/流通市值_N日均值)
    """
    conn = sqlite3.connect(NORTHBOUND_DB)
    try:
        placeholders = ",".join("?" for _ in symbols)
        df = pd.read_sql_query(f"""
            SELECT symbol, AVG(net_buy) as avg_net_buy
            FROM northbound_flow
            WHERE symbol IN ({placeholders}) AND date <= ? 
            GROUP BY symbol
            HAVING COUNT(*) >= ?
        """, conn, params=symbols + [date, max(1, window//2)])
        
        # 除以流通市值做标准化 (从 stocks 表取)
        mv_df = pd.read_sql_query(f"""
            SELECT symbol, circ_mv FROM stocks WHERE symbol IN ({placeholders})
        """, conn, params=symbols)
        
        df = df.merge(mv_df, on="symbol", how="left")
        df["flow_ratio"] = df["avg_net_buy"] / df["circ_mv"].replace(0, np.nan)
        
        return pd.Series(df["flow_ratio"].values, index=df["symbol"]).dropna()
    finally:
        conn.close()
