# Phase 9: 生产级部署验证 — 深化执行计划

## 总体策略
将前 8 个 Phase 的所有组件打包为生产就绪的部署制品：
- 容器化编排：Docker Compose / K8s Helm Charts / Kustomize
- CI/CD 流水线：构建、测试、扫描、部署、回滚
- 可观测性运维：SLO/SLI 告警、Runbook、On-call 轮值
- 安全加固：镜像扫描、运行时安全、密钥管理、网络策略
- 性能基准：全链路压测、容量规划、弹性伸缩验证
- 灾难恢复：备份/恢复演练、RPO/RTO 验证、混沌工程

---

## Phase 9 任务清单

| # | 任务 | 验收标准 | 预估工时 |
|---|------|----------|----------|
| 9.1 | ✅ **Docker 生产镜像构建** - 多阶段构建、非 root、最小基础镜像、SBOM 生成 | 镜像 <500MB、无高危 CVE、可复现构建 | 3h |
| 9.2 | ✅ **K8s Helm Chart 编排** - 所有微服务 Chart、依赖管理、Values 环境差异化 | `helm install` 一键部署、Dev/Staging/Prod 隔离 | 4h |
| 9.3 | ✅ **CI/CD 流水线** - GitHub Actions/GitLab CI、多阶段、自动化测试、镜像推送 | PR 检查 <10min、主分支自动部署 Staging | 3h |
| 9.4 | ✅ **健康检查与就绪探针** - Liveness/Readiness/Startup、依赖检查、优雅关闭 | 启动 <30s、故障自愈 <60s、零停机部署 | 2h |
| 9.5 | ✅ **SLO/SLI 运维大盘** - 可用性/延迟/数据新鲜度/同步延迟 SLO、Burn Rate 告警 | 业务级 SLO 可视化、多窗口告警 (1h/6h/24h) | 3h |
| 9.6 | ✅ **Runbook 与 On-call** - 故障处理手册、升级/回滚/扩容/证书轮换 SOP、On-call 轮值 | 核心故障 <15min 定位、<30min 恢复 | 2h |
| 9.7 | ✅ **安全加固** - 镜像签名验证、运行时 Falco、网络策略、密钥轮换、最小权限 | 通过 CIS Kubernetes Benchmark、零高危漏洞 | 3h |
| 9.8 | ✅ **全链路性能压测** - 数据摄入/因子计算/风控/下单全链路、容量规划、弹性伸缩验证 | 104因子×5000股×1000天 <1h、P99 延迟达标 | 4h |
| 9.9 | ✅ **灾难恢复演练** - ETCD/PostgreSQL/DuckDB 备份恢复、RPO<5min/RTO<30min、混沌工程 | 季度演练通过、自动化恢复脚本 | 3h |

---

## 执行顺序
```
9.1 → 9.2 → 9.3 → 9.4 → 9.5 → 9.6 → 9.7 → 9.8 → 9.9
```

---

## 归档结构
```
docs/deepening/phase9/
├── PLAN.md                    # 本文件
├── 9.1_docker_images.md
├── 9.2_helm_charts.md
├── 9.3_ci_cd_pipeline.md
├── 9.4_health_probes.md
├── 9.5_slo_sli_dashboard.md
├── 9.6_runbook_oncall.md
├── 9.7_security_hardening.md
├── 9.8_performance_benchmark.md
├── 9.9_disaster_recovery.md
└── SUMMARY.md
```

---

## 立即开始：Phase 9.1 - Docker 生产镜像构建
