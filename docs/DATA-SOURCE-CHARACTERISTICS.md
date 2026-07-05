---
document: DATA-SOURCE-CHARACTERISTICS
version: 1.0
date: 2026-07-05
status: living
---
# 数据源特性总览

每个数据源的详细特性、优势、短板、适用场景。配合 [DATA-SOURCE-CATALOG](DATA-SOURCE-CATALOG.md) (接入状态) 和 [DATA-SOURCE-AUDIT-2026-07-05](DATA-SOURCE-AUDIT-2026-07-05.md) (连通性审计) 使用。

---

## 1. akshare (东方财富) — 主力数据源

**接入方式**: Python 包 (`pip install akshare`), Py3.14  
**底层**: 东方财富网 (eastmoney.com) HTTP API 封装

### 可用数据
| 接口 | 用途 | 量级 | 速度 |
|------|------|------|------|
| `stock_info_a_code_name` | 全A股列表 | 5,528只 | <1s |
| `stock_zh_a_hist(adjust="qfq")` | OHLCV 日线 | 7.35M行 | ~200ms/只 |
| `stock_value_em` | PE/PB/总市值 | — | ~200ms/只 |
| `stock_lhb_detail_em` | 龙虎榜 | 25K行 | <1s |
| `stock_margin_detail_sse/szse` | 融资融券 | 478K行 | <1s |
| `stock_board_industry_cons_em` | 行业分类 | — | <1s |

### 优势
- **数据最全面**: OHLCV + 估值 + 龙虎榜 + 融资融券 + 行业, 一个包覆盖多条数据链
- **免费无注册**: 不需要 token/账号, 开箱即用
- **前复权支持**: `adjust="qfq"` 直接返回前复权数据
- **换手率数据**: 唯一一个提供完整历史换手率的免费源

### 短板
- **间歇性 ConnectionError**: `stock_zh_a_hist` 偶发 `RemoteDisconnected`, 东方财富对 OHLCV 接口有隐形反爬
- **资金流向不可用**: `stock_individual_fund_flow` 已被东方财富主动拒绝
- **北向资金截止 2024-08**: API 硬截断, 2024年8月后无新数据
- **大宗交易不可用**: API KeyError
- **单点故障风险**: 行业/PE/PB/LHB/融资融券全部依赖它

### 限流
- 约 60 calls/min (通过 Redis RateLimiter 管控)
- 逐只查询时 200ms 间隔较安全

### 最佳场景
日常 OHLCV 同步、估值数据、龙虎榜、融资融券的主力源。不适合高频实时行情。

---

## 2. tencent (腾讯财经) — 回退链主源

**接入方式**: HTTP API (不需要任何包), 任意 Python 版本  
**底层**: `web.ifzq.gtimg.cn`

### 可用数据
| 接口 | 用途 | 量级 |
|------|------|------|
| `/appstock/app/fqkline/get` | OHLCV 日线 (qfq 前复权) | 单次最多500行 |

### 优势
- **免费无注册**: 零门槛
- **前复权**: qfq 数据, 无需手动复权
- **批量拉取**: 单次请求 500 条, 速度快
- **稳定**: 当前最稳定的免费 HTTP 源, 无明显反爬

### 短板
- **无换手率**: 不提供 turnover 数据, 需配合 akshare
- **volume 单位是手**: 需在代码中 `/100` 转换为股
- **amount 单位是千元**: 需在代码中 `*1000` 转换为元
- **仅日线**: 无分钟级数据

### 限流
无明显频率限制

### 最佳场景
OHLCV 日线的主力回退源。当前在 `all_sources` 回退链第二位 (pytdx → tencent → akshare)。

---

## 3. pytdx (通达信) — 二进制协议

**接入方式**: Python 包 (`pip install pytdx`), Py3.14  
**底层**: TCP 连接通达信行情服务器 (180.153.18.170:7709)

### 可用数据
| 接口 | 用途 | 量级 |
|------|------|------|
| `get_security_bars` | OHLCV 日线 | 批量 (二进制协议) |

### 优势
- **批量速度快**: 0.8-1.2s/批次, 二进制协议比 HTTP 轻量
- **免费**: 不需要注册
- **独立于 HTTP 源**: 不受东方财富/腾讯的反爬影响

### 短板
- **无换手率**: 不提供 turnover
- **需手动前复权**: 返回未复权数据, 代码中需自行计算 (L553-592)
- **服务器不在自己控制下**: 180.153.18.170 偶发不可达
- **volume 单位是手, amount 单位是元**: 需转换

### 限流
无明显频率限制, 但服务器偶发不可达

### 最佳场景
当前回退链第一位 (按 speed EMA 排序最快)。适合批量补充 OHLCV 缺口。

---

## 4. baostock (证券宝) — 行业分类+股票列表

**接入方式**: Python 包 (`pip install baostock`), Py3.12  
**底层**: baostock 自有服务器

### 可用数据
| 接口 | 用途 | 量级 |
|------|------|------|
| `query_history_k_data_plus` | OHLCV + PE/PB | 含 peTTM/pbMRQ 字段 |
| `query_stock_industry` | 证监会行业分类 | 5,532行 |
| `query_stock_basic` | 全A股列表 | 8,819行 |

### 优势
- **行业分类完整**: 证监会标准行业分类, 5,532 条
- **股票列表最全**: 8,819只 (含指数/B股)
- **K线自带 PE/PB**: 不需要单独拉估值接口
- **免费无注册**: login/logout 即可

### 短板
- **需要 Py3.12**: baostock 0.9.2 依赖已废弃的 `DataFrame.append()`, 在 Py3.14 上不可用
- **前次误判为宕机**: 沙箱网络阻断导致误报, 实际服务正常
- **无换手率**: 不提供 turnover

### 限流
无明显频率限制

### 最佳场景
行业分类主源 + 股票列表校验。`daily_basic.py` 已 deprecated, 不再用作 OHLCV 源。

---

## 5. tushare — Token化数据接口

**接入方式**: Python 包 (`pip install tushare`), Py3.12  
**底层**: tushare.pro HTTP API  
**凭证**: `TUSHARE_TOKEN` (存于 `config/.env`)

### 可用数据
| 接口 | 用途 | 免费 tier |
|------|------|:--:|
| `stock_basic` | 股票列表 | 1次/小时 ⚠️ |
| `pro.daily` | OHLCV 日线 | 200次/分钟 |
| `daily_basic` | PE/PB/换手率 | 200次/分钟 |

### 优势
- **数据规范**: 标准化的字段和格式, 比 akshare 的爬虫封装更可靠
- **估值数据**: daily_basic 可替代 JQData 填补 PE/PB 真空
- **token 已验证有效**: 当前 token 可用

### 短板
- **免费 tier 极严**: `stock_basic` 仅 1次/小时, 不适合高频同步
- **需要 Py3.12**: tushare 1.4.29 仅安装在 `.venv-tushare`
- **代码未接入**: `_fetch_batch_tushare()` 函数存在但不在 `all_sources` 列表中

### 限流
免费 200 calls/min (大部分接口), stock_basic 1次/小时

### 最佳场景
作为 OHLCV 的 backup 备源 + PE/PB 数据补充。需接入 `all_sources` 回退链。

---

## 6. JQData (聚宽) — 历史估值数据

**接入方式**: Python 包 (`pip install jqdatasdk`), Py3.12  
**底层**: Thrift RPC → 39.107.190.114:7000  
**凭证**: `JQDATA_USER` + `JQDATA_PASS` (存于 `config/.env`)  
**Trial**: 2025-03-26 ~ 2026-04-02 (数据范围)

### 可用数据
| 接口 | 用途 | 数据范围 |
|------|------|------|
| `get_fundamentals(query(valuation))` | PE/PB/PS/PCF | ≤ 2026-04-02 |
| 财务报表 | 利润表/资产负债表/现金流 | ≤ 2026-04-02 |
| 分析师预测 | 盈利预测 | ≤ 2026-04-02 |

### 优势
- **高质量估值数据**: 机构级数据质量, 标准化
- **Auth 仍成功**: trial 过期但连接未断, 历史数据仍可查
- **Python 接口优雅**: 声明式 query API

### 短板
- **数据截止 2026-04-02**: trial 到期后无新数据, 存在 3 个月 PE/PB 真空
- **需要 Py3.12**: jqdatasdk 仅安装在 `.venv-tushare`
- **不是"已死"**: 审计文档原写 "已停" 有误导, 实际是对历史数据仍可用

### 限流
trial 限制, 具体数额不明

### 最佳场景
补充 2026-04-02 之前的 PE/PB/财报历史数据。不适合做实时数据源。

---

## 7. sina (新浪财经) — 实时行情参考

**接入方式**: HTTP API (不需要任何包), 任意 Python 版本  
**底层**: `money.finance.sina.com.cn`

### 可用数据
| 接口 | 用途 | 限制 |
|------|------|:--:|
| 历史K线 (JSON) | OHLCV 日线 | 未复权 ⚠️ |
| 实时行情 (hq.sinajs.cn) | 实时报价 | 延迟极低 |

### 优势
- **实时行情延迟低**: `hq.sinajs.cn` 接近实时
- **免费无注册**
- **独立于东方财富/腾讯**

### 短板
- **历史K线未复权**: 除权日会出现单日 -34% 的跳变, 不能用于回测
- **无换手率**
- **已从回退链移除** (P3): 不适合做 OHLCV 源

### 最佳场景
实时行情监控/看盘。不适合量化回测的历史数据。

---

## 8. netease (网易财经) — 已停运

**接入方式**: HTTP API  
**测试结果**: ❌ 历史日线 HTTP 502, 实时行情 DNS 失败  
**结论**: 网易免费 API 已停运或更换域名, 不可用。

---

## 9. 同花顺 (10jqka) — 待测试

**接入方式**: HTTP API (JSONP 格式)  
**底层**: `d.10jqka.com.cn` (K线), `q.10jqka.com.cn` (行情)

### 已知特点
- K线数据用 JSONP 包裹, 需手动提取 JSON
- 返回前复权 (qfq) 数据
- 代码格式: 6位纯数字 (如 `600519`)
- 周期参数: 09=日线, 10=周线, 11=月线
- 反爬: 同花顺对程序化访问较敏感, 可能需要 User-Agent + Referer

### 待确认
- 数据范围 (历史深度)
- 频率限制
- 换手率是否提供
- 稳定性

---

## 10. 雪球 (xueqiu) — 待测试

**接入方式**: HTTP API (需 Cookie)  
**底层**: `stock.xueqiu.com`

### 已知特点
- 需要先访问 `xueqiu.com` 获取 Cookie
- K线接口: `stock.xueqiu.com/v5/stock/chart/kline.json`
- 股票列表: `xueqiu.com/service/v5/stock/screener/quote/list`
- 返回 JSON, 格式规范

### 待确认
- Cookie 有效期
- 数据范围
- 频率限制
- 复权方式

---

## 总结矩阵

| 源 | OHLCV | PE/PB | 行业 | 换手率 | 复权 | 免费 | 稳定性 |
|----|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| **akshare** | ✅ | ✅ | ✅ | ✅ | qfq | ✅ | ⚠️ 间歇 |
| **tencent** | ✅ | — | — | — | qfq | ✅ | ✅ |
| **pytdx** | ✅ | — | — | — | 手动 | ✅ | ✅ |
| **baostock** | ✅ | ✅ | ✅ | — | 前复权 | ✅ | ✅ |
| **tushare** | ✅ | ✅ | — | ✅ | 多种 | ⚠️ 需token | ✅ |
| **JQData** | — | ✅ | — | — | — | ❌ trial | ✅ |
| **sina** | ⚠️ | — | — | — | ❌ 无 | ✅ | ✅ |
| **netease** | ❌ | — | — | — | — | — | ❌ |
| **同花顺** | ? | ? | ? | ? | ? | ✅ | 待测 |
| **雪球** | ? | ? | ? | ? | ? | ✅ | 待测 |

## 推荐回退链

```
pytdx → tencent → tushare → akshare
```

(akshare 放末位因为它间歇性 ConnectionError; tushare 接入后放 akshare 前面作为缓冲)
