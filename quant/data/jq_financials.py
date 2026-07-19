"""JQData 财务数据 — 资产负债表/利润表/现金流量表 (模板7: 每表有主).

数据源: JQData (joinquant.com), 每日100万条配额.
更新时间: 季度 (财报发布后3-5个工作日).
"""
import sqlite3
import logging
import os as _os
from quant.utils.date import validate_date_format

_log = logging.getLogger("data.jq_financials")

DB = _os.path.join(_os.path.dirname(__file__), "market.db")


def ensure_tables(conn: sqlite3.Connection):
    """幂等建表 + 索引 (模板7: 每表有主, 索引在模块内维护)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS financial_balance (
            symbol TEXT NOT NULL,
            stat_date TEXT NOT NULL,
            pub_date TEXT,
            total_assets REAL,
            total_liability REAL,
            total_owner_equities REAL,
            equities_parent_company_owners REAL,
            minority_interests REAL,
            fixed_assets REAL,
            intangible_assets REAL,
            good_will REAL,
            inventories REAL,
            account_receivable REAL,
            total_current_assets REAL,
            total_current_liability REAL,
            shortterm_loan REAL,
            longterm_loan REAL,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (symbol, stat_date)
        );
        CREATE TABLE IF NOT EXISTS financial_income (
            symbol TEXT NOT NULL,
            stat_date TEXT NOT NULL,
            pub_date TEXT,
            total_operating_revenue REAL,
            operating_revenue REAL,
            operating_cost REAL,
            operating_profit REAL,
            net_profit REAL,
            total_profit REAL,
            income_tax_expense REAL,
            administration_expense REAL,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (symbol, stat_date)
        );
        CREATE TABLE IF NOT EXISTS financial_cash_flow (
            symbol TEXT NOT NULL,
            stat_date TEXT NOT NULL,
            pub_date TEXT,
            net_operate_cash_flow REAL,
            net_invest_cash_flow REAL,
            net_finance_cash_flow REAL,
            cash_and_equivalents_at_end REAL,
            goods_sale_and_service_render_cash REAL,
            fix_intan_other_asset_acqui_cash REAL,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (symbol, stat_date)
        );
        CREATE INDEX IF NOT EXISTS idx_fin_balance_date ON financial_balance(stat_date);
        CREATE INDEX IF NOT EXISTS idx_fin_income_date ON financial_income(stat_date);
        CREATE INDEX IF NOT EXISTS idx_fin_cashflow_date ON financial_cash_flow(stat_date);
    """)
    conn.commit()


def upsert_balance(conn: sqlite3.Connection, rows: list[dict]):
    """批量 upsert 资产负债表."""
    _log.info(f"upserting {len(rows)} balance rows")
    for r in rows:
        cols = [k for k in r if k != "symbol" and k != "stat_date"]
        placeholders = ",".join(["?" for _ in cols])
        set_clause = ",".join([f"{c}=excluded.{c}" for c in cols])
        sql = (
            f"INSERT INTO financial_balance (symbol,stat_date,{','.join(cols)}) "
            f"VALUES (?,?,{placeholders}) "
            f"ON CONFLICT(symbol,stat_date) DO UPDATE SET {set_clause}"
        )
        values = [r["symbol"], r["stat_date"]] + [r.get(c) for c in cols]
        conn.execute(sql, values)
    conn.commit()


def upsert_income(conn: sqlite3.Connection, rows: list[dict]):
    """批量 upsert 利润表."""
    _log.info(f"upserting {len(rows)} income rows")
    for r in rows:
        cols = [k for k in r if k not in ("symbol", "stat_date")]
        placeholders = ",".join(["?" for _ in cols])
        set_clause = ",".join([f"{c}=excluded.{c}" for c in cols])
        sql = (
            f"INSERT INTO financial_income (symbol,stat_date,{','.join(cols)}) "
            f"VALUES (?,?,{placeholders}) "
            f"ON CONFLICT(symbol,stat_date) DO UPDATE SET {set_clause}"
        )
        values = [r["symbol"], r["stat_date"]] + [r.get(c) for c in cols]
        conn.execute(sql, values)
    conn.commit()


def upsert_cash_flow(conn: sqlite3.Connection, rows: list[dict]):
    """批量 upsert 现金流量表."""
    _log.info(f"upserting {len(rows)} cash_flow rows")
    for r in rows:
        cols = [k for k in r if k not in ("symbol", "stat_date")]
        placeholders = ",".join(["?" for _ in cols])
        set_clause = ",".join([f"{c}=excluded.{c}" for c in cols])
        sql = (
            f"INSERT INTO financial_cash_flow (symbol,stat_date,{','.join(cols)}) "
            f"VALUES (?,?,{placeholders}) "
            f"ON CONFLICT(symbol,stat_date) DO UPDATE SET {set_clause}"
        )
        values = [r["symbol"], r["stat_date"]] + [r.get(c) for c in cols]
        conn.execute(sql, values)
    conn.commit()
