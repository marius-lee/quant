"""Preload auxiliary data for all factor computations in one batch.

Rather than each factor opening its own SQLite connection (20+ per day),
this module loads all tables once and passes a dict to factor functions.

Factor functions accept an optional `aux` parameter. If present, they use
the preloaded data; if None, they fall back to their own connection (for
backward compatibility with standalone factor computation).
"""

import pandas as pd
import sqlite3
from quant.data.repos._base import DatabaseManager
import os

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
_DB = os.path.join(_ROOT, "data", "market.db")

_AUX_TABLES = [
    "margin_detail",
    "analyst_forecast",
    "fund_hold",
    "financial_income",
    "financial_balance",
    "financial_cashflow",
    "lhb_detail",
    "fund_flow",
    "pledge",
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
        conn = DatabaseManager.get_instance().get_connection(_DB)

    result = {}
    ph = ",".join("?" * len(symbols))


    # stocks: symbol → market, name for board limit detection (ST, STAR, ChiNext)
    try:
        df = pd.read_sql_query(
            "SELECT symbol, market, name FROM stocks WHERE symbol IN (" + ph + ")",
            conn, params=symbols
        )
        result["stocks"] = df.set_index("symbol") if not df.empty else pd.DataFrame(columns=["symbol", "market", "name"]).set_index("symbol")
    except (pd.io.sql.DatabaseError, sqlite3.OperationalError):
        result["stocks"] = pd.DataFrame(columns=["symbol", "market", "name"]).set_index("symbol")

    # margin_detail: 60-day window for all margin-based factors
    try:
        margin_max_date = pd.read_sql_query(
            "SELECT MAX(date) FROM margin_detail WHERE date <= ?", conn, params=(date,)
        ).iloc[0, 0]
        if margin_max_date:
            margin_start = (pd.Timestamp(margin_max_date) - pd.Timedelta(days=65)).strftime("%Y-%m-%d")
            df = pd.read_sql_query(
                "SELECT symbol, date, margin_buy, margin_balance, short_balance, short_total "
                "FROM margin_detail WHERE date >= ? AND date <= ?",
                conn, params=(margin_start, margin_max_date)
            )
            result["margin"] = df if not df.empty else pd.DataFrame(columns=["symbol", "date", "margin_buy", "margin_balance", "short_balance", "short_total"])
        else:
            result["margin"] = pd.DataFrame(columns=["symbol", "date", "margin_buy", "margin_balance", "short_balance", "short_total"])
    except (pd.io.sql.DatabaseError, sqlite3.OperationalError):
        result["margin"] = pd.DataFrame(columns=["symbol", "date", "margin_buy", "margin_balance", "short_balance", "short_total"])

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

    # pledge: latest date
    try:
        df = pd.read_sql_query(
            f"SELECT symbol, pledge_ratio FROM pledge "
            f"WHERE date = (SELECT MAX(date) FROM pledge WHERE date <= ?)",
            conn, params=(date,)
        )
        result["pledge"] = df.set_index("symbol") if not df.empty else pd.DataFrame(columns=["symbol", "pledge_ratio"]).set_index("symbol")
    except (pd.io.sql.DatabaseError, sqlite3.OperationalError):
        result["pledge"] = pd.DataFrame(columns=["symbol", "pledge_ratio"]).set_index("symbol")

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

    # fund_flow: 60-day window with main_net_ratio for compute_main_flow_ratio
    try:
        ff_max = pd.read_sql_query(
            "SELECT MAX(date) FROM fund_flow WHERE date <= ?", conn, params=(date,)
        ).iloc[0, 0]
        if ff_max:
            ff_start = (pd.Timestamp(ff_max) - pd.Timedelta(days=65)).strftime("%Y-%m-%d")
            df = pd.read_sql_query(
                "SELECT symbol, date, main_net_inflow, super_large_net_inflow, main_net_ratio FROM fund_flow "
                "WHERE date >= ? AND date <= ?",
                conn, params=(ff_start, ff_max)
            )
            result["fund_flow"] = df.set_index("symbol") if not df.empty else pd.DataFrame(columns=["symbol", "date", "main_net_inflow", "super_large_net_inflow", "main_net_ratio"]).set_index("symbol")
        else:
            result["fund_flow"] = pd.DataFrame(columns=["symbol", "date", "main_net_inflow", "super_large_net_inflow", "main_net_ratio"]).set_index("symbol")
    except (pd.io.sql.DatabaseError, sqlite3.OperationalError):
        result["fund_flow"] = pd.DataFrame(columns=["symbol", "date", "main_net_inflow", "super_large_net_inflow", "main_net_ratio"]).set_index("symbol")

    return result
