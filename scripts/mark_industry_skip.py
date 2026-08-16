#!/usr/bin/env python3
"""scripts/mark_industry_skip.py — 行业数据缺失标记 (v1.0.0, 幂等)

用途: v516 — 将 baostock 行业数据源缺失的股票标记 skip, 从行业 PIT 同步
pending 池剔除, 使同步可完成. 两类:
  A. 北交所 920 段 (920xxx): baostock query_stock_industry 全部无数据
     (2026-08-16 全市场逐股实证, 数据源不收录, 非格式/网络问题)
  B. 上市 < 30 天次新: baostock 行业数据收录滞后, 暂标记, 日后再同步
     (删 skip 记录即可重试: DELETE FROM industry_history_skip WHERE symbol='...')

用法:
  PYTHONPATH=. .venv/bin/python scripts/mark_industry_skip.py [--dry-run]

幂等性: INSERT OR IGNORE, 安全重跑; --dry-run 只打印不写入.
"""
import sqlite3
import sys
from datetime import date, timedelta

from quant.data.industry_history import _build_table

DB = "quant/data/market.db"
RECENT_DAYS = 30  # 次新滞后阈值, baostock 收录通常数周


def main() -> int:
    dry = "--dry-run" in sys.argv
    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA busy_timeout=120000")
    try:
        _build_table(conn)
        done = {r[0] for r in conn.execute(
            "SELECT DISTINCT symbol FROM industry_history")}
        skip_existing = {r[0] for r in conn.execute(
            "SELECT symbol FROM industry_history_skip")}
        rows = conn.execute(
            "SELECT symbol, list_date FROM stocks ORDER BY symbol").fetchall()
        cutoff = (date.today() - timedelta(days=RECENT_DAYS)).isoformat()
        marks = []  # (symbol, reason)
        for sym, list_date in rows:
            if sym in done or sym in skip_existing:
                continue
            if sym.startswith("920"):
                marks.append((sym, "baostock_920段无行业数据(2026-08-16实证)"))
            elif list_date and list_date >= cutoff:
                marks.append((sym, f"次新上市{list_date}数据滞后, 待baostock收录后重试"))
        print(f"待标记 {len(marks)} 只 (dry={'Y' if dry else 'N'}):")
        from collections import Counter
        for r, n in Counter(m for _, m in marks).items():
            print(f"  {n:3d} 只 — {r}")
        if dry:
            return 0
        now = date.today().isoformat()
        conn.executemany(
            "INSERT OR IGNORE INTO industry_history_skip(symbol, reason, created_at) "
            "VALUES (?, ?, ?)",
            [(s, r, now) for s, r in marks])
        conn.commit()
        print(f"已写入 industry_history_skip {len(marks)} 条; "
              f"skip 表现有 {conn.execute('SELECT COUNT(*) FROM industry_history_skip').fetchone()[0]} 条")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())