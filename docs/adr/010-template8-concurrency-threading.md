---
adr: 010
date: 2026-07-05
status: accepted
---

# ADR 010: 模板 8 并发控制 — 条件约束 + 多源并行 I/O

## 背景

模板 8（并发控制）的全量要求（Celery/Redis/分布式锁/进程池/协程）对单人单机项目是过度设计。
但系统确有独立数据源的 I/O 并行化需求（JQData + Sina + akshare 各自不同的 API key 和 rate-limit 池）。

## 决策

### 模板 8 改为条件约束

当前单人单机 → 限定作用域。满足以下任一条件时立即全量启用：

1. 部署到多台机器 (>1 物理/虚拟服务器)
2. 多个并发用户 (>1 同时活跃)
3. 引入消息队列或分布式任务调度

当前模式用内存 Lock + SSE 广播 (state_broker.py) 替代 Redis。

### 多源并行 I/O 写入模板 5（性能基线）

`ThreadPoolExecutor` 并行化独立数据源的 HTTP 拉取。关键限制：

- 仅当数据源独立（不同 API key、不同 rate-limit 池）时有效
- 同一 API 多线程只会更快触发限流被拒，适得其反

## 落地

- `pipeline.py`: `_post_state()` 改为 fire-and-forget daemon 线程，不再阻塞 pipeline 步骤
- `execution/quote.py`: `fetch_quotes()` 分批并行 HTTP (持仓超 60 只时多批并发)
- `SKILL.md`: 模板 5 加「多源并行 I/O」规则，模板 8 加重构为条件约束 + 激活条款

## 后果

- pipeline 步骤间状态推送不再阻塞（每次省 ~200ms，累积约 1.2s）
- 持仓超 60 只时行情拉取 1.5-2x 加速
- 模板 8 在全量启用前不产生无谓的架构压力
