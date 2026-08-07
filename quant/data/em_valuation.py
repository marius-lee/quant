"""东财估值同步 — datacenter-web RPT_VALUEANALYSIS_DET (test-v306, 2026-07-27).

背景: daily_valuation 近 3 个月缺口的承接源。
  - tushare 免费档无 daily_basic 权限 (2000 积分档, 2026-07-26 实证)
  - JQData 账号窗口 = 前15个月~前3个月 (用户提供), 近 3 个月管不了
  - 东财 datacenter-web 实测畅通 (同日 push2his 被服务级封禁, 域名级独立)

字段覆盖: pe_ttm / pb / ps_ttm / pcf_ttm / market_cap (无 turnover_rate,
  该列留 NULL — turnover 在 daily 表有独立来源)。
"""
import logging
import os
import sqlite3
import time

import requests

from quant.config.constants import _require_cfg
from quant.utils.date import validate_date_format
from quant.utils.logger import get_logger

_log = get_logger("data.em_valuation")

from quant.config.paths import MARKET_DB as DB

_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://data.eastmoney.com/",
}
_REPORT = "RPT_VALUEANALYSIS_DET"
_PAGE_SIZE = 500
_MAX_CONSECUTIVE_DATE_FAILURES = 3

_COL_MAP = {
    "PE_TTM": "pe_ttm",
    "PB_MRQ": "pb",
    "PS_TTM": "ps_ttm",
    "PCF_OCF_TTM": "pcf_ttm",
    "TOTAL_MARKET_CAP": "market_cap",
}


def _fetch_date(date_str: str) -> list[dict]:
    """拉取某日全市场估值 (分页)。失败抛异常 (零 fallback)。"""
    validate_date_format(date_str, source="em_valuation")
    rows: list[dict] = []
    page = 1
    while True:
        params = {
            "reportName": _REPORT,
            "columns": "ALL",
            "filter": f"(TRADE_DATE='{date_str}')",
            "pageNumber": page,
            "pageSize": _PAGE_SIZE,
            "source": "WEB",
            "client": "WEB",
        }
        last_err = None
        for attempt in range(3):
            try:
                r = requests.get(_URL, params=params, headers=_HEADERS, timeout=30)
                r.raise_for_status()
                payload = r.json()
                break
            except Exception as e:
                last_err = e
                if attempt < 2:
                    time.sleep(2 ** attempt)
        else:
            raise RuntimeError(
                f"em_valuation fetch failed {date_str} page {page}: {last_err}")
        if not payload.get("success"):
            raise RuntimeError(
                f"em_valuation api error {date_str} page {page}: {payload.get('message')}")
        result = payload.get("result") or {}
        rows.extend(result.get("data") or [])
        if page >= (result.get("pages") or 0):
            break
        page += 1
        time.sleep(_require_cfg("data.api_delay.em_valuation"))
    return rows


def _insert_rows(conn, rows: list[dict], date_str: str) -> int:
    """写入 daily_valuation。只收 SH/SZ (SECUCODE 后缀), BJ 排除 (与 stocks 口径一致)。"""
    inserted = 0
    for row in rows:
        secucode = str(row.get("SECUCODE", ""))
        if not (secucode.endswith(".SH") or secucode.endswith(".SZ")):
            continue
        symbol = secucode.split(".")[0]
        if len(symbol) != 6:
            continue
        vals = {}
        for em_col, our_col in _COL_MAP.items():
            v = row.get(em_col)
            if v is not None and v == v:
                vals[our_col] = float(v)
        if not vals:
            continue
        cols = ", ".join(vals.keys()) + ", source"
        placeholders = ", ".join("?" for _ in vals) + ", 'eastmoney'"
        conn.execute(
            f"INSERT OR REPLACE INTO daily_valuation (symbol, date, {cols}) "
            f"VALUES (?, ?, {placeholders})",
            (symbol, date_str, *vals.values()))
        inserted += 1
    conn.commit()
    return inserted


def sync_date(date_str: str, conn=None) -> int:
    """同步单日估值, 返回写入行数。"""
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB)
        close_conn = True
    rows = _fetch_date(date_str)
    n = _insert_rows(conn, rows, date_str)
    _log.info(f"em_valuation {date_str}: {n} stocks")
    if close_conn:
        conn.close()
    return n


def sync_range(start: str, end: str, conn=None) -> int:
    """同步日期区间 (按 daily 表交易日, 已有数据的日期跳过)。
    连续 3 个日期失败 → 终止 (源故障, 零 fallback 等人工)。"""
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB)
        close_conn = True
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM daily WHERE date >= ? AND date <= ? ORDER BY date",
        (start, end)).fetchall()]
    synced = {r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM daily_valuation WHERE date >= ? AND date <= ?",
        (start, end)).fetchall()}
    todo = [d for d in dates if d not in synced]
    if not todo:
        _log.info(f"em_valuation: all {len(dates)} dates already synced")
        if close_conn:
            conn.close()
        return 0
    _log.info(f"em_valuation: syncing {len(todo)} dates ({len(synced)} already done)")
    total = 0
    consecutive_fail = 0
    for i, d in enumerate(todo):
        try:
            n = sync_date(d, conn=conn)
            total += n
            consecutive_fail = 0
        except Exception as e:
            consecutive_fail += 1
            _log.warning(f"em_valuation {d} failed ({type(e).__name__}: {e})")
            if consecutive_fail >= _MAX_CONSECUTIVE_DATE_FAILURES:
                _log.error(
                    f"em_valuation aborted: {_MAX_CONSECUTIVE_DATE_FAILURES} "
                    f"consecutive date failures at {d}, synced {i} dates so far")
                break
        time.sleep(_require_cfg("data.api_delay.em_valuation"))
    _log.info(f"em_valuation done: {total} rows")
    if close_conn:
        conn.close()
    return total


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    if len(sys.argv) >= 3:
        sync_range(sys.argv[1], sys.argv[2])
    else:
        print("usage: python -m quant.data.em_valuation START END")
