---
adr: 008
date: 2026-07-05
status: accepted
---

# ADR 008: 前端实时数据架构 v2 — SSE + 5s 轮询

## 背景

前端通过 `setInterval(pollOverview, POLL_MS)` 轮询拉取 `/api/state` + `/api/performance`。
历史残留 `POLL_MS = 15000` (15s)，而系统已实现 Sina 5s 级别的实时行情拉取 (`/api/quotes`)。

后端 SSE `/api/stream` 已通过 `web/state_broker.py` 实现，但前端完全不消费 SSE 推送。

## 决策

**SSE 推送 (primary) + 5s 轮询 (fallback)**：

- `POLL_MS`: 15000 → 5000
- `connectSSE()`: EventSource 消费 `/api/stream`，收到推送立即更新 `renderSignals(state)` + `updateNavStatus(state)`，跳过轮询等待
- `setInterval(pollOverview, POLL_MS)` 保留: SSE 断连时的 fallback + 负责 performance/KPI 数据更新
- SSE 断连: 指数退避重连 (5s → 10s → 20s → 30s max)

### 数据职责划分

| 数据 | 推送方式 | 原因 |
|------|---------|------|
| state (signals, status, progress) | SSE | pipeline 触发 state 变更，推送比轮询更快 |
| performance (KPIs, 估值) | 5s 轮询 | 交易时段市值随股价波动，需要持续更新 |
| quotes (实时行情) | 按需 `/api/quotes` | 仅在 portfolio tab 加载时请求，Sina 数据源 |

## 后果

- 状态变更可见延迟: 15s → <1s (SSE) 或 5s (fallback)
- KPI 刷新间隔: 15s → 5s (交易时段更精确的盈亏显示)
- 前端代码增加 25 行 SSE 逻辑，复杂度可控
