"""融资融券数据同步 — 上交所 SSE JSON API (深交所 SZSE 待修)。

数据源: query.sse.com.cn JSON API 直接调用
API 限流: 请求间隔 >= 5s
表: margin_detail (symbol, date, market, margin_buy, margin_balance, ...)
"""

import os, sqlite3, time, json
import pandas as pd
from datetime import datetime

import requests
from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger
from quant.utils.date import validate_date_format, to_compact
from quant.config.paths import MARKET_DB

logger = get_logger("data.margin")
DB_PATH = MARKET_DB

SSE_HEADERS = {
    "Referer": "https://www.sse.com.cn/",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
}


def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS margin_detail (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            market TEXT NOT NULL,
            margin_buy REAL,
            margin_balance REAL,
            margin_repay REAL,
            short_sell_vol REAL,
            short_balance REAL,
            short_total REAL,
            margin_total REAL,
            PRIMARY KEY (symbol, date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_margin_date ON margin_detail(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_margin_symbol ON margin_detail(symbol)")
    conn.commit()


def _get_trading_days(conn, start, end):
    rows = conn.execute(
        "SELECT DISTINCT date FROM daily WHERE date BETWEEN ? AND ? ORDER BY date",
        (start, end)
    ).fetchall()
    return [r[0] for r in rows]


def _to_float(v):
    if v is None:
        return None
    return float(v)


def _sync_sse_raw(date_str: str, conn) -> int:
    """直接调上交所 JSON API, 返回插入行数。"""
    sse_date = date_str.replace('-', '')  # SSE API requires YYYYMMDD
    url = "https://query.sse.com.cn/marketdata/tradedata/queryMargin.do"
    params = {
        "isPagination": "true", "tabType": "mxtype", "detailsDate": sse_date,
        "pageHelp.pageSize": "5000", "pageHelp.pageNo": "1",
        "pageHelp.beginPage": "1", "pageHelp.endPage": "21"
    }
    r = requests.get(url, params=params, headers=SSE_HEADERS, timeout=_require_cfg("data.http_timeout.sse"))
    data = r.json()
    rows = data.get("result", [])
    if not rows:
        return 0

    # v408: executemany
    # v548: 修复两缺陷 — (1) short_total 漏写: batch 未含融券余额, SQL 列也未含,
    #         9 列 8 值错位绑定 (SH margin_total 恒 NULL); (2) 现在 10 列 10 值对齐
    batch = []
    for row in rows:
        sym = row.get("stockCode", "")
        if not sym or len(sym) < 6:
            continue
        batch.append((sym, date_str,
              _to_float(row.get("rzmre")),
              _to_float(row.get("rzye")),
              _to_float(row.get("rzche")),
              _to_float(row.get("rqmcl")),
              _to_float(row.get("rqyl")),
              _to_float(row.get("rqye")),
              None))
    if batch:
        conn.executemany("""
            INSERT OR REPLACE INTO margin_detail
            (symbol, date, market, margin_buy, margin_balance, margin_repay,
             short_sell_vol, short_balance, short_total, margin_total)
            VALUES (?, ?, 'SH', ?, ?, ?, ?, ?, ?, ?)
        """, batch)
        conn.commit()
        n = len(batch)
    return n



def _get_synced_dates(conn):
    """获取已同步日期 — SH 与 SZ 双市场均已写入才算 synced。

    v430: 原实现按整日粒度 — SZSE 发布延迟 (独立日期) 时该日被永久跳过,
          sync_range 后续运行永不再补. 现按 market 存在性判定, 半同步日保留可补.
    """
    rows = conn.execute(
        "SELECT date, COUNT(DISTINCT market) AS n FROM margin_detail"
        " WHERE market IN ('SH','SZ') GROUP BY date HAVING n = 2"
    ).fetchall()
    return {r[0] for r in rows}


def _sync_szse_wrapper(date_str: str, conn) -> int:
    """深交所融资融券 — akshare wrapper + 指数退避重试 (1-2-4-8s)。"""
    import akshare as ak
    from quant.data.datasource_retry import datasource_retry

    @datasource_retry
    def _fetch_margin(date_compact):
        return ak.stock_margin_detail_szse(date=date_compact)
    df = _fetch_margin(to_compact(date_str))
    if df is None or df.empty:
        return 0

    col_map = {
        '证券代码': 'symbol', '证券简称': 'name',
        '融资余额': 'margin_balance', '融资买入额': 'margin_buy',
        '融券卖出量': 'short_sell_vol', '融券余量': 'short_balance',
        '融券余额': 'short_total', '融资融券余额': 'margin_total',
    }
    df = df.rename(columns={k: v for k, v in col_map.items() if k in df.columns})
    df['date'] = date_str
    df['market'] = 'SZ'
    if 'symbol' in df.columns:
        df['symbol'] = df['symbol'].apply(
            lambda x: str(int(x)).zfill(6) if pd.notna(x) and isinstance(x, (int, float)) else str(x).zfill(6)
        )

    # v408: executemany 替代逐行 INSERT
    # v548: 修复错位 — 原 batch 8 值绑 9 槽 (SQL 无 short_total), margin_total
    #       错位写入 short_total; 现 10 列 10 值对齐 (short_total=融券余额)
    batch = []
    for _, row in df.iterrows():
        sym = str(row.get('symbol', '')).strip()
        if len(sym) < 6:
            continue
        batch.append((sym, date_str,
              _to_float(row.get('margin_buy')),
              _to_float(row.get('margin_balance')),
              None,
              _to_float(row.get('short_sell_vol')),
              _to_float(row.get('short_balance')),
              _to_float(row.get('short_total')),
              _to_float(row.get('margin_total'))))
    if batch:
        conn.executemany("""
            INSERT OR REPLACE INTO margin_detail
            (symbol, date, market, margin_buy, margin_balance, margin_repay,
             short_sell_vol, short_balance, short_total, margin_total)
            VALUES (?, ?, 'SZ', ?, ?, ?, ?, ?, ?, ?)
        """, batch)
        conn.commit()
        n = len(batch)
    return n

def sync_range(start_date: str, end_date: str, conn=None):
    """从 daily 表获取交易日, 同步上交所融资融券。"""
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close_conn = True

    _ensure_table(conn)

    trading_days = _get_trading_days(conn, start_date, end_date)
    if not trading_days:
        print(f"No trading days in daily table for {start_date}~{end_date}")
        if close_conn:
            conn.close()
        return 0

    total = 0
    synced = _get_synced_dates(conn)
    to_sync = [d for d in trading_days if d not in synced]
    skipped = len(trading_days) - len(to_sync)
    ok_dates = skipped  # count in-range already-synced dates
    if skipped > 0:
        print(f"Skipping {skipped} already-synced dates")
    
    for i, date_str in enumerate(to_sync):
        n_sse = _sync_sse_raw(date_str, conn)
        time.sleep(_require_cfg("data.api_delay.margin"))
        n_szse = _sync_szse_wrapper(date_str, conn)
        n = n_sse + n_szse
        total += n
        if n > 0:
            ok_dates += 1
        if (i + 1) % 3 == 0:
            print(f"  [{i+1}/{len(to_sync)}] {date_str}: SSE={n_sse} SZSE={n_szse}, total={total}")
        if i < len(trading_days) - 1:
            time.sleep(_require_cfg("data.api_delay.margin_page"))

    logger.info(f"margin SSE done: {total} rows, {ok_dates}/{len(trading_days)} dates")
    print(f"Done: {total} rows, {ok_dates}/{len(trading_days)} dates with data ({skipped} already synced)")
    if close_conn:
        conn.close()
    return total


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3:
        sync_range(sys.argv[1], sys.argv[2])
    else:
        from quant.utils.date import today_str as _ts
        from datetime import timedelta
        _end = _ts()
        sync_range((pd.Timestamp(_end) - timedelta(days=90)).strftime('%Y-%m-%d'), _end)
