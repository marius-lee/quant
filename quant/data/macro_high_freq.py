"""宏观高频数据同步 — 用电量/货运/信贷/社融/PMI/GDP/CPI/PPI/M2/Shibor/LPR 等高频宏观指标.

数据源: akshare macro_china_* 系列 (国家统计局/央行/电力企业协会/铁路局/海关/证监会)
频率: 月度/季度/年度 (rollback 365 天滚动回补 + 周六全量)
表: macro_high_freq
字段: symbol, date, indicator_name, value, unit, frequency, source
因子: alt_macro_electricity, alt_macro_freight, alt_macro_credit, alt_macro_pmi, alt_macro_gdp, alt_macro_cpi, alt_macro_ppi, alt_macro_m2, alt_macro_shibor, alt_macro_lpr, alt_macro_money_supply, alt_macro_bank_financing, alt_macro_industrial, alt_macro_exports, alt_macro_imports, alt_macro_retail, alt_macro_real_estate, alt_macro_traffic
"""

import re
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

_log = get_logger("data.macro_high_freq")
DB_PATH = MARKET_DB


def _normalize_date(date_str: str) -> str:
    """将各种中文日期格式标准化为 YYYY-MM-DD."""
    if not date_str or not isinstance(date_str, str):
        return date_str
    date_str = date_str.strip()
    # 已经是标准格式
    if re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return date_str
    # 处理 "2025年01月份" -> "2025-01-01"
    m = re.match(r'^(\d{4})年(\d{1,2})月份$', date_str)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-01"
    # 处理 "2025年第1-2季" / "2025年第1-2季度" / "2025年第1季" / "2025年第1季度" -> 季度末日期
    m = re.match(r'^(\d{4})年第(\d+)[-~]?(\d*)季度?$', date_str)
    if m:
        year = m.group(1)
        q1 = int(m.group(2))
        q2 = int(m.group(3)) if m.group(3) else int(m.group(2))
        # 使用季度末日期: Q1=03-31, Q2=06-30, Q3=09-30, Q4=12-31
        end_month = q2 * 3
        return f"{year}-{end_month:02d}-30" if end_month != 12 else f"{year}-12-31"
    # 处理 "2025.10" / "2025.1" -> "2025-10-01"
    m = re.match(r'^(\d{4})[.-](\d{1,2})$', date_str)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-01"
    # 已经是标准格式
    if re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
        return date_str
    return date_str
def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS macro_high_freq (
            symbol TEXT NOT NULL,           -- 指标代码 (如: electricity, freight, credit, pmi, gdp, cpi, ppi, m2, shibor, lpr)
            date TEXT NOT NULL,             -- 数据日期 (YYYY-MM-DD)
            indicator_name TEXT NOT NULL,   -- 指标名称
            value REAL NOT NULL,            -- 数值
            unit TEXT,                      -- 单位
            frequency TEXT,                 -- 频率 (monthly/quarterly/yearly)
            source TEXT DEFAULT 'akshare',  -- 数据源
            PRIMARY KEY (symbol, date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_macro_high_freq_date ON macro_high_freq(date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_macro_high_freq_symbol ON macro_high_freq(symbol)")
    conn.commit()


# 宏观指标映射: akshare 函数 -> 标准化字段
# 函数名已验证在 akshare 中存在且有历史数据 (2026-09-02 测试通过)
# 显式指定 date_col 和 value_col 以解决列名不统一问题
MACRO_INDICATORS = {
    # 用电量/能源
    "electricity": {
        "func": "macro_china_society_electricity",
        "name": "全社会用电量",
        "unit": "亿千瓦时",
        "freq": "monthly",
        "date_col": "统计时间",
        "value_col": "全社会用电量",
    },
    # 货运/物流
    "freight": {
        "func": "macro_china_freight_index",
        "name": "货运指数",
        "unit": "指数",
        "freq": "monthly",
        "date_col": "截止日期",
        "value_col": "波罗的海综合运价指数BDI",
    },
    "traffic_volume": {
        "func": "macro_china_society_traffic_volume",
        "name": "社会客货运量",
        "unit": "亿吨/亿人",
        "freq": "monthly",
        "date_col": "统计时间",
        "value_col": "货运量",
    },
    # 信贷/金融
    "credit": {
        "func": "macro_china_new_financial_credit",
        "name": "新增信贷/社融",
        "unit": "亿元",
        "freq": "monthly",
        "date_col": "月份",
        "value_col": "当月",
    },
    "bank_financing": {
        "func": "macro_china_bank_financing",
        "name": "银行融资/信贷投放",
        "unit": "亿元",
        "freq": "monthly",
        "date_col": "日期",
        "value_col": "最新值",
    },
    # 货币供应
    "m2": {
        "func": "macro_china_m2_yearly",
        "name": "广义货币 M2",
        "unit": "亿元",
        "freq": "monthly",
        "date_col": "日期",
        "value_col": "今值",
    },
    "money_supply": {
        "func": "macro_china_supply_of_money",
        "name": "货币供应量",
        "unit": "亿元",
        "freq": "monthly",
        "date_col": "统计时间",
        "value_col": "货币和准货币（广义货币M2）",
    },
    # PMI
    "pmi": {
        "func": "macro_china_pmi",
        "name": "制造业/非制造业 PMI 综合",
        "unit": "指数",
        "freq": "monthly",
        "date_col": "月份",
        "value_col": "制造业-指数",
    },
    "pmi_yearly": {
        "func": "macro_china_pmi_yearly",
        "name": "年度 PMI",
        "unit": "指数",
        "freq": "yearly",
        "date_col": "日期",
        "value_col": "今值",
    },
    # 价格指数
    "cpi": {
        "func": "macro_china_cpi",
        "name": "居民消费价格指数 CPI",
        "unit": "同比%",
        "freq": "monthly",
        "date_col": "月份",
        "value_col": "全国-当月",
    },
    "ppi": {
        "func": "macro_china_ppi",
        "name": "工业生产者出厂价格指数 PPI",
        "unit": "同比%",
        "freq": "monthly",
        "date_col": "月份",
        "value_col": "当月",
    },
    # GDP/经济增长
    "gdp": {
        "func": "macro_china_gdp",
        "name": "GDP 季度/累计",
        "unit": "亿元",
        "freq": "quarterly",
        "date_col": "季度",
        "value_col": "国内生产总值-同比增长",
    },
    "gdp_yearly": {
        "func": "macro_china_gdp_yearly",
        "name": "年度 GDP",
        "unit": "亿元",
        "freq": "yearly",
        "date_col": "日期",
        "value_col": "今值",
    },
    # 工业/出口/消费
    "industrial_production": {
        "func": "macro_china_industrial_production_yoy",
        "name": "工业增加值增速",
        "unit": "同比%",
        "freq": "monthly",
        "date_col": "日期",
        "value_col": "今值",
    },
    "exports": {
        "func": "macro_china_exports_yoy",
        "name": "出口同比增速",
        "unit": "同比%",
        "freq": "monthly",
        "date_col": "日期",
        "value_col": "今值",
    },
    "imports": {
        "func": "macro_china_imports_yoy",
        "name": "进口同比增速",
        "unit": "同比%",
        "freq": "monthly",
        "date_col": "日期",
        "value_col": "今值",
    },
    "retail": {
        "func": "macro_china_consumer_goods_retail",
        "name": "社会消费品零售总额增速",
        "unit": "同比%",
        "freq": "monthly",
        "date_col": "月份",
        "value_col": "同比增长",
    },
    # 房地产
    "real_estate": {
        "func": "macro_china_real_estate",
        "name": "房地产开发投资/销售",
        "unit": "亿元/万平米",
        "freq": "monthly",
        "date_col": "日期",
        "value_col": "最新值",
    },
    # 利率/汇率
    "shibor": {
        "func": "macro_china_shibor_all",
        "name": "Shibor 全期限利率",
        "unit": "%",
        "freq": "daily",
        "date_col": "日期",
        "value_col": "O/N-定价",
    },
    "lpr": {
        "func": "macro_china_lpr",
        "name": "贷款市场报价利率 LPR",
        "unit": "%",
        "freq": "monthly",
        "date_col": "TRADE_DATE",
        "value_col": "LPR1Y",
    },
    "bank_financing": {
        "func": "macro_china_bank_financing",
        "name": "银行融资/信贷投放",
        "unit": "亿元",
        "freq": "monthly",
        "date_col": "日期",
        "value_col": "最新值",
    },
}


@datasource_retry
def _call_akshare(func_name: str) -> pd.DataFrame:
    """调用 akshare 宏观函数."""
    import akshare as ak
    func = getattr(ak, func_name)
    return func()


def _fetch_all_macro() -> list:
    """拉取所有宏观指标."""
    rows = []
    for symbol, cfg in MACRO_INDICATORS.items():
        try:
            df = _call_akshare(cfg["func"])
            if df is None or df.empty:
                _log.debug(f"macro {symbol} returned empty")
                continue

            # 标准化列名 (akshare 返回格式不统一)
            df.columns = [c.strip() for c in df.columns]

            # 使用显式配置的列名 (避免启发式识别失败)
            date_col = cfg.get("date_col")
            value_col = cfg.get("value_col")
            if date_col is None or value_col is None:
                _log.warning(f"macro {symbol} missing date_col/value_col config")
                continue

            if date_col not in df.columns or value_col not in df.columns:
                _log.warning(f"macro {symbol}: date_col={date_col} or value_col={value_col} not found in columns {df.columns.tolist()}")
                continue

            for _, row in df.iterrows():
                try:
                    date_str = _normalize_date(str(row[date_col]))
                    val = float(row[value_col])
                    if pd.isna(val):
                        continue

                    rows.append((
                        symbol, date_str, cfg["name"], val,
                        cfg["unit"], cfg["freq"], "akshare"
                    ))
                except (ValueError, TypeError, KeyError):
                    continue

            _log.debug(f"macro {symbol}: {len(df)} rows fetched")

        except Exception as e:
            _log.warning(f"macro {symbol} fetch failed: {e}")
            continue

        time.sleep(0.5)  # 限流

    return rows


def sync_range(start_date: str = None, end_date: str = None) -> int:
    """同步宏观高频数据 (滚动回补最近 365 天 + 周六全量)."""
    conn = sqlite3.connect(MARKET_DB, timeout=30)
    conn.execute("PRAGMA busy_timeout = 30000")
    _ensure_table(conn)

    if start_date is None:
        start_date = (datetime.now() - pd.Timedelta(days=365)).strftime("%Y-%m-%d")
    if end_date is None:
        end_date = datetime.now().strftime("%Y-%m-%d")

    try:
        rows = _fetch_all_macro()
        if not rows:
            _log.warning("macro_high_freq: no data fetched")
            return 0

        # 过滤日期范围
        filtered = [r for r in rows if start_date <= r[1] <= end_date]
        if not filtered:
            _log.info(f"macro_high_freq: no data in range {start_date}..{end_date}")
            return 0

        conn.executemany(
            """INSERT OR REPLACE INTO macro_high_freq
               (symbol, date, indicator_name, value, unit, frequency, source)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            filtered
        )
        conn.commit()
        _log.info(f"macro_high_freq synced: {len(filtered)} records ({start_date}..{end_date})")
        return len(filtered)

    except Exception as e:
        _log.error(f"macro_high_freq sync failed: {e}")
        raise
    finally:
        conn.close()


def get_macro(symbol: str, start_date: str, end_date: str) -> pd.DataFrame:
    """查询宏观指标."""
    conn = sqlite3.connect(MARKET_DB)
    try:
        df = pd.read_sql_query(
            """SELECT * FROM macro_high_freq
               WHERE symbol=? AND date BETWEEN ? AND ?
               ORDER BY date""",
            conn, params=(symbol, start_date, end_date)
        )
        return df
    finally:
        conn.close()