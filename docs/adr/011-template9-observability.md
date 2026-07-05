---
adr: 011
date: 2026-07-05
status: accepted
---

# ADR 011: 模板 9 可观测性 — 双级实现

## 背景

系统有 35 个模块使用 logger、174 处打点，但均为 ad-hoc print-style 日志：
- 无 trace_id：不同模块的日志无法关联到同一次 pipeline 调用
- 无结构化：无法用 `jq` 按模块/级别/耗时过滤分析
- 无指标：不知道 pipeline 成功率、耗时趋势、API 调用量
- 无告警：出错后依赖人工检查日志才发现

模板 9 原样要求 OpenTelemetry + Prometheus + Jaeger + Grafana，对单人单机过度。

## 决策

**模板 9 保留硬约束，双级实现：**

### T1 (当前)
| 能力 | 实现 |
|------|------|
| 日志 | JSON 结构化文件 + trace_id (contextvars) |
| 指标 | 内存 Metrics 类 (Counter/Gauge) + SQLite 落盘 + /api/metrics |
| 追踪 | trace_id 注入所有日志行，一次 pipeline 一个 ID |
| 健康 | /api/health (DB + pipeline 最近状态 + 告警) |
| 告警 | 3 条规则，scheduler 评估后 broker→SSE 推前端 |
| 依赖 | 零新增 |

### T2 (多人多机，同模板 8 激活)
Prometheus/Grafana/OpenTelemetry/Jaeger 完整栈。

## 落地

- `utils/logger.py`: `_JsonFormatter` 文件日志, `set_trace_id`/`get_trace_id` contextvars, `_TraceLoggerAdapter` 自动注入
- `monitor/metrics.py`: 新建 — 线程安全计数/gauge, `persist()` 落盘
- `monitor/alerts.py`: 新建 — 回撤/数据滞后/pipeline 失败 3 条规则, SSE 推送
- `pipeline.py`: trace_id 生成, 每步 metrics 计数, 耗时 gauge, _post_state 携带 trace_id
- `web/app.py`: `/api/health` + `/api/metrics` 端点

## 后果

- 同一次 pipeline 运行的所有日志可用 `grep trace_id=xxx logs/quant.log` 拉出完整链路
- `/api/health` 提供一站式系统状态检查 (替代 ad-hoc `curl`/`sqlite3`)
- 告警通过 SSE 实时推送到前端顶部 (无需手动查看日志)
