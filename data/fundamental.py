"""基本面数据同步 — PE/PB/总市值/ROE 批量写入 stocks 表。

数据来源:
  - PE/PB/总市值: akshare stock_value_em (东方财富, 逐只)
    akshare 1.18.64 移除了旧版 stock_a_lg_indicator / stock_a_indicator_lg
  - ROE: EPS/BVPS 推导 → 备选 stock_financial_analysis_indicator_em (需联网)

所有数据写入 stocks 表 (通过 ALTER TABLE 已添加 pe/pb/total_mv/roe 列)。
"""

import time
import sqlite3
from datetime import datetime

from utils.logger import get_logger
from utils.date import to_compact, DEFAULT_START_DATE

logger = get_logger("data.fundamental")


def _safe_float(val) -> float | None:
    """安全转换为 float, 处理 None/'-'/NaN。"""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        if val != val:
            return None
        return float(val)
    s = str(val).strip()
    if s in ("", "-", "--", "nan", "NaN", "None"):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _fetch_value_em(conn: sqlite3.Connection, symbols: list, sleep_ms: int = 200) -> int:
    """逐只获取 PE/PB/总市值 via stock_value_em (东方财富)。

    stock_value_em 返回列 (中文):
      数据日期, 当日收盘价, 当日涨跌幅, 总市值, 流通市值,
      总股本, 流通股本, PE(TTM), PE(静), 市净率, PEG值, 市现率, 市销率

    symbols: 要查询的股票代码列表
    sleep_ms: 请求间隔 (东方财富约 200ms 可稳定, 默认 200ms)

    返回: 更新成功数。
    """
    try:
        import akshare as ak
    except ImportError:
        logger.warning("akshare not installed")
        return 0

    updated = 0
    skipped = 0
    total = len(symbols)
    t_start = time.time()
    for i, sym in enumerate(symbols):
        if i > 0 and i % 50 == 0:
            elapsed = time.time() - t_start
            rate = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / rate if rate > 0 else 0
            logger.info(f"value_em [{i}/{total}] {updated} updated, {skipped} skipped | {rate:.1f}/s, ETA {eta:.0f}s")
        if i > 0:
            time.sleep(sleep_ms / 1000.0)
        try:
            df = ak.stock_value_em(symbol=sym)
            if df is None or df.empty:
                skipped += 1
                continue

            last = df.iloc[-1]

            pe = _safe_float(last.get("PE(TTM)"))
            pb = _safe_float(last.get("市净率"))
            total_mv = _safe_float(last.get("总市值"))

            updates = []
            params = []
            if pe is not None and pe > 0:
                updates.append("pe=?")
                params.append(round(pe, 4))
            if pb is not None and pb >= 0:
                updates.append("pb=?")
                params.append(round(pb, 4))
            if total_mv is not None and total_mv > 0:
                updates.append("total_mv=?")
                params.append(round(total_mv, 2))

            if updates:
                params.append(sym)
                conn.execute(
                    f"UPDATE stocks SET {', '.join(updates)} WHERE symbol=?",
                    params,
                )
                updated += 1
                print(f"\r{updated}/{total}", end="", flush=True)
        except Exception:
            skipped += 1
            continue

    print()  # newline after progress line
    conn.commit()
    logger.info(f"stock_value_em done: {updated} updated, {skipped} skipped (of {total})")

    # P2-2: ROE 从 EPS/BVPS 推导 (fallback, 待接入直接 ROE 源)
    conn.execute("""
        UPDATE stocks SET roe = ROUND(eps / NULLIF(bvps, 0) * 100, 2)
        WHERE eps IS NOT NULL AND bvps IS NOT NULL AND bvps != 0
          AND (roe IS NULL OR roe = 0)
    """)
    conn.commit()
    roe_valid = conn.execute("SELECT COUNT(*) FROM stocks WHERE roe IS NOT NULL").fetchone()[0]
    logger.info(f"ROE derived (EPS/BVPS): {roe_valid} stocks (P2-2: direct fetch not yet available)")
    return updated


def sync_all(conn: sqlite3.Connection, max_fetch: int = 100) -> dict:
    """同步基本面数据 → stocks 表。

    用 stock_value_em 逐只获取 PE/PB/市值。
    max_fetch: 最多获取股票数, -1 = 全部 (全部 ~5000 只约 20-30min)。

    返回: {"count": int}
    """
    rows = conn.execute("""
        SELECT d.symbol FROM daily d
        JOIN stocks s ON d.symbol = s.symbol
        WHERE s.market != 'BJ'
        GROUP BY d.symbol
        ORDER BY AVG(d.amount) DESC
        LIMIT ?
    """, (max_fetch if max_fetch > 0 else 99999,)).fetchall()

    symbols = [r[0] for r in rows]
    # 实测: 东方财富 ban 约 5 req/s, 加上 API 延迟约 2 req/s
    # 500 只约 4-5min (不是 40min)
    est_sec = len(symbols) * 0.5
    logger.info(f"fundamental sync: {len(symbols)} stocks via stock_value_em (~{est_sec/60:.0f}min estimated)")
    count = _fetch_value_em(conn, symbols)
    return {"count": count}


if __name__ == "__main__":
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from data.store import DataStore
    store = DataStore()
    conn = store._connect()
    result = sync_all(conn, max_fetch=20)
    print(f"Done: {result['count']} stocks updated")
    store.close()
