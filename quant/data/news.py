"""新闻情绪数据同步 — 东方财富个股新闻 + SnowNLP 情感分析.

Gap 7a: 另类数据 — 新闻情绪 NLP.
数据源: akshare stock_news_em (东方财富个股新闻)
NLP: SnowNLP 中文情感分析
表: news_sentiment (symbol, date, pub_time, title, sentiment_score, news_count_daily)
"""

import os, sqlite3, time, logging
import pandas as pd
from datetime import datetime

from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger
from quant.utils.date import validate_date_format

logger = get_logger("data.news")
DB_PATH = os.path.join(os.path.dirname(__file__), "market.db")


def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_sentiment (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            pub_time TEXT,
            title TEXT,
            sentiment_score REAL,
            PRIMARY KEY (symbol, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS news_daily_count (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            news_count INTEGER NOT NULL DEFAULT 0,
            avg_sentiment REAL,
            PRIMARY KEY (symbol, date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_sentiment_date ON news_sentiment(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_news_daily_count_date ON news_daily_count(date)")
    conn.commit()


def _sentiment_snownlp(text: str) -> float:
    """SnowNLP 中文情感分析: 返回 [0, 1], >0.6 正面."""
    from snownlp import SnowNLP
    return SnowNLP(text).sentiments


def _sentiment_fallback(text: str) -> float:
    """基于关键词的简单情感分类 (SnowNLP 不可用时的回退).
    
    正面词: 利好/增长/突破/涨停/超预期/回购/增持
    负面词: 利空/下跌/跌停/亏损/减持/违规/处罚/退市
    每个正面词 +0.1, 负面词 -0.1, 从 0.5 起算, 截断到 [0,1].
    """
    pos_words = ["利好", "增长", "突破", "涨停", "超预期", "回购", "增持", "分红", "扭亏",
                 "中标", "签约", "获批", "创新高", "业绩预增", "高送转"]
    neg_words = ["利空", "下跌", "跌停", "亏损", "减持", "违规", "处罚", "退市", "暴雷",
                 "调查", "立案", "问询函", "警示函", "业绩预减", "商誉减值", "质押爆仓"]
    score = 0.5
    for w in pos_words:
        if w in text:
            score += 0.08
    for w in neg_words:
        if w in text:
            score -= 0.08
    return max(0.0, min(1.0, score))


def sync_news_sentiment(start_date: str = None, end_date: str = None, max_per_day: int = 100):
    """拉取东方财富个股新闻并计算情感得分.
    
    Args:
        start_date: YYYY-MM-DD, 默认昨天
        end_date: YYYY-MM-DD, 默认今天
        max_per_day: 每天最多拉取新闻数 (akshare 限流)
    """
    import akshare as ak
    
    if start_date is None:
        start_date = (datetime.now().strftime("%Y-%m-%d"))
    if end_date is None:
        end_date = start_date
    
    conn = sqlite3.connect(DB_PATH)
    _ensure_table(conn)
    
    total_new = 0
    news_df = ak.stock_news_em()
    if news_df.empty:
        logger.warning("stock_news_em returned empty")
        return 0

    # Process each news item
    for _, row in news_df.iterrows():
        if total_new >= max_per_day:
            break
        title = str(row.get("新闻标题", row.get("title", "")))
        if not title:
            continue

        # Extract symbol from news content/code
        sym = str(row.get("关键词", row.get("code", ""))).strip()
        if not sym or len(sym) < 6:
            continue

        pub_time = str(row.get("发布时间", row.get("pub_time", "")))[:19]
        date = pub_time[:10] if pub_time else start_date

        # Sentiment analysis
        score = _sentiment_snownlp(title)

        conn.execute(
            "INSERT OR REPLACE INTO news_sentiment (symbol, date, pub_time, title, sentiment_score) "
            "VALUES (?, ?, ?, ?, ?)",
            (sym, date, pub_time, title, round(score, 4))
        )
        total_new += 1
    conn.execute("""
        INSERT OR REPLACE INTO news_daily_count (symbol, date, news_count, avg_sentiment)
        SELECT symbol, date, COUNT(*), AVG(sentiment_score)
        FROM news_sentiment
        WHERE date BETWEEN ? AND ?
        GROUP BY symbol, date
    """, (start_date, end_date))

    conn.commit()
    logger.info(f"news_sentiment synced: {total_new} news items")
    
    return total_new


def get_news_sentiment(symbol: str, date: str) -> float:
    """获取指定股票在指定日期的新闻情感得分.
    
    Returns: 0-1 浮点数, 无数据返回 None.
    """
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT avg_sentiment FROM news_daily_count WHERE symbol=? AND date=?",
        (symbol, date)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def get_news_count(symbol: str, date: str) -> int:
    """获取指定股票在指定日期的新闻数量."""
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT news_count FROM news_daily_count WHERE symbol=? AND date=?",
        (symbol, date)
    ).fetchone()
    conn.close()
    return row[0] if row else 0
