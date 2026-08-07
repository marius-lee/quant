#!/usr/bin/env python3
"""JQData 财务数据回填 — 利润表/资产负债表/现金流量表.

回填 2019-2026 所有季度报告, 跳过已有数据.
依赖: JQData 账号 (环境变量 JQDATA_USER / JQDATA_PASS).
运行:
  PYTHONPATH=. .venv/bin/python3 scripts/backfill_financials_jq.py
"""
import os, sys, sqlite3, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

from quant.utils.logger import get_logger
from quant.data.jq_financials import ensure_tables, upsert_balance, upsert_income, upsert_cash_flow

logger = get_logger("backfill.financials_jq")
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "quant", "data", "market.db")

# 回填范围
YEARS = list(range(2019, 2027))
TABLES = [
    ("balance",     upsert_balance,     "financial_balance"),
    ("income",      upsert_income,      "financial_income"),
    ("cash_flow",   upsert_cash_flow,   "financial_cash_flow"),
]


def main():
    # 1. JQData 认证
    user = os.environ.get("JQDATA_USER", "")
    passwd = os.environ.get("JQDATA_PASS", "")
    if not user or not passwd:
        logger.error("JQDATA_USER/JQDATA_PASS not set in .env, abort")
        sys.exit(1)

    from jqdatasdk import auth, get_fundamentals, query, balance, income, cash_flow
    auth(user, passwd)
    logger.info("JQData auth OK")

    # 2. 加载已有数据
    conn = sqlite3.connect(DB, timeout=30)
    ensure_tables(conn)
    existing = set()
    for _, _, tbl in TABLES:
        rows = conn.execute(f"SELECT symbol, stat_date FROM {tbl}").fetchall()
        existing.update((r[0], str(r[1])[:10]) for r in rows)
    logger.info(f"existing records: {len(existing)}")

    # 3. 逐表逐年回填
    cls_map = {"balance": balance, "income": income, "cash_flow": cash_flow}
    # JQData 三表日期字段统一为 statDate (camelCase)
    date_field_map = {"balance": balance.statDate, "income": income.statDate, "cash_flow": cash_flow.statDate}
    total = 0
    t0 = time.monotonic()

    for year in YEARS:
        for api_name, upsert_fn, tbl in TABLES:
            # 检查该年是否已有数据 (stat_date 格式: YYYY-MM-DD)
            year_prefix = f"{year}-"
            year_existing = sum(1 for s, d in existing if str(d)[:7].startswith(year_prefix))
            if year_existing > 100:  # 粗略: >100行就跳过
                logger.info(f"{tbl} {year}: {year_existing} rows exist, skip")
                continue

            cls = cls_map[api_name]
            date_field = date_field_map[api_name]
            q = query(cls).filter(
                date_field >= f"{year}-01-01",
                date_field <= f"{year}-12-31"
            )
            try:
                df = get_fundamentals(q)
            except Exception as e:
                logger.warning(f"{tbl} {year}: JQData failed ({e})")
                continue

            if df is None or df.empty:
                logger.info(f"{tbl} {year}: JQData returned empty")
                continue

            # 标准化: JQData 日期字段名因表而异
            for r in rows:
                code = str(r.get("code", ""))
                r["symbol"] = code.split(".")[0].zfill(6) if "." in code else code.zfill(6)
                sd = str(r.get("stat_date", "") or r.get("statDate", "") or r.get("day", ""))
                r["stat_date"] = sd[:10]

            # 去重: 过滤已有
            new_rows = [r for r in rows
                        if (r["symbol"], r["stat_date"]) not in existing]
            if not new_rows:
                logger.info(f"{tbl} {year}: all {len(rows)} rows already exist")
                continue

            upsert_fn(conn, new_rows)
            conn.commit()
            for r in new_rows:
                existing.add((r["symbol"], r["stat_date"]))
            total += len(new_rows)
            elapsed = time.monotonic() - t0
            logger.info(f"{tbl} {year}: {len(new_rows)} new rows "
                        f"(total={total}, {elapsed:.0f}s)")

    conn.close()
    elapsed = time.monotonic() - t0
    logger.info(f"financials backfill done: {total} rows in {elapsed/60:.1f}min")


if __name__ == "__main__":
    main()
