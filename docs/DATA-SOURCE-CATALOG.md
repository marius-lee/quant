---
document: DATA-SOURCE-CATALOG
version: 2.0
date: 2026-07-05
---
# 数据源目录 — A股量化选股系统


**最后更新**: 2026-07-05
**最新完整审查**: [DATA-SOURCE-AUDIT-2026-07-05.md](DATA-SOURCE-AUDIT-2026-07-05.md) (9 源逐项审查)


**凭证配置**: 复制 `config/env.example` 为 `config/.env` 并填入实际值；`.env` 在 `.gitignore` 中，不提交。
## 已接入

| 数据 | 表/模块 | 量级 | 状态 |
|------|---------|------|------|
| OHLCV 日线 (qfq) | daily | 16.5M行, 2020-2026 | ✅ tencent+akshare+pytdx |
| 股票基本信息 | stocks | 5,525只 | ✅ PE/PB/市值/行业 |
| 北向资金 | northbound_flow | 579K行, 466只 | ✅ 截止2024-08 (API硬截断) |
| 龙虎榜 | lhb_detail | 25K行, 4,292只 | ✅ 2025-01~2026-07 |
| 涨停池 | limit_up_pool | 1,249行, 12天 | ✅ 仅保留~1月 |
| 融资融券 | margin_detail | 478K行, 118天 | ✅ SSE+SZSE |
| 因子快照 | factor_snapshot | — | ✅ |
| 因子注册表 | factor_registry | — | ✅ |

## 已尝试但不可用

| 数据 | 原因 |
|------|------|
| 资金流向 (stock_individual_fund_flow) | IP被东方财富封 |
| 大宗交易 (stock_dzjy_mrmx/mrtj) | API KeyError / NoneType |
| 股东增减持 | 数据太少 (14条/只) |
| 深交所融资融券 (直接JSON) | ConnectionReset, akshare wrapper可用 |
| 融资余额变化率 因子 | IC=+0.004 → 弃用 |
| 融资买入占比 因子 | SSE IC=0.043, SSE+SZSE IC=0.004 → 弃用 |

## 未探索但可用的 akshare 函数

### 分析师/预测
- stock_analyst_detail_em — 分析师评级明细
- stock_analyst_rank_em — 分析师排名
- stock_profit_forecast_em — 盈利预测
- stock_profit_forecast_ths — 盈利预测(同花顺)
- stock_rank_forecast_cninfo — 业绩预告

### 机构持仓
- stock_report_fund_hold — 基金持仓
- stock_report_fund_hold_detail — 基金持仓明细
- stock_fund_stock_holder — 基金股东
- stock_main_stock_holder — 主要股东
- stock_circulate_stock_holder — 流通股东
- stock_restricted_release_stockholder_em — 限售解禁

### 股权质押
- stock_gpzy_pledge_ratio_em — 股权质押比例
- stock_gpzy_pledge_ratio_detail_em — 质押明细
- stock_gpzy_individual_pledge_ratio_detail_em — 个股质押

### 行业/概念
- stock_board_industry_cons_em — 行业板块成分股
- stock_board_concept_cons_em — 概念板块成分股
- stock_board_industry_spot_em — 行业实时行情
- stock_board_concept_spot_em — 概念实时行情
- stock_board_industry_hist_em — 行业历史行情
- stock_board_concept_hist_em — 概念历史行情
- stock_industry_pe_ratio_cninfo — 行业PE

### 分红
- stock_history_dividend_detail — 分红明细
- stock_dividend_cninfo — 分红公告

### 限售/IPO
- stock_ipo_info — IPO 信息
- stock_info_sh_delist — 上交所退市

### 港股通机构
- stock_hsgt_institution_statistics_em — 沪深股通机构统计

## 评估标准

新数据源必须满足:
1. 覆盖 ≥2,000 只 A 股
2. 历史数据 ≥2 年 (或 ≥365 交易日)
3. 日频或周频更新
4. 能构建截面因子 (cross-sectional)

## 完整数据源生态 (2026-07-05)

### 已测试并可用
| 源 | 类型 | 数据 | 限流 | 复权 |
|----|------|------|:--:|:--:|
| akshare (东方财富) | Python包 | OHLCV/PE/PB/LHB/融资融券/行业 | 60 call/min | qfq |
| tencent (腾讯财经) | HTTP API | OHLCV 日线 | 无明显限制 | qfq |
| pytdx (通达信) | TCP协议 | OHLCV 日线 | 无明显限制 | 手动qfq |
| baostock (证券宝) | Python包 | OHLCV/行业/股票列表 | 无明显限制 | 前复权 |
| tushare | Python包 | OHLCV/PE/PB/股票列表 | 200 call/min (免费) | 多种 |
| JQData (聚宽) | Python包 | PE/PB/财报/预测 | trial 限制 | — |

### 已测试但有限制
| 源 | 限制 |
|----|------|
| netease (网易财经) | HTTP 502 + DNS 失败 — 免费 API 已停运 |
| sina (新浪财经) | 仅返回未复权数据, 除权日跳变严重 — 不适合量化 |

### 不存在的源
| 公司 | 说明 |
|------|------|
| 阿里 | 无公开A股行情API (仅有云服务) |
| 字节跳动 | 无公开行情API |
| 百度 | gupiao.baidu.com 已停运 |
| 搜狐 | q.stock.sohu.com 已停运 |

### 待测试
| 源 | 说明 |
|----|------|
| 雪球 (xueqiu.com) | 需cookie, 社区驱动的数据源 |
| 同花顺 (10jqka) | 有数据但反爬严格 |
