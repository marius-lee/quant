# ADR 031: 并行因子计算的 SQLite 锁争抢 —— 禁止在 IC/因子计算中使用多线程

**状态**: 已决
**日期**: 2026-07-13
**作者**: Codex (回溯归档)
**关联**: P78 (9040df2), ADR 029

## 背景

两次尝试在因子计算/IC 计算中引入多线程并行，均失败：

1. **ProcessPoolExecutor**（P68, 约 Jul 9-10）：`factor/stats_cache.py` 用进程池并行计算因子 chunk。失败原因：worker 卡在 I/O 时 shutdown 无响应，留孤儿进程。→ `9040df2` 改为 ThreadPoolExecutor。

2. **ThreadPoolExecutor**（P78, Jul 10）：`factor/stats_cache.py` + `factor/ic.py` 用线程池并行。失败原因：8 个线程同时打 SQLite，锁争抢导致串行 19min → 并行 >1h。→ `f4ffe64` 回退串行。

## 根因

**不是进程 vs 线程的问题，是 SQLite 的并发模型。**

SQLite WAL 模式支持多读单写：
- 多个线程可以同时 `SELECT`（读）
- 但只有一个线程可以 `WRITE`，写时所有读阻塞

因子计算的实际情况：
- `compute_str`、`compute_skewness_60d`、`compute_short_interest` 等 8+ 个因子内部有独立的 SQLite 查询
- `store.get_fundamentals()` 每个日期调一次
- `store.get_daily()` 在旧架构中每个日期调一次
- 8 线程 × 65 日期 × 多次查询 = 数千次 SQLite 连接/查询争抢

**串行方案反而更快的理由**：
1. 1 次 `store.get_daily(start, end)` 加载全窗口（1 次 SQL）
2. 串行处理每个日期（无锁争抢）
3. 内存切片替代 SQL 查询

## 决策

1. **禁止在 factor/ic.py 中使用 ThreadPoolExecutor**（已执行，`f4ffe64`）
2. **factor/stats_cache.py 的 ThreadPoolExecutor 保留**——但仅限于 _thread_compute_chunk 内部，每个 worker 打开独立 DataStore，且 chunk 粒度足够大使得 SQL 查询占比小
3. **将来如果需要加速**：先向量化因子内部的独立 SQL 查询，再考虑并行
4. **编码规则**：在有独立 SQLite 查询的代码路径中引入并行之前，必须先验证向量化是否到位

## 教训

- 在现有代码上做性能优化时，**必须先查 git log 看之前是否有人试过并失败了**
- `git log --grep="parallel\|ThreadPool\|ProcessPool"` 会发现 P78 迁移和之前的问题
- 盲目的并行化在 SQLite 场景下几乎总是适得其反

## 关联

- ADR 029: 四层回测
- P78: ProcessPoolExecutor→ThreadPoolExecutor 迁移 (9040df2)
- HANDOFF.md: 性能优化建议 (提到 ztd/IC 瓶颈)
