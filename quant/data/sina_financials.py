"""sina 财务三表同步 — 调度链 weekly_full 注册用 (v491).

背景: 财务三表 (financial_income/balance/cashflow) 此前完全不在调度链
(table_registry 无注册 → 晚间链/周度维护不拉) — JQ 权限窗口仅 2025q2~2026q1,
权限外季度无人自动拉取 → 历史缺口 (income/cashflow 2019-2023 全缺) 永远补不上,
只能靠手动脚本. 本模块把 easy_tdx.sina 全历史拉取封装为 sync() (无参全量,
幂等跳过已有), 供 table_registry 以 weekly_full 模式注册, 周六维护自动执行.

数据源: 新浪财经 CompanyFinanceService (免费, 单股一次拉 50 期全历史).
"""
import time as _time
import sqlite3 as _sqlite3
from pathlib import Path as _Path

from quant.utils.logger import get_logger

_log = get_logger("data.sina_financials")

_DB = _Path(__file__).resolve().parents[1] / "data" / "market.db"

# 三表: (报表类型, 字段映射, 表名)
_TABLES = [
    ("fzb", "financial_balance"),
    ("lrb", "financial_income"),
    ("llb", "financial_cashflow"),
]

_BALANCE_MAP = {
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

_INCOME_MAP = {
    "营业总收入": "total_operating_revenue",
    "营业收入": "operating_revenue",
    "营业成本": "operating_cost",
    "营业利润": "operating_profit",
    "净利润": "net_profit",
    "利润总额": "total_profit",
    "所得税费用": "income_tax_expense",
    "管理费用": "administration_expense",
}

_CASHFLOW_MAP = {
    "经营活动产生的现金流量净额": "net_operate_cash_flow",
    "投资活动产生的现金流量净额": "net_invest_cash_flow",
    "筹资活动产生的现金流量净额": "net_finance_cash_flow",
    "期末现金及现金等价物余额": "cash_and_equivalents_at_end",
    "销售商品、提供劳务收到的现金": "goods_sale_and_service_render_cash",
    "购建固定资产、无形资产和其他长期资产所支付的现金": "fix_intan_other_asset_acqui_cash",
}

_MAP_BY_TABLE = {
    "financial_balance": _BALANCE_MAP,
    "financial_income": _INCOME_MAP,
    "financial_cashflow": _CASHFLOW_MAP,
}


def _upsert_one(conn, table: str, row: dict) -> None:
    """按 (symbol, stat_date) upsert 一行. row 含 symbol, stat_date, pub_date + 字段."""
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


def _latest_report_end(today=None) -> str:
    """最近一个已结束的报告期 (季末) — 如 2026-08-14 → 2026-06-30."""
    from datetime import date as _d, timedelta as _td
    d = _d.fromisoformat(today) if today else _d.today()
    y, m = d.year, d.month
    if m >= 10:
        return f"{y}-09-30"
    if m >= 7:
        return f"{y}-06-30"
    if m >= 4:
        return f"{y}-03-31"
    return f"{y-1}-12-31"


def sync() -> int:
    """全量幂等同步财务三表 — 单股 50 期, 跳过已有 (symbol, stat_date).

    v491 快速路径: 该股某表 MAX(stat_date) 已达最近报告期 → 跳过该表 HTTP
    (weekly_full 周六跑 + 早间链 7 天兜底都走本入口; 无快速路径时每周
    15000 次请求 2-4h, 早间链 30min 窗口跑不完).

    Returns: 新插入行数.
    """
    from easy_tdx.sina.client import SinaClient

    conn = _sqlite3.connect(str(_DB), timeout=30)
    from quant.data.jq_financials import ensure_tables
    ensure_tables(conn)

    existing = set()
    for _, tbl in _TABLES:
        if tbl == "financial_income":
            # v545: 行存在但 operating_cost/administration_expense 为 NULL 不算已同步
            # (2020-2024 历史导入行缺这两列, sina 接口现已返回 → 必须重拉补齐)
            rows = conn.execute(
                "SELECT symbol, stat_date FROM financial_income "
                "WHERE operating_cost IS NOT NULL AND administration_expense IS NOT NULL").fetchall()
        else:
            rows = conn.execute(f"SELECT symbol, stat_date FROM {tbl}").fetchall()
        existing.update((r[0], str(r[1])[:10]) for r in rows)

    symbols = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM stocks ORDER BY symbol").fetchall()]

    # 快速路径: 每股票每表已有 MAX(stat_date) (未达最近报告期 → 需要拉取)
    # v492 增强: 上市早但历史深度不足 (MIN(stat_date) 太新, JQ 窗口空洞未
    # 补) → 也要拉. 单靠 MAX 判定会漏: JQ 已写 2025q2-2026q1 的股票当
    # target 推进后 MAX 达标 → 跳过 → 2019-2023 缺口永不补 (sina num=50
    # 覆盖 13 年, 拉一次即可补齐). 新股 (list_date 晚) 天然历史短, 不判.
    target_end = _latest_report_end()
    hist_ref = f"{int(target_end[:4]) - 8}-{target_end[5:]}"      # 上市早于 8 年前视为老股
    hist_cover = f"{int(target_end[:4]) - 3}-06-30"               # 老股历史须覆盖 3 年前
    list_dates = {r[0]: r[1] for r in conn.execute(
        "SELECT symbol, list_date FROM stocks").fetchall()}
    need_fetch: dict[str, set[str]] = {}
    for symbol in symbols:
        ld = list_dates.get(symbol)
        if ld is None or ld > hist_ref:
            # 新股或未知上市日期: 仅按 MAX(stat_date) 判定 (快速路径原逻辑)
            for _, tbl in _TABLES:
                mx = conn.execute(
                    f"SELECT MAX(stat_date) FROM {tbl} WHERE symbol=?",
                    (symbol,)).fetchone()[0]
                if mx is None or str(mx)[:10] < target_end:
                    need_fetch.setdefault(symbol, set()).add(tbl)
            continue
        # 老股: MAX 未达最新报告期 或 历史起点过新 (JQ 空洞未补) → 拉取
        for _, tbl in _TABLES:
            mn, mx = conn.execute(
                f"SELECT MIN(stat_date), MAX(stat_date) FROM {tbl} WHERE symbol=?",
                (symbol,)).fetchone()
            needs_cost = False
            if tbl == "financial_income":
                # v545: 存在 operating_cost/administration_expense 为 NULL 的行 → 重拉补齐
                needs_cost = conn.execute(
                    "SELECT 1 FROM financial_income WHERE symbol=? AND "
                    "(operating_cost IS NULL OR administration_expense IS NULL) LIMIT 1",
                    (symbol,)).fetchone() is not None
            if (mx is None or str(mx)[:10] < target_end or mn is None or mn > hist_cover
                    or needs_cost):
                need_fetch.setdefault(symbol, set()).add(tbl)

    client = SinaClient()
    total = 0
    t0 = _time.monotonic()
    n_fetch = sum(len(v) for v in need_fetch.values())
    _log.info(f"sina_financials sync: {len(symbols)} symbols, "
              f"{n_fetch} symbol-table 需拉取 (目标报告期 {target_end}), "
              f"已有 {len(symbols)*len(_TABLES)-n_fetch} 达标跳过")
    for i, symbol in enumerate(symbols):
        fetch_tbls = need_fetch.get(symbol)
        if not fetch_tbls:
            continue
        for report_type, table_name in _TABLES:
            if table_name not in fetch_tbls:
                continue
            mapping = _MAP_BY_TABLE[table_name]
            try:
                df = client.get_financial_report(symbol, report_type=report_type, num=50)
            except Exception as e:
                _log.warning(f"{symbol} {report_type}: fetch failed ({type(e).__name__}: {e})")
                continue
            if df is None or df.empty:
                continue
            for _, raw in df.iterrows():
                stat_date = str(raw.get("报告期", ""))[:10]
                if (symbol, stat_date) in existing:
                    continue
                mapped = {}
                for cn, en in mapping.items():
                    v = raw.get(cn)
                    if v is not None and v == v:
                        try:
                            mapped[en] = float(v)
                        except (TypeError, ValueError):
                            continue
                if not mapped:
                    continue
                try:
                    _upsert_one(conn, table_name, {
                        "symbol": symbol, "stat_date": stat_date,
                        "pub_date": stat_date, **mapped,
                    })
                    total += 1
                    existing.add((symbol, stat_date))
                except Exception as e:
                    _log.warning(f"{symbol} {stat_date} {table_name}: upsert failed ({e})")
        # v552: 每 symbol 完成后立即 commit — 原每 200 只才 commit, 使 HTTP 网络
        # 请求落在 sqlite3 deferred 写事务窗口内 (首条 INSERT 起持写锁), 回填
        # 历史缺口时连续持写锁 10-30 分钟 (2026-08-19 backfill 事故同构).
        # 现在事务只覆盖纯内存循环 (每股 ≤3 表 × 50 期 INSERT), 秒级以内.
        conn.commit()
        if (i + 1) % 200 == 0:
            _log.info(f"sina_financials sync: {i+1}/{len(symbols)} ({total} new, "
                      f"{_time.monotonic()-t0:.0f}s)")
    conn.commit()
    conn.close()
    _log.info(f"sina_financials sync done: {total} new rows "
              f"in {(_time.monotonic()-t0)/60:.1f}min")
    return total
