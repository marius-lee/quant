---
adr: 14
status: accepted
date: 2026-07-05
---
# ADR 014: Redis 缓存后端 — API 去重层与分布式限流

## Context

系统涉及多个外部数据源 (JQData, Tushare, baostock, akshare)，每个都有速率限制或网络不稳定性:

- JQData trial 已于 2026-04-02 结束
- baostock 服务不可用 (Socket error)
- Tushare 免费接口 200 calls/min 配额
- akshare 无限制但偶发不稳定

原计划 `data/cache.py` 使用文件系统缓存，分析中暴露出以下局限:

1. 限流不是分布式的 — 多机部署时进程 A 不知道进程 B 的配额消耗
2. 同步无幂等锁 — 多进程可能同时对同一天触发 `jq_valuation.sync_date()`
3. TTL 需要手动清理 — 文件缓存的过期数据残留
4. 无可观测性 — 文件缓存命中率需要手写计数器

## Decision
## Decision

**采用 Redis 作为缓存后端**，职责严格限定为 **API 去重 + 分布式限流**。消费者 (pipeline/web) 不从 Redis 读数据，只从 SQLite 读。

### 集成接入点

| 文件 | 函数 | 缓存策略 | 限流策略 |
|------|------|---------|---------|
| `data/jq_valuation.py` | `sync_date()` | DataCache (4h TTL, key=date) | RateLimiter (30/min) |
| `data/store.py` | `sync_stock_list()` | DataCache (24h TTL, key="symbols") | RateLimiter (200/min, tushare) |
| `data/store.py` | `sync_industry()` | DataCache (24h TTL, key="mapping") | — (baostock/akshare fallback) |
| `data/store.py` | `_fetch_batch_tushare()` | — | RateLimiter (200/min) |
| `data/store.py` | `_fetch_akshare_daily()` | — | RateLimiter (60/min) |

每个模块通过 `_init_cache()` 懒初始化 backend/cache/limiter，Redis 不可达时自动降级为 NoopBackend。

### 架构

```
              Redis (API dedup layer)
              cache:{ns}:{key}   msgpack, EX
              lock:{ns}:{key}    NX EX
              ratelimit:{ns}     SORTED SET
                         |
                         | miss -> API call
  jq_valuation           |              JQData
  store.py               |              Tushare
                         |              akshare
         | write         |
         v
  SQLite market.db  <-- pipeline / web 只从这里读
```

### Redis Key Schema

| Pattern | 类型 | TTL | 用途 |
|---------|------|-----|------|
| quant:cache:{ns}:{key} | STRING (msgpack) | EX 4h | API 响应缓存 |
| quant:lock:{ns}:{key} | STRING (NX) | EX 300s | 同步幂等锁 |
| quant:ratelimit:{ns} | SORTED SET | EX 3x window | 滑动窗口限流 |

### 组件设计

- CacheBackend (ABC) — 抽象接口: get/set/delete/rate_limit_acquire/lock_acquire/lock_release/ping
- RedisBackend — hiredis C 扩展, decode_responses=False (存 msgpack bytes)
- NoopBackend — Redis 不可达时降级: 无缓存、不限流 (fail-open)
- RateLimiter — 基于 SORTED SET 滑动窗口 + 本地令牌桶 (降级用)
- DataCache — 基于 backend 的 API 响应缓存 (msgpack 序列化)
- RetryConfig / with_fallback — 保持原样 (无状态逻辑)

### 降级路径

`get_backend(config)`:
1. Redis 可 ping 通 -> RedisBackend
2. Redis 不可达 -> NoopBackend (fail-open)
3. 运行时 Redis 断开 -> 自动降级, 定期重试恢复

NoopBackend 下:
- 缓存永远 miss -> API 可能被重复调用 (不影响正确性)
- 限流永远放行 -> 可能触发 API rate-limit (好于阻塞数据同步)

## Consequences

### 优势

- 分布式限流: SORTED SET sliding window, 多进程共享配额
- 幂等同步锁: SET NX EX 防止多进程重复调用 API
- 自动 TTL: EXPIRE 零维护成本
- 可观测性: `redis-cli INFO stats` 直接给 hit/miss 比率
- msgpack 序列化: 比 JSON 省 30%%+ 空间, hiredis C 解析器减 CPU

### 代价与缓解

| 代价 | 缓解方案 |
|------|---------|
| Redis 进程依赖 | `brew services start redis`, 文档化在 setup 中 |
| 内存占用 | `maxmemory 256mb` + `allkeys-lru` 淘汰策略 |
| crash 丢数据 | 接受 — SQLite 是 source of truth, 缓存丢失不影响 |
| 网络延迟 | localhost TCP < 0.1ms, 远低于 API 调用 200ms+ |
| 多一个 pip 依赖 | `redis[hiredis]>=5.0` 仅加一行 |

### 不做什么

- 不做查询缓存: 消费者不从 Redis 读数据 (避免缓存一致性问题)
- 不做 Redis 持久化: AOF/RDB 均不启用 (缓存 = 可丢失)
- 不替换 SQLite: `daily_valuation` 表保持为 permanent store

## Alternatives Considered

| 方案 | 拒绝原因 |
|------|---------|
| 文件缓存 (JSON) | 无分布式限流、无原子 TTL、需手动清理 |
| SQLite 缓存表 | 复合键索引无问题, 但跨进程限流需额外锁 |
| Redis 做查询缓存 | 缓存一致性复杂, SQLite PRAGMA cache 已足够 |
