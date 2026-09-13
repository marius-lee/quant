"""ESG 评分数据同步 — 新浪财经 ESG 评分 (华证/综合/MSCI).

数据源: akshare stock_esg_hz_sina / stock_esg_msci_sina
频率: 月度/季度 (weekly_full 全量幂等)
表: esg_score
字段: symbol, data_year, esg_score, env_score, social_score, gov_score,
      carbon_emission, green_revenue_pct, source, rating_date
因子: alt_esg, alt_env, alt_social, alt_gov, alt_carbon, alt_green_rev
"""

import time
import logging
import sqlite3
import pandas as pd
from datetime import datetime
from typing import Optional

from quant.config.constants import _require_cfg
from quant.utils.logger import get_logger
from quant.config.paths import MARKET_DB
from quant.data.datasource_retry import datasource_retry

_log = get_logger("data.esg_score")
DB_PATH = MARKET_DB


def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS esg_score (
            symbol TEXT NOT NULL,
            data_year TEXT NOT NULL,        -- 数据年份 (YYYY)
            esg_score REAL,                 -- 综合 ESG 评分
            env_score REAL,                 -- 环境评分
            social_score REAL,              -- 社会评分
            gov_score REAL,                 -- 治理评分
            carbon_emission REAL,           -- 碳排放
            green_revenue_pct REAL,         -- 绿色收入占比
            source TEXT NOT NULL,           -- hz_sina / msci_sina
            rating_date TEXT,               -- 评级日期
            PRIMARY KEY (symbol, data_year, source)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_esg_score_symbol ON esg_score(symbol)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_esg_score_year ON esg_score(data_year)")
    conn.commit()


@datasource_retry
def _fetch_esg_hz() -> pd.DataFrame:
    """拉取华证 ESG 评分."""
    import akshare as ak
    return ak.stock_esg_hz_sina()


@datasource_retry
def _fetch_esg_msci() -> pd.DataFrame:
    """拉取 MSCI ESG 评分."""
    import akshare as ak
    return ak.stock_esg_msci_sina()


def _process_esg_hz(df: pd.DataFrame) -> list:
    """处理华证 ESG 数据."""
    rows = []
    for _, row in df.iterrows():
        sym = str(row.get("股票代码", row.get("symbol", ""))).zfill(6)
        if not sym or len(sym) != 6:
            continue

        try:
            esg = float(row.get("ESG评分", row.get("esg_score", 0)) or 0)
            env = float(row.get("环境评分", row.get("env_score", 0)) or 0)
            social = float(row.get("社会评分", row.get("social_score", 0)) or 0)
            gov = float(row.get("治理评分", row.get("gov_score", 0)) or 0)
            carbon = float(row.get("碳排放", row.get("carbon_emission", 0)) or 0)
            green_pct = float(row.get("绿色收入占比", row.get("green_revenue_pct", 0)) or 0)
            year = str(row.get("数据年份", row.get("year", datetime.now().year)))
            rating_date = str(row.get("评级日期", row.get("rating_date", "")))[:10]
        except (ValueError, TypeError):
            continue

        if esg == 0 and env == 0 and social == 0 and gov == 0:
            continue

        rows.append((
            sym, year, esg, env, social, gov, carbon, green_pct,
            "hz_sina", rating_date
        ))
    return rows


def _process_esg_msci(df: pd.DataFrame) -> list:
    """处理 MSCI ESG 数据."""
    rows = []
    for _, row in df.iterrows():
        sym = str(row.get("股票代码", row.get("symbol", ""))).zfill(6)
        if not sym or len(sym) != 6:
            continue

        try:
            esg = str(row.get("ESG评分", row.get("esg_score", "")))
            env = float(row.get("环境总评", row.get("env_score", 0)) or 0)
            social = float(row.get("社会责任总评", row.get("social_score", 0)) or 0)
            gov = float(row.get("治理总评", row.get("gov_score", 0)) or 0)
            year = str(row.get("数据年份", row.get("year", datetime.now().year)))
            rating_date = str(row.get("评级日期", row.get("rating_date", "")))[:10]
        except (ValueError, TypeError):
            continue

        # MSCI 评分是字母等级 (AAA/AA/A/BBB/BB/B/CCC)
        esg_map = {"AAA": 100, "AA": 90, "A": 80, "BBB": 70, "BB": 60, "B": 50, "CCC": 40}
        esg_score = esg_map.get(esg, 0)

        if esg_score == 0 and env == 0 and social == 0 and gov == 0:
            continue

        rows.append((
            sym, year, esg_score, env, social, gov, 0.0, 0.0,
            "msci_sina", rating_date
        ))
    return rows


def sync_range(start_date: str = None, end_date: str = None) -> int:
    """同步 ESG 评分数据 (全量幂等, 周六调用).

    Args:
        start_date: 兼容参数, 实际全量拉取
        end_date: 兼容参数
    """
    # v552: 受害加固
    conn = sqlite3.connect(MARKET_DB, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    _ensure_table(conn)

    total = 0
    try:
        # 华证 ESG
        try:
            df_hz = _fetch_esg_hz()
            if not df_hz.empty:
                rows_hz = _process_esg_hz(df_hz)
                if rows_hz:
                    conn.executemany(
                        """INSERT OR REPLACE INTO esg_score
                           (symbol, data_year, esg_score, env_score, social_score,
                            gov_score, carbon_emission, green_revenue_pct, source, rating_date)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        rows_hz
                    )
                    total += len(rows_hz)
                    _log.info(f"esg_score (hz_sina) synced: {len(rows_hz)} records")
        except Exception as e:
            _log.warning(f"esg_score hz_sina failed: {e}")

        # MSCI ESG
        try:
            df_msci = _fetch_esg_msci()
            if not df_msci.empty:
                rows_msci = _process_esg_msci(df_msci)
                if rows_msci:
                    conn.executemany(
                        """INSERT OR REPLACE INTO esg_score
                           (symbol, data_year, esg_score, env_score, social_score,
                            gov_score, carbon_emission, green_revenue_pct, source, rating_date)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        rows_msci
                    )
                    total += len(rows_msci)
                    _log.info(f"esg_score (msci_sina) synced: {len(rows_msci)} records")
        except Exception as e:
            _log.warning(f"esg_score msci_sina failed: {e}")

        conn.commit()
        _log.info(f"esg_score total synced: {total} records")
        return total

    except Exception as e:
        _log.error(f"esg_score sync failed: {e}")
        raise
    finally:
        conn.close()


def get_esg_score(symbol: str, year: str = None, source: str = None) -> pd.DataFrame:
    """查询 ESG 评分."""
    conn = sqlite3.connect(MARKET_DB)
    try:
        sql = "SELECT * FROM esg_score WHERE symbol=?"
        params = [symbol]
        if year:
            sql += " AND data_year=?"
            params.append(year)
        if source:
            sql += " AND source=?"
            params.append(source)
        sql += " ORDER BY data_year DESC"
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()