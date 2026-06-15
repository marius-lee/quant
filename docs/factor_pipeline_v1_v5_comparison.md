# 因子计算管道方案对比 (V1-V5)

日期: 2026-06-05

## 背景

原始因子重建用时 2 小时以上。逐批 200 只计算因子 + 批内 Winsorize + SQLite 逐批 COMMIT。

## 五版方案

| 维度 | V1 当前 | V2 两遍法 | V3 Parquet管道 | V4 DuckDB引擎 | V5 SQLite-free |
|------|:--:|:--:|:--:|:--:|:--:|
| **Winsorize正确性** | ❌ 200只/批 | ✅ 全量 | ✅ 全量 | ✅ 全量 | ✅ 全量 |
| **全量重建时间** | 2h+ | ~2m45s | ~2m | ~2m | ~2m |
| **额外依赖** | 无 | 无 | 无 | DuckDB | DuckDB |
| **实现状态** | — | ✅ 已实现 | — | — | — |

## 关键发现

1. Polars `over("date")` 比 `group_by().agg()` 慢 900 倍 (GitHub #16457)
2. Polars 惰性求值: Pass 1 只物化聚合结果(~250K值), 不物化全量因子(~25M值)
3. DuckDB 列式引擎归一化约 3s, 比 Polars pandas 快 ~5x
4. SQLite 写入瓶颈: executemany 310K 行/批

## V2 加速来源

1. **两遍分离** — Pass 1 全量阈值 → Pass 2 复用 (~40%)
2. **group_by.agg 替代 over()** — 只物化聚合结果 (~30%)
3. **向量化 groupby clip** — 替代 90K 次 pandas 赋值 (~15%)
4. **日期标准化** — 删掉所有 str.slice + replace (~5%)
5. **REAL 替代 TEXT** — 省 float() 转换 (~3%)
6. **删冗余 OHLCV 列** — 每行少写 5 列 (~2%)

## 选型决策

V2 已实现 (2m45s), V3-V5 的边际收益不抵额外成本。保留 V5 (SQLite-free) 作为远期规划。

## 来源

- Polars GitHub Issue #16457: `over()` 900x slower than `group_by.agg.join`
- SQLite INSERT 性能: SQLPro 2024 benchmark (200K+ rows/s with WAL+prepared statements)
- DuckDB vs SQLite: 查询快 5x, 存储少 80% (腾讯云 2025)
