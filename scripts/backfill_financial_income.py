#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
backfill_financial_income.py — 补数: financial_income 缺失列 (operating_cost / administration_expense)

用途:   v544 根因修复配套补数。sina_financials sync 的"行已存在即跳过"语义导致
        2020-2024 期这两列 (历史导入时缺失) 永不补齐。本脚本对仍为 NULL 的行
        用 sina lrb 接口重拉该股全量利润表, 仅更新缺失列 (COALESCE 不覆盖已有值)。
        银行/保险/券商无营业成本科目 (sina 返回 None) → 保持 NULL, 属合理缺项。
版本:   v1.4 (2026-08-19) — 失败可诊断化 + 自动重试: v1.3 诊断确认 448 只失败
        全部为 sina 网络层超时 (SSL handshake/read timeout), 瞬时性 (重试即恢复),
        与并发负载相关 (7 并发即全超时, 注释"8 并发触发限流")。v1.4 三项:
        ① 并发 3→2 缓解限流; ② 每请求失败自动重试 2 次 (指数退避 1.5s/2.25s);
        ③ 失败明细逐条落日志 (原仅存内存, 结束才打印前 10)。
        v1.3 锁死根因修复: 原共享 1 连接 + 每 200 只才 commit, sqlite3 deferred
        事务首条 UPDATE 起持写锁 ~19 分钟/窗口, 锁杀物化轮子进程与 web 调度任务
        (database is locked)。重构: fetch 网络并行, UPDATE 串行执行 (主线程单写者),
        每 50 只 commit, 锁窗口秒级。
        v1.2 扩展: income 补 total_profit/income_tax_expense + cashflow 4 列 (同源 llb);
        v1.1 起 3 并发 (8 并发触发限流)
用法:   PYTHONPATH=. .venv/bin/python3 scripts/backfill_financial_income.py
幂等:   可重复执行 — 只处理仍为 NULL 的行, 已补值不被覆盖。
耗时:   5560 股 × 2 请求 × 2 并发 ≈ 6-10 小时 (每 50 只打点)。
"""
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from easy_tdx.sina.client import SinaClient

from quant.config.paths import MARKET_DB
from quant.utils.logger import get_logger

_log = get_logger("scripts.backfill_financial_income")

_WORKERS = 2  # v1.4: 3→2 缓解 sina 限流 (7 并发实测全超时)
_COMMIT_EVERY = 50  # v1.3: 短事务 — 每 50 只 commit, 锁窗口秒级 (原 200 只 ≈ 19 分钟)
_MAX_RETRIES = 2  # v1.4: 每请求失败自动重试 2 次 (网络瞬时超时重试即恢复)

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


def _fetch_updates(client, symbol: str) -> tuple[list, str | None]:
    """单股票: 拉 lrb + llb → 组装 UPDATE 参数列表. 只 fetch 不写库 (v1.3).
    返回 (updates, 失败信息或 None); updates 元素 = (table, set_clause, params).
    v1.4: 每报告类型失败自动重试 _MAX_RETRIES 次 (指数退避), 仍失败返回该类型错误。"""
    updates: list = []
    for report_type, table, cn_map in (("lrb", "financial_income", _INC_CN),
                                       ("llb", "financial_cashflow", _CF_CN)):
        for attempt in range(_MAX_RETRIES + 1):
            try:
                df = client.get_financial_report(symbol, report_type=report_type, num=50)
                break
            except Exception as e:
                if attempt < _MAX_RETRIES:
                    _log.warning(f"{symbol} {report_type} 第{attempt + 1}次失败, "
                                 f"退避后重试: {type(e).__name__} {e}")
                    time.sleep(1.5 ** (attempt + 1))
                    continue
                return updates, f"{report_type} 重试{_MAX_RETRIES}次仍失败 {type(e).__name__}: {e}"
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
            updates.append((table, set_clause,
                            list(mapped.values()) + [symbol, stat_date]))
    return updates, None


def main() -> None:
    t0 = time.monotonic()
    conn = sqlite3.connect(str(MARKET_DB), timeout=60)
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
    pending: list = []
    # v1.3: fetch 网络并行 (线程池), UPDATE 串行执行 (主线程单写者) — 锁窗口 = 每
    # _COMMIT_EVERY 只的写入耗时 (秒级), 不再阻塞 web/物化的写事务
    with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futs = {pool.submit(_fetch_updates, client, s): s for s in symbols}
        for fut in as_completed(futs):
            updates, err = fut.result()
            done += 1
            if err:
                fail_symbols.append((futs[fut], err))
                _log.warning(f"补数失败 {futs[fut]}: {err}")  # v1.4: 逐条落日志 (原仅内存)
            pending.extend(updates)
            if done % _COMMIT_EVERY == 0:
                for table, set_clause, params in pending:
                    cur = conn.execute(
                        f"UPDATE {table} SET {set_clause} WHERE symbol=? AND stat_date=?",
                        params)
                    updated_rows += cur.rowcount
                pending = []
                conn.commit()
                if touched_symbols == 0 and updated_rows:
                    touched_symbols = done
                _log.info(f"进度 {done}/{len(symbols)} | 已补 {updated_rows} 行 "
                          f"({done} 只) | 失败 {len(fail_symbols)} "
                          f"| 耗时 {time.monotonic()-t0:.1f}s")
    for table, set_clause, params in pending:
        cur = conn.execute(
            f"UPDATE {table} SET {set_clause} WHERE symbol=? AND stat_date=?",
            params)
        updated_rows += cur.rowcount
    conn.commit()
    conn.close()
    _log.info(f"补数完成: {updated_rows} 行 ({done}/{len(symbols)} 只) "
              f"| 失败 {len(fail_symbols)}: {fail_symbols[:10]} "
              f"| 总计 {time.monotonic()-t0:.1f}s")


if __name__ == "__main__":
    main()