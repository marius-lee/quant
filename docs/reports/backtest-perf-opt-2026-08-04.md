# 回测性能瓶颈分析与优化 (test-v397 P0/P1)

日期: 2026-08-04

## 耗时结构 (1 年 244 天全量回测, 优化前)

| 阶段 | 次数 | 单次耗时 | 总计 | 占比 |
|------|------|----------|------|------|
| 预加载数据+原语 | 1 | ~10-15s | ~12s | ~7% |
| IC 重算 (OOS verify) | 4 | ~20-40s | ~120s | ~70% |
| 逐日因子加载 (gzip) | 244 | ~10-30ms | ~5s | ~3% |
| 逐日因子级中性化 | 244 | ~50-100ms | ~20s | ~12% |
| 其他 (sleeve/optimizer/HMM/执行) | — | — | ~13s | ~8% |
| **总计** | | | **~170s** | |

## P0: 因子值全量内存预加载

**问题**: IC 重算每次调用 `run_oos_check` → 新建 FactorStore → 对 ~180 个采样日逐日 `load()` (gzip 解压 + CSV parse)。1 年 4 次 IC = 720 次文件打开。主循环每天同样 gzip I/O。

**方案**: 回测启动时 `FactorStore.bulk_load()` 一次性加载所有日期因子值到 `{date: {factor: Series}}` dict。内存 ~47MB (800股 × 30因子 × 244天 × 8B)。传入 `generate_signals` 和 `compute_backtest_ic`，跳过一切 gzip I/O。

**修改文件**:
- `quant/factor/store.py` — 新增 `bulk_load()` 方法
- `quant/pipeline.py` — `generate_signals()` 接受 `factor_cache` 参数
- `quant/factor/stats_cache.py` — `compute_backtest_ic()` 接受 `factor_cache`
- `quant/scheduler/oos_verify.py` — `run_oos_check()` 接受 `factor_cache`
- `quant/backtest/loop.py` — 启动时预加载, 传入所有路径

**预期节省**: ~110s (IC 重算从磁盘 I/O 降为内存扫描)

## P1: 因子中性化共享投影矩阵

**问题**: 30 个因子各自做 `_joint_neutralize(alpha, industries, market_caps)`。内部构造完全相同的设计矩阵 X，执行 30 次 `lstsq(X, y)`。计算量 O(30 × K²N)，K≈32 行业哑变量。

**方案**: `_build_neutralize_projection()` 预构建投影矩阵 P = I - X(X'X)⁻¹X'。然后对每个因子 y，残差 = P @ y。计算量从 O(K²N) 降到 O(KN)，~30x 加速。

**修改文件**:
- `quant/risk/neutralize.py` — 新增 `_build_neutralize_projection()`, `_apply_neutralize_batch()`, `neutralize_factors_batch()`
- `quant/pipeline.py` — Step 3 改用 `neutralize_factors_batch()` 替代逐因子循环

**预期节省**: ~24s

## 总计

P0 + P1 预期将 1 年回测从 ~170s 降到 ~35s，**提速约 5 倍**。
