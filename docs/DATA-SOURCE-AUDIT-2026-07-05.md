---
document: DATA-SOURCE-AUDIT
version: 1.0
date: 2026-07-05
status: snapshot
---
# 数据源全面审查 — A股量化选股系统

**审查日期**: 2026-07-05  CST
**审查范围**: 所有 9 个数据源
**Python 环境**: `.venv` (Py3.14) 和 `.venv-tushare` (Py3.12)


**凭证存储**: 详见 `config/env.example` 模板。实际凭证存放于 `config/.env`（在 `.gitignore` 中，不提交 git）。
## 双环境包分布

| 包 | .venv (Py3.14) | .venv-tushare (Py3.12) |
|----|:--:|:--:|
| akshare 1.18.64 | ✅ | ❌ |
| pytdx 1.72 | ✅ | ❌ |
| PyYAML 6.0.3 | ✅ | ❌ |
| redis 8.0.1 + hiredis 3.4.0 | ✅ | ❌ |
| tushare 1.4.29 | ❌ | ✅ |
| jqdatasdk 1.9.8 | ❌ | ✅ |
| baostock 0.9.2 | ✅ | ✅ |
| msgpack 1.2.1 | ✅ | ✅ |
| tickflow | ❌ (not in reqs) | ❌ |
| zzshare | ❌ (not installed) | ❌ |

**结论**: 数据拉取需要两个环境——akshare/pytdx 跑 Py3.14，tushare/JQData 跑 Py3.12。跨环境调用需用 `subprocess` 或明确指定 venv 路径。

---

## 逐源审查

### 1. akshare (东方财富, 单机免费)
**包**: akshare 1.18.64 → `.venv` Py3.14
**速度**: 逐只, ~200ms/只, 5000只/天≈17min/日
**接口**:

| 函数 | 用途 | 状态 | 备注 |
|------|------|:--:|------|
| stock_info_a_code_name | 全A股列表 | ✅ | 5,525 只 |
| stock_zh_a_hist (adjust="qfq") | OHLCV 日线 | ✅ | 唯一提供历史换手率的源 |
| stock_value_em | PE/PB/总市值 | ✅ | 逐只, 200ms 间隔 |
| stock_lhb_detail_em | 龙虎榜 | ✅ | 25,490 行 |
| stock_hsgt_individual_em | 北向资金 | ⚠️ | 截止 2024-08 (API 硬截断) |
| stock_margin_detail_sse/szse | 融资融券 | ✅ | 478,632 行 |
| stock_individual_fund_flow | 资金流向 | ❌ | **IP 被东方财富封** |
| stock_dzjy_mrmx/mrtj | 大宗交易 | ❌ | API KeyError / NoneType |
| stock_shareholder_change_ths | 股东增减持 | ❌ | 数据太少 (14条/只) |

**已知问题**:
- `adjust=""` bug: 旧代码未复权 → 已修 (P3), 当前代码用 `adjust="qfq"`
- IP 封禁风险: 东方财富 API 敏感, 历史上有资金流向被封经历 (2026-07-03)
- 依赖单一源: 是行业分类、PE/PB、LHB、融资融券的主力源
- rate limit: 60 calls/min (已通过 Redis RateLimiter 管控)

**数据库行数**: daily=7.35M, stocks=5,525, margin_detail=478K, lhb=25K

### 2. tencent (腾讯财经)
**包**: 无 → HTTP API, 任意 Py 版本
**速度**: 批量 (URL 支持多日期), 约 0.3-0.5s/请求

| 功能 | URL | 状态 |
|------|-----|:--:|
| OHLCV 日线 (qfq 前复权) | web.ifzq.gtimg.cn | ✅ |

**优势**:
- 免费 + 无注册
- qfq 前复权, 无需手动复权
- 稳定, 是当前回退链的主源

**缺点**:
- 无换手率数据 → 换手率依赖 akshare
- vol/100→手, amt/1000→千元 → 需在代码中转换

### 3. pytdx (通达信)
**包**: pytdx 1.72 → `.venv` Py3.14
**速度**: 二进制协议, 0.8-1.2s/批次
**服务器**: 180.153.18.170:7709

| 功能 | 状态 | 备注 |
|------|:--:|------|
| OHLCV 日线 | ⚠️ 偶发不可达 | manual qfq adjustment |

**当前在回退链第一位** (via `_source_speed` 排序):
- 优点: 批量快, vol=手, 免费
- 缺点: 无换手率, 服务器不在我们控制范围内
- 手动 qfq 调整实现: L553-592

### 4. baostock (证券宝)
**包**: baostock 0.9.2 → 两环境均有
**测试结果**: ❌ **两版本均不可用**

| Py 版本 | 错误 |
|---------|------|
| Py3.14 | `[Errno 57] Socket is not connected` |
| Py3.12 | `[Errno 57] Socket is not connected` |

**影响范围**:
- `data/daily_basic.py` → DEPRECATED (P24), 已从 pipeline 移除
- `store.py::sync_industry()` → 主源失败, 回退 akshare
- `store.py::backfill_turnover()` → 已改用 akshare

**结论**: **baostock 服务器已不可用**。所有 baostock 路径均需走 akshare 回退。

### 5. tushare
**包**: tushare 1.4.29 → `.venv-tushare` Py3.12
**凭证**: TUSHARE_TOKEN 环境变量（当前未设置）
**免费 tier limit**: 200 calls/min

| 函数 | 用途 | 状态 |
|------|------|:--:|
| stock_basic | 股票列表 | 需 token |
| pro.daily (ts_code batch) | OHLCV 日线 | 需 token |
| daily_basic | PE/PB/换手率 | 需 token |

**当前在代码中的位置**:
- `store.py::sync_stock_list()` — 主源 (优先于 akshare), 但 L879 注释说 "有 token 时作为备源"
- `store.py::_fetch_batch_tushare()` — **有实现但不在 all_sources 列表中**
- `store.py::sync_fundamentals()` — 委托给 `data/fundamental.py`

**问题**:
1. Token 未在环境中 → 当前不可用
2. `_fetch_batch_tushare()` 存在但从未被调用 (不在 all_sources 中)
3. 注释 "zzshare 主源 → tushare(tokened) → 腾讯财经 → akshare 兜底" 与代码不一致
4. L900 `if source == "tushare"` 路径永不触发

### 6. JQData (聚宽)
**包**: jqdatasdk 1.9.8 → `.venv-tushare` Py3.12
**凭证**: JQDATA_USER + JQDATA_PASS 环境变量（当前未设置）
**Trial**: 2025-03-26 ~ 2026-04-02 (已过期)

| 功能 | 状态 | 数据行数 |
|------|:--:|------|
| daily valuation (PE/PB/PS/PCF) | 已停 (trial expired) | 419,899 行 |
| financial_income/balance/cash_flow | 已停 (trial expired) | 21,697 行 |
| analyst_forecast | 已停 | 2,358 行 |

**数据真空**: 2026-04-03 起 PE/PB 回退到 `stocks` 表静态快照。

### 7. sina (新浪财经)
**包**: 无 → HTTP API
**状态**: ⚠️ **P3 从回退链中移除**

**原因**: 返回未复权数据，除权日单日跳-34%
代码 `_fetch_sina_daily()` 仍然存在 (L366-393) 但不在 `all_sources` 列表中

### 8. tickflow
**包**: NOT INSTALLED
**requirements.txt**: `tickflow>=0.1`

`_fetch_tickflow_daily()` 代码存在 (L490-531) 但未安装包。`all_sources` 列表中不包含 tickflow。

### 9. zzshare
**包**: NOT INSTALLED

`_fetch_zzshare_daily()` 代码存在 (L463-487) 但不在 `requirements.txt`，也未安装。注释说 "主源" 但实际不参与。

---

## 实际生效的 daily OHLCV 回退链

**代码 (L857-859)**:
```
pytdx → tencent → akshare
```
按 speed EMA 动态排序。tushare, sina, tickflow, zzshare 不在链中。

**注释 (L793)**: "zzshare 主源 → tushare(tokened) → 腾讯财经 → akshare 兜底"

**结论**: 注释与代码不一致 — 属于僵尸文档。

---

## 各数据表来源总结

| 表 | 行数 | 日期范围 | 当前源 | 状态 |
|----|------|----------|--------|:--:|
| daily | 7,348,020 | 2020-01 ~ 2026-07 | pytdx→tencent→akshare | ✅ |
| stocks | 5,525 | — | akshare | ✅ |
| daily_valuation | 419,899 | 2025-12 ~ 2026-04 | JQData (已停) | ⚠️ 有真空 |
| financial_{income,balance,cash} | 21,697 | — | JQData (已停) | ⚠️ 无新数据 |
| northbound_flow | 579,161 | ~2024-08 | akshare (已截断) | ⚠️ 有真空 |
| lhb_detail | 25,490 | 2025-01 ~ 2026-07 | akshare | ✅ |
| margin_detail | 478,632 | 2026-01 ~ 2026-07 | akshare wrapper | ✅ |
| limit_up_pool | 1,249 | ~1 月 | akshare | ✅ |
| fund_hold | 23,051 | — | akshare | ✅ |
| analyst_forecast | 2,358 | — | JQData (已停) | ⚠️ |
| benchmark_daily | 1,574 | — | Sina | ⚠️ DNS偶发失败 |

---

## 风险评估

| 风险 | 严重度 | 说明 |
|------|:--:|------|
| **akshare 是单点故障** | 高 | 行业/PE/PB/LHB/北向/融资融券 全部依赖 akshare。IP 被封 = 多条数据链全断 |
| **PE/PB 3 个月真空** | 高 | EP/BP/ROE 因子依赖的估值数据停在 2026-04-02 |
| **tushare 代码存在但未接入** | 中 | `_fetch_batch_tushare()` 可做第二 OHLCV 源，但不在 all_sources 中 |
| **僵尸代码** | 低 | sina/tickflow/zzshare/tushare 的 fetch 函数存在但未用 |
| **dual-venv 复杂性** | 低 | 数据同步需要两个 Python 环境，运维文档缺失 |

---

## 优先级建议

1. **P0 — 补 PE/PB 真空**: JQData 不能再用了，需要替代估值源。Tushare 的 `daily_basic` 接口或 akshare 的 `stock_value_em`。两种都需要凭证或 IP 白名单。
2. **P1 — 将 tushare 接入 daily 回退链**: 有 token 时，`_fetch_batch_tushare` 应作为第二源，给 akshare 减负。
3. **P2 — 清理僵尸代码**: 移除 sina/tickflow/zzshare 的未用 fetch 函数，或接入 `all_sources`。
4. **P3 — 为 akshare 添加第二个源**: 至少行业分类和 PE/PB 应该有备选方案。
