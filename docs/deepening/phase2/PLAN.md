# Phase 2: Dagster 编排深化 — 深化执行计划

## 总体策略
每个 Phase 产出：
1. **生产就绪代码** - 补全缺失功能、异常处理、边界条件
2. **集成测试** - 端到端验证
3. **性能基准** - 关键指标基线
4. **文档/ADR** - 设计决策记录

---

## Phase 2: Dagster 编排深化

| # | 任务 | 验收标准 | 预估工时 |
|---|------|----------|----------|
| 2.1 | ✅ **Asset 检查点** - 15 个 Asset 产出 `metadata` (duration_ms, rows/targets/sells/buys/saved/errors) | Dagster UI 可见完整物化信息，支持 `context.add_output_metadata()` | 2h |
| 2.2 | ✅ **分区动态生成** - `DynamicPartitionsDefinition` 从 `trade_calendar.json` 自动生成，非交易日自动跳过 | 无需手动维护，支持历史回填 | 2h |
| 2.3 | ✅ **Sensor 精确控制** - 基于 `get_trading_period()`/`is_market_open()` 精确控制，30秒间隔、午休暂停、精确启停 | 零延迟启停，午休自动暂停 | 1.5h |
| 2.4 | ✅ **资源依赖注入** - 4 个资源支持 `EnvVar` 多环境注入 (Dev/Staging/Prod) | 隔离配置，`EnvVar` 注入就绪 | 2h |
| 2.5 | ✅ **Run 级重试策略** - 日线 3次/10s指数退避、周度 2次/60s、错误码感知重试 | 瞬时失败自动恢复，永久失败快速失败 | 1.5h |
| 2.6 | ✅ **Dagster UI 部署** - Dockerfile 多阶段构建、Docker Compose 编排、启动脚本、环境变量管理 | 一键启动，UI 可视化全链路，生产就绪 | 1.5h |

---

## 执行顺序

```
2.1 → 2.2 → 2.3 → 2.4 → 2.5 → 2.6
```

每项完成：
1. 代码提交 + 单测通过
2. 运行集成测试验证
3. 记录 `docs/deepening/phase2/PHASE_2_ITEM_X.md` 归档文档
4. 更新本计划表 ✅

---

## 归档结构

```
docs/deepening/phase2/
├── PLAN.md                    # 本文件
├── 2.1_asset_checkpoints.md
├── 2.2_dynamic_partitions.md
├── 2.3_sensor_precision.md
├── 2.4_resource_di.md
├── 2.5_retry_policy.md
├── 2.6_dagster_deployment.md
└── SUMMARY.md                 # 总结报告
```

---

## 开始执行：Phase 2.1 Asset 检查点