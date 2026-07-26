"""个股资金流向数据同步 — 东方财富直连 (绕过 akshare).

aksare 的 stock_individual_fund_flow 触发 RemoteDisconnected,
urllib.request 直连 HTTP 200 → 改用直连方式。
"""

import json, os, sqlite3, time
from quant.config.constants import _require_cfg
import requests
from quant.utils.logger import get_logger

logger = get_logger("data.fund_flow")
# ── 列名常量 (DDL 与查询共引) ──
FF_SYMBOL                 = "symbol"
FF_DATE                   = "date"
FF_CLOSE                  = "close"
FF_CHANGE_PCT             = "change_pct"
FF_MAIN_NET_INFLOW        = "main_net_inflow"
FF_MAIN_NET_RATIO         = "main_net_ratio"
FF_SUPER_LARGE_NET_INFLOW = "super_large_net_inflow"
FF_SUPER_LARGE_NET_RATIO  = "super_large_net_ratio"
FF_LARGE_NET_INFLOW       = "large_net_inflow"
FF_LARGE_NET_RATIO        = "large_net_ratio"
FF_MID_NET_INFLOW         = "mid_net_inflow"
FF_MID_NET_RATIO          = "mid_net_ratio"
FF_SMALL_NET_INFLOW       = "small_net_inflow"
FF_SMALL_NET_RATIO        = "small_net_ratio"

DB_PATH = os.path.join(os.path.dirname(__file__), "market.db")

_FUND_FLOW_URL = (
    "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
    "?lmt=0&klt=101&secid={secid}"
    "&fields1=f1,f2,f3,f7"
    "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65"
    "&ut=b2884a393a59ad64002292a3e90d46a5"
)

# ── 东财封 python-requests 指纹降级通道 (2026-07-26 实证) ──
# requests 直连 RemoteDisconnected, 同参数 curl HTTP 200/0.47s。
# 模块级探测: 首次 requests 失败 → 本会话后续全部走 curl 子进程。
_CURL_MODE = None  # None=未探测, True=curl, False=requests


def _http_get_json(url: str, headers: dict):
    """GET JSON, requests 优先, 被封自动降级 curl 子进程。"""
    global _CURL_MODE
    if _CURL_MODE is not True:
        try:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            _CURL_MODE = False
            return resp.json()
        except Exception:
            _CURL_MODE = True
            logger.info("fund_flow: requests blocked, fallback to curl subprocess")
    import subprocess
    args = ["curl", "-sS", "-m", "30"]
    for k, v in headers.items():
        args += ["-H", f"{k}: {v}"]
    args.append(url)
    out = subprocess.run(args, capture_output=True, text=True, timeout=40)
    if out.returncode != 0 or not out.stdout.strip():
        raise ConnectionError(f"curl failed rc={out.returncode}: {out.stderr[:200]}")
    return json.loads(out.stdout)


def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fund_flow (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            close REAL,
            change_pct REAL,
            main_net_inflow REAL,
            main_net_ratio REAL,
            super_large_net_inflow REAL,
            super_large_net_ratio REAL,
            large_net_inflow REAL,
            large_net_ratio REAL,
            mid_net_inflow REAL,
            mid_net_ratio REAL,
            small_net_inflow REAL,
            small_net_ratio REAL,
            PRIMARY KEY (symbol, date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ff_date ON fund_flow(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ff_symbol ON fund_flow(symbol)")
    conn.commit()


def _market_code(symbol: str) -> str:
    """将 symbol 转为东方财富 secid: 6xxxxx → 1, 0xxxxx/3xxxxx → 0."""
    if symbol.startswith(("6", "68")):
        return f"1.{symbol}"
    return f"0.{symbol}"


def sync_single_stock(symbol: str, market: str = None, conn=None) -> int:
    """同步单只股票的资金流向历史数据。返回新增行数。"""
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close_conn = True

    _ensure_table(conn)
    secid = _market_code(symbol)
    url = _FUND_FLOW_URL.format(secid=secid)

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Referer": "https://data.eastmoney.com/",
    }

    data = None
    last_err = None
    for attempt in range(4):
        try:
            data = _http_get_json(url, headers)
            break
        except Exception as e:
            last_err = e
            if attempt < 3:
                delay = 2 ** attempt  # 1s, 2s, 4s
                time.sleep(delay)
    if data is None:
        logger.warning(f"fund_flow fetch failed for {symbol} after 4 attempts: {type(last_err).__name__}: {last_err}")
        if close_conn:
            conn.close()
        return 0

    klines = data.get("data", {}).get("klines", [])
    if not klines:
        if close_conn:
            conn.close()
        return 0

    n = 0
    for line in klines:
        parts = line.split(",")
        if len(parts) < 13:
            continue
        # f51=date, f52=close, f53=change_pct, f54=main_net_inflow, f55=main_net_ratio
        # f56=super_large_net_inflow, f57=super_large_net_ratio
        # f58=large_net_inflow, f59=large_net_ratio
        # f60=mid_net_inflow, f61=mid_net_ratio
        # f62=small_net_inflow, f63=small_net_ratio
        try:
            conn.execute(
                f"INSERT OR REPLACE INTO fund_flow "
                f"({FF_SYMBOL}, {FF_DATE}, {FF_CLOSE}, {FF_CHANGE_PCT}, {FF_MAIN_NET_INFLOW}, {FF_MAIN_NET_RATIO}, "
                f"  {FF_SUPER_LARGE_NET_INFLOW}, {FF_SUPER_LARGE_NET_RATIO}, {FF_LARGE_NET_INFLOW}, {FF_LARGE_NET_RATIO}, "
                f"  {FF_MID_NET_INFLOW}, {FF_MID_NET_RATIO}, {FF_SMALL_NET_INFLOW}, {FF_SMALL_NET_RATIO}) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            , (
                symbol,
                parts[0],                            # date
                _float(parts[1]),                    # close
                _float(parts[2]),                    # change_pct
                _float(parts[3]),                    # main_net_inflow
                _float(parts[4]),                    # main_net_ratio
                _float(parts[5]),                    # super_large_net_inflow
                _float(parts[6]),                    # super_large_net_ratio
                _float(parts[7]),                    # large_net_inflow
                _float(parts[8]),                    # large_net_ratio
                _float(parts[9]),                    # mid_net_inflow
                _float(parts[10]),                   # mid_net_ratio
                _float(parts[11]),                   # small_net_inflow
                _float(parts[12]),                   # small_net_ratio
            ))
            n += 1
        except Exception:
            continue

    conn.commit()
    if close_conn:
        conn.close()
    return n


def _float(val: str):
    """安全转换, 空字符串 → None."""
    val = (val or "").strip()
    if not val or val == "-":
        return None
    try:
        return float(val)
    except ValueError:
        return None


def sync_all(max_stocks: int = 500, conn=None):
    """同步市值最大的 N 只股票的资金流向数据。"""
    close_conn = False
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        close_conn = True

    _ensure_table(conn)

    symbols = [r[0] for r in conn.execute(
        "SELECT symbol FROM stocks WHERE market IN ('SH','SZ') ORDER BY total_mv DESC"
    ).fetchall()]

    if max_stocks:
        symbols = symbols[:max_stocks]

    total = ok = fail = 0
    for i, sym in enumerate(symbols):
        n = sync_single_stock(sym, conn=conn)
        total += n
        if n > 0:
            ok += 1
        else:
            fail += 1
        if (i + 1) % 10 == 0:
            print(f"  [{i+1}/{len(symbols)}] ok={ok} fail={fail} total_rows={total}")
        time.sleep(_require_cfg("data.api_delay.fund_flow"))

    logger.info(f"fund_flow sync done: {total} rows for {ok} stocks ({fail} failed)")
    print(f"Done: {total} rows, {ok} ok, {fail} failed")

    if close_conn:
        conn.close()
    return total


if __name__ == "__main__":
    import sys
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    sync_all(max_stocks=n)
