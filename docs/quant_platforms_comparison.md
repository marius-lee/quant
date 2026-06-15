# 优秀量化软件平台对比

日期: 2026-06-05

## 一、国际平台

### QuantConnect / LEAN Engine
| 维度 | 说明 |
|------|------|
| 架构 | 云端 + 开源 LEAN 引擎 (C#核心, Python/C#/F#策略) |
| 业务逻辑 | **Alpha → Portfolio → Execution → Risk** 五模块分离 |
| 因子管道 | Universe Selection → Alpha Creation → Portfolio Construction → Execution |
| 回测 | 事件驱动, 50万次/月, 建模滑点/佣金/分红 |
| 部署 | 云端 + Docker本地 + 混合 |
| 费用 | 免费~$80/月 |

**核心设计模式**: 信号生成、组合构建、执行、风控完全解耦, 每层可独立测试和替换。

### WorldQuant Brain / Alpha Factory

**7 阶段管道**:
1. Universe & Data → 2. Alpha Mining (公式组合生成) → 3. Backtest & Screen (OOS优先) → 4. Alpha Combiner (Meta-Model) → 5. Portfolio → 6. Execution & TCA → 7. 组织扩展

**算子库**: rank, ts_rank, ts_zscore, ts_mean, group_neutralize, correlation, decay_linear 等 20+ 算子, 模板组合生成海量 alpha。

**2024-2025 前沿**: LLM Multi-Agent 架构 (BrainAlpha), 660-cell 探索网格 + RAG + Jaccard 多样性门控。

### QuantRocket / Moonshot
- Docker 部署, 深度 Interactive Brokers 集成
- Moonshot: 向量化回测, factor-driven pipeline
- 无代码/低代码选项

## 二、开源框架

| 框架 | 语言 | Star | 优势 | 劣势 | 状态 |
|------|------|:--:|------|------|:--:|
| **Backtrader** | Python | 20.7k | 优雅的事件驱动, 120+指标 | 已停更, 实盘需自对接 | ⚠️ |
| **vnpy** | Python+C++ | 37.4k | 唯一回测+实盘全链路, 20+交易接口 | 学习曲线极陡 | ✅ 活跃 |
| **Zipline** | Python+Cython | 17k | 学术级严谨, Pipeline API | 不支持A股, 无实盘 | ⚠️ |
| **NautilusTrader** | Rust+Python | 新兴 | 生产级, AI-first | 较新, 生态小 | ✅ |
| **QLib (微软)** | Python | 38.5k | AutoML集成, 因子表达引擎 | 侧重AI, 非全栈 | ✅ |

### 架构设计模式 (2024 共识)

```
数据层 → 因子计算引擎 → 因子数据库(DuckDB/Parquet) → IC筛选 → ML模型 → 组合构建 → 执行
```

关键分离:
- **研究 vs 生产**: 统一事件驱动引擎, 同一策略代码用于回测和实盘
- **因子计算 vs 归一化**: 计算独立于横截面操作
- **OMS vs EMS**: 订单管理与执行管理分离

## 三、A股平台

| 平台 | 定位 | 特色 | 实盘 |
|------|------|------|:--:|
| **聚宽** | 社区驱动 | 50万用户, 10万+策略 | ❌ 2024.12终止 |
| **掘金** | 机构级本地 | C++ 50μs延迟, 17家券商 | ✅ |
| **BigQuant** | AI原生 | AutoML, 可视化AI | ✅ |
| **米筐** | 数据+投研 | RQData数据质量最优 | ⚠️ 期货 |
| **优矿** | 因子研究 | 400+因子库, 通联数据 | ❌ |
| **QMT** | 券商本地 | 极速柜台, VS Code/PyCharm | ✅ |
| **PTrade** | 券商云端 | 云端托管, TWAP/VWAP | ✅ |

## 四、对我们系统的启示

| 优秀实践 | 我们的现状 | 差距 |
|------|------|:--:|
| Alpha→Portfolio→Risk→Execution 分层 | 全部耦合在 engine/ 目录 | 🔴 |
| 因子计算与归一化分离 | V2 已实现 | ✅ |
| Pipeline API (Zipline) | 无声明式因子定义 | 🟡 |
| Factor Database (DuckDB/Parquet) | SQLite 存因子 | 🟡 |
| OOS-first 评估 (WorldQuant) | 70/30 单次分割 | 🟡 |
| 事件驱动回测 (QuantConnect) | 向量化为主 | 🟡 |
| Purged K-Fold CV (López de Prado) | 无 | 🔴 |
| 研究→生产统一引擎 | 研究脚本分散 | 🔴 |

## 来源

- [Jonathan Kinlay: Comprehensive Comparison of Algorithmic Trading Platforms (2025)](https://jonathankinlay.com/2025/06/comprehensive-comparison-of-algorithmic-trading-platforms/)
- [WeChat: LEAN Engine Architecture](http://mp.weixin.qq.com/s?__biz=MzI4MDA4MzQzMg==)
- [DeepWiki: WorldQuant Brain Alpha](https://deepwiki.com/yhyyds666/worldquant-brain-alpha)
- [WeChat: A股量化平台对比](http://mp.weixin.qq.com/s?__biz=MzE5MTIxMDAxOQ==)
- [WeChat: 开源量化框架测评](http://mp.weixin.qq.com/s?__biz=MzU4MzcyNzc3Nw==)
