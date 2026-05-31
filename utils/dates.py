"""日期格式工具 — 全系统统一 YYYYMMDD 存储，pd.Timestamp 仅计算时使用。

规范:
  - SQLite 层: YYYYMMDD 字符串 (例如 "20260131")
  - 计算层: pd.Timestamp
  - 配置文件: "YYYY-MM-DD" 字符串 (例如 "2020-01-01")
"""

import pandas as pd


def norm_yyyymmdd(d) -> str | None:
    """将任意日期格式标准化为 YYYYMMDD 字符串。"""
    if d is None:
        return None
    if isinstance(d, (pd.Timestamp,)):
        return d.strftime("%Y%m%d")
    if isinstance(d, str):
        return d.replace("-", "") if "-" in d else d
    return str(d)


def norm_yyyy_mm_dd(d) -> str | None:
    """将任意日期格式标准化为 YYYY-MM-DD 字符串。"""
    if d is None:
        return None
    if isinstance(d, (pd.Timestamp,)):
        return d.strftime("%Y-%m-%d")
    s = str(d)
    if "-" in s:
        return s
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def to_timestamp(d) -> pd.Timestamp | None:
    """将日期字符串转换为 pd.Timestamp。"""
    if d is None:
        return None
    if isinstance(d, (pd.Timestamp,)):
        return d
    return pd.to_datetime(d)


def compare_dates(a, b) -> int:
    """比较两个日期（任意格式），返回 -1/0/1。"""
    na, nb = norm_yyyymmdd(a), norm_yyyymmdd(b)
    if na is None and nb is None:
        return 0
    if na is None:
        return -1
    if nb is None:
        return 1
    if na < nb:
        return -1
    if na > nb:
        return 1
    return 0


def max_date(a, b):
    """返回两个日期中较晚的那个（任意格式，保持a的原始格式）。"""
    if compare_dates(a, b) >= 0:
        return a
    return b
