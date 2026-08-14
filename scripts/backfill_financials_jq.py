#!/usr/bin/env python3
"""JQData 财务数据回填 — 利润表/资产负债表/现金流量表.

回填缺失季度报告 (按报告期 statDate 拉取, 用 statDate='YYYYqN' 参数,
非 date 参数), 跳过已有数据.

注意: JQData 账号权限仅覆盖 statDate 2025-05-06 ~ 2026-05-13 的报告期
(实测 2024Q4 仅 13 行、2019-2023 返回 0 行), 历史季度拉不到是预期行为,
会以 WARNING 记录而非报错.

依赖: JQData 账号 (环境变量 JQDATA_USER / JQDATA_PASS).
运行:
  PYTHONPATH=. .venv/bin/python3 scripts/backfill_financials_jq.py
"""
import os, sys, sqlite3, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

from quant.utils.logger import get_logger
from quant.data.jq_financials import ensure_tables

logger = get_logger("backfill.financials_jq")
DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "quant", "data", "market.db")

# 报告期范围 (YYYYqN). 默认仅覆盖权限窗口内的季度 (2025q2~2026q1),
# 权限外季度 (2019-2023 等) 每次调用返回空, 徒耗配额, 不进默认范围.
# 若权限扩展, 自行展开: [f"{y}q{q}" for y in range(2019, 2027) for q in range(1, 5)]
QUARTERS = ["2025q2", "2025q3", "2025q4", "2026q1"]
# 某季度已存在 >= 该行数视为完整, 跳过 (全市场约 5200-5400 只)
MIN_ROWS_PER_QUARTER = 4500
TABLES = [
    ("balance",   "financial_balance"),
    ("income",    "financial_income"),
    ("cash_flow", "financial_cashflow"),
]


def main():
    # 1. JQData 认证
    user = os.environ.get("JQDATA_USER", "")
    passwd = os.environ.get("JQDATA_PASS", "")
    if not user or not passwd:
        logger.error("JQDATA_USER/JQDATA_PASS not set in .env, abort")
        sys.exit(1)

    from jqdatasdk import auth, query, balance, income, cash_flow, get_fundamentals
    import pandas as pd
    auth(user, passwd)
    logger.info("JQData auth OK")

    # 2. 加载已有数据
    conn = sqlite3.connect(DB, timeout=30)
    ensure_tables(conn)
    tbl_cols = {}
    for _, tbl in TABLES:
        tbl_cols[tbl] = {r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")}
    existing_by_tbl = {}
    for _, tbl in TABLES:
        rows = conn.execute(f"SELECT symbol, stat_date FROM {tbl}").fetchall()
        existing_by_tbl[tbl] = {(r[0], str(r[1])[:10]) for r in rows}
    total_existing = sum(len(s) for s in existing_by_tbl.values())
    logger.info(f"existing records: {total_existing}")

    # 3. 逐表逐季度回填
    cls_map = {"balance": balance, "income": income, "cash_flow": cash_flow}
    # JQData 列名与库表列名一致 (蛇形), 仅 pubDate/statDate/code 需要转列名
    col_map = {"pubDate": "pub_date", "statDate": "stat_date", "code": "symbol"}
    total = 0
    t0 = time.monotonic()

    for api_name, tbl in TABLES:
        cols = tbl_cols[tbl] - {"symbol", "stat_date", "created_at"}
        existing = existing_by_tbl[tbl]
        cls = cls_map[api_name]
        for quarter in QUARTERS:
            # 检查该季度是否已完整
            # 报告期日期: q1=03-31, q2=06-30, q3=09-30, q4=12-31
            q_idx = int(quarter[-1])
            sd_prefix = f"{quarter[:4]}-{['03-31','06-30','09-30','12-31'][q_idx - 1]}"
            q_existing = sum(1 for s, d in existing if d.startswith(sd_prefix))
            if q_existing >= MIN_ROWS_PER_QUARTER:
                logger.info(f"{tbl} {quarter}: {q_existing} rows exist, skip")
                continue

            try:
                df = get_fundamentals(query(cls), statDate=quarter)
            except Exception as e:
                logger.warning(f"{tbl} {quarter}: JQData query failed ({e})")
                continue

            if df is None or df.empty:
                logger.warning(f"{tbl} {quarter}: JQData returned empty"
                               " (likely outside permission window 2025-05-06~2026-05-13)")
                continue

            new_rows = []
            col_list = sorted(cols)
            for _, r in df.iterrows():
                rec = {"symbol": str(r["code"]).split(".")[0].zfill(6),
                       "stat_date": str(r["statDate"])[:10]}
                if (rec["symbol"], rec["stat_date"]) in existing:
                    continue
                for col in col_list:
                    jq_key = "pubDate" if col == "pub_date" else col
                    v = r.get(jq_key)
                    if v is not None and pd.isna(v):
                        v = None
                    rec[col] = v
                new_rows.append(rec)

            if not new_rows:
                logger.info(f"{tbl} {quarter}: all {len(df)} rows already exist")
                continue

            # upsert (每表每季度一批)
            placeholders = ",".join(["?" for _ in sorted(cols)])
            set_clause = ",".join([f"{c}=excluded.{c}" for c in sorted(cols)])
            sql = (
                f"INSERT INTO {tbl} (symbol,stat_date,{','.join(sorted(cols))}) "
                f"VALUES (?,?,{placeholders}) "
                f"ON CONFLICT(symbol,stat_date) DO UPDATE SET {set_clause}"
            )
            conn.executemany(sql, [
                (r["symbol"], r["stat_date"]) + tuple(r.get(c) for c in sorted(cols))
                for r in new_rows
            ])
            conn.commit()
            existing.update((r["symbol"], r["stat_date"]) for r in new_rows)
            total += len(new_rows)
            elapsed = time.monotonic() - t0
            logger.info(f"{tbl} {quarter}: {len(new_rows)} new rows "
                        f"(total={total}, {elapsed:.0f}s)")

    conn.close()
    elapsed = time.monotonic() - t0
    logger.info(f"financials backfill done: {total} rows in {elapsed/60:.1f}min")


if __name__ == "__main__":
    main()