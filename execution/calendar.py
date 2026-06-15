"""A股交易日历 — 判断交易日、交易时段、下一个交易日。

数据来源优先级:
  1. akshare tool_trade_date_hist_sina() (联网获取)
  2. 本地缓存 + 周末判断 + 已知节假日列表

A股交易时段:
  - 上午: 9:30-11:30
  - 下午: 13:00-15:00
  - 集合竞价: 9:15-9:25 (不交易，仅信号生成参考)
"""

import json
import os
from datetime import date, datetime, time, timedelta
from typing import Optional, Tuple

from utils.logger import get_logger

logger = get_logger("execution.calendar")

CACHE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "trade_calendar.json")

# A股已知节假日 (2025-2026)，格式: "YYYY-MM-DD"
# 来源: 沪深交易所公告。每年1月更新下一年节假日。
_KNOWN_HOLIDAYS: set[str] = {
    # === 2025 ===
    "2025-01-01",  # 元旦
    "2025-01-28", "2025-01-29", "2025-01-30", "2025-01-31", "2025-02-03", "2025-02-04",  # 春节
    "2025-04-04", "2025-04-07",  # 清明节 (4/4周五, 4/7周一补休 — 具体看交易所公告)
    "2025-05-01", "2025-05-02", "2025-05-05",  # 劳动节
    "2025-06-02",  # 端午节 (6/2周一)
    "2025-10-01", "2025-10-02", "2025-10-03", "2025-10-06", "2025-10-07", "2025-10-08",  # 国庆+中秋
    # === 2026 ===
    "2026-01-01", "2026-01-02",  # 元旦
    "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20", "2026-02-23",  # 春节 (2/17除夕周二)
    "2026-04-06",  # 清明节 (4/6周一补休)
    "2026-05-01", "2026-05-04", "2026-05-05",  # 劳动节
    "2026-06-22",  # 端午节 (6/19周五端午? 待确认, 保守加6/22周一)
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",  # 国庆
}


def _load_cache() -> set[str]:
    """从缓存文件加载交易日集合"""
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as f:
                data = json.load(f)
            return set(data.get("trading_days", []))
        except Exception:
            logger.exception("failed to load trade calendar cache")
    return set()


def _save_cache(trading_days: set[str]):
    """保存交易日集合到缓存文件"""
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        with open(CACHE_FILE, "w") as f:
            json.dump({
                "updated_at": datetime.now().isoformat(),
                "trading_days": sorted(trading_days),
            }, f, ensure_ascii=False)
    except Exception:
        logger.exception("failed to save trade calendar cache")


def _fetch_from_akshare() -> Optional[set[str]]:
    """从 akshare 获取历史交易日历。失败返回 None。"""
    try:
        import akshare as ak
        df = ak.tool_trade_date_hist_sina()
        if df is not None and len(df) > 0:
            trade_date_col = df.columns[0]
            days = set(df[trade_date_col].astype(str).tolist())
            logger.info(f"fetched {len(days)} trading days from akshare")
            return days
    except Exception:
        logger.warning("akshare trade calendar unavailable, using local calendar")
    return None


def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # Saturday=5, Sunday=6


def _is_known_holiday(d: date) -> bool:
    return d.strftime("%Y-%m-%d") in _KNOWN_HOLIDAYS


def _is_trading_day_local(d: date) -> bool:
    """本地判断: 非周末 + 非已知节假日"""
    return not _is_weekend(d) and not _is_known_holiday(d)


def get_trading_days() -> set[str]:
    """获取全部已知交易日。优先用缓存，缓存过期则尝试 akshare。"""
    cached = _load_cache()
    today_str = date.today().strftime("%Y-%m-%d")

    # 缓存中有今天之后的日期 → 缓存有效
    if cached and len(cached) > 0 and max(cached) >= today_str:
        return cached

    # 尝试 akshare
    fetched = _fetch_from_akshare()
    if fetched:
        _save_cache(fetched)
        return fetched

    # 回退: 本地计算最近几年的交易日
    logger.info("building local trade calendar (no akshare, no valid cache)")
    days = set()
    start = date(2020, 1, 1)
    end = date.today() + timedelta(days=60)
    current = start
    while current <= end:
        if _is_trading_day_local(current):
            days.add(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    _save_cache(days)
    return days


def is_trading_day(d: Optional[date] = None) -> bool:
    """判断是否为A股交易日。默认今天。"""
    if d is None:
        d = date.today()
    trading_days = get_trading_days()
    return d.strftime("%Y-%m-%d") in trading_days


def get_next_trading_day(from_date: Optional[date] = None) -> date:
    """获取下一个交易日（不含当日）"""
    if from_date is None:
        from_date = date.today()
    trading_days = get_trading_days()
    current = from_date + timedelta(days=1)
    # 最多往后推30天防止死循环
    for _ in range(30):
        if current.strftime("%Y-%m-%d") in trading_days:
            return current
        current += timedelta(days=1)
    # 回退：跳过周末
    while _is_weekend(current):
        current += timedelta(days=1)
    return current


def get_prev_trading_day(from_date: Optional[date] = None) -> date:
    """获取上一个交易日（不含当日）"""
    if from_date is None:
        from_date = date.today()
    trading_days = get_trading_days()
    current = from_date - timedelta(days=1)
    for _ in range(30):
        if current.strftime("%Y-%m-%d") in trading_days:
            return current
        current -= timedelta(days=1)
    while _is_weekend(current):
        current -= timedelta(days=1)
    return current


def get_trading_period(now: Optional[datetime] = None) -> str:
    """返回当前交易时段。

    Returns:
        "盘前"   — (0:00-9:30)
        "上午交易" — (9:30-11:30)
        "午休"     — (11:30-13:00)
        "下午交易" — (13:00-15:00)
        "盘后"     — (15:00-24:00)
        "休市"     — 非交易日
    """
    if not is_trading_day():
        return "休市"
    if now is None:
        now = datetime.now()
    t = now.time()
    if t < time(9, 30):
        return "盘前"
    if t < time(11, 30):
        return "上午交易"
    if t < time(13, 0):
        return "午休"
    if t < time(15, 0):
        return "下午交易"
    return "盘后"


def is_market_open(now: Optional[datetime] = None) -> bool:
    """当前是否可以交易 (9:30-11:30, 13:00-15:00)"""
    period = get_trading_period(now)
    return period in ("上午交易", "下午交易")


def get_next_market_open(from_dt: Optional[datetime] = None) -> datetime:
    """返回下一个开盘时间。如果当前正在交易，返回当前时间。"""
    if from_dt is None:
        from_dt = datetime.now()

    if is_market_open(from_dt):
        return from_dt

    period = get_trading_period(from_dt)
    td = from_dt.date()

    if period == "盘前":
        return datetime.combine(td, time(9, 30))
    if period == "午休":
        return datetime.combine(td, time(13, 0))
    if period in ("盘后", "休市"):
        next_td = get_next_trading_day(td)
        return datetime.combine(next_td, time(9, 30))

    return datetime.combine(get_next_trading_day(td), time(9, 30))


def add_holiday(date_str: str):
    """手动添加节假日（用于补充_KNOWN_HOLIDAYS未覆盖的临时休市）"""
    _KNOWN_HOLIDAYS.add(date_str)
    # 清除缓存，强制重建
    if os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)


def refresh_calendar() -> bool:
    """强制刷新日历缓存。返回是否成功。"""
    if os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)
    fetched = _fetch_from_akshare()
    if fetched:
        _save_cache(fetched)
        return True
    return False
