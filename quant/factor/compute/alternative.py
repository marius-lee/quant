"""另类数据因子计算 — 研报/ESG/宏观高频因子化.

直接读取 alternative_factors 表产出标准化因子值,
供 factor_cache 物化与评估管线使用.
"""

import sqlite3
import pandas as pd
import numpy as np
from typing import Dict, Optional

from quant.config.paths import MARKET_DB
from quant.utils.logger import get_logger

_log = get_logger("factor.compute.alternative")


def _load_table(table: str, symbols: list, date: str, value_col: str = "value", date_col: str = "date") -> pd.Series:
    """从 alternative 数据表加载单日数据."""
    conn = sqlite3.connect(MARKET_DB)
    try:
        placeholders = ",".join(["?"] * len(symbols))
        df = pd.read_sql_query(
            f"""SELECT symbol, {value_col} FROM {table}
                WHERE symbol IN ({placeholders}) AND {date_col} = ?""",
            conn, params=symbols + [date]
        )
        if df.empty:
            return pd.Series(dtype=float, index=pd.Index(symbols, name="symbol"))
        return df.set_index("symbol")[value_col]
    finally:
        conn.close()


def _load_table_by_col(table: str, symbols: list, date: str, value_col: str) -> pd.Series:
    """从 alternative 数据表加载单日指定列数据."""
    return _load_table(table, symbols, date, value_col)


def compute_alt_rpt_sentiment(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """研报情感因子: 单日研报情感均值."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    vals = _load_table("research_report", symbols, date, "sentiment_score", "pub_date")
    if vals.empty:
        return pd.Series(np.nan, index=symbols, name="alt_rpt_sentiment")
    # sentiment_score 已在 [0,1], 转为 [-1,1] 以便 z-score
    vals = (vals - 0.5) * 2
    return _cs_zscore(vals, sparse=True).rename("alt_rpt_sentiment")


def compute_alt_rpt_target_price(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """研报目标价因子: 目标价/当前价 - 1 (隐含上涨空间)."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    vals = _load_table("research_report", symbols, date, "target_price", "pub_date")
    if vals.empty:
        return pd.Series(np.nan, index=symbols, name="alt_rpt_target_price")
    # target_price 需要与当前价配合, 这里返回原始值供上层组合
    close = data["close"].xs(date, level=0) if date in data.index.get_level_values(0) else data["close"].iloc[-1]
    implied = vals / close - 1
    return _cs_zscore(implied.dropna(), sparse=True).rename("alt_rpt_target_price")


def compute_alt_rpt_rating(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """研报评级因子: 共识评级得分 (-1 到 1)."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    vals = _load_table("research_report", symbols, date, "rating", "pub_date")
    if vals.empty:
        return pd.Series(np.nan, index=symbols, name="alt_rpt_rating")
    # rating 需要转换: 买入=1, 增持=0.5, 中性=0, 减持=-0.5, 卖出=-1
    rating_map = {"买入": 1, "强烈推荐": 1, "推荐": 1, "增持": 0.5, "谨慎推荐": 0.5, "持有": 0.5,
                  "中性": 0, "市场表现": 0, "观望": 0, "减持": -0.5, "谨慎持有": -0.5,
                  "卖出": -1, "强烈卖出": -1}
    vals = vals.map(rating_map).fillna(0)
    return _cs_zscore(vals, sparse=True).rename("alt_rpt_rating")


def compute_alt_rpt_consensus(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """研报共识因子: 近 20 日评级均值 (机构共识强度)."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    conn = sqlite3.connect(MARKET_DB)
    try:
        placeholders = ",".join(["?"] * len(symbols))
        df = pd.read_sql_query(
            f"""SELECT symbol, date, rating FROM research_report
                WHERE symbol IN ({",".join(["?"] * len(symbols))})
                AND pub_date <= ? AND pub_date >= date(?, '-20 day')""",
            conn, params=symbols + [date, date]
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_rpt_consensus")

    rating_map = {"买入": 1, "强烈推荐": 1, "推荐": 1, "增持": 0.5, "谨慎推荐": 0.5, "持有": 0.5,
                  "中性": 0, "市场表现": 0, "观望": 0, "减持": -0.5, "谨慎持有": -0.5,
                  "卖出": -1, "强烈卖出": -1}
    df["rating_score"] = df["rating"].map(rating_map).fillna(0)
    daily = df.groupby(["symbol", "pub_date"])["rating_score"].mean().unstack()
    rolling = daily.rolling(window=20, min_periods=5).mean().iloc[-1]
    return _cs_zscore(rolling.dropna(), sparse=True).rename("alt_rpt_consensus")


def compute_alt_esg(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """ESG 综合评分因子."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    # ESG 数据按年更新, 取最新年份
    year = date[:4]
    conn = sqlite3.connect(MARKET_DB)
    try:
        placeholders = ",".join(["?"] * len(symbols))
        df = pd.read_sql_query(
            f"""SELECT symbol, esg_score FROM esg_score
                WHERE symbol IN ({placeholders}) AND data_year = ?""",
            conn, params=symbols + [year]
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_esg")

    vals = df.set_index("symbol")["esg_score"]
    return _cs_zscore(vals, sparse=True).rename("alt_esg")


def compute_alt_env(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """环境评分因子."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    year = date[:4]
    conn = sqlite3.connect(MARKET_DB)
    try:
        placeholders = ",".join(["?"] * len(symbols))
        df = pd.read_sql_query(
            f"""SELECT symbol, env_score FROM esg_score
                WHERE symbol IN ({placeholders}) AND data_year = ?""",
            conn, params=symbols + [year]
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_env")

    vals = df.set_index("symbol")["env_score"]
    return _cs_zscore(vals, sparse=True).rename("alt_env")


def compute_alt_social(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """社会评分因子."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    year = date[:4]
    conn = sqlite3.connect(MARKET_DB)
    try:
        placeholders = ",".join(["?"] * len(symbols))
        df = pd.read_sql_query(
            f"""SELECT symbol, social_score FROM esg_score
                WHERE symbol IN ({placeholders}) AND data_year = ?""",
            conn, params=symbols + [year]
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_social")

    vals = df.set_index("symbol")["social_score"]
    return _cs_zscore(vals, sparse=True).rename("alt_social")


def compute_alt_gov(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """治理评分因子."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    year = date[:4]
    conn = sqlite3.connect(MARKET_DB)
    try:
        placeholders = ",".join(["?"] * len(symbols))
        df = pd.read_sql_query(
            f"""SELECT symbol, gov_score FROM esg_score
                WHERE symbol IN ({placeholders}) AND data_year = ?""",
            conn, params=symbols + [year]
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_gov")

    vals = df.set_index("symbol")["gov_score"]
    return _cs_zscore(vals, sparse=True).rename("alt_gov")


def compute_alt_carbon(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """碳排放因子 (负向: 碳排放越高越差)."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    year = date[:4]
    conn = sqlite3.connect(MARKET_DB)
    try:
        placeholders = ",".join(["?"] * len(symbols))
        df = pd.read_sql_query(
            f"""SELECT symbol, carbon_emission FROM esg_score
                WHERE symbol IN ({placeholders}) AND data_year = ?""",
            conn, params=symbols + [year]
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_carbon")

    vals = df.set_index("symbol")["carbon_emission"]
    # 碳排放负向, 取负后 z-score
    return _cs_zscore(-vals, sparse=True).rename("alt_carbon")


def compute_alt_green_rev(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """绿色收入占比因子."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    year = date[:4]
    conn = sqlite3.connect(MARKET_DB)
    try:
        placeholders = ",".join(["?"] * len(symbols))
        df = pd.read_sql_query(
            f"""SELECT symbol, green_revenue_pct FROM esg_score
                WHERE symbol IN ({placeholders}) AND data_year = ?""",
            conn, params=symbols + [year]
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_green_rev")

    vals = df.set_index("symbol")["green_revenue_pct"]
    return _cs_zscore(vals, sparse=True).rename("alt_green_rev")


def compute_alt_macro_electricity(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """全社会用电量同比因子 (领先指标)."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    # 宏观指标全市场统一, 广播到所有股票
    conn = sqlite3.connect(MARKET_DB)
    try:
        df = pd.read_sql_query(
            """SELECT value FROM macro_high_freq
               WHERE symbol='electricity_consumption' AND date <= ?
               ORDER BY date DESC LIMIT 1""",
            conn, params=(date,)
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_macro_electricity")

    val = df.iloc[0]["value"]
    return pd.Series(val, index=symbols, name="alt_macro_electricity")


def compute_alt_macro_freight(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """铁路/公路/水路/港口货运量合计因子."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    conn = sqlite3.connect(MARKET_DB)
    try:
        df = pd.read_sql_query(
            """SELECT value FROM macro_high_freq
               WHERE symbol IN ('railway_freight','highway_freight','waterway_freight','port_throughput')
               AND date <= ? ORDER BY date DESC""",
            conn, params=(date,)
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_macro_freight")

    # 取最新值求和
    total = df["value"].sum()
    return pd.Series(total, index=symbols, name="alt_macro_freight")


def compute_alt_macro_credit(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """信贷/社融/广义货币 M2 合计因子."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    conn = sqlite3.connect(MARKET_DB)
    try:
        df = pd.read_sql_query(
            """SELECT value FROM macro_high_freq
               WHERE symbol IN ('credit','social_financing','m2')
               AND date <= ? ORDER BY date DESC""",
            conn, params=(date,)
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_macro_credit")

    total = df["value"].sum()
    return pd.Series(total, index=symbols, name="alt_macro_credit")


def compute_alt_macro_pmi(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """制造业/非制造业 PMI 综合因子."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    conn = sqlite3.connect(MARKET_DB)
    try:
        df = pd.read_sql_query(
            """SELECT value FROM macro_high_freq
               WHERE symbol IN ('pmi','pmi_yearly')
               AND date <= ? ORDER BY date DESC""",
            conn, params=(date,)
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_macro_pmi")

    # PMI > 50 为荣枯线, 取均值
    avg_pmi = df["value"].mean()
    return pd.Series(avg_pmi, index=symbols, name="alt_macro_pmi")


def compute_alt_macro_gdp(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """GDP 增速因子."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    conn = sqlite3.connect(MARKET_DB)
    try:
        df = pd.read_sql_query(
            """SELECT value FROM macro_high_freq
               WHERE symbol IN ('gdp','gdp_yearly')
               AND date <= ? ORDER BY date DESC""",
            conn, params=(date,)
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_macro_gdp")

    avg_gdp = df["value"].mean()
    return pd.Series(avg_gdp, index=symbols, name="alt_macro_gdp")


def compute_alt_macro_cpi(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """CPI 因子."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    conn = sqlite3.connect(MARKET_DB)
    try:
        df = pd.read_sql_query(
            """SELECT value FROM macro_high_freq
               WHERE symbol = 'cpi' AND date <= ? ORDER BY date DESC LIMIT 1""",
            conn, params=(date,)
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_macro_cpi")

    val = df.iloc[0]["value"]
    return pd.Series(val, index=symbols, name="alt_macro_cpi")


def compute_alt_macro_ppi(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """PPI 因子."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    conn = sqlite3.connect(MARKET_DB)
    try:
        df = pd.read_sql_query(
            """SELECT value FROM macro_high_freq
               WHERE symbol = 'ppi' AND date <= ? ORDER BY date DESC LIMIT 1""",
            conn, params=(date,)
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_macro_ppi")

    val = df.iloc[0]["value"]
    return pd.Series(val, index=symbols, name="alt_macro_ppi")


def compute_alt_macro_m2(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """M2/货币供应量因子."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    conn = sqlite3.connect(MARKET_DB)
    try:
        df = pd.read_sql_query(
            """SELECT value FROM macro_high_freq
               WHERE symbol IN ('m2','money_supply') AND date <= ? ORDER BY date DESC""",
            conn, params=(date,)
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_macro_m2")

    avg_m2 = df["value"].mean()
    return pd.Series(avg_m2, index=symbols, name="alt_macro_m2")


def compute_alt_macro_shibor(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """Shibor 利率因子."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    conn = sqlite3.connect(MARKET_DB)
    try:
        df = pd.read_sql_query(
            """SELECT value FROM macro_high_freq
               WHERE symbol = 'shibor' AND date <= ? ORDER BY date DESC LIMIT 1""",
            conn, params=(date,)
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_macro_shibor")

    val = df.iloc[0]["value"]
    return pd.Series(val, index=symbols, name="alt_macro_shibor")


def compute_alt_macro_lpr(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """LPR 因子."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    conn = sqlite3.connect(MARKET_DB)
    try:
        df = pd.read_sql_query(
            """SELECT value FROM macro_high_freq
               WHERE symbol = 'lpr' AND date <= ? ORDER BY date DESC LIMIT 1""",
            conn, params=(date,)
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_macro_lpr")

    val = df.iloc[0]["value"]
    return pd.Series(val, index=symbols, name="alt_macro_lpr")


def compute_alt_macro_money_supply(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """货币供应量因子."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    conn = sqlite3.connect(MARKET_DB)
    try:
        df = pd.read_sql_query(
            """SELECT value FROM macro_high_freq
               WHERE symbol = 'money_supply' AND date <= ? ORDER BY date DESC LIMIT 1""",
            conn, params=(date,)
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_macro_money_supply")

    val = df.iloc[0]["value"]
    return pd.Series(val, index=symbols, name="alt_macro_money_supply")


def compute_alt_macro_bank_financing(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """银行融资/信贷投放因子."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    conn = sqlite3.connect(MARKET_DB)
    try:
        df = pd.read_sql_query(
            """SELECT value FROM macro_high_freq
               WHERE symbol = 'bank_financing' AND date <= ? ORDER BY date DESC LIMIT 1""",
            conn, params=(date,)
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_macro_bank_financing")

    val = df.iloc[0]["value"]
    return pd.Series(val, index=symbols, name="alt_macro_bank_financing")


def compute_alt_macro_industrial(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """工业增加值增速因子."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    conn = sqlite3.connect(MARKET_DB)
    try:
        df = pd.read_sql_query(
            """SELECT value FROM macro_high_freq
               WHERE symbol = 'industrial_production' AND date <= ? ORDER BY date DESC LIMIT 1""",
            conn, params=(date,)
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_macro_industrial")

    val = df.iloc[0]["value"]
    return pd.Series(val, index=symbols, name="alt_macro_industrial")


def compute_alt_macro_exports(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """出口增速因子."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    conn = sqlite3.connect(MARKET_DB)
    try:
        df = pd.read_sql_query(
            """SELECT value FROM macro_high_freq
               WHERE symbol = 'exports' AND date <= ? ORDER BY date DESC LIMIT 1""",
            conn, params=(date,)
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_macro_exports")

    val = df.iloc[0]["value"]
    return pd.Series(val, index=symbols, name="alt_macro_exports")


def compute_alt_macro_imports(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """进口增速因子."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    conn = sqlite3.connect(MARKET_DB)
    try:
        df = pd.read_sql_query(
            """SELECT value FROM macro_high_freq
               WHERE symbol = 'imports' AND date <= ? ORDER BY date DESC LIMIT 1""",
            conn, params=(date,)
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_macro_imports")

    val = df.iloc[0]["value"]
    return pd.Series(val, index=symbols, name="alt_macro_imports")


def compute_alt_macro_retail(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """社消零售增速因子."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    conn = sqlite3.connect(MARKET_DB)
    try:
        df = pd.read_sql_query(
            """SELECT value FROM macro_high_freq
               WHERE symbol = 'retail' AND date <= ? ORDER BY date DESC LIMIT 1""",
            conn, params=(date,)
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_macro_retail")

    val = df.iloc[0]["value"]
    return pd.Series(val, index=symbols, name="alt_macro_retail")


def compute_alt_macro_real_estate(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """房地产投资/销售因子."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    conn = sqlite3.connect(MARKET_DB)
    try:
        df = pd.read_sql_query(
            """SELECT value FROM macro_high_freq
               WHERE symbol = 'real_estate' AND date <= ? ORDER BY date DESC LIMIT 1""",
            conn, params=(date,)
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_macro_real_estate")

    val = df.iloc[0]["value"]
    return pd.Series(val, index=symbols, name="alt_macro_real_estate")


def compute_alt_macro_traffic(data: dict, date: str, window: int = 0, aux: dict = None) -> pd.Series:
    """客货运量因子."""
    from quant.factor.compute import _cs_zscore
    symbols = data["close"].columns.tolist()
    conn = sqlite3.connect(MARKET_DB)
    try:
        df = pd.read_sql_query(
            """SELECT value FROM macro_high_freq
               WHERE symbol = 'traffic_volume' AND date <= ? ORDER BY date DESC LIMIT 1""",
            conn, params=(date,)
        )
    finally:
        conn.close()

    if df.empty:
        return pd.Series(np.nan, index=symbols, name="alt_macro_traffic")

    val = df.iloc[0]["value"]
    return pd.Series(val, index=symbols, name="alt_macro_traffic")


# 另类因子注册表
ALTERNATIVE_FACTORS = {
    # 研报因子
    "alt_rpt_sentiment":      (compute_alt_rpt_sentiment,      0),
    "alt_rpt_target_price":   (compute_alt_rpt_target_price,   0),
    "alt_rpt_rating":         (compute_alt_rpt_rating,         0),
    "alt_rpt_consensus":      (compute_alt_rpt_consensus,      20),
    # ESG 因子
    "alt_esg":                (compute_alt_esg,                0),
    "alt_env":                (compute_alt_env,                0),
    "alt_social":             (compute_alt_social,             0),
    "alt_gov":                (compute_alt_gov,                0),
    "alt_carbon":             (compute_alt_carbon,             0),
    "alt_green_rev":          (compute_alt_green_rev,          0),
    # 宏观高频因子
    "alt_macro_electricity":  (compute_alt_macro_electricity,  0),
    "alt_macro_freight":      (compute_alt_macro_freight,      0),
    "alt_macro_credit":       (compute_alt_macro_credit,       0),
    "alt_macro_pmi":          (compute_alt_macro_pmi,          0),
    "alt_macro_gdp":          (compute_alt_macro_gdp,          0),
    "alt_macro_cpi":          (compute_alt_macro_cpi,          0),
    "alt_macro_ppi":          (compute_alt_macro_ppi,          0),
    "alt_macro_m2":           (compute_alt_macro_m2,           0),
    "alt_macro_shibor":       (compute_alt_macro_shibor,       0),
    "alt_macro_lpr":          (compute_alt_macro_lpr,          0),
    "alt_macro_money_supply": (compute_alt_macro_money_supply, 0),
    "alt_macro_bank_financing": (compute_alt_macro_bank_financing, 0),
    "alt_macro_industrial":   (compute_alt_macro_industrial,   0),
    "alt_macro_exports":      (compute_alt_macro_exports,      0),
    "alt_macro_imports":      (compute_alt_macro_imports,      0),
    "alt_macro_retail":       (compute_alt_macro_retail,       0),
    "alt_macro_real_estate":  (compute_alt_macro_real_estate,  0),
    "alt_macro_traffic":      (compute_alt_macro_traffic,      0),
}