# 方案: 物化输入数据完整性 — 表名修复 + 全量验证 + 补数 (2026-08-14)

## 背景
物化因子缓存前需从 2019-01-01 起全量验证输入数据完整性（行存在 + 字段值有效性），
非抽查。过程中发现 3 个数据坑 + 1 个表名 bug：

| 坑 | 事实 | 影响 |
|---|---|---|
| daily.turnover | 2020-2024 全市场 99.9% = 0（tushare daily 无此字段写 0） | turnover 系 6 因子全废, 已定 backfill_turnover(full) 回填（baostock, 已被 IP 封禁中断） |
| financial_cashflow 表名 bug | jq_financials.py 建表 `financial_cash_flow`(带下划线), 物化代码/preload/duckdb/prometheus 读 `financial_cashflow`(无下划线) | 现金流数据物理存在(2024-2025, 21697 行)但物化永远读空表 → ocfp/gp_ta 因子全废 |
| financial_income | 2019-2023 每年仅 6~1047 行(全市场应 5000+), 2024-2025 正常 | sue / earnings_growth_yoy / revenue_growth_yoy / gross_margin_diff 等利润表因子 2019-2023 全废 |
| financial_balance | 2023 仅 2243 只(缺一半), 2024 不全 | accruals / asset_growth / debt_ratio 等资产负债表因子 2023-2024 部分废 |
| financial_cash_flow | 2019-2023 完全缺失(0 行), 2024-2025 有 | ocfp / gp_ta 2019-2023 全废 |
| news_daily_count | 空表(0 行) | 新闻因子已排除出池, 不影响物化 |

## 数据源可用性实测 (2026-08-14)
- tushare: token 有效但无 income/cashflow/balancesheet 接口权限 → 不可补
- JQData: auth 成功但账号权限仅 2025-05-06~2026-05-13 → 不可补 2019-2023
- baostock: query_profit_data / query_cash_flow_data / query_balance_data 全免费 →
  **补 2019-2023 财务数据唯一可行源, 但 IP 被封禁中, 需等解除**

## 方案
1. **表名统一**: `financial_cash_flow` → `financial_cashflow` (SQLite ALTER RENAME +
   jq_financials.py / backfill_financials_bs.py / data_backfill_integrity.py 引用全改)
   - 物化/preload/duckdb/prometheus 已是无下划线名, 不动
2. **验证脚本** `scripts/verify_materialize_inputs.py`: 从 2019-01-01 起逐日顺序检查:
   - daily: 行数 vs 应上市数; close>0 / volume>=0 / turnover>0 占比(值有效性, 0 即 fail)
   - daily_valuation: 行数 vs daily; pe_ttm/pb/market_cap 非空率
   - benchmark_daily: 000300 当日行; close>0
   - margin_detail: 行数下限; margin_balance 非负
   - lhb_detail: 事件型, 允许缺日, 已有行字段非空
   - financial_income/balance/cashflow: 季度覆盖率(该季应有期数); 核心科目非空率
   - stocks: 全表 industry/roe/eps/bvps 非空率
   - 输出: 按年汇总 fail 清单 + 退出码(有 fail → 1)
3. **补数**: baostock 黑名单解除后跑 backfill_financials_bs.py (扩展 TARGETS 覆盖
   income 2019-2023 / balance 2023-2024 / cashflow 2019-2023)
4. **流程**: verify 全绿 → 才允许 materialize_full

## 验收
- verify_materialize_inputs.py 2019-01-01→2026-08-13 全绿(允许容忍因子池外的
  news/analyst/fund_hold/intraday 空表)
- 补数后 turnover + 三财务表缺口清零
- materialize_full 收敛(全部 skip 或仅新日期)