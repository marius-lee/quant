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

## 后续优化 (2026-07-13)

### compute_str / compute_abn_turnover 冗余 SQL 消除

**问题**：这两个因子在 `data["turnover"]` 已可用的情况下（`store.get_daily()` 已 pivot 全部 7 列），仍然用独立 SQL 查询 `daily.turnover`。每个交易日 × 2 次冗余查询。

**根因历史**：
1. 早期 `get_daily` 未包含 turnover 列 → 因子自己查 SQL
2. `get_daily` 后来加上了 turnover 但因子代码未更新
3. 前几次"优化"只修了 ztd 缓存，忽略了这两个因子

**修复**：
- `compute_str`: 删除 ~30 行 `daily.turnover` SQL 查询，改用 `data["turnover"].rolling(...).std()`
- `compute_abn_turnover`: 删除 ~30 行 `daily.turnover` SQL 查询，改用 `data["turnover"].rolling(...).mean()`
- `stocks.total_mv` 和 `stocks.industry` SQL 查询保留（不在 data 中）
- 同时修复 `_dispatch.py` 和 `_alternative.py` 中 `bc034e9` 遗留的 raise 前死代码（日志移到 raise 前）

**教训**：
- 数据层加新列时必须同时更新下游消费者
- `bc034e9` 的 raise 修复不完整，留下 30+ 处死代码未清理，需要单独修复

### 修订 (2026-07-13 晚): 明确不改项

**stocks.total_mv SQL 保留**：
- `compute_str` 和 `compute_abn_turnover` 的市值/行业 SQL 查询**不改**，原因：
  - 每次调用只查 1 行/股票，几十到几百条，数据量小
  - 数据不在 `get_daily()` 的 pivot 列中
  - 改函数签名（从外层批量传入 mv_map）会增加接口复杂度，但收益微小
  - 两者已非主要瓶颈（daily.turnover SQL 才是）
- **改动范围为**：`daily.turnover` SQL → `data["turnover"]`，不改 `stocks` 查询
