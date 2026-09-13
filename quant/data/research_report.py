"""研报数据同步 — 东方财富个股研报 + 情感/目标价/评级因子化.

数据源: akshare stock_research_report_em (东方财富研报)
频率: 日度 (rollback 30 天滚动)
表: research_report
字段: symbol, pub_date, pub_time, title, institute, analyst, rating, target_price, content, sentiment_score, source
因子: alt_rpt_sentiment, alt_rpt_target_price, alt_rpt_rating, alt_rpt_consensus
"""

import os
import time
import logging
import sqlite3
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional

from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger
from quant.config.paths import MARKET_DB
from quant.data.datasource_retry import datasource_retry

_log = get_logger("data.research_report")
DB_PATH = MARKET_DB


def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS research_report (
            symbol TEXT NOT NULL,
            pub_date TEXT NOT NULL,
            pub_time TEXT,
            title TEXT,
            institute TEXT,
            analyst TEXT,
            rating TEXT,
            target_price REAL,
            content TEXT,
            sentiment_score REAL,
            source TEXT DEFAULT 'eastmoney',
            PRIMARY KEY (symbol, pub_date, pub_time, title)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_research_report_date ON research_report(pub_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_research_report_symbol ON research_report(symbol)")
    conn.commit()


def _sentiment_snownlp(text: str) -> float:
    """SnowNLP 中文情感分析: 返回 [0, 1], >0.6 正面."""
    try:
        from snownlp import SnowNLP
        return SnowNLP(text).sentiments
    except Exception as _e:
        logger.warning(f"silent exception: {_e}")
        return _sentiment_fallback(text)


def _sentiment_fallback(text: str) -> float:
    """基于关键词的简单情感分类 (SnowNLP 不可用时的回退)."""
    pos_words = ["利好", "增长", "突破", "涨停", "超预期", "回购", "增持", "分红", "扭亏",
                 "中标", "签约", "获批", "创新高", "业绩预增", "高送转", "买入", "增持", "推荐"]
    neg_words = ["利空", "下跌", "跌停", "亏损", "减持", "违规", "处罚", "退市", "暴雷",
                 "调查", "立案", "问询函", "警示函", "业绩预减", "商誉减值", "质押爆仓", "卖出", "减持"]
    score = 0.5
    for w in pos_words:
        if w in text:
            score += 0.08
    for w in neg_words:
        if w in text:
            score -= 0.08
    return max(0.0, min(1.0, score))


def _rating_to_score(rating: str) -> float:
    """评级映射: 买入=1, 增持=0.5, 中性=0, 减持=-0.5, 卖出=-1."""
    rating_map = {
        "买入": 1, "强烈推荐": 1, "推荐": 1,
        "增持": 0.5, "谨慎推荐": 0.5, "持有": 0.5,
        "中性": 0, "市场表现": 0, "观望": 0,
        "减持": -0.5, "谨慎持有": -0.5,
        "卖出": -1, "强烈卖出": -1,
    }
    return rating_map.get(rating, 0.0)


@datasource_retry
def _fetch_reports() -> pd.DataFrame:
    """拉取东方财富研报数据."""
    import akshare as ak
    return ak.stock_research_report_em()


def _process_reports(df: pd.DataFrame) -> list:
    """处理研报数据, 计算情感/评分."""
    rows = []
    for _, row in df.iterrows():
        title = str(row.get("报告名称", ""))
        if not title:
            continue

        sym = str(row.get("股票代码", "")).zfill(6)
        if not sym or len(sym) != 6:
            continue

        # 使用 '日期' 列作为发布日期 (akshare 返回的列名为 '日期')
        pub_date_raw = str(row.get("日期", "")).strip()
        if pub_date_raw:
            try:
                # 解析日期格式
                pub_date = pd.to_datetime(pub_date_raw).strftime("%Y-%m-%d")
                pub_time = pub_date  # 使用日期作为时间
            except Exception:
                pub_date = datetime.now().strftime("%Y-%m-%d")
                pub_time = pub_date
        else:
            pub_date = datetime.now().strftime("%Y-%m-%d")
            pub_time = pub_date

        institute = str(row.get("机构", row.get("institute", "")))
        analyst = str(row.get("分析师", row.get("analyst", "")))
        rating = str(row.get("评级", row.get("rating", "")))
        target_price = row.get("目标价", row.get("target_price", 0))
        try:
            target_price = float(target_price) if target_price else 0.0
        except (ValueError, TypeError):
            target_price = 0.0

        content = str(row.get("内容摘要", row.get("content", "")))
        url = str(row.get("链接", row.get("url", "")))

        # 情感分析
        sentiment = _sentiment_snownlp(title + " " + content)
        rating_score = _rating_to_score(rating)

        rows.append((
            sym, pub_date, pub_time, title, institute, analyst,
            rating, target_price, content, sentiment, "eastmoney"
        ))
    return rows


def sync_range(start_date: str = None, end_date: str = None) -> int:
    """同步研报数据到 research_report 表.

    Args:
        start_date: YYYY-MM-DD, 默认 30 天前
        end_date: YYYY-MM-DD, 默认今天
    """
    if start_date is None:
        start_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    # v552: 受害加固 — 连接配置
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    _ensure_table(conn)

    try:
        df = _fetch_reports()
        if df.empty:
            _log.warning("stock_research_report_em returned empty")
            return 0

        # 过滤日期范围 - 兼容列名变化
        date_col = '日期' if '日期' in df.columns else ('发布时间' if '发布时间' in df.columns else None)
        if date_col is None:
            _log.warning("research_report: no date column found")
            return 0

        df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
        df = df.dropna(subset=[date_col])
        df = df[(df[date_col] >= start_date) & (df[date_col] <= end_date)]

        if df.empty:
            _log.info(f"research_report: no reports in range {start_date}..{end_date}")
            return 0

        rows = _process_reports(df)
        if not rows:
            return 0

        conn.executemany(
            """INSERT OR REPLACE INTO research_report
               (symbol, pub_date, pub_time, title, institute, analyst, rating,
                target_price, content, sentiment_score, source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows
        )
        conn.commit()
        _log.info(f"research_report synced: {len(rows)} reports ({start_date}..{end_date})")
        return len(rows)

    except Exception as e:
        _log.error(f"research_report sync failed: {e}")
        raise
    finally:
        conn.close()


def sync_date(date_str: str) -> int:
    """单日同步入口 (供 rollback 调用)."""
    return sync_range(date_str, date_str)


def get_reports(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """查询研报数据."""
    conn = sqlite3.connect(DB_PATH)
    try:
        df = pd.read_sql_query(
            """SELECT * FROM research_report
               WHERE symbol=? AND pub_date BETWEEN ? AND ?
               ORDER BY pub_date DESC, pub_time DESC""",
            conn, params=(symbol, start_date, end_date)
        )
        return df
    finally:
        conn.close()