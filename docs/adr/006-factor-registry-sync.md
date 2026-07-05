# ADR 006: compute_factor_stats 同步写入 factor_registry

**日期**: 2026-07-04
**状态**: accepted

## 背景

因子 IC 评估有两条路径：
- `compute_factor_stats()` — 计算因子 IC/IR/相关性
- `get_cached_factor_stats(force_refresh=True)` — 缓存层，调用前者后写入 factor_snapshot + factor_registry

原生设计遵循 CQS：计算函数无副作用，持久化交给缓存层。

## 问题

`compute_factor_stats()` 是公开函数。直接调用它（eval 脚本、临时调试）
会拿到正确的 IC 值，但 `factor_registry` 不更新。后续回测读的还是旧的 IC 权重，
导致因子信号与权重不匹配，回测结果失真。

具体案例：接入 JQData 日度估值后，bp_ratio IC 从 0.062 → 0.092，但 factor_registry
仍保留旧值 0.062。回测用新因子值 × 旧权重，收益从 +36% 崩到 -48%。

## 决策

将 `update_factor_evaluation` 从 `get_cached_factor_stats` 移至 `compute_factor_stats` 内部。
每次计算都同步写入 factor_registry。

## 影响

- `compute_factor_stats()` 有副作用（写 factor_registry）— 不再纯函数
- `get_cached_factor_stats` 只负责 factor_snapshot 缓存，不再重复写 registry
- 消除了两条调用路径导致数据不同步的可能

## 替代方案

- 废弃 `compute_factor_stats()` 作为公开 API，强制走 `force_refresh_cache()`
  — 但改动面大，且违背"可单独计算 IC"的使用场景
