#!/usr/bin/env python3
"""Sync quarterly financials (balance/income/cash_flow) from JQData to market.db.

用法:
  .venv-tushare/bin/python3 data/jq_financials.py                    # 最近8个季度
  .venv-tushare/bin/python3 data/jq_financials.py --quarters 12
"""
import os, sys, time, sqlite3, logging
from datetime import datetime, timedelta
from calendar import monthrange

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("jq_financials")

DB = os.path.join(os.path.dirname(__file__), "market.db")

KEY_COLS = {
    "balance": [
        "total_assets", "total_liability", "total_owner_equities",
        "equities_parent_company_owners", "minority_interests",
        "fixed_assets", "intangible_assets", "good_will",
        "inventories", "account_receivable", "total_current_assets",
        "total_current_liability", "shortterm_loan", "longterm_loan",
    ],
    "income": [
        "total_operating_revenue", "operating_revenue", "operating_cost",
        "operating_profit", "net_profit", "total_profit",
        "income_tax_expense", "selling_expense", "administration_expense",
        "finance_expense", "rd_expense",
    ],
    "cash_flow": [
        "net_operate_cash_flow", "net_invest_cash_flow", "net_finance_cash_flow",
        "cash_and_equivalents_at_end", "goods_sale_and_service_render_cash",
        "fix_intan_other_asset_acqui_cash",
    ]
}

TABLE_OBJECTS = {}  # populated at runtime


def _ensure_tables(conn):
    for tbl in ["balance", "income", "cash_flow"]:
        conn.execute(f"""
            CREATE TABLE IF NOT EXISTS financial_{tbl} (
                symbol TEXT,
                stat_date TEXT,
                pub_date TEXT,
                PRIMARY KEY (symbol, stat_date)
            )
        """)
    conn.commit()


def _quarter_end_dates(n_quarters):
    """Generate last calendar day of each quarter, going back n quarters."""
    today = datetime.today()
    # 当前季度第一天
    q = ((today.month - 1) // 3) * 3 + 1
    year = today.year
    dates = []
    for _ in range(n_quarters):
        # 往前退一个季度
        q -= 3
        if q < 1:
            q += 12
            year -= 1
        # 季度最后一个月
        end_month = q + 2
        end_year = year
        if end_month > 12:
            end_month -= 12
            end_year += 1
        last_day = monthrange(end_year, end_month)[1]
        dates.append(datetime(end_year, end_month, min(last_day, 31)).strftime("%Y-%m-%d"))
    return sorted(dates)


def _sync_table(conn, tbl_name, quarter_dates):
    from jqdatasdk import auth, get_fundamentals, query, logout

    auth(os.environ.get("JQDATA_USER", ""), os.environ.get("JQDATA_PASS", ""))

    tbl_obj = TABLE_OBJECTS[tbl_name]
    total = 0
    for i, q_date in enumerate(quarter_dates):
        t0 = time.time()
        try:
            df = get_fundamentals(query(tbl_obj), date=q_date)
        except Exception as e:
            logger.warning(f"{tbl_name} {q_date}: {e}")
            continue

        if df is None or df.empty:
            logger.warning(f"{tbl_name} {q_date}: empty")
            continue

        key_cols = KEY_COLS[tbl_name]
        existing = [c for c in key_cols if c in df.columns]
        for col in existing:
            try:
                conn.execute(f"ALTER TABLE financial_{tbl_name} ADD COLUMN {col} REAL")
            except sqlite3.OperationalError:
                pass

        inserted = 0
        for _, row in df.iterrows():
            code = str(row.get("code", ""))
            if not code or "." not in code:
                continue
            symbol = code.split(".")[0]
            if len(symbol) != 6:
                continue
            stat_date = str(row.get("statDate", ""))
            pub_date = str(row.get("pubDate", ""))
            if not stat_date:
                continue

            vals = {c: float(row[c]) for c in existing if row.get(c) is not None and row[c] == row[c]}
            if not vals:
                continue

            cols_sql = ", ".join(vals.keys())
            pl = ", ".join("?" * len(vals))
            params = list(vals.values())
            conn.execute(
                f"INSERT OR REPLACE INTO financial_{tbl_name} (symbol, stat_date, pub_date, {cols_sql}) "
                f"VALUES (?, ?, ?, {pl})",
                (symbol, stat_date, pub_date, *params),
            )
            inserted += 1

        conn.commit()
        elapsed = time.time() - t0
        total += inserted
        print(f"  [{i+1}/{len(quarter_dates)}] {tbl_name} {q_date}: {inserted} stocks, {elapsed:.1f}s")

    logout()
    return total


def sync_financials(n_quarters=8):
    conn = sqlite3.connect(DB)
    _ensure_tables(conn)

    # Import JQData table objects
    from jqdatasdk import balance as bal, income as inc, cash_flow as cf
    TABLE_OBJECTS["balance"] = bal
    TABLE_OBJECTS["income"] = inc
    TABLE_OBJECTS["cash_flow"] = cf

    quarter_dates = _quarter_end_dates(n_quarters)
    logger.info(f"Syncing {n_quarters} quarters: {quarter_dates[0]} ~ {quarter_dates[-1]}")

    grand_total = 0
    for tbl_name in ["balance", "income", "cash_flow"]:
        synced = set(r[0] for r in conn.execute(
            f"SELECT DISTINCT stat_date FROM financial_{tbl_name}"
        ).fetchall())
        todo = [d for d in quarter_dates if d not in synced]
        if not todo:
            logger.info(f"{tbl_name}: all {len(quarter_dates)} quarters synced")
            continue
        logger.info(f"{tbl_name}: {len(todo)} quarters to sync")
        n = _sync_table(conn, tbl_name, todo)
        grand_total += n

    conn.close()
    logger.info(f"Done: {grand_total} rows total")


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--quarters", type=int, default=8)
    args = p.parse_args()
    sync_financials(n_quarters=args.quarters)
