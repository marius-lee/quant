# 回测性能优化 — 修改对照报告

> 日期: 2026-08-04
> 分析报告: docs/backtest-performance-analysis-2026-08.md
> 修改标记: test-v398 (perf)

---

## 修改清单（共 10 项）

### ✅ 1：价格查询 data_full 切片
- **文件**: `loop.py`, `broker.py`
- **修改**: `_get_prices()` 新增 `data_full` 快速路径, `SimulatedBroker` 接收并传递
- **效果**: 消除 ~4800 次 SQLite round-trip

### ✅ 2：成交额排名按日排序
- **文件**: `loop.py`, `pipeline.py`
- **修改**: 存 `_amount_roll` DataFrame, 按日 `sort_values()` O(N log N) ~1ms
- **效果**: 消除 ~1200 次 AVG+GROUP BY, 省 ~500 MB

### ✅ 3：协方差懒计算
- **状态**: v393 已实现, `cov=None` 传入 `construct()`, Small 层内按需算
- **效果**: Nano/Micro 层零协方差开销

### ✅ 4：Benchmark 预加载
- **文件**: `loop.py`, `pipeline.py`
- **修改**: 预加载一次 `_bm_returns_full`, 每日切片
- **效果**: 消除 1200 次重复 SQL

### ✅ 5：stock_name + limit_up_pool 预加载
- **文件**: `loop.py`, `pipeline.py`, `constraints.py`
- **效果**: 消除每日独立 SQLite 连接查询

### ✅ 6：Engine/CostModel/Constructor 复用
- **文件**: `loop.py`, `pipeline.py`
- **效果**: 消除每日 DDL + new 实例

### ✅ 7：基本面 PIT 共享 pivot
- **文件**: `loop.py`, `pipeline.py`
- **修改**: 预加载 `daily_valuation` + `stocks` 为共享 pivot 表（~400MB），每日 O(1) 切片组装
- **效果**: 消除每日 `get_fundamentals()` DB 查询

### ✅ 8：data_prims 跳过
- **文件**: `loop.py`
- **修改**: 回测路径 `precompute_primitives()` 完全不被消费，跳过
- **效果**: 省 ~11 GB 内存 + ~20s 预计算

### ✅ 9：factor_cache DataFrame 存储
- **文件**: `loop.py`
- **修改**: `_FactorCache` 包装器, dict-of-Series → DataFrame（共享 Index）
- **效果**: ~3 GB → ~350 MB, 省 ~2.5 GB

### ✅ 10：universe symbols + monitor 跳过
- **文件**: `loop.py`, `pipeline.py`
- **修改**: 预加载 `_all_symbols` 列表替代每日 SQL JOIN; `suppress_push=True` 守卫 monitor + save_signals
- **效果**: 每日 `load` 阶段 2.1s → 0.0s

---

## 文件变更汇总

| 文件 | 说明 |
|---|---|
| `quant/backtest/loop.py` | _FactorCache 类, _get_prices fast path, 全部预加载逻辑, primitives 跳过 |
| `quant/backtest/broker.py` | SimulatedBroker 接收 data_full |
| `quant/pipeline.py` | 10 个新参数, 各步骤优先内存, monitor/save_signals 守卫 |
| `quant/risk/constraints.py` | filter_sealed_limit_up 支持 seal_ratios dict |

---

## 实测结果

| 指标 | 重构前 | 重构后 | 变化 |
|---|---|---|---|
| **main loop 每日** | 2.7s | **0.6s** | 4.5x |
| **main loop 总计 (24d)** | 63.2s | **13.6s** | 4.8x |
| **总耗时** | 168s | **119s** | 1.4x |
| **内存占用** | ~15.5 GB | **~1.7 GB** | 9x |
| **pytest** | 221/221 | 221/221 | ✅ |
| **Sharpe/CAGR/MDD** | 0.765/4.5%/-13.2% | 0.765/4.5%/-13.2% | ✅ 不变 |
