"""东财财务主指标回填 — 修复 financial_income/financial_cashflow 2019-2023 历史缺口 (test-v527).

背景: sue/ocfp 因子在 2020-2023 blocked, 根因 financial_income(利润表)/financial_cashflow(现金流表)
2019-2023 历史缺失 (financial_balance 表同区间完整 5466-5552 只/年)。v525 的 sina 三表回填
(backfill_financials.py) 实测覆盖率仅 7-14% (sina 接口 num<=50, 部分股票仅存近端期)。
东财 datacenter-web RPT_F10_FINANCE_MAINFINADATA (em_valuation.py 同域, 项目已验证可访问)
提供全历史 (1996+) 单接口:
  - PARENTNETPROFIT(归母净利)      → financial_income.net_profit          (sue 因子: Bernard & Thomas 1989)
  - TOTALOPERATEREVE(营业总收入)   → financial_income.total_operating_revenue
  - OPERATE_INCOME_PK(营业收入)    → financial_income.operating_revenue
  - OPERATE_PROFIT_PK(营业利润)    → financial_income.operating_profit
  - NETCASH_OPERATE_PK(经营现金流) → financial_cashflow.net_operate_cash_flow (ocfp 因子: 华泰 2016)

性能: columns 显式精简 (结论自 2026-08-17 实测: ALL → 6 列 = 400ms → 21ms/请求, ×18);
      批量 4 只/请求 (pageSize 上限 500 保证不截断 4×107 期) + 6 并发 ≈ 15-25 分钟全市场。

用法: env PYTHONPATH=. .venv/bin/python scripts/backfill_financials_em.py
幂等: 按 (symbol, stat_date) upsert, 已有行仅更新非 NULL 字段; 可重复执行。
并发: 线程池拉取 (datacenter 无状态), DB 写入仅主线程 (sqlite 线程安全);
      依赖 market.db WAL 模式, 可与物化 subprocess 段并发。
"""
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from quant.utils.logger import get_logger

_log = get_logger("backfill.financials_em")

try:
    from quant.config.paths import MARKET_DB as DB
except Exception:
    DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "quant", "data", "market.db")

_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://emweb.securities.eastmoney.com/",
}
_REPORT = "RPT_F10_FINANCE_MAINFINADATA"
_COLUMNS = "REPORT_DATE,SECUCODE,PARENTNETPROFIT,TOTALOPERATEREVE,OPERATE_INCOME_PK,OPERATE_PROFIT_PK,NETCASH_OPERATE_PK"
_PAGE_SIZE = 500
_BATCH = 4
_WORKERS = 6
_RETRY = 2
_MAX_CONSECUTIVE_FAILURES = 10
_LOG_EVERY = 500
_COMMIT_EVERY = 100

TARGET_QUARTERS = {
    f"{y}-{m:02d}-{d}"
    for y in range(2019, 2025)
    for m, d in [(3, 31), (6, 30), (9, 30), (12, 31)]
}

_INCOME_MAP = {
    "PARENTNETPROFIT": "net_profit",
    "TOTALOPERATEREVE": "total_operating_revenue",
    "OPERATE_INCOME_PK": "operating_revenue",
    "OPERATE_PROFIT_PK": "operating_profit",
}
_CASHFLOW_MAP = {
    "NETCASH_OPERATE_PK": "net_operate_cash_flow",
}


def _secucode(symbol: str) -> str:
    if symbol.startswith("6"):
        return f"{symbol}.SH"
    if symbol.startswith(("0", "3")):
        return f"{symbol}.SZ"
    if symbol.startswith(("4", "8", "9")):
        return f"{symbol}.BJ"
    raise ValueError(f"unknown market prefix: {symbol}")


def _to_date(stat_date_str: str) -> str:
    """2020-06-30 00:00:00 → 2020-06-30"""
    return str(stat_date_str)[:10]


def _fetch_batch(symbols: list[str]) -> list[dict]:
    codes = ",".join(f'"{_secucode(s)}"' for s in symbols)
    last_err = None
    for attempt in range(_RETRY + 1):
        try:
            r = requests.get(
                _URL,
                params={
                    "reportName": _REPORT,
                    "columns": _COLUMNS,
                    "pageSize": _PAGE_SIZE,
                    "sortColumns": "REPORT_DATE",
                    "sortTypes": "-1",
                    "filter": f'(SECUCODE in ({codes}))',
                },
                headers=_HEADERS,
                timeout=20,
            )
            r.raise_for_status()
            j = r.json()
            if not j.get("success"):
                raise RuntimeError(f"em api failed: {j.get('message')}")
            return (j.get("result") or {}).get("data") or []
        except Exception as e:
            last_err = e
            time.sleep(0.5 * (attempt + 1))
    raise last_err


def _upsert(conn, table: str, symbol: str, stat_date: str, fields: dict):
    if not fields:
        return
    cols = list(fields.keys())
    placeholders = ", ".join("?" for _ in cols)
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in cols)
    sql = (
        f"INSERT INTO {table} (symbol, stat_date, pub_date, {', '.join(cols)}) "
        f"VALUES (?, ?, ?, {placeholders}) "
        f"ON CONFLICT(symbol, stat_date) DO UPDATE SET {set_clause}"
    )
    vals = [symbol, stat_date, stat_date] + [fields[c] for c in cols]
    conn.execute(sql, vals)


def run() -> dict:
    t0 = time.time()
    conn = sqlite3.connect(DB, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    symbols = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM stocks ORDER BY symbol")]
    total = len(symbols)
    batches = [symbols[i : i + _BATCH] for i in range(0, total, _BATCH)]
    _log.info(
        "stocks: %d batches: %d (target quarters: %d, workers: %d)",
        total, len(batches), len(TARGET_QUARTERS), _WORKERS,
    )

    inserted = 0
    skipped = 0
    done = 0
    consec_fail = 0

    with ThreadPoolExecutor(max_workers=_WORKERS) as ex:
        futs = {}
        for b in batches:
            f = ex.submit(_fetch_batch, b)
            futs[f] = b
        for i, fut in enumerate(as_completed(futs), 1):
            batch = futs[fut]
            try:
                rows = fut.result()
            except Exception as e:
                consec_fail += 1
                if consec_fail >= _MAX_CONSECUTIVE_FAILURES:
                    raise RuntimeError(
                        f"{_MAX_CONSECUTIVE_FAILURES} consecutive batch failures (cur={batch[0]})"
                    ) from e
                _log.warning("fetch failed for %s (consec=%d): %s", batch[0], consec_fail, str(e)[:80])
                continue
            consec_fail = 0
            done += len(batch)

            by_sym: dict[str, list[dict]] = {}
            for raw in rows:
                code = str(raw.get("SECUCODE", ""))
                sym = code.split(".")[0]
                by_sym.setdefault(sym, []).append(raw)

            for sym, sym_rows in by_sym.items():
                hit = 0
                for raw in sym_rows:
                    sd = _to_date(raw.get("REPORT_DATE", ""))
                    if sd not in TARGET_QUARTERS:
                        continue
                    hit += 1
                    inc = {v: raw.get(k) for k, v in _INCOME_MAP.items() if raw.get(k) is not None}
                    cfl = {v: raw.get(k) for k, v in _CASHFLOW_MAP.items() if raw.get(k) is not None}
                    _upsert(conn, "financial_income", sym, sd, inc)
                    _upsert(conn, "financial_cashflow", sym, sd, cfl)
                if hit:
                    inserted += hit
                else:
                    skipped += 1

            if i % (_COMMIT_EVERY // _BATCH) == 0:
                conn.commit()
            if done % _LOG_EVERY <= _BATCH or i == len(batches):
                conn.commit()
                el = time.time() - t0
                _log.info(
                    "[%.0fm] %s: %d/%d (%.1f%%) rate=%.1fstk/s inserted=%d skipped=%d elapsed=%.1fs",
                    el // 60, "financials", done, total, 100.0 * done / total,
                    done / el, inserted, skipped, el,
                )

    conn.commit()
    conn.close()
    el = time.time() - t0
    _log.info(
        "DONE: %d stocks in %.1fs, inserted %d (target-period rows), skipped %d (no target data)",
        total, el, inserted, skipped,
    )
    return {"stocks": total, "inserted": inserted, "skipped": skipped, "seconds": el}


if __name__ == "__main__":
    result = run()
    print(f"OK financials_em: {result}")