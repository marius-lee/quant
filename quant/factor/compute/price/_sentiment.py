"""新闻情绪因子 — Gap 7a: 另类数据 NLP.
v375: aux["news"] 预加载替代 per-date SQL (消除 4 SQL查询/日期).
"""

import numpy as np
import pandas as pd

from quant.utils.date import to_str
from quant.factor.registry import _cs_zscore
from quant.utils.logger import get_logger as _get_logger

_log = _get_logger("factor.compute.sentiment")


def _news_from_aux(aux: dict, symbols: list, date: str, window_days: int):
    """从 aux["news"] DataFrame 提取窗口内新闻聚合 (替代 SQL).
    返回 (sentiment_series, count_series)."""
    news = aux.get("news", pd.DataFrame())
    sentiment = pd.Series(0.5, index=symbols, dtype=float)
    counts = pd.Series(0, index=symbols, dtype=int)
    if news.empty or "date" not in news.columns:
        return sentiment, counts

    ts = pd.Timestamp(to_str(date))
    from_ts = ts - pd.Timedelta(days=window_days - 1)
    news_dates = pd.to_datetime(news["date"])
    mask = (news_dates >= from_ts) & (news_dates <= ts)
    w = news.loc[mask]
    if w.empty:
        return sentiment, counts

    grp = w.groupby("symbol").agg(
        avg_sent=("avg_sentiment", "mean"),
        total_cnt=("news_count", "sum"))
    for sym in grp.index.intersection(symbols):
        sentiment[sym] = grp.loc[sym, "avg_sent"]
        counts[sym] = int(grp.loc[sym, "total_cnt"])
    return sentiment, counts


def compute_news_sentiment_1d(data, date, window=0, aux=None):
    """当日新闻情感得分. v375: aux 替代 SQL."""
    symbols = list(data["close"].columns)
    if aux is not None and "news" in aux:
        sentiment, _ = _news_from_aux(aux, symbols, date, 1)
    else:
        sentiment = pd.Series(0.5, index=symbols, dtype=float)
    return _cs_zscore(sentiment, sparse=True).rename("news_sentiment_1d")


def compute_news_volume_5d(data, date, window=0, aux=None):
    """5日新闻数量. v375: aux 替代 SQL."""
    symbols = list(data["close"].columns)
    if aux is not None and "news" in aux:
        _, counts = _news_from_aux(aux, symbols, date, 5)
    else:
        counts = pd.Series(0, index=symbols, dtype=int)
    return _cs_zscore(counts.astype(float), sparse=True).rename("news_volume_5d")


def compute_news_abnormal_20d(data, date, window=0, aux=None):
    """20日异常新闻量. v375: aux 替代 SQL."""
    symbols = list(data["close"].columns)
    result = pd.Series(0.0, index=symbols)
    if aux is None or "news" not in aux:
        return _cs_zscore(result, sparse=True).rename("news_abnormal_20d")

    news = aux["news"]
    if news.empty or "date" not in news.columns:
        return _cs_zscore(result, sparse=True).rename("news_abnormal_20d")

    ts = pd.Timestamp(to_str(date))
    news_dates = pd.to_datetime(news["date"])
    # Current: last 5 days avg
    cur_mask = (news_dates >= ts - pd.Timedelta(days=5)) & (news_dates <= ts)
    cur_grp = news.loc[cur_mask].groupby("symbol")["news_count"].mean()
    # Baseline: prior 20 days avg
    base_mask = (news_dates >= ts - pd.Timedelta(days=40)) & (news_dates < ts - pd.Timedelta(days=5))
    base_grp = news.loc[base_mask].groupby("symbol")["news_count"].mean()

    for sym in symbols:
        cur = cur_grp.get(sym, 0)
        base = base_grp.get(sym, 0)
        if base > 0:
            result[sym] = (cur - base) / base

    return _cs_zscore(result, sparse=True).rename("news_abnormal_20d")
