# 回测系统性能分析报告

> 日期: 2026-08-04
> 范围: `quant/backtest/` + `quant/pipeline.py` + `quant/execution/` + 数据层
> 方法: 全链路逐文件阅读，非推测

---

## 一、回测主循环流程（确认）

```
run_backtest()
 ├─ 预加载 data_full (全部日线 in memory) ✅
 ├─ 预加载 factor_cache (全部因子值 in memory) ✅ test-v397 P0
 ├─ 预计算 primitives (共享算子) ✅
 ├─ 计算初始 Walk-forward IC ✅
 │
 └─ for each trading_day:
      ├─ [调仓日] generate_signals() → Step 0~5
      │   ├─ Step 0: 创建 CostModel/PortfolioConstructor (每天 new)
      │   ├─ Step 2: get_symbols() + slice data_full ✅
      │   ├─ Step 2.3: get_fundamentals() DB 🔴
      │   ├─ Step 2.3: get_stock_names() DB 🔴
      │   ├─ Step 2.3: limit_up_pool 独立 SQLite 连接 🔴
      │   ├─ Step 2.5: rank_by_turnover() DB 🔴
      │   ├─ Step 3: 因子值读 factor_cache ✅
      │   ├─ Step 3: get_benchmark() DB 🔴
      │   ├─ Step 3: AlphaModel.combine(kwargs)
      │   ├─ Step 3.5: neutralize
      │   ├─ Step 4: covariance_matrix() 🔴 (最大CPU瓶颈)
      │   └─ Step 5: PortfolioConstructor.construct()
      │
      ├─ [非调仓日] 只跑 execute_risk_only()
      │
      ├─ execute_signals() → ExecutionModel
      │   ├─ 冷却过滤 (内存) ✅
      │   ├─ 硬止损检查 ✅
      │   ├─ compute_trades() (delta)
      │   └─ engine.execute() → 写入 sim_trades
      │
      ├─ _get_prices() 逐日查 DB 🔴 (本应从 data_full 切片!)
      ├─ get_mtm_capital() 逐日查 DB 🔴
      ├─ 因子追踪 FactorTracker.record_day() O(N*M)
      └─ [每60天] compute_backtest_ic() → run_oos_check() 全量重算

后处理: metrics / diagnosis / stress_test / persist
```

---

## 二、核心性能瓶颈（按影响排序）

### 瓶颈 #1：协方差矩阵每调仓日重算 — 最大的 CPU 黑洞

**位置**: `pipeline.py` Step 4 → `quant/risk/covariance.py::covariance_matrix()`

每个调仓日都要对 800×800 的收益面板计算协方差矩阵。这是 O(N²×T) 的计算量（N=800, T≈252）。即使有 `covariance_subset()` 优化（只算 top-30），这是单日计算最重的操作。

**业界标准**: QuantConnect/Backtrader 用滚动缓存，EWMA 增量更新，Numba/Cython 加速。

**量化影响**: `covariance_subset(30 stocks, 252 days)` ~50ms/次，1200 天 ≈ 60 秒。

### 瓶颈 #2：`_get_prices()` 逐日 SQLite 查询 — 预加载数据被浪费

**位置**: `loop.py` 第 32-41 行

`data_full` 已在启动时预加载，但 `_get_prices()` 完全绕过它，每次都走 `store.get_daily()` 查 SQLite。

**每交易日 ≥ 4 次 SQLite round-trip**。1200 天 = 4800 次 SQL 查询，每次 pivot+ffill。累计 25-70 秒。

### 瓶颈 #3：`rank_by_turnover()` 逐日聚合查询

**位置**: `pipeline.py` Step 2.5 → `store.py` 第 2158-2170 行

对 3000+ 股票做 7 日成交额均值 + GROUP BY + ORDER BY，每天一次。单次 300-500ms，1200 天 = 360-600 秒。

**业界标准**: 预处理为日截面 rank 表，O(1) 查表。

### 瓶颈 #4：`get_benchmark()` 每日期重复加载

**位置**: `pipeline.py` 第 225-227 行

每次都从 `2025-12-01` 开始加载沪深 300。1200 次重复加载同样数据。浪费 10-30 秒。

### 瓶颈 #5：`generate_signals()` 每天新创建 ExecutionEngine/CostModel/PortfolioConstructor

**位置**: `pipeline.py` 第 94-97 行

ExecutionEngine.__init__ 每次都走 TradeRepo DDL，每天一次。1200 天 × 3-5ms = 4-6 秒。

### 瓶颈 #6：`limit_up_pool` 每日独立 SQLite 连接

**位置**: `pipeline.py` 第 166-177 行

每天新建独立连接查涨停池，不走 DataStore 复用。完整 open → query → close。

### 瓶颈 #7：`get_stock_names()` 每日期查询

**位置**: `pipeline.py` 第 163 行

股票名称在回测期间不变，应预加载到 dict。

### 瓶颈 #8：`get_fundamentals()` 每日期查询

**位置**: `pipeline.py` 第 146 行

PE/PB/ROE 在回测期间静态，不需要逐日查 SQLite。

---

## 三、架构/设计问题

### 问题 A：`execute_signals()` 内部又建一个 `ExecutionEngine`

`SimulatedBroker.execute()` → `execute_signals()` 内部创建新的 ExecutionEngine，与 broker 持有的 engine 是不同实例。各维护独立 TradeRepo 连接。

### 问题 B：因子追踪 O(N×M) 嵌套循环

FactorTracker.record_day() 每日调用，20×15=300 次迭代，1200 天 = 36 万次。

### 问题 C：Walk-forward IC 重训重复创建 DataStore

每 60 天打开新 DataStore 实例加载历史数据，不复用主循环连接。

---

## 四、与业界标准对比

| 维度 | QuantConnect/Backtrader 标准 | 当前实现 | 差距 |
|---|---|---|---|
| 数据预加载 | 全量加载到内存，回测期间 0 DB 访问 | data_full 已加载但未充分使用 | 🔴 |
| 协方差 | EWMA 增量更新 / 周频重算 | 每日重算（但有子集优化） | 🟡 |
| 成交额排名 | 预处理静态截面 rank 表 | 每日 SQL AVG+GROUP BY | 🔴 |
| 价格查询 | 内存 DataFrame 切片 | SQLite 逐日 pivot+ffill | 🔴 |
| 因子值 | 预物化内存读取 | ✅ test-v397 P0 | ✅ |
| 连接管理 | 连接池 / 单连接复用 | 混合 | 🟡 |
| Benchmark | 加载一次，复权后切片 | 每日从头加载 | 🔴 |
| 基本面 | 季度快照 + ffill | 每日查询 | 🔴 |
| 交易成本 | Almgren-Chriss / Barra | 有简化版 AC 模型 | 🟢 |
| 风控 | ATR止损 + VaR | ✅ 完善 | ✅ |

---

## 五、回测耗时估算

假设 2020-01-01 到 2026-06-30，~1580 个交易日，daily rebalance，800 股：

| 操作 | 当前耗时估算 |
|---|---|
| 协方差矩阵（每调仓日） | 100-300s |
| 成交额排名 rank_by_turnover | 360-600s |
| 价格查询 _get_prices (SQLite) | 25-70s |
| 基本面/Benchmark/名称查询 | 30-80s |
| 因子值（已优化为内存） | ~5s |
| Alpha 合成 + 中性化 | ~20s |
| 组合优化 + 执行 | ~30s |
| Walk-forward IC 重训（每60天） | ~120s |
| **总计估算** | **690-1225 秒 (11-20 分钟)** |

优化后预期: **~163-233 秒 (3-4 分钟)**，加速比 3-5x。

---

## 六、优化方案清单

| # | 方案 | 改动量 | 预期收益 |
|---|---|---|---|
| 1 | 价格查询改为从 data_full 内存切片 | ~15行 | 25-70s |
| 2 | 成交额排名预处理为截面表 | ~50行 | 360-600s |
| 3 | sleeve模式下跳过协方差计算 | ~5行 | 50-200s |
| 4 | Benchmark预加载一次后切片 | ~10行 | 10-30s |
| 5 | 基本面/股票名称/涨跌停池预加载 | ~30行 | 20-60s |
| 6 | 复用ExecutionEngine/CostModel实例 | ~20行 | 5-10s |
