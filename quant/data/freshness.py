"""数据表新鲜度 SLO 监控 — 独立审计 P0-2 (2026-07-26).

背景: fund_flow 曾停滞 5 个月 (2026-02-27 起), margin 停滞 17 天
(2026-07-09 起) — 无自动同步, 无告警, 两因子在池每晚空算。

机制: 每表 SLO = MAX(date) 距今天数上限 (自然日, 覆盖周末/发布滞后)。
超限即 stale, 由调用方 (daily_data) 告警。因子依赖映射:
  fund_flow     → fund_flow_3m 等主力流因子
  margin_detail → margin_*/short_interest 两融因子
  daily         → 全部价量因子
  daily_valuation → ep_ratio/bp 等估值因子 (PIT 覆盖源)
  adj_factor    → qfq 复权链 (B-08)
"""
import os
import sqlite3
from datetime import date as _date

# 表 → SLO 自然日。策略常量, 暂不进 config.yaml (改动频繁度低)。
SLOS = {
    "daily": 4,            # 行情: 收盘后当晚同步; 4 天覆盖长假
    "fund_flow": 6,        # 东财个股资金流
    "margin_detail": 6,    # 两融 T+1 发布, 再给 2 天宽限
    "daily_valuation": 6,  # 聚宽估值
    "adj_factor": 15,      # 复权因子变化低频
}

from quant.config.paths import MARKET_DB as DB_PATH

# 源表 → 依赖它的因子 (物化池裁剪用, 审计 P0-3)。
# fund_flow: compute_main_flow_ratio 经 aux["fund_flow"] 读同一张表 (实证 _preload.py:139)
# margin_detail: margin_* 经 aux["margin"], short_interest 直查 (_alternative.py)
TABLE_TO_FACTORS = {
    "fund_flow": {"fund_flow_3m", "main_flow_ratio"},
    "margin_detail": {"margin_balance_chg", "margin_buy_ratio_5d", "short_interest"},
}


def check_freshness(today: str = None, db_path: str = None) -> list[dict]:
    """检查各表新鲜度。

    Returns:
        [{table, max_date, lag_days, slo, stale}] — stale=True 即超 SLO。
        表缺失/空表 → max_date=None, stale=True。
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
                    f"SELECT MAX(date) FROM {table}").fetchone()[0]
            lag = None
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
    """
    stale_tables = {r["table"] for r in check_freshness(today, db_path) if r["stale"]}
    out = set()
    for t in stale_tables:
        out |= TABLE_TO_FACTORS.get(t, set())
    return out
