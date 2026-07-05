# ADR 016 — 数据源注册表

日期: 2026-07-05 | 状态: accepted
关联: ADR 014 (Redis 缓存), ADR 015 (生产配置标准)

## 背景

项目依赖多种外部数据源拉取 A 股日线、基本面、融资融券、龙虎榜等数据。各数据源在协议类型、Python 环境、可用时段、稳定性上差异显著。需要一份集中文档记录各来源的状态、可用接口和已知限制。

核心规则: 以后任何涉及数据源选择、替换或拉取逻辑修改的任务，必须先回溯本文档，再做方案。

## 汇总表（2026-07-05 测试）

| # | 数据源 | 协议 | 认证 | 可用性 | venv | 日线 | 基本面 | 亮点 |
|---|--------|------|------|--------|------|------|--------|------|
| 1 | tencent | HTTP API | 无 | 稳定 | 3.14 | qfq复权 500行 | — | 主力源 |
| 2 | baostock | SDK | login | 稳定 | 3.12 | 3行 | 行业5532 股列8819 | 0.9.2不兼容pandas3.0 |
| 3 | tushare | SDK | token | 限流1次/h | 3.12 | daily_basic | 估值fallback | 免费接口 |
| 4 | JQData | SDK | 用户名+密码 | trial过期 | 3.12 | — | — | auth成功但数据0行 |
| 5 | akshare | HTTP API | 无 | 部分封禁 | 3.14 | OHLCV被封 | stock_list 5528 | LHB/margin/northbound可用 |
| 6 | sina | HTTP API | 无 | 部分可用 | 3.14 | 未复权K线 | — | benchmark源 |
| 7 | pytdx | TCP socket | 无 | 稳定 | 3.14 | 3 bars | — | 通达信备用 |
| 8 | 东方财富 | HTTP | 无 | 封禁中 | 3.14 | — | — | 通过akshare代理 |
| 9 | 网易 | HTTP API | 无 | 已死 | 3.14 | — | — | 502+DNS不可达 |
| 10 | 同花顺 | HTTP API | — | 无法接入 | 3.14 | — | — | v6 404 |
| 11 | 雪球 | HTTP API | Cookie | 部分可用 | 3.14 | K线400 | 股列5000 | 列表可用 |
| 12 | 中证指数 | 官网 | — | 未测试 | — | — | 指数成分股 | 待评估 |

## 各数据源详细分析

### 1. tencent — 主力日线数据源
- 端点: qt.gtimg.cn
- 能力: 全A股 qfq（前复权）日线，每次50只，每只最高500行
- 在项目中: data/store.py _fetch_tencent_daily()，daily_sync.py 主源
- 优点: 免费、无需认证、响应快速、格式稳定
- 成本: 零

### 2. baostock — 基础数据源（证券宝）
- SDK: baostock 0.9.2，.venv-tushare (Py3.12)
- 认证: bs.login() 匿名登录
- 能力: A股日K线、行业分类(5532行)、股票列表(8819只)
- 兼容性风险: baostock 0.9.2 内用 DataFrame.append()，pandas >= 2.0 已移除
- 项目中: data/daily_basic.py（已标记 DEPRECATED）
- 成本: 零

### 3. tushare — 估值 fallback 源
- SDK: tushare，.venv-tushare (Py3.12)
- 认证: TUSHARE_TOKEN (config/.env)
- 免费限制: stock_basic 1次/小时
- 项目中: data/jq_valuation.py 作为 JQData 自动 fallback
- 成本: 零（免费层）

### 4. JQData — 基本面源（试用过期）
- SDK: jqdatasdk，.venv-tushare (Py3.12)
- 认证: JQDATA_USER + JQDATA_PASS (config/.env)
- 状态: Auth 成功，get_valuation() 返回 0 行 — trial 过期
- 降级: data/jq_valuation.py 实现 JQData → tushare fallback，日志记录
- 成本: 零（trial）

### 5. akshare — 专项数据源（部分被封）
- SDK: akshare，.venv (Py3.14)
- 可用: stock_list (5528行)、LHB明细(452行)、融资融券(1981行)、北向资金(1683行)
- 不可用: stock_zh_a_hist() OHLCV — 东方财富 ConnectionError，IP 被封
- 原因: akshare 直连东方财富页面，频繁请求触发反爬
- 项目中: data/lhb.py, data/margin.py, data/northbound.py
- 成本: 零

### 6. sina — 基准指数源
- 端点: money.finance.sina.com.cn
- 能力: 未复权日K线、沪深300指数数据
- 项目中: data/benchmark.py
- 已知问题: 偶发 DNS 失败，sync_benchmark() 无 fallback
- 成本: 零

### 7. pytdx — 通达信备用源
- 协议: TCP socket
- SDK: pytdx，.venv (Py3.14)
- 项目中: data/store.py _fetch_pytdx_daily()
- 成本: 零

### 8-12: 已死或无法接入
- 网易(#9): HTTP 502 + DNS 不可达，确认已死
- 同花顺(#10): v6 端点404，stockpage HTML非JSON
- 雪球(#11): K线400，仅股票列表可用(5000条)
- 东方财富(#8): 通过akshare代理，IP已封
- 中证指数(#12): 未测试，待评估

## 数据源分层

### 第一层 主力

| 数据类型 | 主力源 | 状态 |
|----------|--------|------|
| A股日线 | tencent | 正常 |
| 股票列表 | baostock / tencent | 正常 |
| 行业分类 | baostock | 正常 |
| 估值 PE/PB | tushare (JQData fallback) | trial过期后自动切换 |
| 融资融券 | akshare | 正常 |
| 龙虎榜 | akshare | 正常 |
| 北向资金 | akshare | 正常 |
| 基准指数 | sina | 偶发DNS失败 |

### 第二层 备用

| 数据类型 | 备用源 | 触发条件 |
|----------|--------|----------|
| 日线 | pytdx | tencent 不可用 |
| 日线 | sina | tencent/pytdx 均不可用 |
| 估值 | JQData | tushare 不可用 |

### 第三层 已排除

网易(502)、同花顺(404)、雪球K线(400)、东方财富直连(IP被封)

## 认证凭证管理

凭证存储在 config/.env:
- TUSHARE_TOKEN
- JQDATA_USER / JQDATA_PASS

读取方式: scripts/test_all_sources.sh 用 export 注入环境变量
安全: .env 在 .gitignore 中，不入版本控制

## 测试命令

    bash scripts/test_all_sources.sh   # 全量连通性测试，约2分钟

## 短期建议

1. data/benchmark.py 加 fallback（sina DNS 偶发失败）
2. 购买 tushare 付费接口，消除 1次/小时限制
3. 不推荐继续尝试同花顺、雪球、网易 — 已确认不可用
