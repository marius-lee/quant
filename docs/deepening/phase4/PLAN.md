# Phase 4: CDC 增量同步架构 — 深化执行计划

## 总体策略
构建生产级 CDC (Change Data Capture) 增量同步引擎，实现 SQLite → DuckDB 的毫秒级增量同步，支持多表、Schema 演进、Exactly-once 语义。

---

## Phase 4 任务清单

| # | 任务 | 验收标准 | 预估工时 |
|---|------|----------|----------|
| 4.1 | **WAL 监听器** - 基于 SQLite WAL 模式的实时变更捕获 | 毫秒级捕获 INSERT/UPDATE/DELETE，零丢失 | 3h |
| 4.2 | **Schema 演进处理** - 自动检测并应用 Schema 变更 | ALTER TABLE 自动应用，向后兼容 | 2h |
| 4.3 | **Exactly-once 语义** - 幂等写入 + 位点管理 + 事务边界 | 重复运行不重复数据，崩溃可恢复 | 3h |
| 4.4 | **多表同步编排** - 依赖感知的拓扑排序同步 | FK 依赖自动解析，并行度自适应 | 2h |
| 4.5 | **增量同步性能优化** - 批量 UPSERT + 列裁剪 + 向量化 | 百万行/秒吞吐，延迟 <100ms | 2h |
| 4.6 | **CDC 监控与告警** - 延迟/积压/错误率实时监控 | P99 延迟 <1s，积压告警 | 1.5h |

---

## 执行顺序
```
4.1 → 4.2 → 4.3 → 4.4 → 4.5 → 4.6
```

---

## 归档结构
```
docs/deepening/phase4/
├── PLAN.md                    # 本文件
├── 4.1_wal_listener.md
├── 4.2_schema_evolution.md
├── 4.3_exactly_once.md
├── 4.4_multi_table_orchestration.md
├── 4.5_performance_optimization.md
├── 4.6_monitoring_alerting.md
└── SUMMARY.md                 # 总结报告
```

---

## 立即开始：Phase 4.1 - WAL 监听器