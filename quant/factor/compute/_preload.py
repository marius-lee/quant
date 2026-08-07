"""Preload auxiliary data for all factor computations in one batch.

Rather than each factor opening its own SQLite connection (20+ per day),
this module loads all tables once and passes a dict to factor functions.

Factor functions accept an optional `aux` parameter. If present, they use
the preloaded data; if None, they fall back to their own connection (for
backward compatibility with standalone factor computation).

Chunk-level API (ADR-043):
  preload_aux_data_chunk(symbols, date_from, date_to) → load once per chunk
  slice_aux_for_date(aux_full, date) → per-date slice, output = preload_aux_data
  This eliminates 12 SQL queries/date → 12 queries/chunk (200x reduction).
"""

import pandas as pd
import sqlite3
import time as _time
from quant.data.repos._base import DatabaseManager
from quant.utils.logger import get_logger
import os

_log = get_logger("factor.preload")

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
from quant.config.paths import MARKET_DB as _DB

_AUX_TABLES = [
    "margin_detail",
    "analyst_forecast",
    "fund_hold",
    "financial_income",
    "financial_balance",
    "financial_cashflow",
    "lhb_detail",
    # v376: fund_flow/pledge removed — no factor reads them from aux
]


def preload_aux_data(symbols: list, date: str, conn=None) -> dict:
    """Preload all auxiliary tables for a given date and symbol set.

    Returns a dict like:
        {
            "margin": DataFrame(symbol, margin_buy, margin_balance, ...),
            "analyst": DataFrame(symbol, buy_count, report_count, ...),
            "fund_hold": DataFrame(...),
            "financial_income": DataFrame(...),
            ...
        }

    Factor functions check `aux.get("margin")` instead of doing their own query.
    """
    if conn is None:
        conn = DatabaseManager.market()
        _own_conn = True
    else:
        _own_conn = False

    result = {}
    ph = ",".join("?" * len(symbols))


    # stocks: symbol → market, name for board limit detection (ST, STAR, ChiNext)
    # ADR-043 layer1: +total_mv+industry for abn_turnover/str market-cap neutralization
    try:
        df = pd.read_sql_query(
            "SELECT symbol, market, name, total_mv, industry FROM stocks WHERE symbol IN (" + ph + ")",
            conn, params=symbols
        )
        result["stocks"] = df.set_index("symbol") if not df.empty else pd.DataFrame(
            columns=["symbol", "market", "name", "total_mv", "industry"]).set_index("symbol")
    except (pd.io.sql.DatabaseError, sqlite3.OperationalError):
        result["stocks"] = pd.DataFrame(
            columns=["symbol", "market", "name", "total_mv", "industry"]).set_index("symbol")

    # margin_detail: 60-day window for all margin-based factors
    # ADR-043 layer1: +margin_total for compute_short_interest
    try:
        margin_max_date = pd.read_sql_query(
            "SELECT MAX(date) FROM margin_detail WHERE date <= ?", conn, params=(date,)
        ).iloc[0, 0]
        if margin_max_date:
            margin_start = (pd.Timestamp(margin_max_date) - pd.Timedelta(days=65)).strftime("%Y-%m-%d")
            df = pd.read_sql_query(
                "SELECT symbol, date, margin_buy, margin_balance, short_balance, short_total, "
                "CAST(margin_balance + short_balance AS REAL) AS margin_total "
                "FROM margin_detail WHERE date >= ? AND date <= ?",
                conn, params=(margin_start, margin_max_date)
            )
            result["margin"] = df if not df.empty else pd.DataFrame(
                columns=["symbol", "date", "margin_buy", "margin_balance",
                         "short_balance", "short_total", "margin_total"])
        else:
            result["margin"] = pd.DataFrame(
                columns=["symbol", "date", "margin_buy", "margin_balance",
                         "short_balance", "short_total", "margin_total"])
    except (pd.io.sql.DatabaseError, sqlite3.OperationalError):
        result["margin"] = pd.DataFrame(
            columns=["symbol", "date", "margin_buy", "margin_balance",
                     "short_balance", "short_total", "margin_total"])

    # analyst_forecast: latest sync_date per symbol (all rating columns)
    try:
        df = pd.read_sql_query(
            "SELECT symbol, buy_count, overweight_count, neutral_count, underweight_count, report_count "
            "FROM analyst_forecast "
            "WHERE sync_date = (SELECT MAX(sync_date) FROM analyst_forecast WHERE sync_date <= ?)",
            conn, params=(date,)
        )
        # PIT: always set key — empty df means no prior data exists, factors return NaN gracefully
        result["analyst"] = df.set_index("symbol") if not df.empty else pd.DataFrame(columns=["symbol", "buy_count", "overweight_count", "neutral_count", "underweight_count", "report_count"]).set_index("symbol")
    except (pd.io.sql.DatabaseError, sqlite3.OperationalError):
        result["analyst"] = pd.DataFrame(columns=["symbol", "buy_count", "overweight_count", "neutral_count", "underweight_count", "report_count"]).set_index("symbol")

    # fund_hold: latest date (ratio + change_ratio for fund_change factor)
    try:
        df = pd.read_sql_query(
            "SELECT symbol, fund_count, change_ratio FROM fund_hold "
            "WHERE report_date = (SELECT MAX(report_date) FROM fund_hold WHERE report_date <= ?)",
            conn, params=(date,)
        )
        result["fund_hold"] = df.set_index("symbol") if not df.empty else pd.DataFrame(columns=["symbol", "fund_count", "change_ratio"]).set_index("symbol")
    except (pd.io.sql.DatabaseError, sqlite3.OperationalError):
        result["fund_hold"] = pd.DataFrame(columns=["symbol", "fund_count", "change_ratio"]).set_index("symbol")

    # financial tables: TTM data
    for tbl in ["financial_income", "financial_balance", "financial_cashflow"]:
        try:
            df = pd.read_sql_query(
                f"SELECT * FROM {tbl} WHERE stat_date <= ? ORDER BY stat_date",
                conn, params=(date,)
            )
            result[tbl] = df if not df.empty else pd.DataFrame(columns=df.columns)
        except (pd.io.sql.DatabaseError, sqlite3.OperationalError):
            result[tbl] = pd.DataFrame(columns=["symbol", "stat_date"])

    # lhb_detail: 90-day window with all columns for lhb factors
    try:
        df = pd.read_sql_query(
            "SELECT symbol, trade_date, net_buy, buy_amt, sell_amt, change_pct, close, circ_mv, post_5d "
            "FROM lhb_detail "
            "WHERE trade_date <= ? AND trade_date >= date(?, '-90 days') ORDER BY trade_date DESC",
            conn, params=(date, date)
        )
        result["lhb"] = df if not df.empty else pd.DataFrame(columns=["symbol", "trade_date", "net_buy", "buy_amt", "sell_amt", "change_pct", "close", "circ_mv", "post_5d"])
    except (pd.io.sql.DatabaseError, sqlite3.OperationalError):
        result["lhb"] = pd.DataFrame(columns=["symbol", "trade_date", "net_buy", "buy_amt", "sell_amt", "change_pct", "close", "circ_mv", "post_5d"])

    if _own_conn:
        conn.close()
    return result


# ═══════════════════════════════════════════════════════════
# Chunk-level aux preload (ADR-043) — 12 queries/chunk vs 12/date
# ═══════════════════════════════════════════════════════════

def preload_aux_data_chunk(symbols: list, date_from: str, date_to: str,
                           conn=None) -> dict:
    """一次加载整个 chunk 日期范围的 aux 数据，不做单日过滤。

    调用方用 slice_aux_for_date() 按日期切片。
    输出格式：与 preload_aux_data 相同结构，但日期维度未裁剪。

    Args:
        symbols: 股票列表
        date_from: chunk 起始日期 (YYYY-MM-DD)
        date_to: chunk 结束日期 (YYYY-MM-DD)
        conn: 可选 SQLite 连接

    Returns:
        dict: 与 preload_aux_data 同构, 含完整 chunk 日期范围数据
    """
    t0 = _time.monotonic()
    _log.info("preload_aux_data_chunk: %d symbols, %s → %s",
              len(symbols), date_from, date_to)

    if conn is None:
        conn = DatabaseManager.market()
        _own_conn = True
    else:
        _own_conn = False

    result = {}
    ph = ",".join("?" * len(symbols))

    # stocks: 单日快照，全量返回 (ADR-043 layer1: +total_mv+industry)
    try:
        df = pd.read_sql_query(
            "SELECT symbol, market, name, total_mv, industry FROM stocks WHERE symbol IN (" + ph + ")",
            conn, params=symbols
        )
        result["stocks"] = df.set_index("symbol") if not df.empty else pd.DataFrame(
            columns=["symbol", "market", "name", "total_mv", "industry"]).set_index("symbol")
    except (pd.io.sql.DatabaseError, sqlite3.OperationalError):
        result["stocks"] = pd.DataFrame(
            columns=["symbol", "market", "name", "total_mv", "industry"]).set_index("symbol")

    # margin_detail: chunk 范围 + 65d 前置窗口 (ADR-043 layer1: +margin_total)
    margin_start = (pd.Timestamp(date_from) - pd.Timedelta(days=65)).strftime("%Y-%m-%d")
    try:
        df = pd.read_sql_query(
            "SELECT symbol, date, margin_buy, margin_balance, short_balance, short_total, "
            "CAST(margin_balance + short_balance AS REAL) AS margin_total "
            "FROM margin_detail WHERE date >= ? AND date <= ? ORDER BY date",
            conn, params=(margin_start, date_to)
        )
        result["margin"] = df if not df.empty else pd.DataFrame(
            columns=["symbol", "date", "margin_buy", "margin_balance",
                     "short_balance", "short_total", "margin_total"])
    except (pd.io.sql.DatabaseError, sqlite3.OperationalError):
        result["margin"] = pd.DataFrame(
            columns=["symbol", "date", "margin_buy", "margin_balance",
                     "short_balance", "short_total", "margin_total"])

    # analyst_forecast: 用 chunk_end 取最新快照
    try:
        df = pd.read_sql_query(
            "SELECT symbol, buy_count, overweight_count, neutral_count, "
            "underweight_count, report_count FROM analyst_forecast "
            "WHERE sync_date = (SELECT MAX(sync_date) FROM analyst_forecast "
            "WHERE sync_date <= ?)",
            conn, params=(date_to,)
        )
        result["analyst"] = df.set_index("symbol") if not df.empty else pd.DataFrame(
            columns=["symbol", "buy_count", "overweight_count", "neutral_count",
                     "underweight_count", "report_count"]).set_index("symbol")
    except (pd.io.sql.DatabaseError, sqlite3.OperationalError):
        result["analyst"] = pd.DataFrame(
            columns=["symbol", "buy_count", "overweight_count", "neutral_count",
                     "underweight_count", "report_count"]).set_index("symbol")

    # fund_hold: 60d 历史窗口 (ADR-043 layer1: 替代单日快照, 支持 fund_flow_3m 均值)
    fh_start = (pd.Timestamp(date_from) - pd.Timedelta(days=65)).strftime("%Y-%m-%d")
    try:
        df = pd.read_sql_query(
            "SELECT symbol, report_date, fund_count, change_ratio FROM fund_hold "
            "WHERE report_date >= ? AND report_date <= ? ORDER BY report_date",
            conn, params=(fh_start, date_to)
        )
        result["fund_hold"] = df if not df.empty else pd.DataFrame(
            columns=["symbol", "report_date", "fund_count", "change_ratio"])
    except (pd.io.sql.DatabaseError, sqlite3.OperationalError):
        result["fund_hold"] = pd.DataFrame(
            columns=["symbol", "report_date", "fund_count", "change_ratio"])

    # financial tables: chunk 范围 + symbol 过滤 (ADR-043: 补 symbol 过滤, 消除全表扫描)
    for tbl in ["financial_income", "financial_balance", "financial_cashflow"]:
        try:
            df = pd.read_sql_query(
                f"SELECT * FROM {tbl} WHERE symbol IN ({ph}) "
                f"AND stat_date <= ? ORDER BY stat_date",
                conn, params=symbols + [date_to]
            )
            result[tbl] = df if not df.empty else pd.DataFrame(
                columns=["symbol", "stat_date"])
        except (pd.io.sql.DatabaseError, sqlite3.OperationalError):
            result[tbl] = pd.DataFrame(columns=["symbol", "stat_date"])

    # lhb_detail: chunk 范围 + 90d 前置窗口
    lhb_start = (pd.Timestamp(date_from) - pd.Timedelta(days=90)).strftime("%Y-%m-%d")
    try:
        df = pd.read_sql_query(
            "SELECT symbol, trade_date, net_buy, buy_amt, sell_amt, change_pct, "
            "close, circ_mv, post_5d FROM lhb_detail "
            "WHERE trade_date >= ? AND trade_date <= ? ORDER BY trade_date DESC",
            conn, params=(lhb_start, date_to)
        )
        result["lhb"] = df if not df.empty else pd.DataFrame(
            columns=["symbol", "trade_date", "net_buy", "buy_amt", "sell_amt",
                     "change_pct", "close", "circ_mv", "post_5d"])
    except (pd.io.sql.DatabaseError, sqlite3.OperationalError):
        result["lhb"] = pd.DataFrame(
            columns=["symbol", "trade_date", "net_buy", "buy_amt", "sell_amt",
                     "change_pct", "close", "circ_mv", "post_5d"])

    # intraday_snapshot: chunk 日期范围 (ADR-043 layer1: 替代 3 个 intraday 因子 per-date 查询)
    # v418 (R10): 附带 total days 计数 — 供 _snapshot_matured 门控 (快照积累<60日跳过因子)
    try:
        df = pd.read_sql_query(
            "SELECT symbol, date, open_30min, prev_close, open_30min_vol, close_5min "
            "FROM intraday_snapshot WHERE date >= ? AND date <= ? ORDER BY date",
            conn, params=(date_from, date_to)
        )
        result["intraday_snapshot"] = df if not df.empty else pd.DataFrame(
            columns=["symbol", "date", "open_30min", "prev_close",
                     "open_30min_vol", "close_5min"])
        result["intraday_snapshot_days"] = int(
            result["intraday_snapshot"]["date"].nunique())
    except (pd.io.sql.DatabaseError, sqlite3.OperationalError):
        result["intraday_snapshot"] = pd.DataFrame(
            columns=["symbol", "date", "open_30min", "prev_close",
                     "open_30min_vol", "close_5min"])
        result["intraday_snapshot_days"] = 0

    # news_daily_count: chunk 日期范围 (v366: 消除 3 个新闻因子 per-date SQL)
    try:
        df = pd.read_sql_query(
            "SELECT symbol, date, avg_sentiment, news_count "
            "FROM news_daily_count WHERE date >= ? AND date <= ? ORDER BY date",
            conn, params=(date_from, date_to)
        )
        result["news"] = df if not df.empty else pd.DataFrame(
            columns=["symbol", "date", "avg_sentiment", "news_count"])
    except (pd.io.sql.DatabaseError, sqlite3.OperationalError):
        result["news"] = pd.DataFrame(
            columns=["symbol", "date", "avg_sentiment", "news_count"])

    if _own_conn:
        conn.close()

    elapsed = _time.monotonic() - t0
    _log.info("preload_aux_data_chunk: %d tables loaded in %.1fs",
              len(result), elapsed)
    return result


def slice_aux_for_date(aux_full: dict, date: str) -> dict:
    """从 chunk 级 aux_full 中按日期切片。

    输出格式与 preload_aux_data 完全一致，下游因子函数零改动。
    单日快照表 (stocks/analyst/fund_hold/pledge) 直接复用引用。
    时间窗口表 (margin/lhb/fund_flow/financial) 按 date 过滤。

    Args:
        aux_full: preload_aux_data_chunk 的返回值
        date: 目标日期 (YYYY-MM-DD)

    Returns:
        dict: 与 preload_aux_data 同构，该日期的 aux 数据
    """
    result = {}
    ts = pd.Timestamp(date)

    # 单日快照表：直接复用引用（无日期维度）
    for key in ["stocks", "analyst"]:
        if key in aux_full:
            result[key] = aux_full[key]

    # margin: 取 ≤date 的最新日期，往前 65d 窗口
    margin = aux_full.get("margin", pd.DataFrame())
    if not margin.empty and "date" in margin.columns:
        m_dates = pd.to_datetime(margin["date"])
        valid = m_dates <= ts
        if valid.any():
            margin_max = m_dates[valid].max()
            margin_start = margin_max - pd.Timedelta(days=65)
            in_window = (m_dates >= margin_start) & (m_dates <= margin_max)
            result["margin"] = margin.loc[in_window]
        else:
            result["margin"] = pd.DataFrame(columns=margin.columns)
    else:
        result["margin"] = margin

    # lhb: 90d 窗口
    lhb = aux_full.get("lhb", pd.DataFrame())
    if not lhb.empty and "trade_date" in lhb.columns:
        lhb_dates = pd.to_datetime(lhb["trade_date"])
        lhb_start = ts - pd.Timedelta(days=90)
        in_window = (lhb_dates >= lhb_start) & (lhb_dates <= ts)
        result["lhb"] = lhb.loc[in_window]
    else:
        result["lhb"] = lhb

    # financial tables: stat_date ≤ date
    for tbl in ["financial_income", "financial_balance", "financial_cashflow"]:
        tbl_df = aux_full.get(tbl, pd.DataFrame())
        if not tbl_df.empty and "stat_date" in tbl_df.columns:
            result[tbl] = tbl_df.loc[pd.to_datetime(tbl_df["stat_date"]) <= ts]
        else:
            result[tbl] = tbl_df

    # fund_hold: report_date 60d 窗口 (ADR-043 layer1: 改为历史窗口)
    fh = aux_full.get("fund_hold", pd.DataFrame())
    if not fh.empty and "report_date" in fh.columns:
        fh_dates = pd.to_datetime(fh["report_date"])
        fh_start = ts - pd.Timedelta(days=65)
        in_window = (fh_dates >= fh_start) & (fh_dates <= ts)
        result["fund_hold"] = fh.loc[in_window]
    else:
        result["fund_hold"] = fh

    # intraday_snapshot: 精确日期匹配 (ADR-043 layer1)
    # v418 (R10): 透传 intraday_snapshot_days 总天数计数 (门控用)
    snap = aux_full.get("intraday_snapshot", pd.DataFrame())
    if "intraday_snapshot_days" in aux_full:
        result["intraday_snapshot_days"] = aux_full["intraday_snapshot_days"]
    if not snap.empty and "date" in snap.columns:
        result["intraday_snapshot"] = snap.loc[pd.to_datetime(snap["date"]) == ts]
    else:
        result["intraday_snapshot"] = snap

    # news: 不切片 — 新闻因子需要多日窗口 (1d/5d/20d), 因子内部自行过滤
    result["news"] = aux_full.get("news", pd.DataFrame())

    return result
