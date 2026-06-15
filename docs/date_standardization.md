# 日期格式标准化

日期: 2026-06-05

## 问题

项目中存在两种日期格式混用:
- `daily.date`: `20260604` (compact)
- `factors.date`: `2026-06-04` (standard)
- 代码到处 `replace("-","")` / `strftime("%Y%m%d")` 来回转换

## 标准

**ISO 8601: YYYY-MM-DD**

理由:
1. 国际标准, SQLite 内置日期函数原生支持
2. 字符串排序 = 时间排序 (`2026-06-04` < `2026-06-05` 在字典序中成立)
3. 项目中 90% 的表已用此格式

## 改动

1. `utils/date.py` — 新增 `DEFAULT_START_DATE = "2020-01-01"` 全局常量
2. `daily.date` — 1637 万行一次性迁移 (170s)
3. `lhb_detail.trade_date` — 9.9 万行迁移 (0.2s)
4. `factors.date` — 重建时已用标准格式
5. 所有 `strftime("%Y%m%d")` / `replace("-","")` / `str.slice(0,10)` → `to_str()` / `to_compact()`

## 例外

tushare/akshare 外部 API 只接受 YYYYMMDD → 调用时用 `to_compact()` 转换, 已加注释标注。

## 来源

- ISO 8601: International standard for date and time representation
- SQLite date functions: https://www.sqlite.org/lang_datefunc.html
