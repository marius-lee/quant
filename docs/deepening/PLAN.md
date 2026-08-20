# 深化执行计划 — 逐项完成，完成后归档

## 总体策略
每个 Phase 产出：
1. **生产就绪代码** - 补全缺失功能、异常处理、边界条件
2. **集成测试** - 端到端验证
3. **性能基准** - 关键指标基线
4. **文档/ADR** - 设计决策记录

---

## Phase 1: 数据源抽象层深化

| # | 任务 | 验收标准 | 预估工时 |
|---|------|----------|----------|
| 1.1 | ✅ **TushareSource 完善** - 补充 `fund_flow`、`margin`、`lhb`、`northbound`、`dividend`、`income`、`balance`、`cashflow`、`index_daily` 等操作实现 | 所有 `operation` 类型可跑通，单测覆盖 ≥80% | 2h |
| 1.2 | ✅ **AkshareSource 完善** - 补充 `margin`、`lhb`、`index_daily`、`dividend`、`income`、`balance`、`cashflow`、`holder_trade`、`pledge` 等操作实现 | 东财源接口全覆盖，重试/限流生效 | 2h |
| 1.3 | ✅ **TickFlowSource 完善** - 补充 `level2`、`options`、`ticks`、`moneyflow` 等注册版高级操作 | 免费版/注册版自动切换，Level-2/期权/逐笔/资金流全覆盖 | 1.5h |
| 1.4 | ✅ **BaostockSource 深化** - 补充 12 个操作 + 实现 Akshare 自动回退机制 | 主源+回退源高可用，12个操作全覆盖 | 1h |
| 1.5 | ✅ **统一错误码体系** - 定义 `DataSourceErrorCode` 枚举 (26码+3决策方法) | 错误码可被上层识别并决策重试/降级/告警 | 1h |
| 1.6 | ✅ **Registry 自动发现 + 热重载** - 自动发现 BaseDataSource 子类、config.yaml 热重载、插件式扩展 | 配置变更无需重启，插件式扩展就绪 | 1h |
| 1.7 | **集成测试** - `test_data_sources_integration.py` 覆盖 6 源 × 主要操作 | CI 绿灯，无 Flaky | 1h |

---

## Phase 2: Dagster 编排深化

| # | 任务 | 验收标准 | 预估工时 |
|---|------|----------|----------|
| 2.1 | **Asset 检查点** - 每个 Asset 产出 `metadata` (rows, duration, freshness) | Dagster UI 可见完整物化信息 | 1h |
| 2.2 | **分区动态生成** - 交易日分区基于 `trade_calendar.json` 自动计算，非交易日跳过 | 无需手动维护分区定义 | 1h |
| 2.3 | **Sensor 精确控制** - `monitor_sensor` 基于实时行情时间窗 (09:30/15:00) 精确启停 | 盘中风控守护进程零延迟启停 | 1.5h |
| 2.4 | **资源依赖注入** - `DataSourceRegistry`/`FactorStore`/`TradeRepo` 资源化，支持多环境 | Dev/Staging/Prod 隔离配置 | 1h |
| 2.5 | **Run 级重试策略** - 任务级 `RetryPolicy` (指数退避、最大重试、特定错误码重试) | 瞬时失败自动恢复，永久失败快速失败 | 1h |
| 2.6 | **Dagster UI 部署** - Docker Compose 编排 `dagster-webserver` + `dagster-daemon` + PostgreSQL | 一键启动，UI 可视化全链路 | 1h |

---

## Phase 3: 分布式因子计算深化

| # | 任务 | 验收标准 | 预估工时 |
|---|------|----------|----------|
| 3.1 | **Ray Cluster 模式** - 支持 `ray start --head` / `ray start --address`，KubeRay CRD 部署 | 生产集群可横向扩展 | 2h |
| 3.2 | **Actor 复用池** - `FactorStore` Actor 池化，避免每 Task 重复初始化 DB 连接 | 单 Task 开销 <50ms，吞吐提升 2x | 2h |
| 3.3 | **分区策略自动选择** - 基于因子数/日期数/符号数自动选 `date`/`factor`/`composite` | 无需手动配置，自适应最优 | 1.5h |
| 3.4 | **增量物化** - 仅物化 `latest_cached_date+1` 到 `today`，跳过已缓存日期 | 日增量 <5min，全量回补可控 | 1.5h |
| 3.5 | **内存压力保护** - `ray.wait` 批量收集 + `object_store_memory` 监控 + OOM 自动重试 | 无 OOM Crash，大分区自动拆分 | 1h |
| 3.6 | **因子级重试隔离** - 单因子失败不影响其他，失败因子进入 `quarantine` 待人工 | 部分失败不阻塞整体 | 1h |
| 3.7 | **性能基准** - 104因子 × 5000股 × 1000天 完整物化 <1h (8核/32G) | 基准报告入库 | 1h |

---

## Phase 4: CDC 增量同步深化

| # | 任务 | 验收标准 | 预估工时 |
|---|------|----------|----------|
| 4.1 | **触发器自动化管理** - `CREATE TRIGGER` 幂等部署，Schema 变更自动感知重建 | `ALTER TABLE` 后自动同步触发器 | 1.5h |
| 4.2 | **LSN 精确语义** - `transaction_id` 分组，同一事务多表变更原子应用到 DuckDB | 多表事务一致性 | 1h |
| 4.3 | **DuckDB 并行 UPSERT** - 批量 `COPY` + `ON CONFLICT`，利用列存并行写入 | 同步延迟 <2s (千行级) | 1.5h |
| 4.4 | **因子缓存精确失效** - CDC 事件 → 受影响因子列表 → 仅失效相关分区 | 无全量重算，精确增量 | 2h |
| 4.5 | **数据校验规则扩展** - 20+ 规则覆盖 (PK/FK/范围/分布/跨表一致性) | 校验通过率 100%，失败自动告警 | 1h |
| 4.6 | **CDC 监控大盘** - 捕获延迟/同步延迟/积压/错误率 实时图表 | Grafana 可视化，SLA 告警 | 1h |

---

## Phase 5: 可观测性深化

| # | 任务 | 验收标准 | 预估工时 |
|---|------|----------|----------|
| 5.1 | **OpenTelemetry 依赖锁定** - `opentelemetry-instrument` 自动埋点 Flask/requests/sqlite3 | 零代码侵入，全链路追踪 | 1h |
| 5.2 | **Exemplars 关联** - 指标附带 `trace_id`，Grafana 点击跳转到 Trace | 指标-日志-追踪三位一体 | 1h |
| 5.3 | **告警规则库** - 30+ 预置规则 (PnL/回撤/数据延迟/同步失败/资源耗尽) | 告警噪音 <5%/天，覆盖率 100% | 2h |
| 5.4 | **多渠道通知模板** - 企微/钉钉/飞书/Slack/Telegram/Email 统一模板 | 渠道新增 <10min | 1h |
| 5.5 | **SLO/SLI 仪表盘** - 可用性/延迟/数据新鲜度/同步延迟 SLO 可视化 | 业务级 SLO 监控 | 1h |
| 5.6 | **归档策略** - 指标/日志/追踪分级留存 (热/温/冷)，成本优化 | 存储成本 <预算 50% | 1h |

---

## 执行顺序

```
Phase 1.1 → 1.2 → 1.3 → 1.4 → 1.5 → 1.6 → 1.7
    ↓
Phase 2.1 → 2.2 → 2.3 → 2.4 → 2.5 → 2.6
    ↓
Phase 3.1 → 3.2 → 3.3 → 3.4 → 3.5 → 3.6 → 3.7
    ↓
Phase 4.1 → 4.2 → 4.3 → 4.4 → 4.5 → 4.6
    ↓
Phase 5.1 → 5.2 → 5.3 → 5.4 → 5.5 → 5.6
```

每项完成：
1. 代码提交 + 单测通过
2. 运行集成测试验证
3. 记录 `docs/deepening/PHASE_X_ITEM_Y.md` 归档文档
4. 更新本计划表 ✅

---

## 归档结构

```
docs/deepening/
├── PLAN.md                    # 本文件
├── phase1/
│   ├── 1.1_tushare_source.md
│   ├── 1.2_akshare_source.md
│   └── ...
├── phase2/
│   ├── 2.1_asset_checkpoints.md
│   └── ...
├── phase3/
├── phase4/
├── phase5/
└── SUMMARY.md                 # 总结报告
```

---

开始执行：**Phase 1.1 TushareSource 完善**