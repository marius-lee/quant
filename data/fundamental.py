"""基本面数据同步 — PE/PB/总市值/ROE 批量写入 stocks 表。

数据来源:
  - PE/总市值: akshare stock_a_lg_indicator (批量, 免费)
  - PB: akshare stock_a_indicator_lg (逐只, 慢但免费)
  - ROE: 暂无免费批量源 → 用 PE/PB 推算, 后续接入专业数据

所有数据写入 stocks 表 (通过 ALTER TABLE 已添加 pe/pb/total_mv/roe 列)。
"""

import time
import sqlite3
from datetime import datetime

from utils.logger import get_logger
from utils.date import to_compact, DEFAULT_START_DATE

logger = get_logger("data.fundamental")


def _fetch_pe_mv_akshare(conn: sqlite3.Connection) -> int:
    """akshare 批量获取 PE + 总市值 → stocks.pe, stocks.total_mv。

    stock_a_lg_indicator: 免费, 约返回 5000+ 只股票的基础财务指标。
    字段: code, pe, pb, total_mv, ...
    注意: pe/pb 原始值为 float, 空值时可能为 '-' 或 None。

    返回: 更新的股票数。
    """
    try:
        import akshare as ak
        df = ak.stock_a_lg_indicator(symbol="all")
    except Exception as e:
        logger.warning(f"PE/MV bulk fetch failed: {e}")
        return 0

    if df is None or df.empty:
        return 0

    updated = 0
    for _, row in df.iterrows():
        try:
            code = str(row.get("code", "")).zfill(6)
            if len(code) != 6:
                continue

            pe_val = row.get("pe")
            pb_val = row.get("pb")
            mv_val = row.get("total_mv")

            # 转换为 float, 过滤无效值
            pe = _safe_float(pe_val)
            pb = _safe_float(pb_val)
            total_mv = _safe_float(mv_val)

            # 只更新非 NULL 的列
            updates = []
            params = []
            if pe is not None and pe > 0:
                updates.append("pe=?")
                params.append(round(pe, 4))
            if pb is not None and pb > 0:
                updates.append("pb=?")
                params.append(round(pb, 4))
            if total_mv is not None and total_mv > 0:
                updates.append("total_mv=?")
                params.append(round(total_mv, 2))

            if updates:
                params.append(code)
                conn.execute(
                    f"UPDATE stocks SET {', '.join(updates)} WHERE symbol=?",
                    params,
                )
                updated += 1
        except Exception:
            continue

    conn.commit()
    logger.info(f"PE/MV bulk: {updated} stocks updated (akshare)")

    # 派生 ROE = EPS / BVPS (会计恒等式)
    conn.execute("""
        UPDATE stocks SET roe = ROUND(eps / NULLIF(bvps, 0) * 100, 2)
        WHERE eps IS NOT NULL AND bvps IS NOT NULL AND bvps != 0
          AND (roe IS NULL OR roe = 0)
    """)
    conn.commit()
    roe_valid = conn.execute("SELECT COUNT(*) FROM stocks WHERE roe IS NOT NULL").fetchone()[0]
    logger.info(f"ROE derived (EPS/BVPS): {roe_valid} stocks")
    return updated


def _fetch_pb_individual(conn: sqlite3.Connection, max_fetch: int = -1) -> int:
    """逐只补充 PB (stock_a_lg_indicator 可能缺 PB，用 stock_a_indicator_lg 兜底)。

    max_fetch: 最多拉取数, -1 = 全部 (很慢, ~1h for 5000 stocks)。

    返回: 更新的股票数。
    """
    # 找出 PB 为 NULL 的股票
    rows = conn.execute(
        "SELECT symbol FROM stocks WHERE pb IS NULL OR pb <= 0"
    ).fetchall()

    if max_fetch > 0:
        rows = rows[:max_fetch]

    if not rows:
        logger.info("PB: no missing stocks")
        return 0

    logger.info(f"PB: {len(rows)} stocks need backfill")
    symbols = [r[0] for r in rows]

    try:
        import akshare as ak
    except ImportError:
        logger.warning("akshare not available, skipping PB backfill")
        return 0

    updated = 0
    for sym in symbols:
        try:
            df = ak.stock_a_indicator_lg(symbol=sym)
            if df is None or df.empty:
                continue
            last = df.iloc[-1]
            pb_val = _safe_float(last.get("pb"))
            if pb_val is not None and pb_val > 0:
                conn.execute(
                    "UPDATE stocks SET pb=? WHERE symbol=?",
                    (round(pb_val, 4), sym),
                )
                updated += 1
            time.sleep(0.5)  # akshare 频率控制
        except Exception:
            continue

    conn.commit()
    logger.info(f"PB backfill: {updated}/{len(symbols)} stocks updated")
    return updated


def _safe_float(val) -> float | None:
    """安全转换为 float, 处理 None/'-'/NaN。"""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        if val != val:  # NaN check
            return None
        return float(val)
    s = str(val).strip()
    if s in ("", "-", "--", "nan", "NaN", "None"):
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def sync_all(conn: sqlite3.Connection, max_pb_fetch: int = 100) -> dict:
    """同步所有基本面数据 → stocks 表。

    max_pb_fetch: PB 逐只回填上限 (设为 -1 回填全部, 但很慢)。
                  默认 100 (约 50 秒), 可逐步增加。

    返回: {"pe_count": int, "pb_count": int}
    """
    pe_count = _fetch_pe_mv_akshare(conn)
    pb_count = _fetch_pb_individual(conn, max_fetch=max_pb_fetch)
    return {"pe_count": pe_count, "pb_count": pb_count}


if __name__ == "__main__":
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from data.store import DataStore
    store = DataStore()
    conn = store._connect()
    result = sync_all(conn, max_pb_fetch=20)
    print(f"Done: PE={result['pe_count']}, PB={result['pb_count']}")
    store.close()
