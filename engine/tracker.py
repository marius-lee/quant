"""推荐追踪分析 — 评估历史推荐的准确性和逻辑。

每次新推荐入库后:
  1. 查出上一次推荐的股票
  2. 获取这些股票从推荐日到当前的实际涨跌幅
  3. 分析: 涨幅是否与预测得分正相关? 命中率多少? 平均收益?
  4. 写入 tracking 表, 供 Web 展示和模型反馈

分析维度:
  - 命中率: 推荐的股票有多少实际涨了
  - 平均收益: 等权持有的话实际能赚多少
  - 得分相关: 高分股是否确实比低分股涨得好?
  - 超额收益: 相对大盘(沪深300)的超额
  - 涨停命中: 推荐的股票有没有涨停?
"""
import sqlite3
import os
import json
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from utils.logger import get_logger

logger = get_logger("engine.tracker")

from web.db import get_conn as _results_conn
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "results.db")  # only used for DB_PATH reference


def init_tracking():
    """创建追踪分析表"""
    conn = _results_conn()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tracking (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_date TEXT NOT NULL,          -- 追踪日期(今天)
            rec_date TEXT NOT NULL,            -- 推荐日期(上次推荐的日期)
            run_id INTEGER REFERENCES runs(id),
            symbol TEXT NOT NULL,
            name TEXT,
            rec_price REAL,                    -- 推荐时价格
            rec_score REAL,                    -- 推荐时得分
            rank INTEGER,                      -- 推荐时的排名
            latest_price REAL,                 -- 最新价格
            change_pct REAL,                   -- 涨跌幅(%)
            days_held INTEGER,                 -- 持有天数
            is_up INTEGER,                     -- 是否上涨(0/1)
            is_limit_up INTEGER,               -- 是否涨停(0/1)
            benchmark_chg REAL,                -- 同期基准涨跌(%)
            excess_return REAL                 -- 超额收益(%)
        );

        CREATE TABLE IF NOT EXISTS tracking_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_date TEXT NOT NULL UNIQUE,    -- 追踪日期
            rec_date TEXT NOT NULL,             -- 被追踪的推荐日期
            n_picks INTEGER,                   -- 推荐数
            hit_rate REAL,                     -- 上涨比例
            avg_return REAL,                   -- 平均收益(%)
            max_return REAL,                   -- 最大涨幅(%)
            min_return REAL,                   -- 最小涨幅(%)
            excess_avg REAL,                   -- 平均超额收益(%)
            score_corr REAL,                   -- 得分与实际收益的相关性
            limit_up_hits INTEGER,             -- 涨停命中数
            raw_json TEXT                      -- 完整数据 JSON
        );
    """)
    conn.commit()
    logger.info("tracking tables initialized")


def track_previous_picks(store=None) -> dict:
    """追踪上一次推荐的表现。

    查出最近一次非今天的 run 的 picks, 获取它们的最新价格, 计算收益。
    """
    conn = _results_conn()
    conn.row_factory = sqlite3.Row

    # 找到最新的 run
    latest = conn.execute("SELECT id, run_at FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    if not latest:
        return {"error": "no runs found"}

    latest_date_str = latest["run_at"][:10]

    # 找到上一次不同日期的 run (不是今天的)
    prev = conn.execute(
        "SELECT id, run_at FROM runs WHERE run_at < ? ORDER BY id DESC LIMIT 1",
        (latest_date_str,)
    ).fetchone()

    if not prev:
        return {"error": "no previous run to track", "latest_date": latest_date_str}

    prev_date_str = prev["run_at"][:10]
    picks = conn.execute(
        "SELECT * FROM picks WHERE run_id=? ORDER BY rank", (prev["id"],)
    ).fetchall()

    if not picks:
        return {"error": "no picks in previous run"}

    # 如果已有这条追踪记录，跳过重复计算
    existing = conn.execute(
        "SELECT COUNT(*) FROM tracking_summary WHERE rec_date=? AND track_date=?",
        (prev_date_str, latest_date_str)
    ).fetchone()[0]
    if existing > 0:
        return {"status": "already_tracked", "rec_date": prev_date_str, "track_date": latest_date_str}

    # 获取这些股票的最新价格
    symbols = [p["symbol"] for p in picks]
    close_data = _get_recent_closes(store, symbols, prev_date_str, latest_date_str)

    if close_data is None or close_data.empty:
        return {"error": "no price data available"}

    # 逐只计算
    tracking_rows = []
    n_up, n_limit_up = 0, 0
    returns_list = []
    scores_list = []
    today_str = latest_date_str

    # 基准对比: 沪深300同期复利回报（循环外计算一次）
    bench_compound = _get_benchmark_return(store, prev_date_str, today_str)

    for p in picks:
        sym = p["symbol"]
        rec_price = p["price"]
        rec_score = p["score"]
        rank = p["rank"]

        if sym not in close_data.columns:
            continue

        sym_close = close_data[sym].dropna()
        if len(sym_close) < 2:
            continue

        entry_price = sym_close.iloc[0]
        exit_price = sym_close.iloc[-1]

        if entry_price <= 0:
            continue

        chg_pct = round((exit_price / entry_price - 1) * 100, 2)
        days = len(sym_close) - 1
        is_up = 1 if chg_pct > 0 else 0

        # 涨停检测: 单日涨幅>=9.5%
        daily_chg = sym_close.pct_change()
        is_limit = 1 if daily_chg.max() > 0.095 else 0

        excess = round(chg_pct - bench_compound, 2)

        if is_up:
            n_up += 1
        if is_limit:
            n_limit_up += 1
        returns_list.append(chg_pct)
        scores_list.append(rec_score)

        conn.execute("""
            INSERT INTO tracking (track_date, rec_date, run_id, symbol, name,
                rec_price, rec_score, rank, latest_price, change_pct,
                days_held, is_up, is_limit_up, benchmark_chg, excess_return)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (today_str, prev_date_str, prev["id"], sym, p["name"],
              rec_price, rec_score, rank, exit_price, chg_pct,
              days, is_up, is_limit, round(bench_compound, 2), excess))

    # 汇总
    n = len(returns_list)
    avg_ret = round(np.mean(returns_list), 2) if returns_list else 0
    hit_rate = round(n_up / n * 100, 1) if n > 0 else 0
    max_ret = round(max(returns_list), 2) if returns_list else 0
    min_ret = round(min(returns_list), 2) if returns_list else 0
    excess_avg = round(sum(r - bench_compound for r in returns_list) / n, 2) if n > 0 else 0

    # 得分-收益相关性: 高分股是否确实涨得更多?
    score_corr = round(np.corrcoef(scores_list, returns_list)[0, 1], 4) if len(scores_list) >= 3 and len(set(scores_list)) > 1 else None

    summary = {
        "track_date": today_str,
        "rec_date": prev_date_str,
        "n_picks": n,
        "hit_rate": hit_rate,
        "avg_return": avg_ret,
        "max_return": max_ret,
        "min_return": min_ret,
        "excess_avg": excess_avg,
        "score_corr": score_corr if score_corr and not np.isnan(score_corr) else None,
        "limit_up_hits": n_limit_up,
    }

    conn.execute("""
        INSERT OR REPLACE INTO tracking_summary
        (track_date, rec_date, n_picks, hit_rate, avg_return, max_return, min_return,
         excess_avg, score_corr, limit_up_hits, raw_json)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
    """, (today_str, prev_date_str, n, hit_rate, avg_ret, max_ret, min_ret,
          excess_avg, score_corr, n_limit_up, json.dumps(summary)))

    conn.commit()

    logger.info(f"tracked {prev_date_str} picks: {n} stocks, "
                f"hit_rate={hit_rate}%, avg={avg_ret}%, score_corr={score_corr}")
    return summary


def get_tracking_history(limit: int = 10) -> list:
    """获取追踪历史"""
    conn = _results_conn()
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM tracking_summary ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        detail = conn.execute(
            "SELECT * FROM tracking WHERE rec_date=? AND track_date=? ORDER BY rank",
            (d["rec_date"], d["track_date"])
        ).fetchall()
        d["details"] = [dict(x) for x in detail]
        result.append(d)
    return result


def get_tracking_stats() -> dict:
    """全局追踪统计: 所有推荐的整体表现"""
    conn = _results_conn()
    conn.row_factory = sqlite3.Row

    total = conn.execute("SELECT COUNT(*) FROM tracking").fetchone()[0]
    if total == 0:
        return {"total_tracked": 0}

    avg_ret = conn.execute("SELECT AVG(change_pct) FROM tracking").fetchone()[0]
    hit_rate = conn.execute(
        "SELECT AVG(CAST(is_up AS REAL)) FROM tracking"
    ).fetchone()[0]
    avg_excess = conn.execute("SELECT AVG(excess_return) FROM tracking").fetchone()[0]
    limit_count = conn.execute("SELECT SUM(is_limit_up) FROM tracking").fetchone()[0]

    # 按得分分组的平均收益 (高分 vs 低分)
    high_score = conn.execute(
        "SELECT AVG(change_pct) FROM tracking WHERE rank <= 1"
    ).fetchone()[0]
    mid_score = conn.execute(
        "SELECT AVG(change_pct) FROM tracking WHERE rank > 1 AND rank <= 3"
    ).fetchone()[0]


    return {
        "total_tracked": total,
        "avg_return": round(avg_ret, 2) if avg_ret else 0,
        "hit_rate": round(hit_rate * 100, 1) if hit_rate else 0,
        "avg_excess": round(avg_excess, 2) if avg_excess else 0,
        "limit_up_hits": limit_count or 0,
        "high_score_avg_return": round(high_score, 2) if high_score else 0,
        "mid_score_avg_return": round(mid_score, 2) if mid_score else 0,
    }


def _get_benchmark_return(store, start_date: str, end_date: str) -> float:
    """获取沪深300在追踪期间的复利收益率(%)"""
    if store is None:
        return 0.0
    try:
        bench_returns = store.get_benchmark("000300", start=start_date.replace("-", ""))
        if bench_returns.empty:
            return 0.0
        # 截取追踪时间段的收益序列
        start_dt = pd.to_datetime(start_date)
        end_dt = pd.to_datetime(end_date)
        period = bench_returns.loc[start_dt:end_dt]
        if len(period) < 2:
            return 0.0
        compound = ((1 + period).prod() - 1) * 100
        return round(compound, 2)
    except Exception:
        return 0.0


def _get_recent_closes(store, symbols: list, start_date: str, end_date: str) -> pd.DataFrame:
    """获取指定日期范围的收盘价"""
    if store is None:
        return None
    try:
        raw = store.get_daily(symbols, start=start_date.replace("-", ""),
                              end=end_date.replace("-", ""))
        if raw.empty:
            return None
        return raw["close"].sort_index().dropna(how="all")
    except Exception as e:
        logger.warning(f"failed to get closes for tracking: {e}")
        return None
