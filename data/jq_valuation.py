#!/usr/bin/env python3
"""Sync daily valuation (PE/PB/PS/PCF/market_cap) from JQData to market.db.

JQData trial covers 2025-03-26 ~ 2026-04-02.
One API call per date fetches all stocks' valuation.

用法:
  .venv-tushare/bin/python3 data/jq_valuation.py                    # 全量同步
  .venv-tushare/bin/python3 data/jq_valuation.py 2026-01-01 2026-04-01
"""
import os, sys, time, sqlite3, logging
from config.constants import _require_cfg
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("jq_valuation")

from data.cache import get_backend, DataCache, RateLimiter

# ── Module-level cache (lazy init) ──
_cache = None
_limiter = None
_tushare_limiter = None

def _init_cache():
    global _cache, _limiter, _tushare_limiter
    if _cache is not None:
        return
    # .venv-tushare 无 yaml, config.loader 不可用, 直接传空走 NoopBackend
    try:
        from config.loader import load as _load_config
        cfg = _load_config()
    except Exception as e:
        raise  # 错误不吞
        logger.warning(f"config load failed, using empty config: {e}")
        cfg = {}
    backend = get_backend(cfg)
    _cache = DataCache("jq_valuation", ttl_hours=4, backend=backend)
    _limiter = RateLimiter("jqdata", calls_per_minute=30, backend=backend)
    _tushare_limiter = RateLimiter("tushare_valuation", calls_per_minute=180, backend=backend)
    logger.debug("jq_valuation cache initialized (backend=%s)", type(backend).__name__)

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

# tushare daily_basic 字段 → JQData 兼容字段名, 保证 _insert_valuation_rows 无需修改
TUSHARE_TO_JQ_MAP = {
    "pe_ttm": "pe_ratio",
    "pb": "pb_ratio",
    "ps_ttm": "ps_ratio",
    "pcf_ratio": "pcf_ratio",
    "total_mv": "market_cap",
    "turnover_rate": "turnover_ratio",
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


def _fetch_tushare_valuation_rows(date_str):
    """从 tushare daily_basic 获取估值数据, 返回 JQData 兼容格式的 rows。
    JQData trial 截止 2026-04-02 后, 作为 PE/PB 回退源。
    """
    token = os.environ.get("TUSHARE_TOKEN", "")
    if not token:
        logger.warning("TUSHARE_TOKEN not set, tushare fallback unavailable")
        return None
    _init_cache()
    _tushare_limiter.wait()
    try:
        import tushare as ts
        ts.set_token(token)
        pro = ts.pro_api()
        date_compact = date_str.replace("-", "")
        df = pro.daily_basic(
            trade_date=date_compact,
            fields="ts_code,trade_date,pe_ttm,pb,ps_ttm,total_mv,turnover_rate",
        )
    except Exception as e:
        raise  # 错误不吞
        logger.warning(f"tushare daily_basic failed for {date_str}: {e}")
        return None
    if df is None or df.empty:
        return None
    rows = []
    for _, row in df.iterrows():
        ts_code = str(row.get("ts_code", ""))
        if "." not in ts_code:
            continue
        vals = {"code": ts_code}
        for t_col, jq_col in TUSHARE_TO_JQ_MAP.items():
            v = row.get(t_col)
            if v is not None and v == v:
                vals[jq_col] = float(v)
        if len(vals) > 1:
            rows.append(vals)
    logger.info(f"tushare daily_basic {date_str}: {len(rows)} stocks")
    return rows or None

def sync_date(date_str, conn):
    """Sync PE_TTM/PB for one date. API 响应缓存 (4h TTL, P88: 本地 NoopBackend)。"""
    _init_cache()

    # 1. 尝试缓存命中
    cached_data = _cache.get(date_str)
    if cached_data is not None:
        inserted = _insert_valuation_rows(conn, cached_data, date_str)
        logger.info(f"daily_basic {date_str}: {inserted} stocks (cache hit)")
        return inserted

    # 2. 调用 JQData API
    _limiter.wait()
    from jqdatasdk import auth, get_fundamentals, query, valuation, logout
    auth(os.environ.get("JQDATA_USER", ""), os.environ.get("JQDATA_PASS", ""))
    try:
        q = query(valuation)
        df = get_fundamentals(q, date=date_str)
    except Exception as e:
        raise  # 错误不吞
        logger.warning(f"JQData query failed for {date_str}: {e}")
        logout()
        return 0
    if df is None or df.empty:
        logger.info(f"JQData returned empty for {date_str}, trying tushare...")
        logout()
        raw = _fetch_tushare_valuation_rows(date_str)
        if raw is None:
            return 0
        _cache.put(date_str, raw)
        inserted = _insert_valuation_rows(conn, raw, date_str)
        logger.info(f"daily_valuation {date_str}: {inserted} stocks (tushare)")
        return inserted

    # 3. 缓存原始响应 (msgpack)
    raw = df.to_dict(orient="records")
    _cache.put(date_str, raw)

    # 4. 写入 SQLite
    inserted = _insert_valuation_rows(conn, raw, date_str)
    logout()
    logger.info(f"daily_basic {date_str}: {inserted} stocks (API)")
    return inserted


def _insert_valuation_rows(conn, rows: list, date_str: str) -> int:
    """将 API 响应行写入 daily_valuation 表。返回插入行数。"""
    inserted = 0
    for row in rows:
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
            (symbol, date_str, *params))
        inserted += 1
    conn.commit()
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
            time.sleep(_require_cfg("data.api_delay.jq_valuation"))
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
