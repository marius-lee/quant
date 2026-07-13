"""新闻情绪因子 — Gap 7a: 另类数据 NLP.

因子:
  news_sentiment_1d  — 当日新闻情感得分 (0=负面, 1=正面)
  news_volume_5d     — 过去5天新闻数量 (异常媒体关注度)
  news_abnormal_20d  — 20天新闻量偏离均值 (异常热度)

数据来源: data/news.py — 东方财富个股新闻 + SnowNLP/Snownlp 中文情感分析.
"""

import numpy as np
import pandas as pd
import sqlite3
import os as _os

from config.constants import _market_db_path
from factor.registry import _cs_zscore
from utils.logger import get_logger as _get_logger

_log = _get_logger("factor.compute.sentiment")


def _get_news_series(symbols: list, date: str, window: int = 1) -> "pd.Series":
    """从 news_daily_count 表获取指定窗口内的新闻数据.
    
    Args:
        symbols: 股票代码列表
        date: 查询日期 YYYY-MM-DD
        window: 回看天数
    Returns:
        sentiment Series (index=symbol, value=avg_sentiment or 0.5)
        count Series (index=symbol, value=news_count)
    """
    db = _market_db_path()
    conn = sqlite3.connect(db)
    
    from_date = pd.Timestamp(date) - pd.Timedelta(days=window - 1)
    from_str = from_date.strftime("%Y-%m-%d")
    
    sentiment = pd.Series(0.5, index=symbols, dtype=float)
    counts = pd.Series(0, index=symbols, dtype=int)
    
    try:
        placeholders = ",".join("?" for _ in symbols)
        rows = conn.execute(
            f"SELECT symbol, AVG(avg_sentiment), SUM(news_count) "
            f"FROM news_daily_count "
            f"WHERE symbol IN ({placeholders}) AND date BETWEEN ? AND ? "
            f"GROUP BY symbol",
            symbols + [from_str, date]
        ).fetchall()
        
        for sym, avg_s, total_c in rows:
            if sym in sentiment.index and avg_s is not None:
                sentiment[sym] = avg_s
            if sym in counts.index and total_c is not None:
                counts[sym] = total_c
    except sqlite3.OperationalError:
        _log.warning("news_daily_count table not found — run data/news.py sync first")
    finally:
        conn.close()
    
    return sentiment, counts


def compute_news_sentiment_1d(data: "pd.DataFrame", date: str, window: int = 0) -> "pd.Series":
    """当日新闻情感得分: 正面新闻→高分.
    
    来源: 东方证券(2019) — 新闻情感对次日收益有正向预测力, IC≈0.02~0.04.
    逻辑: 正面新闻吸引关注度→短期资金流入; 负面新闻引发恐慌→短期抛压.
    """
    symbols = list(data["close"].columns)
    sentiment, _ = _get_news_series(symbols, date, window=1)
    sentiment = _cs_zscore(sentiment, sparse=True)
    return sentiment.rename("news_sentiment_1d")


def compute_news_volume_5d(data: "pd.DataFrame", date: str, window: int = 0) -> "pd.Series":
    """5日新闻数量: 媒体异常关注度.
    
    来源: 方正证券(2020) — 新闻量暴增的股票短期波动率上升, 对动量因子有加成效应.
    逻辑: 异常高的新闻量意味着信息冲击, 短期价格可能剧烈波动.
    """
    symbols = list(data["close"].columns)
    _, counts = _get_news_series(symbols, date, window=5)
    # News volume as z-score, higher=more media attention
    result = _cs_zscore(counts.astype(float), sparse=True)
    return result.rename("news_volume_5d")


def compute_news_abnormal_20d(data: "pd.DataFrame", date: str, window: int = 0) -> "pd.Series":
    """20日异常新闻量: 当前新闻量 vs 过去20日均值的偏离.
    
    来源: 国泰君安(2021) — 异常新闻热度与短期反转效应相关.
    A股散户主导, 新闻热度骤升→散户追涨→短期高点.
    逻辑: 正向偏离 = 过度关注 = 短期过热信号 (负向因子).
    """
    symbols = list(data["close"].columns)
    db = _market_db_path()
    conn = sqlite3.connect(db)
    
    result = pd.Series(0.0, index=symbols)
    
    try:
        date_ts = pd.Timestamp(date)
        from_20 = (date_ts - pd.Timedelta(days=20)).strftime("%Y-%m-%d")
        from_40 = (date_ts - pd.Timedelta(days=40)).strftime("%Y-%m-%d")
        
        placeholders = ",".join("?" for _ in symbols)
        # Current: last 5 days avg
        rows_cur = conn.execute(
            f"SELECT symbol, AVG(news_count) FROM news_daily_count "
            f"WHERE symbol IN ({placeholders}) AND date BETWEEN ? AND ? "
            f"GROUP BY symbol",
            symbols + [from_20, date]
        ).fetchall()
        # Baseline: prior 20 days avg
        rows_base = conn.execute(
            f"SELECT symbol, AVG(news_count) FROM news_daily_count "
            f"WHERE symbol IN ({placeholders}) AND date BETWEEN ? AND ? "
            f"GROUP BY symbol",
            symbols + [from_40, from_20]
        ).fetchall()
        
        cur_map = {r[0]: r[1] for r in rows_cur if r[1]}
        base_map = {r[0]: r[1] for r in rows_base if r[1]}
        
        for sym in symbols:
            cur = cur_map.get(sym, 0)
            base = base_map.get(sym, 0)
            if base > 0:
                # Abnormal = (current - baseline) / baseline
                result[sym] = (cur - base) / base
    except sqlite3.OperationalError:
        _log.warning("news_daily_count table not found")
    finally:
        conn.close()
    
    result = _cs_zscore(result, sparse=True)
    return result.rename("news_abnormal_20d")
