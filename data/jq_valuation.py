#!/usr/bin/env python3
"""Sync daily valuation (PE/PB/PS/PCF/market_cap) from JQData to market.db.

JQData trial covers 2025-03-26 ~ 2026-04-02.
One API call per date fetches all stocks' valuation.

用法:
  .venv-tushare/bin/python3 data/jq_valuation.py                    # 全量同步
  .venv-tushare/bin/python3 data/jq_valuation.py 2026-01-01 2026-04-01
"""
import os, sys, time, sqlite3, logging
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("jq_valuation")

DB = os.path.join(os.path.dirname(__file__), "market.db")

TRIAL_START = "2025-03-26"
TRIAL_END = "2026-04-02"

COL_MAP = {
    "pe_ratio": "pe_ttm",
    "pb_ratio": "pb",
    "ps_ratio": "ps_ttm",
    "pcf_ratio": "pcf_ttm",
    "market_cap": "market_cap",
    "turnover_ratio": "turnover_rate",
}


def _get_trading_dates(conn, start, end):
    rows = conn.execute(
        "SELECT DISTINCT date FROM daily WHERE date >= ? AND date <= ? ORDER BY date",
        (start, end),
    ).fetchall()
    if rows:
        return [r[0] for r in rows]
    dates = []
    d = datetime.strptime(start, "%Y-%m-%d")
    end_d = datetime.strptime(end, "%Y-%m-%d")
    while d <= end_d:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return dates


def sync_date(date_str, conn):
    from jqdatasdk import auth, get_fundamentals, query, valuation, logout
    auth(os.environ.get("JQDATA_USER", ""), os.environ.get("JQDATA_PASS", ""))
    try:
        q = query(valuation)
        df = get_fundamentals(q, date=date_str)
    except Exception as e:
        logger.warning(f"JQData query failed for {date_str}: {e}")
        logout()
        return 0
    if df is None or df.empty:
        logger.warning(f"JQData returned empty for {date_str}")
        logout()
        return 0
    inserted = 0
    for _, row in df.iterrows():
        code = str(row.get("code", ""))
        if not code or "." not in code:
            continue
        symbol = code.split(".")[0]
        if len(symbol) != 6:
            continue
        vals = {}
        for jq_col, our_col in COL_MAP.items():
            v = row.get(jq_col)
            if v is not None and v == v:
                vals[our_col] = float(v)
        if not vals:
            continue
        cols = ", ".join(vals.keys())
        placeholders = ", ".join("?" for _ in vals)
        params = list(vals.values())
        conn.execute(
            f"INSERT OR REPLACE INTO daily_valuation (symbol, date, {cols}) "
            f"VALUES (?, ?, {placeholders})",
            (symbol, date_str, *params),
        )
        inserted += 1
    conn.commit()
    logout()
    return inserted


def sync_range(start=TRIAL_START, end=TRIAL_END, max_dates=0):
    conn = sqlite3.connect(DB)
    dates = _get_trading_dates(conn, start, end)
    synced_dates = set(r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM daily_valuation WHERE date >= ? AND date <= ?",
        (start, end),
    ).fetchall())
    todo = [d for d in dates if d not in synced_dates]
    if max_dates > 0:
        todo = todo[:max_dates]
    if not todo:
        logger.info(f"All {len(dates)} dates already synced")
        conn.close()
        return
    logger.info(f"Syncing {len(todo)} dates ({len(synced_dates)} already done)")
    total_rows = 0
    t0 = time.time()
    for i, d in enumerate(todo):
        n = sync_date(d, conn)
        total_rows += n
        elapsed = time.time() - t0
        rate = (i + 1) / elapsed if elapsed > 0 else 0
        eta = (len(todo) - i - 1) / rate if rate > 0 else 0
        print(f"\r  [{i+1}/{len(todo)}] {d}: {n} stocks, {rate:.1f}/s, ETA {eta:.0f}s", end="", flush=True)
        if i < len(todo) - 1:
            time.sleep(0.15)
    print()
    conn.close()
    logger.info(f"Done: {total_rows} rows for {len(todo)} dates ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("start", nargs="?", default=TRIAL_START)
    p.add_argument("end", nargs="?", default=TRIAL_END)
    p.add_argument("--max", type=int, default=0)
    args = p.parse_args()
    sync_range(args.start, args.end, max_dates=args.max)
