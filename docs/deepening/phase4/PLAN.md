# Phase 4: CDC 增量同步架构 — 深化执行计划

## 总体策略
构建生产级 CDC (Change Data Capture) 增量同步引擎，实现 SQLite → DuckDB 的毫秒级增量同步，支持多表、Schema 演进、Exactly-once 语义。

---

## Phase 4 任务清单

| # | 任务 | 验收标准 | 预估工时 |
|---|------|----------|----------|
| 4.1 | ✅ **WAL 监听器** - WALListener + TriggerBasedListener 变更捕获 | 毫秒级捕获 INSERT/UPDATE/DELETE，支持 WAL/Trigger 双模式 | 3h |
| 4.2 | ✅ **Schema 演进处理** - SchemaEvolutionManager 自动 ALTER TABLE/ADD/DROP/RENAME | 自动应用，向后兼容 | 2h |
| 4.3 | ✅ **Exactly-once 语义** - IncrementalSyncer 幂等写入 + 位点管理 + 事务边界 | 重复运行不重复，崩溃可恢复 | 3h |
| 4.4 | ✅ **多表同步编排** - CDCSyncerOrchestrator 拓扑排序、并行/串行、失败重试 | FK 依赖自动解析，并行度自适应 | 2h |
| 4.5 | ✅ **增量同步性能优化** - VectorizedUpserter 列式批量 UPSERT + 列裁剪 + 预编译语句缓存 | 百万行/秒吞吐，延迟 <100ms | 2h |
| 4.6 | ✅ **CDC 监控与告警** - CDCMonitor 实时指标 + 告警规则引擎 + Prometheus 导出 | P99 延迟 <1s，积压/错误率/资源告警 | 1.5h |

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