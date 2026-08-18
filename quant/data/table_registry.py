"""数据表注册表 — 每张表的同步/审计/修复单一真相源 (v479).

背景 (v478 复盘): 数据缺失反复发生, 根因是任务覆盖不全 + 失败被吞 +
检测只看新鲜度不看完整性. 本注册表把"表 → 同步方式 → 完整性规则 → 因子依赖"
集中声明, 供晚间链 (daily_data) / 早间补拉链 (repair) / 周度维护
(data_maintenance) / 完整性审计 (data_health) 统一消费.

模式语义:
  primary     — 主流程同步 (晚间链 update_daily), 失败即任务 failed, 不在此循环
  rollback    — 滚动窗口补拉 (start=today-window_days .. today), 自带 T+1 迟发补偿
  weekly_full — 周六全量幂等重拉 (事件型/低频: dividend/股本快照), 周期 >7 天兜底
  none        — 无自动同步 (仅审计; 人工/其他链维护)

审计规则 (data_health.audit_table):
  freshness  — MAX(date) 滞后 > slo_days → fail (事件型 slo=None 跳过)
  gap_dates  — 最近 lookback 交易日内, daily 有而本表缺的日期 → fail
  coverage   — 最近 5 交易日每日行数 < min_rows_per_day → fail (事件型跳过)
  total_rows — COUNT(*) < min_total_rows → fail (累计底线, 如 dividend ≥ 5 万)
  custom     — custom_check(conn) → (ok, detail)
"""
from dataclasses import dataclass, field
from typing import Callable, Optional

# 从 quant.data.X import sync_* — 延迟导入避免模块加载时序 (quant.data 各模块
# 顶层连库), 统一在 _lazy_sync 包装器内 import.

# [v491: _FIN_FACTOR_NAMES 定义见文件末尾 — 财务三表注册引用, 需在 REGISTRY 前可用]
_FIN_FACTOR_NAMES: set[str] = {
    "roe_reported", "ocfp", "roa", "debt_ratio", "accruals", "asset_growth",
    "gp_ta", "sue", "holder_reduction", "pledge_ratio", "dividend_yield",
}

# ── 因子映射: 表 → 依赖它的因子 (factor_cache 物化裁剪 unavailable_factors 用) ──
FACTORS_BY_TABLE: dict[str, frozenset[str]] = {
    "daily_valuation": frozenset({"ep_ratio", "bp_ratio", "ocfp"}),
    "dividend": frozenset({"dividend_yield"}),
    "stocks": frozenset({"sue"}),
    "fund_flow": frozenset({"fund_flow_3m", "main_flow_ratio"}),
    "margin_detail": frozenset({"margin_balance_chg", "margin_buy_ratio_5d",
                                "margin_buy_ratio", "short_interest"}),
    "lhb_detail": frozenset({"lhb_freq_60d", "lhb_intensity_5d", "lhb_net_buy_20d",
                             "lhb_post_quality", "lhb_reversal_5d"}),
    "limit_up_pool": frozenset({"limit_touch_no_seal", "limit_up_prox_5d",
                                "net_limit_ratio"}),
    "limit_down_pool": frozenset({"net_limit_ratio"}),
    # v491: 财务三表 → 基本面因子 (与 REGISTRY 中 financial_* 的 factors 同源,
    # 见文件前部 _FIN_FACTOR_NAMES; 保持两份一致, 供 factor_cache 裁剪)
    "financial_income": frozenset(_FIN_FACTOR_NAMES),
    "financial_balance": frozenset(_FIN_FACTOR_NAMES),
    "financial_cashflow": frozenset(_FIN_FACTOR_NAMES),
}


def _lazy_sync(module: str, fn: str, wraps_rollback: bool = False):
    """延迟导入包装 — 统一同步函数签名.

    Args:
        module: quant.data.<module>
        fn: 模块内函数名
        wraps_rollback: True → 函数签名 (start, end) 直接用; False → () 无参全量
    """
    def _call(start: Optional[str] = None, end: Optional[str] = None) -> int:
        import importlib
        mod = importlib.import_module(f"quant.data.{module}")
        f = getattr(mod, fn)
        if wraps_rollback:
            return f(start, end)
        return f()
    _call.__name__ = f"{module}.{fn}"
    return _call


def _sync_fund_flow(start: Optional[str] = None, end: Optional[str] = None) -> int:
    from quant.data.fund_flow import sync_all
    return sync_all(days=100)


def _sync_margin(start: Optional[str] = None, end: Optional[str] = None) -> int:
    from quant.data.margin import sync_range
    return sync_range(start, end)


def _sync_lhb_days(start: Optional[str] = None, end: Optional[str] = None) -> int:
    """lhb 无区间接口 → 逐日 sync_date (发布晚于 19:00 时次日回补)."""
    import sqlite3
    from datetime import date as _date, timedelta as _td
    from quant.data.lhb import sync_date
    from quant.config.paths import MARKET_DB
    total = 0
    c = sqlite3.connect(MARKET_DB)
    days = [r[0] for r in c.execute(
        "SELECT DISTINCT date FROM daily WHERE date BETWEEN ? AND ? ORDER BY date",
        (start, end)).fetchall()]
    c.close()
    for d in days:
        try:
            total += sync_date(d)
        except Exception:
            raise
    return total


def _sync_limit_days(start: Optional[str] = None, end: Optional[str] = None) -> int:
    """limit_up/down 逐日 sync_date + sync_down_date."""
    import sqlite3
    from quant.data.limit_up import sync_date as _up, sync_down_date as _down
    from quant.config.paths import MARKET_DB
    total = 0
    c = sqlite3.connect(MARKET_DB)
    days = [r[0] for r in c.execute(
        "SELECT DISTINCT date FROM daily WHERE date BETWEEN ? AND ? ORDER BY date",
        (start, end)).fetchall()]
    c.close()
    for d in days:
        total += _up(d)
        total += _down(d)
    return total


def _sync_limit_down(start: Optional[str] = None, end: Optional[str] = None) -> int:
    """仅跌停池逐日 sync_down_date (与涨停池同循环拉取时的独立修复)."""
    import sqlite3
    from quant.data.limit_up import sync_down_date
    from quant.config.paths import MARKET_DB
    total = 0
    c = sqlite3.connect(MARKET_DB)
    days = [r[0] for r in c.execute(
        "SELECT DISTINCT date FROM daily WHERE date BETWEEN ? AND ? ORDER BY date",
        (start, end)).fetchall()]
    c.close()
    for d in days:
        total += sync_down_date(d)
    return total


def _sync_em_valuation(start: Optional[str] = None, end: Optional[str] = None) -> int:
    from quant.data.em_valuation import sync_range
    return sync_range(start, end)


def _check_stocks_coverage(conn) -> tuple[bool, str]:
    """自定义规则: total_shares 覆盖率 ≥99% (非北交所 92xxx)."""
    tot = conn.execute(
        "SELECT COUNT(*) FROM stocks WHERE symbol NOT LIKE '92%'").fetchone()[0]
    filled = conn.execute(
        "SELECT COUNT(*) FROM stocks WHERE symbol NOT LIKE '92%' "
        "AND total_shares IS NOT NULL AND total_shares > 0").fetchone()[0]
    if tot == 0:
        return (True, "stocks 空表跳过")
    pct = filled / tot * 100
    ok = pct >= 99.0
    return (ok, f"total_shares 覆盖 {filled}/{tot} = {pct:.1f}% (≥99%)")


def _fin_income_field_check(conn) -> tuple[bool, str]:
    """v547: financial_income 字段级检查 — 最新报告期 operating_cost /
    administration_expense NaN 率 ≤50% (v544 事件: 2020-2024 全 NaN 缺字段
    靠"有行"审计漏过 — 行级完整 ≠ 字段完整). 银行/保险无营业成本科目,
    NaN 率显著低于 50% 阈值, 不会误报."""
    mx = conn.execute("SELECT MAX(stat_date) FROM financial_income").fetchone()[0]
    if mx is None:
        return False, "空表"
    cost_nan, admin_nan, tot = conn.execute(
        "SELECT "
        "SUM(CASE WHEN operating_cost IS NULL OR operating_cost != operating_cost THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN administration_expense IS NULL OR administration_expense != administration_expense THEN 1 ELSE 0 END), "
        "COUNT(*) FROM financial_income WHERE stat_date=?", (mx,)).fetchone()
    cost_nan, admin_nan = cost_nan or 0, admin_nan or 0
    cost_pct, admin_pct = cost_nan / tot * 100, admin_nan / tot * 100
    if cost_pct > 50 or admin_pct > 50:
        return (False,
                f"最新期 {mx}: operating_cost NaN {cost_pct:.0f}% / "
                f"administration_expense NaN {admin_pct:.0f}% (≤50%)")
    return (True,
            f"最新期 {mx}: cost NaN {cost_pct:.0f}% / admin NaN {admin_pct:.0f}% (≤50%)")


@dataclass(frozen=True)
class TableSpec:
    table: str                        # market.db 表名
    date_col: str                     # 日期列
    mode: str                         # primary | rollback | weekly_full | none
    sync_main: Optional[Callable[..., int]] = None   # (start, end) 或 () 全量
    window_days: Optional[int] = None  # rollback 回补窗口 (自然日)
    min_rows_per_day: Optional[int] = None  # 日行数底线 (事件型 None 跳过)
    min_total_rows: Optional[int] = None    # 累计行数底线
    slo_days: Optional[int] = None          # max_date 滞后 SLO (事件型 None)
    factors: frozenset[str] = frozenset()
    custom_check: Optional[Callable[[object], tuple[bool, str]]] = None
    repair_eligible: bool = True      # v492: 早间链兜底资格 (慢表 False → 仅周度链)
    desc: str = ""


# ─────────────────────────────────────────────────────────────────────
# 注册表 (顺序 = 晚间链循环顺序)
# ─────────────────────────────────────────────────────────────────────
REGISTRY: dict[str, TableSpec] = {
    "daily": TableSpec(
        table="daily", date_col="date", mode="primary",
        min_rows_per_day=5000, slo_days=4,
        desc="主力行情 (晚间链主流程 update_daily, 失败即 failed)"),
    "daily_valuation": TableSpec(
        table="daily_valuation", date_col="date", mode="rollback",
        sync_main=_lazy_sync("em_valuation", "sync_range", wraps_rollback=True),
        window_days=14, min_rows_per_day=5200, slo_days=6,
        factors=FACTORS_BY_TABLE["daily_valuation"],
        desc="估值 (东财源, 封禁时 audit fail → partial 可见)"),
    "adj_factor": TableSpec(
        table="adj_factor", date_col="date", mode="none",
        min_rows_per_day=None, slo_days=15,
        desc="复权因子 (事件型: 仅调整日有行; 独立 stage, 失败在晚间链层面)"),
    "fund_flow": TableSpec(
        table="fund_flow", date_col="date", mode="rollback",
        sync_main=_sync_fund_flow, window_days=100,
        min_rows_per_day=450, slo_days=6,
        factors=FACTORS_BY_TABLE["fund_flow"],
        desc="个股资金流 (东财源; 晚间链仅维护市值前 500)"),
    "margin_detail": TableSpec(
        table="margin_detail", date_col="date", mode="rollback",
        sync_main=_sync_margin, window_days=30,
        min_rows_per_day=1600, slo_days=6,
        factors=FACTORS_BY_TABLE["margin_detail"],
        desc="两融 (SSE 沪市, T+1 发布 → 次日回补)"),
    "lhb_detail": TableSpec(
        table="lhb_detail", date_col="trade_date", mode="rollback",
        sync_main=_sync_lhb_days, window_days=7,
        min_rows_per_day=None, slo_days=6,
        factors=FACTORS_BY_TABLE["lhb_detail"],
        desc="龙虎榜 (发布可能晚于 19:00 → 次日回补)"),
    "limit_up_pool": TableSpec(
        table="limit_up_pool", date_col="date", mode="rollback",
        sync_main=_sync_limit_days, window_days=7,
        min_rows_per_day=None, slo_days=6,
        factors=FACTORS_BY_TABLE["limit_up_pool"],
        desc="涨停池 (逐日, 事件型)"),
    "limit_down_pool": TableSpec(
        table="limit_down_pool", date_col="date", mode="rollback",
        sync_main=_sync_limit_down, window_days=7,
        min_rows_per_day=None, slo_days=6,
        factors=FACTORS_BY_TABLE["limit_down_pool"],
        desc="跌停池 (独立逐日; 常态与涨停池同循环拉取, 单独失败可独立修复)"),
    "dividend": TableSpec(
        table="dividend", date_col="ex_date", mode="weekly_full",
        sync_main=_lazy_sync("dividend", "sync_range"),
        min_total_rows=50000, slo_days=None,
        factors=FACTORS_BY_TABLE["dividend"],
        desc="分红 (新浪源全量幂等, 周六刷; 事件型不判新鲜度)"),
    "stocks": TableSpec(
        table="stocks", date_col="list_date", mode="weekly_full",
        sync_main=_lazy_sync("stocks_snapshot", "refresh_all"),
        min_total_rows=5000, slo_days=None,
        custom_check=_check_stocks_coverage,
        factors=FACTORS_BY_TABLE["stocks"],
        desc="股票快照 (股本 baostock + 列表 tushare, 周六全量)"),
    "benchmark_daily": TableSpec(
        table="benchmark_daily", date_col="date", mode="rollback",
        sync_main=_lazy_sync("benchmark", "sync_benchmark"),
        window_days=10, min_rows_per_day=None, slo_days=15,
        desc="指数基准 (baostock 幂等全量, 便宜)"),
    # v491: 财务三表接入调度链 — 此前完全不在注册表, JQ 权限窗口 (2025q2~2026q1)
    # 外的季度 (income/cashflow 2019-2023 全缺) 无人自动拉取, 只能手动脚本.
    # 注册 weekly_full (周六 data_maintenance 全量刷新 + 早间链 7 天兜底),
    # 源 = sina (quant/data/sina_financials.sync), 幂等跳过已有行.
    # 审计: date_col=stat_date + slo=None (事件型, 不判新鲜度) — gap/coverage
    # 检查按 date 列跳过; total_rows 底线防清库; custom 检查最近报告期覆盖.
    # v492: repair_eligible=False — sina 首轮全量 4-5h > 早间链 30min 窗口,
    # 每天触发必超时被杀 (v491 注册后每早白跑). 财务表只由周六 data_maintenance
    # (12h 窗口) 维护, 早间链不兜底.
    "financial_income": TableSpec(
        table="financial_income", date_col="stat_date", mode="weekly_full",
        sync_main=_lazy_sync("sina_financials", "sync"),
        min_total_rows=100000, slo_days=None,
        factors=frozenset(_FIN_FACTOR_NAMES),
        repair_eligible=False,
        custom_check=_fin_income_field_check,
        desc="利润表 (sina 全历史幂等; JQ 窗口外季度由本任务补)"),
    "financial_balance": TableSpec(
        table="financial_balance", date_col="stat_date", mode="weekly_full",
        sync_main=_lazy_sync("sina_financials", "sync"),
        min_total_rows=100000, slo_days=None,
        factors=frozenset(_FIN_FACTOR_NAMES),
        repair_eligible=False,
        desc="资产负债表 (sina 全历史幂等)"),
    "financial_cashflow": TableSpec(
        table="financial_cashflow", date_col="stat_date", mode="weekly_full",
        sync_main=_lazy_sync("sina_financials", "sync"),
        min_total_rows=100000, slo_days=None,
        factors=frozenset(_FIN_FACTOR_NAMES),
        repair_eligible=False,
        desc="现金流量表 (sina 全历史幂等)"),
}


def spec(name: str) -> TableSpec:
    return REGISTRY[name]


def rollback_specs() -> list[TableSpec]:
    """晚间链循环: mode=rollback 的表 (升序依赖: 估值/资金流先行)."""
    return [s for s in REGISTRY.values() if s.mode == "rollback"]


def weekly_full_specs() -> list[TableSpec]:
    return [s for s in REGISTRY.values() if s.mode == "weekly_full"]


def factors_for_tables(tables: set[str]) -> set[str]:
    """表集合 → 受影响因子集合 (物化裁剪用)."""
    out: set[str] = set()
    for t in tables:
        out |= REGISTRY[t].factors if t in REGISTRY else set()
    return out
