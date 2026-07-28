# ADR-039: 因子缓存存储优化 (SQLite → gzip CSV)

**日期**: 2026-07-28
**状态**: ✅ 已实施
**依赖**: ADR-037 (因子审计), ADR-038 (业务改进)

---

## 问题

`factor_cache.db` (SQLite) 膨胀至 10 GB，含 195K phantom symbols。

## 根因分析

| 根因 | 影响 |
|---|---|
| Phantom symbols (195K vs 5.5K 有效) | 20M 垃圾行 |
| raw_value ≡ zscore 双列冗余 | 50% 空间浪费 |
| SQLite B-tree + 3 个索引 | 2x 开销 |

## 解决路径

### Phase 1: SQLite 清洗 (ADR-039)
- 过滤 phantom symbols → 195K → 5.5K
- 合并 raw_value+zscore → value
- 结果: 10 GB → 3.7 GB (63% 缩减)

### Phase 2: gzip CSV 替代 (ADR-039 v2)
- 格式: 每日期一个 gzip CSV (`factor_cache/YYYY-MM-DD.csv.gz`)
- 列: `symbol,factor,value` (逗号分隔)
- 零外部依赖 (Python stdlib gzip)
- gzip 对重复字符串压缩率极高 (symbol/factor 重复 → 字典编码效应)

## 结果

| 指标 | SQLite (原始) | SQLite (清洗后) | gzip CSV |
|---|---|---|---|
| 大小 | 10,026 MB | 3,768 MB | **206 MB** |
| 缩减 | — | 63% | **98%** |
| 读取 1 日期 | 196ms | — | 110ms |
| 依赖 | sqlite3 | sqlite3 | **stdlib only** |

## 实现

`quant/factor/store.py` 完全重写:
- `materialize()`: 逐日计算 → gzip CSV 写入
- `load()`: gzip 解压 → 逐行解析 → {factor: Series}
- `_get_existing_factors()`: 扫描 CSV 前 200 行取 DISTINCT factor
- `is_materialized()`: 文件存在 + factor 数量校验
- `trim_to_max_days()`: 删除过期文件
- 旧 SQLite 备份: `factor_cache.db.bak` (确认无问题后可删除)

## API 兼容性

`FactorStore` 公开 API 完全不变:
- `FactorStore(db_path=...)` — db_path 此时仅用于定位缓存目录
- `materialize()`, `load()`, `is_materialized()`, `trim_to_max_days()` — 签名不变
- `pipeline.py`, `scheduler/factor_cache.py`, `backtest/loop.py` — 零改动

## 测试

- 221 个测试全部通过 ✅
- `test_v305_factor_cache_trailing_slice.py` 适配 gzip CSV 后端 ✅
