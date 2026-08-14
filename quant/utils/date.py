"""日期工具 — 统一 YYYY-MM-DD (ISO 8601) 格式。

项目中所有日期操作必须通过此模块，禁止硬编码 strftime/replace/[:10]。

v494 加固: to_str() 支持 int/float compact (20260605 / 20260605.0),
统一解决 tushare (int YYYYMMDD) / baostock / 财务报告期 / 手写 strftime
混用导致的反复日期格式问题。
"""

from datetime import date, datetime

import re as _re

_DATE_RE = _re.compile(r'^\d{4}-\d{2}-\d{2}$')
_COMPACT_RE = _re.compile(r'^\d{4}\d{2}\d{2}$')
_ISO_DT_RE = _re.compile(r'^\d{4}-\d{2}-\d{2}$')


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
    """任意日期 → 'YYYY-MM-DD' 字符串。单点归一化。

    兼容 (v494 全类型覆盖):
      str ISO      '2026-06-05'              → '2026-06-05'
      str compact  '20260605'                → '2026-06-05'
      str datetime '2026-06-05 08:30:00'     → '2026-06-05'
      int          20260605                  → '2026-06-05'   (tushare 返回)
      float        20260605.0                → '2026-06-05'
      datetime/date/pd.Timestamp/pd.Period   → strftime
      None/''                               → ''

    不可解析输入 → 原样返回 str(d)[:10] (由上层 validate_date_format 拦截).
    """
    if d is None:
        return ""
    if isinstance(d, str):
        s = d.strip()
        if not s:
            return ""
        if len(s) >= 8 and _COMPACT_RE.match(s[:8]):
            return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
        if "-" in s:
            return s[:10]
        return s[:10]
    if isinstance(d, (int, float)) and not isinstance(d, bool):
        try:
            return _compact_int_to_iso(d)
        except ValueError:
            return str(d)[:10]
    if isinstance(d, (date, datetime)):
        return d.strftime(DATE_FMT)
    if hasattr(d, "strftime"):  # pd.Timestamp, pd.Period
        return d.strftime(DATE_FMT)
    return str(d)[:10]


def _compact_int_to_iso(d) -> str:
    """int/float YYYYMMDD → ISO. 20260605 → '2026-06-05'.

    支持: 20260605, 20260605.0; 浮点精度误差 (20260604.999999) 取整;
    位数不足/超长的宽松处理 (tushare 偶发 7/9 位噪音).
    """
    s = str(int(round(float(d))))
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    if len(s) > 8:  # 容忍尾部噪音 (如 2026060500) — 取前 8 位
        s = s[:8]
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    raise ValueError(f"not a valid compact date: {d!r}")


def to_compact(d) -> str:
    """任意日期 → 'YYYYMMDD' 无横线格式。
    v494: int/float 直接原样转 (20260605 → '20260605'), 字符串去横线.
    """
    if isinstance(d, (int, float)) and not isinstance(d, bool):
        try:
            return _compact_int_to_iso(d).replace("-", "")
        except ValueError:
            raise
    return as_compact(d)


# ═══════════════════════════════════════════════════════════════════
# 日期格式策略 (2026-07-21 全盘审计; v494 强化执行)
# ═══════════════════════════════════════════════════════════════════
#
# 内部标准: YYYY-MM-DD (ISO 8601). SQLite TEXT 列统一此格式.
# 对接到外部 API 时, 各 _fetch_* 方法负责在入口处转换.
#
# 各数据源期望格式:
#   tushare:    YYYYMMDD  (start_date, end_date; 返回 int)
#   tickflow:   不使用日期过滤 (count=10000, 后过滤)
#   zzshare:    YYYYMMDD  (start_date, end_date)
#   pytdx:      YYYY-MM-DD (后过滤比较, 不传 API)
#   sina:       YYYY-MM-DD (后过滤比较, API 返回此格式)
#   tencent:    YYYYMMDD  (beg, end 参数)
#   akshare:    YYYYMMDD  (start_date, end_date)
#   baostock:   YYYY-MM-DD (start_date, end_date)
#   eastmoney:  YYYYMMDD   (K线/行情 API)
#
# 规则 (v494 强制执行):
#   1. 所有对 tushare/akshare/tencent/zzshare/eastmoney API 的日期参数,
#      必须通过 to_compact()/as_compact() 转换. 禁止手动 .replace("-", "").
#   2. 所有对 sina/pytdx/baostock 的日期参数或后过滤比较, 使用 YYYY-MM-DD 格式,
#      通过 as_iso() 或 to_str() 转换.
#   3. SQLite 存取统一 YYYY-MM-DD — 由 store.py 的 _date_ok regex 保证.
#   4. tushare/baostock 返回值 (统计日期/报告期) 必须过 to_str()
#      (两者返回 compact / 各自格式), 禁止裸 str(x)/x[:10].


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


# v494: strftime 手写散点防护 — 全项目禁止含日期语义的 strftime,
# 由本模块兜底; 仅保留无日期语义 (如 trace_id) 的 strftime.
def strftime_iso(dt) -> str:
    """datetime/date/Timestamp → ISO 字符串. 等同 to_str, 语义化别名. 应对逃逸散点."""
    return to_str(dt)