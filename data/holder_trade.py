"""大股东增减持数据同步 — akshare 同花顺股东增减持 API。

数据源: akshare.stock_shareholder_change_ths(symbol=...) (免费, 无需积分, 逐只拉取)
表: holder_trade (symbol, ann_date, holder_name, holder_type, change_vol, change_ratio, direction, avg_price)

因子: 过去60日大股东减持金额/流通市值 = holder_reduction. 减持→负面信号→负溢价.
"""
import os, sqlite3, time
from config.constants import _require_cfg

import pandas as pd
import akshare as ak
from utils.logger import get_logger

logger = get_logger("data.holder_trade")
DB_PATH = os.path.join(os.path.dirname(__file__), "market.db")


def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS holder_trade (
            symbol TEXT NOT NULL,
            ann_date TEXT NOT NULL,
            holder_name TEXT,
            holder_type TEXT,
            change_vol REAL,
            change_ratio REAL,
            direction TEXT,
            avg_price REAL,
            PRIMARY KEY (symbol, ann_date, holder_name)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_holder_date ON holder_trade(ann_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_holder_symbol ON holder_trade(symbol)")
    conn.commit()


def _parse_change_vol(raw: str) -> tuple:
    """解析 '增持470.37万' 或 '减持280.00万' → (direction, vol_shares)

    来源: 同花顺 变动数量 字段格式固定: '增持/减持 + 数量 + 单位(万/亿/股)'
    """
    if pd.isna(raw) or not raw:
        return ('', 0.0)

    s = str(raw)
    if s.startswith('增持'):
        direction = 'in'
        num_str = s[2:]
    elif s.startswith('减持'):
        direction = 'out'
        num_str = s[2:]
    else:
        return ('', 0.0)

    if '亿' in num_str:
        vol = float(num_str.replace('亿', '')) * 1_0000_0000
    elif '万' in num_str:
        vol = float(num_str.replace('万', '')) * 1_0000
    else:
        vol = float(num_str.replace('股', ''))

    return (direction, vol)


def _get_stock_pool(conn) -> list:
    """从 stocks 表获取股票池 code 列表."""
    rows = conn.execute("SELECT DISTINCT symbol FROM stocks").fetchall()
    return [r[0] for r in rows if r[0]]


def sync_range(start_date: str, end_date: str, conn=None) -> int:
    """同步大股东增减持数据 (akshare 逐只拉取). 返回新增行数."""
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=_require_cfg("data.sqlite.timeout"))
        conn.execute(f"PRAGMA busy_timeout = {_require_cfg('data.sqlite.busy_timeout')}")
        close_conn = True

    _ensure_table(conn)

    symbols = _get_stock_pool(conn)
    if not symbols:
        logger.warning("holder_trade: no stock pool found")
        if close_conn:
            conn.close()
        return 0

    total = 0
    for i, sym in enumerate(symbols):
        df = ak.stock_shareholder_change_ths(symbol=sym)

        if df is None or df.empty:
            continue

        df['公告日期'] = pd.to_datetime(df['公告日期'], errors='coerce')
        # 按 date 范围过滤
        if start_date:
            df = df[df['公告日期'] >= pd.Timestamp(start_date)]
        if end_date:
            df = df[df['公告日期'] <= pd.Timestamp(end_date)]

        if df.empty:
            continue

        for _, row in df.iterrows():
            direction, vol = _parse_change_vol(row.get('变动数量'))
            conn.execute("""
                INSERT OR REPLACE INTO holder_trade
                (symbol, ann_date, holder_name, holder_type, change_vol, change_ratio, direction, avg_price)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                sym,
                str(row['公告日期'])[:10] if pd.notna(row['公告日期']) else '',
                str(row.get('变动股东', '')),
                '',  # akshare 无 holder_type
                vol,
                0.0,  # akshare 无 change_ratio
                direction,
                0.0,  # akshare 返回的交易均价多为"未披露"
            ))
            total += 1

        # 进度 & rate limit
        if (i + 1) % 50 == 0:
            conn.commit()
            logger.info(f"holder_trade [{i+1}/{len(symbols)}] {total} rows")
            time.sleep(_require_cfg("data.api_delay.holder_trade"))

    conn.commit()
    logger.info(f"holder_trade done: {total} rows from {len(symbols)} stocks")
    if close_conn:
        conn.close()
    return total


if __name__ == "__main__":
    import sys
    start = sys.argv[1] if len(sys.argv) > 1 else '2025-01-01'
    end = sys.argv[2] if len(sys.argv) > 2 else '2026-07-01'
    sync_range(start, end)
