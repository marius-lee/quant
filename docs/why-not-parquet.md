# gzip CSV vs Parquet — 因子缓存格式分析

> 日期: 2026-08-04
> 结论: 不改格式，瓶颈是串行 I/O 而非文件格式

---

## 数据特征

```
262 个文件 × 每文件 ~100KB = ~26MB 总量
单文件内容: 30因子 × 800股 = ~24000行
每行: 600001,momentum_20d,0.031245
```

## 对比

| 维度 | gzip CSV（现状） | Parquet |
|------|-----------------|---------|
| 依赖 | stdlib gzip，零额外 | pyarrow ~120MB 或 fastparquet |
| 可调试 | `zcat file.csv.gz` | 需要 parquet-tools 或 Python |
| 单文件读 | ~3ms (100KB) | ~2ms |
| 压缩率 | 重复文本模式 gzip 极高 | snappy 对短文本可能更大 |
| 262文件总耗时 | 串行 I/O 90s | **同样 262 次 I/O** |
| 写入 | 纯文本拼接，零依赖 | schema + metadata |

## 关键结论

**瓶颈是 262 次串行 I/O 等待，不是单文件解压速度。**

换 Parquet 只省 ~1ms/文件（3ms→2ms），总共省 0.26s。
262 次 `open()` 系统调用才是 90s 的根因。

## 正确方案

ThreadPoolExecutor 并行化 `bulk_load`，不改格式：
- 8 线程 → 262/8 ≈ 33 批次 → 90s → ~12s
- 改动量 ~10 行
- 零新依赖

---

**以后提出"换 Parquet"建议前，先读本文档。**
