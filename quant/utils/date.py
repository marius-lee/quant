"""日期工具 — 统一 YYYY-MM-DD (ISO 8601) 格式。

项目中所有日期操作应通过此模块，避免到处硬编码 strftime/replace。
"""

from datetime import date, datetime

import re as _re
_DATE_RE = _re.compile(r'^\d{4}-\d{2}-\d{2}$')

def validate_date_format(date_str, source="unknown"):
    """Validate date string is YYYY-MM-DD. Returns bool.
    Logs WARNING on invalid format.
    Usage: if not validate_date_format(d, 'lhb_detail'): continue
    """
    if _DATE_RE.match(str(date_str)):
        return True
    from quant.utils.logger import get_logger
    get_logger("quant.utils.date").warning(
        f"[{source}] invalid date format: {repr(date_str)}, skipping row"
    )
    return False


DATE_FMT = "%Y-%m-%d"
DEFAULT_START_DATE = "2020-01-01"  # 来源: 2020年前A股审批制+壳价值, 市场结构根本不同。无严格来源, 合理切分点。


def today_str() -> str:
    """今天的日期字符串: '2026-06-05'"""
    return date.today().isoformat()


def to_str(d) -> str:
    """任意日期 → 'YYYY-MM-DD' 字符串。

    兼容: str, datetime, date, pd.Timestamp, None
    """
    if d is None:
        return ""
    if isinstance(d, str):
        d = d.strip()
        if not d:  # d is str, empty string check OK per template 1
            return ""
        if "-" in d:
            return d[:10]  # already YYYY-MM-DD
        # compact format '20260604' → '2026-06-04'
        if len(d) == 8 and d.isdigit():
            return f"{d[:4]}-{d[4:6]}-{d[6:8]}"
        return d[:10]
    if isinstance(d, (date, datetime)):
        return d.strftime(DATE_FMT)
    if hasattr(d, "strftime"):  # pd.Timestamp, pd.Period
        return d.strftime(DATE_FMT)
    return str(d)[:10]


def to_compact(d) -> str:
    """任意日期 → 'YYYYMMDD' 无横线格式。
    别名: as_compact(). 向后兼容旧调用.
    """
    return as_compact(d)


# ═══════════════════════════════════════════════════════════════════
# 日期格式策略 (2026-07-21 全盘审计)
# ═══════════════════════════════════════════════════════════════════
#
# 内部标准: YYYY-MM-DD (ISO 8601). SQLite TEXT 列统一此格式.
# 对接到外部 API 时, 各 _fetch_* 方法负责在入口处转换.
#
# 各数据源期望格式:
#   tushare:    YYYYMMDD  (start_date, end_date)
#   tickflow:   不使用日期过滤 (count=10000, 后过滤)
#   zzshare:    YYYYMMDD  (start_date, end_date)
#   pytdx:      YYYY-MM-DD (后过滤比较, 不传 API)
#   sina:       YYYY-MM-DD (后过滤比较, API 返回此格式)
#   tencent:    YYYYMMDD  (beg, end 参数)
#   akshare:    YYYYMMDD  (start_date, end_date)
#
# 规则:
#   1. 所有对 tushare/akshare/tencent/zzshare API 的日期参数,
#      必须通过 as_compact() 转换. 禁止手动 .replace("-", "").
#   2. 所有对 sina/pytdx 的后过滤日期比较, 使用 YYYY-MM-DD 格式,
#      通过 as_iso() 或 to_str() 转换.
#   3. SQLite 存取统一 YYYY-MM-DD — 由 store.py 的 _date_ok regex 保证.


def as_compact(d) -> str:
    """转换为 YYYYMMDD 格式 (无横线).

    适用于: tushare / akshare / tencent / zzshare API 参数.
    等价于 to_compact(), 语义更明确.
    来源: 2026-07-21 日期格式全盘审计.
    """
    return to_str(d).replace("-", "")


def as_iso(d) -> str:
    """转换为 YYYY-MM-DD 格式 (ISO 8601).

    适用于: SQLite 存取 / sina / pytdx 后过滤比较.
    来源: 2026-07-21 日期格式全盘审计.
    """
    return to_str(d)
