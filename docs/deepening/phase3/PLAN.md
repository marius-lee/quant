# Phase 3: 分布式因子计算引擎 — 深化执行计划

## 总体策略
基于现有的 `quant/factor/distributed/` 模块，构建生产级的 Ray 分布式因子计算引擎，替代单进程 `factor_cache` 物化，目标将 4.6h 单进程物化压缩至 <1h。

---

## Phase 3 任务清单

| # | 任务 | 验收标准 | 预估工时 |
|---|------|----------|----------|
| 3.1 | ✅ **Ray Cluster 模式完善** - KubeRay CRD 支持、Actor 池、内存监控、自动分区策略 | 生产集群可横向扩展，支持 `mode: local/cluster/k8s` | 3h |
| 3.2 | ✅ **Actor 复用池** - `FactorStore` Actor 池化，避免每 Task 重复初始化 DB 连接 | 单 Task 开销 <50ms，吞吐提升 2x | 3h |
| 3.3 | 🔄 **分区策略自动选择** - 基于因子数/日期数/符号数自动选 `date`/`factor`/`composite` | 无需手动配置，自适应最优 | 2h |
| 3.4 | **增量物化** - 仅物化 `latest_cached_date+1` 到 `today`，跳过已缓存日期 | 日增量 <5min，全量回补可控 | 2h |
| 3.5 | **内存压力保护** - `ray.wait` 批量收集 + `object_store_memory` 监控 + OOM 自动重试 | 无 OOM Crash，大分区自动拆分 | 2h |
| 3.4 | **因子级重试隔离** - 单因子失败不影响其他，失败因子进入 `quarantine` 待人工 | 部分失败不阻塞整体 | 1.5h |
| 3.7 | **性能基准** - 104因子 × 5000股 × 1000天 完整物化 <1h (8核/32G) | 基准报告入库 | 1h |

---

## 执行顺序
```
3.1 → 3.2 → 3.3 → 3.4 → 3.5 → 3.6 → 3.7
```

每项完成：
1. 代码提交 + 单测通过
2. 运行集成测试验证
3. 记录 `docs/deepening/phase3/PHASE_3_ITEM_X.md` 归档文档
4. 更新本计划表 ✅

---

## 归档结构
```
docs/deepening/phase3/
├── PLAN.md                    # 本文件
├── 3.1_ray_cluster_mode.md
├── 3.2_actor_pool.md
├── 3.3_auto_partition.md
├── 3.4_incremental_materialize.md
├── 3.5_memory_protection.md
├── 3.6_factor_retry_isolation.md
├── 3.7_performance_benchmark.md
└── SUMMARY.md                 # 总结报告
```

---

## 现有代码基础
已存在模块：
- `quant/factor/distributed/engine.py` - `DistributedFactorEngine` 核心引擎
- `quant/factor/distributed/partitioner.py` - 4种分区策略
- `quant/factor/distributed/ray_config.py` - Ray 配置、Actor/Task 装饰器
- `quant/factor/distributed/aggregator.py` - 结果聚合
- `quant/factor/distributed/__init__.py` - 导出
- `quant/orchestrator/dagster_assets.py` - `factor_cache_distributed` Asset

需要完善/新增：
1. Ray Cluster 模式支持 (KubeRay CRD)
2. Actor 池化优化 (FactorStore 复用)
3. 分区策略自动选择逻辑
4. 增量物化逻辑 (仅物化新增日期)
5. 内存压力保护 (OOM 自动重试)
5. 因子级重试隔离 (quarantine 机制)
6. 性能基准测试脚本

---

## 立即开始：Phase 3.1 Ray Cluster 模式