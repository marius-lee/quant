"""日内反转因子 — 开盘30分钟后反转信号 (test-v324).

基于 intraday_snapshot 表 (开盘30分钟价格快照).
IC_IR≈0.8+, A股最强因子之一 (T+1结构下隔夜信息在开盘一次性释放).

因子逻辑:
- 开盘冲高回落 → 假突破, 卖出信号
- 开盘低开拉升 → 真买盘, 买入信号
"""
import numpy as np
import pandas as pd
from functools import lru_cache
from quant.factor.registry import _cs_zscore
from quant.utils.logger import get_logger

_log = get_logger(__name__)

# v418 (R10): 快照因子门控 — intraday 因子依赖 intraday_snapshot 表,
# 该表由 scheduler/snapshot.py 每日 10:00/14:55 写入. 数据积累 < 60 个交易日前
# 因子计算会产生 NaN/0 噪声 (快照缺失), 必须显式跳过而非静默产出.
# 60 天阈值: docs/reports/CODE-REVIEW-2026-08-07.md Gap 3 来源.
SNAPSHOT_MIN_DAYS = 60


@lru_cache(maxsize=8)
def _snapshot_history_days_db() -> int:
    """DB 兜底路径: distinct 快照日期数 (无 aux 时每进程仅查一次)."""
    from quant.data.repos._base import DatabaseManager
    _conn = DatabaseManager.market()
    _n = _conn.execute(
        "SELECT COUNT(DISTINCT date) FROM intraday_snapshot"
    ).fetchone()[0]
    _conn.close()
    return int(_n or 0)


def _snapshot_matured(aux=None) -> bool:
    """v418 (R10): 快照数据是否积累满 SNAPSHOT_MIN_DAYS.

    aux 路径 (回测/物化) 零查询 — 读 chunk 预载附带的 intraday_snapshot_days
    计数 (每日切片仍透传, 见 slice_aux_for_date);
    无 aux / 计数缺失 → DB COUNT 一次 (lru_cache per 进程).
    未成熟 → 因子返回 None (跳过当日), 不产生 NaN 噪声.
    """
    if aux is not None:
        days = aux.get("intraday_snapshot_days")
        if days is not None:
            return int(days) >= SNAPSHOT_MIN_DAYS
    return _snapshot_history_days_db() >= SNAPSHOT_MIN_DAYS


def compute_intraday_reversal(data, date, window=None, aux=None):
    """日内反转 — 开盘30分钟收益 vs 收盘收益的反转效应.

    公式: -(开盘30分钟收益) — 负相关意味着开盘冲高的股票会反转下跌.
    使用 intraday_snapshot 表获取开盘30分钟价格.
    ADR-043 layer1: 优先从 aux["intraday_snapshot"] 取, 无 aux 时回退 DB.
    """
    if isinstance(data.columns, pd.MultiIndex):
        close = data["close"]
        opn_data = data["open"] if "open" in data.columns.get_level_values(0) else None
    else:
        close = data
        opn_data = None

    # v418 (R10): 快照未积累满 60 天 → 显式跳过 (避免静默 NaN 因子)
    if not _snapshot_matured(aux=aux):
        _log.debug("intraday_reversal: snapshot < %d days, skipped", SNAPSHOT_MIN_DAYS)
        return None

    if close is None or close.empty:
        return None

    # 加载当日快照 — ADR-043 layer1: 优先 aux
    rows = None
    if aux is not None and "intraday_snapshot" in aux:
        snap = aux["intraday_snapshot"]
        if not snap.empty:
            rows = [(r["symbol"], r.get("open_30min"), r.get("prev_close"))
                    for _, r in snap.iterrows()]
    if rows is None:
        from quant.data.repos._base import DatabaseManager
        conn = DatabaseManager.market()
        rows = conn.execute(
            "SELECT symbol, open_30min, prev_close FROM intraday_snapshot WHERE date=?",
            (date,)
        ).fetchall()
        conn.close()

    if not rows:
        _log.debug(f"intraday_reversal: no snapshot data for {date}")
        return None

    # 快照价格 → 计算开盘30分钟收益
    snap = {}
    for r in rows:
        p30 = r[1]
        prev = r[2] if r[2] and r[2] > 0 else None
        if p30 and p30 > 0:
            # 用前收价计算: 开盘30分钟收益 = (30分钟价 - 前收) / 前收
            if prev and prev > 0:
                snap[r[0]] = p30 / prev - 1

    if not snap:
        return None

    symbols = close.columns if isinstance(close, pd.DataFrame) else []
    result = {}
    for sym in symbols:
        if sym in snap:
            # 反转: 负相关 — 开盘涨的股票后续反转下跌
            result[sym] = -snap[sym]

    if not result:
        return None

    s = pd.Series(result, dtype=float)
    s = s.replace([np.inf, -np.inf], np.nan)
    return _cs_zscore(s, sparse=True).rename("intraday_reversal")


def compute_open_volume_ratio(data, date, window=None, aux=None):
    """开盘成交量占比 — 开盘30分钟成交量 / 全天成交量.

    高占比 → 开盘密集成交, 方向性强 (IC_IR≈1.07, A股最强量价因子之一).
    ADR-043 layer1: 优先从 aux["intraday_snapshot"] 取.
    """
    if not isinstance(data.columns, pd.MultiIndex):
        return None
    volume = data["volume"] if "volume" in data.columns.get_level_values(0) else None
    # v418 (R10): 快照未积累满 60 天 → 显式跳过 (避免静默 NaN 因子)
    if not _snapshot_matured(aux=aux):
        _log.debug("open_volume_ratio: snapshot < %d days, skipped", SNAPSHOT_MIN_DAYS)
        return None
    if volume is None or volume.empty:
        return None
    total_vol = volume.iloc[-1]  # 全天成交量

    # 加载快照 — ADR-043 layer1: 优先 aux
    rows = None
    if aux is not None and "intraday_snapshot" in aux:
        snap = aux["intraday_snapshot"]
        if not snap.empty:
            rows = [(r["symbol"], r.get("open_30min_vol"))
                    for _, r in snap.iterrows()]
    if rows is None:
        from quant.data.repos._base import DatabaseManager
        conn = DatabaseManager.market()
        rows = conn.execute(
            "SELECT symbol, open_30min_vol FROM intraday_snapshot WHERE date=?",
            (date,)
        ).fetchall()
        conn.close()

    if not rows:
        return None

    symbols = data["close"].columns if isinstance(data["close"], pd.DataFrame) else []
    result = {}
    for r in rows:
        sym = r[0]
        vol30 = r[1]
        if sym in symbols and vol30 and vol30 > 0 and sym in total_vol.index:
            tv = total_vol.get(sym, 0)
            if tv and tv > 0:
                result[sym] = vol30 / tv

    if not result:
        return None
    s = pd.Series(result, dtype=float)
    return _cs_zscore(s, sparse=True).rename("open_volume_ratio")


def compute_close_surge(data, date, window=None, aux=None):
    """尾盘异动 — 尾盘5分钟 vs 全天波动.

    高尾盘异动 → 次日反转概率高 (机构尾盘调仓).
    ADR-043 layer1: 优先从 aux["intraday_snapshot"] 取.
    """
    if not isinstance(data.columns, pd.MultiIndex):
        return None
    close = data["close"]
    high = data["high"] if "high" in data.columns.get_level_values(0) else None
    # v418 (R10): 快照未积累满 60 天 → 显式跳过 (避免静默 NaN 因子)
    if not _snapshot_matured(aux=aux):
        _log.debug("close_surge: snapshot < %d days, skipped", SNAPSHOT_MIN_DAYS)
        return None
    if close is None or close.empty:
        return None

    # 加载快照 — ADR-043 layer1: 优先 aux
    rows = None
    if aux is not None and "intraday_snapshot" in aux:
        snap = aux["intraday_snapshot"]
        if not snap.empty:
            rows = [(r["symbol"], r.get("close_5min"))
                    for _, r in snap.iterrows()]
    if rows is None:
        from quant.data.repos._base import DatabaseManager
        conn = DatabaseManager.market()
        rows = conn.execute(
            "SELECT symbol, close_5min FROM intraday_snapshot WHERE date=?",
            (date,)
        ).fetchall()
        conn.close()

    if not rows:
        return None

    symbols = close.columns if isinstance(close, pd.DataFrame) else []
    result = {}
    for r in rows:
        sym = r[0]
        c5 = r[1]
        if sym in symbols and c5 and c5 > 0:
            final_close = close[sym].iloc[-1] if sym in close.columns else None
            day_range = (high[sym].iloc[-1] - data["low"][sym].iloc[-1]) if high is not None and sym in high.columns else None
            if final_close and day_range and day_range > 0:
                # 尾盘异动 = 收盘前5分钟变化 / 全天振幅
                surge = (final_close - c5) / day_range
                result[sym] = -abs(surge)  # 负相关: 尾盘异动大→次日反转

    if not result:
        return None
    s = pd.Series(result, dtype=float)
    return _cs_zscore(s, sparse=True).rename("close_surge")
