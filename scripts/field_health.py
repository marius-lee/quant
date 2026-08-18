#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
field_health.py — 全表字段级体检: 逐表逐数值列 NaN/NULL 率 (全量 + 最新期 + 历史期)

用途:   v547 事件 (financial_income 两列 2020-2024 全 NaN 漏过行级审计) 后,
        对 market.db 全部数据表做字段级扫描 — 行级完整 ≠ 字段完整.
版本:   v1.0 (2026-08-19)
用法:   PYTHONPATH=. .venv/bin/python3 scripts/field_health.py [--top 20]
输出:   报告排序: 最新期 NaN 率降序; 表.列: 全量% | 最新期% | 历史期% (仅数值列)
幂等:   只读, 不写库.
"""
import argparse
import sqlite3

from quant.config.paths import MARKET_DB

# 非数据表 (内部/派生/空表) — 跳过
_SKIP = {
    "data_audit", "derived_daily", "evaluation_runs", "experiments",
    "factor_crowd_snapshot", "factor_ic_daily", "factor_registry",
    "factor_snapshot", "meta", "news_daily_count", "news_sentiment",
    "sqlite_sequence", "task_runs", "industry_history_skip",
}
_NUMERIC = {"INTEGER", "REAL"}
_WARN_PCT = 5.0      # 最新期 NaN 率超 5% → 报告 (关注)
_SEVERE_PCT = 50.0   # 超 50% → 严重缺失


def _nan_pct(conn, table: str, col: str, where: str = "") -> float:
    cond = f"{col} IS NULL OR {col} != {col}"
    n, tot = conn.execute(
        f"SELECT SUM(CASE WHEN {cond} THEN 1 ELSE 0 END), COUNT(*) "
        f"FROM {table} {where}").fetchone()
    return 0.0 if not tot else (n or 0) / tot * 100


def main(top: int = 30) -> None:
    conn = sqlite3.connect(str(MARKET_DB))
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        if r[0] not in _SKIP]
    rows = []
    for t in tables:
        cols = conn.execute(f"PRAGMA table_info({t})").fetchall()
        date_col = next((c[1] for c in cols if c[1] in
                         ("date", "stat_date", "trade_date", "report_date", "ann_date")), None)
        mx = None
        if date_col:
            mx = conn.execute(f"SELECT MAX({date_col}) FROM {t}").fetchone()[0]
        for c in cols:
            name, ctype = c[1], c[2]
            if name in ("symbol", date_col, "pub_date", "id", "name", "reason",
                        "holder_name", "holder_type", "direction", "market",
                        "record_date", "ex_date", "end_date", "industry",
                        "list_date", "delist_date", "list_status", "created_at",
                        "updated_at", "effective_from"):
                continue
            if ctype not in _NUMERIC:
                continue
            tot_nan = _nan_pct(conn, t, name)
            latest_nan = _nan_pct(conn, t, name, f"WHERE {date_col} = '{mx}'") if mx else 0.0
            hist_nan = _nan_pct(conn, t, name, f"WHERE {date_col} < '{mx}'") if mx else 0.0
            if latest_nan > _WARN_PCT or tot_nan > _WARN_PCT:
                rows.append((f"{t}.{name}", tot_nan, latest_nan, hist_nan, mx))
    conn.close()
    rows.sort(key=lambda r: -r[2])
    print(f"{'表.列':<44}{'全量%':>8}{'最新期%':>9}{'历史期%':>9}  最新日期")
    for r in rows[:top]:
        flag = " <<< 严重" if r[2] > _SEVERE_PCT else ""
        print(f"{r[0]:<44}{r[1]:>8.1f}{r[2]:>9.1f}{r[3]:>9.1f}  {r[4]}{flag}")
    print(f"\n共 {len(rows)} 列超 5% 阈值 (展示前 {min(top, len(rows))})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=30)
    main(top=ap.parse_args().top)