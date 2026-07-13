"""沪深 300 基准数据 — akshare 免费 API 拉取指数日线，存 SQLite。

用于回测收益对比 (M5)。
用法:
  from data.benchmark import get_benchmark_returns
  bm_returns = get_benchmark_returns("2026-01-01", "2026-06-30")  # pd.Series
"""

import sqlite3
from data.repos._base import DatabaseManager, os
from datetime import datetime
import pandas as pd
from utils.logger import get_logger

logger = get_logger("data.benchmark")
_MARKET_DB = os.path.join(os.path.dirname(__file__), "market.db")

BENCHMARKS = {
    "000300": "沪深300",
    "000905": "中证500",
    "000016": "上证50",
}


def _init_db():
    conn = sqlite3.connect(_MARKET_DB)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS benchmark_daily (
            index_code TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL,
            volume REAL, amount REAL,
            PRIMARY KEY (index_code, date)
        )
    """)
    conn.commit()
    return conn


def sync_benchmark(index_code: str = "000300") -> int:
    """同步指数日线数据。返回新写入行数。"""
    import akshare as ak
    conn = _init_db()
    # 获取已有日期范围
    row = conn.execute(
        "SELECT MAX(date) FROM benchmark_daily WHERE index_code=?",
        (index_code,)
    ).fetchone()
    last_date = row[0] if row and row[0] else "2020-01-01"

    df = ak.stock_zh_index_daily(symbol=f"sh{index_code}")
    if df is None or df.empty:
        logger.warning(f"benchmark {index_code}: empty response")
        return 0
    new_rows = 0
    for _, row in df.iterrows():
        d = str(row.get("date", ""))[:10]
        if d <= last_date:
            continue
        conn.execute(
            """INSERT OR IGNORE INTO benchmark_daily
               (index_code, date, open, high, low, close, volume, amount)
               VALUES (?,?,?,?,?,?,?,?)""",
            (index_code, d,
             float(row.get("open", 0) or 0),
             float(row.get("high", 0) or 0),
             float(row.get("low", 0) or 0),
             float(row.get("close", 0) or 0),
             float(row.get("volume", 0) or 0),
             float(row.get("amount", 0) or 0))
        )
        new_rows += 1
    conn.commit()
    logger.info(f"benchmark {index_code} ({BENCHMARKS.get(index_code, '')}): {new_rows} new rows")
    return new_rows


def get_benchmark_returns(index_code: str = "000300",
                           start: str = "2020-01-01",
                           end: str = None) -> pd.Series:
    """获取指数日收益率序列 (close-to-close, 百分比)。

    返回: pd.Series(index=date, value=pct_return)
    """
    if end is None:
        end = datetime.today().strftime("%Y-%m-%d")
    conn = sqlite3.connect(_MARKET_DB)
    df = pd.read_sql_query(
        "SELECT date, close FROM benchmark_daily WHERE index_code=? AND date>=? AND date<=? ORDER BY date",
        conn, params=(index_code, start, end)
    )
    conn.close()
    if df.empty:
        return pd.Series(dtype=float, name="benchmark_return")
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    returns = df["close"].pct_change().dropna() * 100
    returns.name = "benchmark_return"
    return returns


def get_benchmark_cumulative(index_code: str = "000300",
                              start: str = "2020-01-01",
                              end: str = None) -> pd.Series:
    """获取指数累计收益序列 (百分比, 初始=0%)。

    返回: pd.Series(index=date, value=cumulative_return_pct)
    """
    returns = get_benchmark_returns(index_code, start, end)
    if returns.empty:
        return returns
    cumulative = (1 + returns / 100).cumprod()
    cumulative = (cumulative - 1) * 100
    cumulative.name = f"{index_code}_cumulative"
    return cumulative
