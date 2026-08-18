#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_financial_income.py — 补数: financial_income 缺失列 (operating_cost / administration_expense)

用途:   v544 根因修复配套补数。sina_financials sync 的"行已存在即跳过"语义导致
        2020-2024 期这两列 (历史导入时缺失) 永不补齐。本脚本对仍为 NULL 的行
        用 sina lrb 接口重拉该股全量利润表, 仅更新缺失列 (COALESCE 不覆盖已有值)。
        银行/保险/券商无营业成本科目 (sina 返回 None) → 保持 NULL, 属合理缺项。
版本:   v1.2 (2026-08-19) — 扩展: income 补 total_profit/income_tax_expense + cashflow 4 列 (同源 llb); v1.1 起 3 并发 (8 并发触发限流)
用法:   PYTHONPATH=. .venv/bin/python3 scripts/backfill_financial_income.py
幂等:   可重复执行 — 只处理仍为 NULL 的行, 已补值不被覆盖。
耗时:   5558 股 × 8 并发 ≈ 60-90 分钟 (每 200 股打点)。
"""
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from easy_tdx.sina.client import SinaClient

from quant.config.paths import MARKET_DB
from quant.utils.logger import get_logger

_log = get_logger("scripts.backfill_financial_income")

_WORKERS = 3

# v1.2: 全表字段级体检 (scripts/field_health.py) 扩展 — financial_income 还缺
# total_profit (利润总额) / income_tax_expense (所得税费用) (74-75% NaN);
# financial_cashflow 4 列 79% NaN (net_invest/net_finance/cash_and_equivalents/
# goods_sale) — 同源 sina llb, 一并补齐
_INC_CN = {
    "operating_cost": "营业成本",
    "administration_expense": "管理费用",
    "total_profit": "利润总额",
    "income_tax_expense": "所得税费用",
}
_CF_CN = {
    "net_invest_cash_flow": "投资活动产生的现金流量净额",
    "net_finance_cash_flow": "筹资活动产生的现金流量净额",
    "cash_and_equivalents_at_end": "期末现金及现金等价物余额",
    "goods_sale_and_service_render_cash": "销售商品、提供劳务收到的现金",
}


def _fetch_and_update(client, conn, symbol: str) -> tuple[int, str | None]:
    """单股票: 拉 lrb + llb → 更新缺失列. 返回 (更新行数, 失败信息或 None)."""
    per_sym = 0
    for report_type, table, cn_map in (("lrb", "financial_income", _INC_CN),
                                       ("llb", "financial_cashflow", _CF_CN)):
        try:
            df = client.get_financial_report(symbol, report_type=report_type, num=50)
        except Exception as e:
            return per_sym, f"{report_type} {type(e).__name__}: {e}"
        if df is None or df.empty:
            continue
        for _, raw in df.iterrows():
            stat_date = str(raw.get("报告期", ""))[:10]
            mapped = {}
            for en, cn in cn_map.items():
                v = raw.get(cn)
                if v is not None and v == v:
                    try:
                        mapped[en] = float(v)
                    except (TypeError, ValueError):
                        pass
            if not mapped:
                continue
            set_clause = ", ".join(f"{c} = COALESCE({c}, ?)" for c in mapped)
            cur = conn.execute(
                f"UPDATE {table} SET {set_clause} WHERE symbol=? AND stat_date=?",
                list(mapped.values()) + [symbol, stat_date])
            per_sym += cur.rowcount
    return per_sym, None


def main() -> None:
    t0 = time.monotonic()
    conn = sqlite3.connect(str(MARKET_DB), timeout=60, check_same_thread=False)
    symbols = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM financial_income WHERE "
        "(operating_cost IS NULL OR administration_expense IS NULL "
        "OR total_profit IS NULL OR income_tax_expense IS NULL) "
        "UNION "
        "SELECT DISTINCT symbol FROM financial_cashflow WHERE "
        "(net_invest_cash_flow IS NULL OR net_finance_cash_flow IS NULL "
        "OR cash_and_equivalents_at_end IS NULL OR goods_sale_and_service_render_cash IS NULL) "
        "ORDER BY symbol")]
    _log.info(f"待补数股票: {len(symbols)} 只 ({_WORKERS} 并发)")

    client = SinaClient()
    updated_rows = 0
    touched_symbols = 0
    fail_symbols: list[tuple[str, str]] = []
    done = 0
    lock = threading.Lock()
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futs = {pool.submit(_fetch_and_update, client, conn, s): s for s in symbols}
        for fut in as_completed(futs):
            per_sym, err = fut.result()
            with lock:
                done += 1
                if per_sym:
                    updated_rows += per_sym
                    touched_symbols += 1
                if err:
                    fail_symbols.append((futs[fut], err))
                if done % 200 == 0:
                    conn.commit()
                    _log.info(f"进度 {done}/{len(symbols)} | 已补 {updated_rows} 行 "
                              f"({touched_symbols} 只) | 失败 {len(fail_symbols)} "
                              f"| 耗时 {time.monotonic()-t0:.1f}s")
    conn.commit()
    conn.close()
    _log.info(f"补数完成: {updated_rows} 行 ({touched_symbols}/{len(symbols)} 只有更新) "
              f"| 失败 {len(fail_symbols)}: {fail_symbols[:10]} "
              f"| 总计 {time.monotonic()-t0:.1f}s")


if __name__ == "__main__":
    main()