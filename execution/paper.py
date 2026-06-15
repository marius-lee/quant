"""Paper trading — 信号记录 vs 实际收益追踪。

每天推荐发布后自动记录信号价格。N天后对比实际涨跌，计算：
  - 命中率（信号预测上涨，实际也涨的比例）
  - 累计收益（等权持有信号标的的收益）
  - 信号得分相关性（高分是否真的涨得更多）

数据表 (results.db):
  paper_signals: 每条信号一条记录
  paper_scores: 每日汇总评分
"""

import json, os, sqlite3, numpy as np, pandas as pd
from datetime import date, datetime
from utils.logger import get_logger

logger = get_logger("execution.paper")

from web.db import get_conn as _conn


def init_paper():
    c = _conn()
    c.executescript("""
        CREATE TABLE IF NOT EXISTS paper_signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT,
            score REAL,
            signal_price REAL,
            rank INTEGER,
            actual_price REAL,
            change_pct REAL,
            is_up INTEGER,
            scored_at TEXT
        );
        CREATE TABLE IF NOT EXISTS paper_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            score_date TEXT NOT NULL UNIQUE,
            n_signals INTEGER,
            hit_rate REAL,
            avg_return REAL,
            cumulative_return REAL,
            score_corr REAL,
            raw_json TEXT
        );
    """)
    c.commit()


def record_signals(recommendations: list, store=None):
    """Record buy signals with their reference prices."""
    init_paper()
    c = _conn()
    today = date.today().isoformat()
    now = datetime.now().isoformat()

    n = 0
    for rec in recommendations[:20]:
        sym = rec.get("symbol", "")
        if not sym:
            continue
        # Check duplicate
        dup = c.execute(
            "SELECT id FROM paper_signals WHERE signal_date=? AND symbol=?",
            (today, sym)
        ).fetchone()
        if dup:
            continue
        c.execute(
            "INSERT INTO paper_signals (signal_date,symbol,name,score,signal_price,rank,scored_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (today, sym, rec.get("name",""), rec.get("score",0),
             rec.get("last_price",0), rec.get("rank", n+1), now)
        )
        n += 1
    c.commit()
    logger.info(f"paper: recorded {n} signals for {today}")


def score_signals(store=None):
    """Score unscored signals from previous days against latest prices.

    关键: 跳过今天的信号 (同一天 signal_price == latest_price → change_pct 永远 0)。
    """
    c = _conn()
    today = date.today().isoformat()
    unscored = c.execute(
        "SELECT id, signal_date, symbol, signal_price FROM paper_signals "
        "WHERE actual_price IS NULL AND signal_date < ? ORDER BY signal_date",
        (today,)
    ).fetchall()
    if not unscored:
        logger.info("paper: no unscored signals")
        return {"n_scored": 0}

    # Group by signal_date
    from collections import defaultdict
    groups = defaultdict(list)
    for row in unscored:
        groups[row[1]].append(row)

    total = 0
    for sd, rows in groups.items():
        symbols = [r[2] for r in rows]
        if store:
            try:
                closes = _get_closes(store, symbols, sd)
            except Exception:
                closes = {}
        else:
            closes = {}

        for row in rows:
            rid, rdate, rsym, rprice = row
            latest = closes.get(rsym, rprice)
            if not latest or latest <= 0:
                continue
            chg = round((latest / rprice - 1) * 100, 2) if rprice > 0 else 0
            is_up = 1 if chg > 0 else 0
            now = datetime.now().isoformat()
            c.execute(
                "UPDATE paper_signals SET actual_price=?, change_pct=?, is_up=?, scored_at=? WHERE id=?",
                (round(latest,2), chg, is_up, now, rid)
            )
            total += 1
    c.commit()

    # Recompute daily scores for all scored days
    _recompute_scores(c)
    logger.info(f"paper: scored {total} signals")
    return {"n_scored": total}


def _get_closes(store, symbols, signal_date):
    """Get latest available closes after signal_date."""
    raw = store.get_daily(symbols)
    if raw.empty or "close" not in raw:
        return {}
    closes = raw["close"].sort_index()
    sd = pd.to_datetime(signal_date)
    future = closes.loc[closes.index >= sd]
    if not future.empty:
        latest_row = future.iloc[-1]
    else:
        past = closes.loc[closes.index <= sd]
        if past.empty:
            return {}
        latest_row = past.iloc[-1]
    return {s: float(latest_row[s]) for s in symbols if s in latest_row.index and not pd.isna(latest_row[s])}


def _recompute_scores(c):
    """Recalculate all daily score summaries."""
    today = date.today().isoformat()
    rows = c.execute(
        "SELECT DISTINCT signal_date FROM paper_signals WHERE actual_price IS NOT NULL ORDER BY signal_date"
    ).fetchall()

    cum_ret = 0
    for (sd,) in rows:
        data = c.execute(
            "SELECT change_pct, is_up, score FROM paper_signals WHERE signal_date=? AND actual_price IS NOT NULL",
            (sd,)
        ).fetchall()
        if not data:
            continue
        n = len(data)
        changes = [d[0] for d in data]
        ups = [d[1] for d in data]
        scores = [d[2] for d in data]
        hit_rate = round(sum(ups) / n * 100, 1)
        avg_ret = round(float(np.mean(changes)), 2)
        cum_ret += avg_ret

        # Score correlation
        corr = None
        if n >= 3 and len(set(scores)) > 1 and len(set(changes)) > 1:
            try:
                corr = round(float(np.corrcoef(scores, changes)[0, 1]), 4)
                if np.isnan(corr):
                    corr = None
            except Exception:
                pass

        c.execute(
            "INSERT OR REPLACE INTO paper_scores (score_date,n_signals,hit_rate,avg_return,cumulative_return,score_corr,raw_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (sd, n, hit_rate, avg_ret, round(cum_ret, 2), corr,
             json.dumps({"n": n, "hit": hit_rate, "avg": avg_ret, "cum": cum_ret, "corr": corr}))
        )

    # Mark today's paper_scores entry if not yet
    today_row = c.execute("SELECT id FROM paper_scores WHERE score_date=?", (today,)).fetchone()
    if not today_row:
        # Insert a placeholder so the latest entry is always "today"
        c.execute(
            "INSERT OR REPLACE INTO paper_scores (score_date,n_signals,hit_rate,avg_return,cumulative_return,score_corr,raw_json) "
            "VALUES (?,?,?,?,?,?,?)",
            (today, 0, 0, 0, 0, None, "{}")
        )

    c.commit()


def get_performance(days=30):
    """Get paper trading performance summary."""
    init_paper()
    c = _conn()
    c.row_factory = sqlite3.Row

    scores = c.execute(
        "SELECT * FROM paper_scores ORDER BY score_date DESC LIMIT ?", (days,)
    ).fetchall()

    # Stats
    all_rows = c.execute(
        "SELECT AVG(change_pct) as avg, AVG(is_up) as hit, COUNT(*) as n FROM paper_signals WHERE actual_price IS NOT NULL"
    ).fetchone()

    # Recent signals with scores
    recent = c.execute(
        "SELECT * FROM paper_signals WHERE actual_price IS NOT NULL ORDER BY signal_date DESC, rank ASC LIMIT 20"
    ).fetchall()

    # Unscored signals
    pending = c.execute("SELECT COUNT(*) FROM paper_signals WHERE actual_price IS NULL").fetchone()[0]

    return {
        "ok": True,
        "total_signals": all_rows["n"],
        "avg_return": round(all_rows["avg"] or 0, 2),
        "hit_rate": round((all_rows["hit"] or 0) * 100, 1),
        "pending": pending,
        "history": [dict(r) for r in scores[-15:]],
        "recent": [dict(r) for r in recent],
    }
