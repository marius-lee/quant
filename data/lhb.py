"""龙虎榜数据同步 — 东方财富 API, 按月批量拉取。

数据源: akshare.stock_lhb_detail_em (东方财富) — 日期范围接口, ~2000行/月
表: lhb_detail (symbol, trade_date, close, change_pct, turnover_rate,
    net_buy, buy_amt, sell_amt, reason, name, circ_mv, post_1d, post_2d, post_5d, post_10d)

历史数据可通过按月遍历全部回溯, ~1-2 分钟即可补齐 1 年数据。
"""

import os
import sqlite3
import time
from config.constants import _require_cfg
from datetime import datetime, timedelta

import pandas as pd
from utils.logger import get_logger

logger = get_logger("data.lhb")

DB_PATH = os.path.join(os.path.dirname(__file__), "market.db")


def _ensure_table(conn):
    """创建或升级 lhb_detail 表结构。"""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lhb_detail (
            symbol TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            close REAL,
            change_pct REAL,
            turnover_rate REAL,
            net_buy REAL,
            buy_amt REAL,
            sell_amt REAL,
            reason TEXT,
            name TEXT,
            circ_mv REAL,
            post_1d REAL,
            post_2d REAL,
            post_5d REAL,
            post_10d REAL,
            PRIMARY KEY (symbol, trade_date)
        )
    """)
    # 动态加列 (兼容旧 schema)
    for col, typ in [('name','TEXT'),('circ_mv','REAL'),
                      ('post_1d','REAL'),('post_2d','REAL'),
                      ('post_5d','REAL'),('post_10d','REAL')]:
        try:
            conn.execute(f"ALTER TABLE lhb_detail ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass  # 列已存在
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lhb_date ON lhb_detail(trade_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lhb_symbol ON lhb_detail(symbol)")
    conn.commit()


def sync_month(year: int, month: int, conn=None) -> int:
    """同步一个月(YYYY-MM)的龙虎榜明细。返回新增行数。"""
    import akshare as ak

    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close_conn = True

    _ensure_table(conn)

    # 计算月份起止日期
    start = f"{year}{month:02d}01"
    import calendar
    last_day = calendar.monthrange(year, month)[1]
    end = f"{year}{month:02d}{last_day:02d}"

    n = 0
    try:
        df = ak.stock_lhb_detail_em(start_date=start, end_date=end)
        if df is None or df.empty:
            logger.info(f"lhb {year}-{month:02d}: 0 rows")
            return 0

        # 列映射: API 中文 → 数据库英文
        col_map = {
            '代码': 'symbol', '名称': 'name',
            '上榜日': 'trade_date', 'TRADE_DATE': 'trade_date',
            '收盘价': 'close', 'CLOSE_PRICE': 'close',
            '涨跌幅': 'change_pct', 'CHANGE_RATE': 'change_pct',
            '换手率': 'turnover_rate', 'TURNOVERRATE': 'turnover_rate',
            '龙虎榜净买额': 'net_buy', '龙虎榜买入额': 'buy_amt',
            '龙虎榜卖出额': 'sell_amt', '上榜原因': 'reason',
            '流通市值': 'circ_mv',
            '上榜后1日': 'post_1d', '上榜后2日': 'post_2d',
            '上榜后5日': 'post_5d', '上榜后10日': 'post_10d',
        }
        existing = set(col_map.keys()) & set(df.columns)
        df = df.rename(columns={k: v for k, v in col_map.items() if k in existing})

        if 'symbol' in df.columns:
            df['symbol'] = df['symbol'].astype(str).str.zfill(6)
        if 'trade_date' in df.columns:
            df['trade_date'] = pd.to_datetime(df['trade_date']).dt.strftime('%Y-%m-%d')

        # 写入
        cols = ['symbol','trade_date','close','change_pct','turnover_rate',
                'net_buy','buy_amt','sell_amt','reason','name','circ_mv',
                'post_1d','post_2d','post_5d','post_10d']
        cols = [c for c in cols if c in df.columns]

        for _, row in df.iterrows():
            try:
                placeholders = ','.join('?' * len(cols))
                conn.execute(
                    f"INSERT OR REPLACE INTO lhb_detail ({','.join(cols)}) VALUES ({placeholders})",
                    [row.get(c) for c in cols]
                )
                n += 1
            except Exception as e_row:
                logger.debug(f"lhb row skip: {e_row}")

        conn.commit()
        logger.info(f"lhb {year}-{month:02d}: {n} rows")

    except Exception as e:
        logger.warning(f"lhb {year}-{month:02d} failed: {e}")

    if close_conn:
        conn.close()
    return n



def sync_date(date_str: str) -> int:
    """同步单日所在月份的龙虎榜。daily_sync.py step4 适配接口。"""
    dt = datetime.strptime(date_str, "%Y-%m-%d")
    return sync_month(dt.year, dt.month)
def sync_range(start_year: int, start_month: int,
               end_year: int = None, end_month: int = None,
               conn=None) -> int:
    """按月同步[start_year-start_month, end_year-end_month]区间龙虎榜。

    默认 end = 当前月份。适合初次全量回填和按月增量更新。
    """
    now = datetime.today()
    if end_year is None:
        end_year = now.year
    if end_month is None:
        end_month = now.month

    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close_conn = True

    total = 0
    y, m = start_year, start_month
    while (y < end_year) or (y == end_year and m <= end_month):
        n = sync_month(y, m, conn=conn)
        total += n
        m += 1
        if m > 12:
            m = 1
            y += 1
        time.sleep(_require_cfg("data.api_delay.lhb"))

    logger.info(f"lhb sync done: {total} rows")

    if close_conn:
        conn.close()
    return total


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        sync_range(int(sys.argv[1]), int(sys.argv[2]))
    else:
        # 默认: 从 2025-01 同步到当前月
        sync_range(2025, 1)
