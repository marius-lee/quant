# Phase 5: 可观测性增强 — 深化执行计划

## 总体策略
深度集成 Prometheus/Grafana/Alerting，构建全栈可观测性平台：
- 指标采集标准化 (Prometheus)
- 仪表盘即代码
- 告警即代码
- 分布式追踪
- 日志聚合

---

## Phase 5 任务清单

| # | 任务 | 验收标准 | 预估工时 |
|---|------|----------|----------|
| 5.1 | **Prometheus 深度集成** - 全组件指标标准化、ServiceDiscovery、Remote Write | 所有组件暴露 `/metrics`，支持 ServiceDiscovery，支持 Remote Write 到 Thanos/Cortex | 3h |
| 5.2 | **Grafana 仪表盘即代码** - Jsonnet/Go 生成、版本控制、自动部署 | 所有仪表盘代码化，CI/CD 自动同步到 Grafana | 3h |
| 5.3 | **Alertmanager 告警路由** - 分组、抑制、静默、模板 | 完整告警生命周期，支持多租户 | 2h |
| 5.4 | **分布式追踪** - OpenTelemetry SDK、Jaeger/Tempo、Trace Context 传播 | 端到端追踪，P99 延迟可视化 | 3h |
| 5.5 | **日志聚合** - Loki + Promtail、结构化日志、LogQL | 全链路日志检索，关联 Trace/Metric | 2h |
| 5.6 | **SLO/SLI 体系** - SLI 定义、SLO 目标、Error Budget、Burn Rate 告警 | 业务级 SLO，Error Budget 告警 | 2h |

---

## 执行顺序
```
5.1 → 5.2 → 5.3 → 5.4 → 5.5 → 5.6
```

---

## 归档结构
```
docs/deepening/phase5/
├── PLAN.md                    # 本文件
├── 5.1_prometheus_integration.md
├── 5.2_grafana_dashboards.md
├── 5.3_alertmanager_routing.md
├── 5.4_distributed_tracing.md
├── 5.5_log_aggregation.md
├── 5.6_slo_sli.md
└── SUMMARY.md                 # 总结报告
```

---

## 立即开始：Phase 5.1 - Prometheus 深度集成