"""数据表新鲜度 SLO 监控 — 独立审计 P0-2 (2026-07-26), v479 迁至注册表.

背景: fund_flow 曾停滞 5 个月 (2026-02-27 起), margin 停滞 17 天
(2026-07-09 起) — 无自动同步, 无告警, 两因子在池每晚空算。

机制: 每表 SLO = MAX(date) 距今天数上限 (自然日, 覆盖周末/发布滞后)。
超限即 stale, 由调用方 (daily_data) 告警。因子依赖映射:
  fund_flow     → fund_flow_3m 等主力流因子
  margin_detail → margin_*/short_interest 两融因子
  daily         → 全部价量因子
  daily_valuation → ep_ratio/bp 等估值因子 (PIT 覆盖源)
  adj_factor    → qfq 复权链 (B-08)

v479: SLOS / TABLE_TO_FACTORS 的单一真相源迁至
quant/data/table_registry.py — 本模块保持旧 API (check_freshness /
unavailable_factors) 兼容, 实现改为从注册表聚合, 覆盖全部注册表.
"""
import os
import sqlite3
from datetime import date as _date

from quant.config.paths import MARKET_DB as DB_PATH
from quant.data.table_registry import REGISTRY, factors_for_tables

# 兼容旧调用方: {table: slo} (事件型 slo=None → 不判 stale)
SLOS = {name: s.slo_days for name, s in REGISTRY.items()}

# 兼容旧调用方: {table: {factor,...}}
TABLE_TO_FACTORS = {name: set(s.factors) for name, s in REGISTRY.items()}


def check_freshness(today: str = None, db_path: str = None) -> list[dict]:
    """检查各表新鲜度。

    Returns:
        [{table, max_date, lag_days, slo, stale}] — stale=True 即超 SLO。
        表缺失/空表 → max_date=None, stale=True; 事件型 (slo=None) 不判 stale。
    """
    today_d = _date.fromisoformat(today) if today else _date.today()
    conn = sqlite3.connect(db_path or DB_PATH, timeout=10)
    try:
        existing = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        results = []
        for table, slo in SLOS.items():
            max_date = None
            if table in existing:
                max_date = conn.execute(
                    f"SELECT MAX({REGISTRY[table].date_col}) FROM {table}").fetchone()[0]
            lag = None
            stale = False
            if slo is not None:
                stale = True
                if max_date:
                    lag = (today_d - _date.fromisoformat(str(max_date)[:10])).days
                    stale = lag > slo
            results.append({"table": table, "max_date": max_date,
                            "lag_days": lag, "slo": slo, "stale": stale})
        return results
    finally:
        conn.close()


def unavailable_factors(today: str = None, db_path: str = None) -> set:
    """源表超 SLO → 该表衍生因子名集合。

    用途: factor_cache 物化池按数据可用性裁剪 — 源停滞期间不空算,
    is_materialized 可对齐, 源恢复后因子自动回池补算 (missing 过滤)。
    v479: 因子映射来自注册表 (覆盖 dividend/stocks/lhb/limit 等全部表)。
    """
    stale_tables = {r["table"] for r in check_freshness(today, db_path) if r["stale"]}
    return factors_for_tables(stale_tables)
