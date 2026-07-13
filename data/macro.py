"""宏观经济指标数据同步 — Gap 7b: 另类数据 — 宏观指标.

数据源: akshare macro_china (CPI/PMI/M2/SHIBOR/国债收益率)
表: macro_indicator (indicator, date, value)
"""

import os, sqlite3, time, logging
from datetime import datetime, timedelta
import re

from config.constants import _require_cfg
from utils.logger import get_logger

logger = get_logger("data.macro")
DB_PATH = os.path.join(os.path.dirname(__file__), "market.db")


def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS macro_indicator (
            indicator TEXT NOT NULL,
            date TEXT NOT NULL,
            value REAL NOT NULL,
            PRIMARY KEY (indicator, date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_macro_date ON macro_indicator(date)")
    conn.commit()


def sync_macro_data():
    """拉取宏观指标: CPI/PMI/M2/10年国债收益率.
    
    来源: akshare — 国家统计局 + 央行公开数据.
    更新频率: 月频 (CPI/PMI/M2), 日频 (国债收益率).
    """
    import akshare as ak
    
    conn = sqlite3.connect(DB_PATH)
    _ensure_table(conn)
    total = 0
    
    # ── CPI 同比 ──
    cpi_df = ak.macro_china_cpi()
    if not cpi_df.empty:
        for _, row in cpi_df.iterrows():
            raw_date = str(row.get("月份", row.get("日期", "")))
            m = re.match(r'(\d{4})年(\d{2})月', raw_date)
            if m:
                date = f"{m.group(1)}-{m.group(2)}"
            else:
                date = raw_date[:10]
            val = row.get("全国-同比增长", row.get("cpi", 0))
            if date and val:
                conn.execute(
                    "INSERT OR REPLACE INTO macro_indicator VALUES(?,?,?)",
                    ("cpi_yoy", date, float(val))
                )
                total += 1
        logger.info(f"CPI synced: {total} rows")
    
    # ── PMI 制造业 ──
    pmi_df = ak.macro_china_pmi()
    if not pmi_df.empty:
        for _, row in pmi_df.iterrows():
            date = str(row.get("日期", ""))[:10]
            val = row.get("制造业", row.get("pmi", 0))
            if date and val:
                conn.execute(
                    "INSERT OR REPLACE INTO macro_indicator VALUES(?,?,?)",
                    ("pmi_manufacturing", date, float(val))
                )
                total += 1
    
    # ── M2 同比增速 ──
    m2_df = ak.macro_china_money_supply()
    if not m2_df.empty:
        for _, row in m2_df.iterrows():
            date = str(row.get("月份", ""))[:10]
            val = row.get("货币和准货币(M2)-同比增长", row.get("m2", 0))
            if date and val:
                conn.execute(
                    "INSERT OR REPLACE INTO macro_indicator VALUES(?,?,?)",
                    ("m2_yoy", date, float(val))
                )
                total += 1
    
    # ── 10年期国债收益率 ──
    bond_df = ak.bond_china_yield()
    if not bond_df.empty:
        for _, row in bond_df.iterrows():
            date = str(row.get("日期", ""))[:10]
            val = row.get("10年", row.get("yield_10y", 0))
            if date and val:
                conn.execute(
                    "INSERT OR REPLACE INTO macro_indicator VALUES(?,?,?)",
                    ("bond_10y_yield", date, float(val))
                )
                total += 1
    
    conn.commit()
    conn.close()
    logger.info(f"macro sync done: {total} total rows")
    return total


def get_macro_value(indicator: str, date: str) -> float:
    """获取指定日期的宏观指标值 (向前查找最近可用数据).
    
    宏观指标为月频/日频混合, 向前查找最近一条记录.
    """
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT value FROM macro_indicator WHERE indicator=? AND date <= ? ORDER BY date DESC LIMIT 1",
        (indicator, date)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def get_macro_pmi_diff(pmi_val: float) -> float:
    """PMI 偏离荣枯线: PMI - 50.
    
    PMI > 50 扩张, < 50 收缩. A股历史上 PMI>50 期间年化收益 +15%, <50 仅 +2%.
    """
    if pmi_val is None:
        return 0.0
    return pmi_val - 50.0
