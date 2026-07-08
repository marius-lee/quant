# ADR 027: ProcessPoolExecutor 正确实现 — worker 自加载数据

**日期**: 2026-07-08
**状态**: accepted
**替代**: ADR 010 模板8 (条件约束)

## 背景

因子评估管道 (Phase 2) 的并发经历了三次失败尝试:

| 尝试 | 方案 | 问题 |
|------|------|------|
| 1 (47fa2df) | ProcessPoolExecutor + initargs 传 187MB DataFrame | pickle 1-2min 启动延迟 |
| 2 (9677c6e) | ProcessPoolExecutor + initargs 传 35MB DataFrame | pickle 仍慢 + 父进程 WAL 锁 |
| 3 (baf5f6e) | ThreadPoolExecutor stateless worker | GIL 串行化, 6线程=1线程 |

**根因**: 三次尝试都试图把大数据通过 pickle 传给子进程。macOS spawn 模式每个子进程独立序列化/反序列化。

## 决策

**ProcessPoolExecutor worker 自加载数据** — initargs 只传元数据:

```
Main thread:   symbols=['000001',...] + chunk_dates=['2026-06-01',...] + factor_names=[...]
               → submit → pickle < 10KB

Worker process: DataStore() → get_daily() → compute → return results (small)
```

### 关键设计

- **ZERO DataFrame pickling**: 每个 worker 打开独立 DataStore (WAL 并发读)
- **每日数据一次加载**: worker 加载 chunk 完整日期范围的 daily 数据
- **基本面按日期加载**: fundamentals/financials 每日不同, 按需查询
- **主线程关闭连接**: `store.close()` 在 spawn 前调用, 避免 WAL 锁继承

### 性能对比 (100股×30天×31因子, M1 Max)

| 方案 | 启动开销 | 计算时间 | 总计 |
|------|---------|---------|------|
| ThreadPoolExecutor | ~65s (preload) | ~75s (GIL串行) | ~140s |
| ProcessPoolExecutor (本方案) | <1s (pickle 10KB) | ~15s (6核并行) | ~16s |

**约 9× 加速**。

## 后果

- 因子计算耗时从 ~140s 降至 ~16s (100股规模)
- 移除了主线程 fundamentals/financials 预加载逻辑 (~30行代码)
- Worker 进程各自持有完整数据副本 (内存 6×, 但因子值返回轻量)
- 6 进程并发读 SQLite WAL: OS page cache 共享物理页, 无额外磁盘 I/O
