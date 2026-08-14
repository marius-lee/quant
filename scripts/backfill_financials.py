#!/usr/bin/env python3
"""财务数据回填 — easy_tdx.sina 拉取 2019-2024 年三表, upsert 到 market.db。

单股拉取 50 期 (2013→今), 只写入缺失季度 (2019-2024)。
逐股串行 (Sina 接口无批量), 约 5000 股 × 3 报表 = 15000 次请求。

运行:
  .venv/bin/python3 scripts/backfill_financials.py

恢复/续跑: 自动跳过已有 stat_date, 安全重跑。"""
import os
import sys
import sqlite3
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '.env'))

from quant.utils.logger import get_logger
from easy_tdx.sina.client import SinaClient

logger = get_logger("backfill.financials")

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "quant", "data", "market.db")

# ── 字段映射: Sina 中文列名 → DB 英文字段 ──
BALANCE_MAP = {
    "资产总计": "total_assets",
    "负债合计": "total_liability",
    "所有者权益(或股东权益)合计": "total_owner_equities",
    "归属于母公司股东权益合计": "equities_parent_company_owners",
    "少数股东权益": "minority_interests",
    "固定资产净额": "fixed_assets",
    "无形资产": "intangible_assets",
    "商誉": "good_will",
    "存货": "inventories",
    "应收账款": "account_receivable",
    "流动资产合计": "total_current_assets",
    "流动负债合计": "total_current_liability",
    "短期借款": "shortterm_loan",
    "长期借款": "longterm_loan",
}

INCOME_MAP = {
    "营业总收入": "total_operating_revenue",
    "营业收入": "operating_revenue",
    "营业成本": "operating_cost",
    "营业利润": "operating_profit",
    "净利润": "net_profit",
    "利润总额": "total_profit",
    "所得税费用": "income_tax_expense",
    "管理费用": "administration_expense",
}

CASHFLOW_MAP = {
    "经营活动产生的现金流量净额": "net_operate_cash_flow",
    "投资活动产生的现金流量净额": "net_invest_cash_flow",
    "筹资活动产生的现金流量净额": "net_finance_cash_flow",
    "期末现金及现金等价物余额": "cash_and_equivalents_at_end",
    "销售商品、提供劳务收到的现金": "goods_sale_and_service_render_cash",
    "购建固定资产、无形资产和其他长期资产所支付的现金": "fix_intan_other_asset_acqui_cash",
}

# 只回填这些年份的季度 (已有的跳过) — v490: 扩至 2024 (income/cashflow 2023+2024Q1/Q2/Q4 缺失,
# balance 2024Q1/Q2/Q4 缺失; JQ 权限窗口仅 2025q2~2026q1, 历史季度拉不到)
TARGET_QUARTERS = {
    f"{y}-{m:02d}-{d}"
    for y in range(2019, 2025)
    for m, d in [(3, 31), (6, 30), (9, 30), (12, 31)]
}


def _map_row(row: dict, mapping: dict[str, str]) -> dict:
    """将 Sina 中文行映射为英文字段, None 值跳过。"""
    result = {}
    for cn_name, en_name in mapping.items():
        v = row.get(cn_name)
        if v is not None and v == v:  # NaN check
            result[en_name] = float(v)
    return result


def _to_date(stat_date_str: str) -> str:
    """2026-03-31 00:00:00 → 2026-03-31"""
    return stat_date_str[:10]


def _upsert_one(conn, table: str, row: dict):
    """按 (symbol, stat_date) upsert 一行。row 含 symbol, stat_date, pub_date + 字段。"""
    fields = {k: v for k, v in row.items() if k not in ("symbol", "stat_date", "pub_date")}
    if not fields:
        return
    cols = list(fields.keys())
    placeholders = ", ".join("?" for _ in cols)
    set_clause = ", ".join(f"{c}=excluded.{c}" for c in cols)
    sql = (
        f"INSERT INTO {table} (symbol, stat_date, pub_date, {', '.join(cols)}) "
        f"VALUES (?, ?, ?, {placeholders}) "
        f"ON CONFLICT(symbol, stat_date) DO UPDATE SET {set_clause}"
    )
    vals = [row["symbol"], row["stat_date"], row.get("pub_date")]
    vals += [fields[c] for c in cols]
    conn.execute(sql, vals)


def _load_existing(conn) -> set:
    """返回所有表的已有 (symbol, stat_date) 集合 (跨三表合并)。"""
    existing = set()
    for tbl in ["financial_balance", "financial_income", "financial_cashflow"]:
        rows = conn.execute(f"SELECT symbol, stat_date FROM {tbl}").fetchall()
        existing.update((r[0], r[1]) for r in rows)
    return existing


def main():
    logger.info("financials backfill start — target 2019-2022")

    # 1. 获取所有股票代码
    stock_conn = sqlite3.connect(DB, timeout=30)
    symbols = [r[0] for r in stock_conn.execute(
        "SELECT DISTINCT symbol FROM stocks ORDER BY symbol"
    ).fetchall()]
    stock_conn.close()
    logger.info(f"stocks to process: {len(symbols)}")

    # 2. 加载已有数据 (用于跳过)
    conn = sqlite3.connect(DB, timeout=30)
    from quant.data.jq_financials import ensure_tables
    ensure_tables(conn)
    existing = _load_existing(conn)
    logger.info(f"existing financial records (all tables): {len(existing)}")

    # 3. 逐股拉取
    client = SinaClient()
    total_inserted = 0
    t0 = time.monotonic()

    for i, symbol in enumerate(symbols):
        # 检查是否全部完成
        all_done = all((symbol, q) in existing for q in TARGET_QUARTERS)
        if all_done:
            continue

        # 三表拉取
        stock_inserted = 0
        for report_type, mapping, table_name in [
            ("fzb", BALANCE_MAP, "financial_balance"),
            ("lrb", INCOME_MAP, "financial_income"),
            ("llb", CASHFLOW_MAP, "financial_cashflow"),
        ]:
            try:
                df = client.get_financial_report(symbol, report_type=report_type, num=50)
            except Exception as e:
                logger.warning(f"{symbol} {report_type}: fetch failed ({type(e).__name__}: {e})")
                continue

            if df is None or df.empty:
                continue

            for _, raw in df.iterrows():
                stat_date = _to_date(str(raw.get("报告期", "")))
                if stat_date not in TARGET_QUARTERS:
                    continue
                if (symbol, stat_date) in existing:
                    continue

                pub_date = _to_date(str(raw.get("报告期", stat_date)))
                mapped = _map_row(raw.to_dict(), mapping)
                if not mapped:
                    continue

                row = {
                    "symbol": symbol,
                    "stat_date": stat_date,
                    "pub_date": pub_date,
                    **mapped,
                }
                for retry in range(3):
                    try:
                        _upsert_one(conn, table_name, row)
                        stock_inserted += 1
                        existing.add((symbol, stat_date))
                        break
                    except Exception as e:
                        if retry < 2:
                            time.sleep(0.5 * (retry + 1))
                        else:
                            logger.warning(f"{symbol} {stat_date} {table_name}: upsert failed ({e})")

        if stock_inserted:
            conn.commit()
            total_inserted += stock_inserted

        # 进度日志 (每 20 股; 终端 + 日志同步输出)
        if (i + 1) % 20 == 0:
            elapsed = time.monotonic() - t0
            rate = (i + 1) / elapsed
            remaining = (len(symbols) - i - 1) / rate if rate > 0 else 0
            msg = (
                f"[{elapsed/60:.0f}m] financials: {i+1}/{len(symbols)} "
                f"({(i+1)/len(symbols)*100:.1f}%) rate={rate:.1f}stk/s "
                f"inserted={total_inserted} ETA={remaining/60:.0f}min cur={symbol}"
            )
            logger.info(msg)
            print(msg, flush=True)

    conn.close()
    elapsed = time.monotonic() - t0
    logger.info(f"financials backfill done: {total_inserted} rows in {elapsed/60:.1f}min")


if __name__ == "__main__":
    main()
