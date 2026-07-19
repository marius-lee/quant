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

    仅用于 get_daily(start=...) 参数 (向后兼容).
    """
    return to_str(d).replace("-", "")
