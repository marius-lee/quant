<!--
  架构改进方案 — 2026-07-13
  基于 zipline / backtrader / vnpy 等行业框架的成熟模式，
  针对当前项目四个模块提出改进方案。

  优先级: 4(数据管线) → 2(因子向量化) → 3(风险预算) → 1(事件驱动回测)
  理由: 前两个直接缩短回测耗时，第三个影响实盘安全性，第四个是架构重构。
-->

# 架构改进方案

## 1. 回测引擎：事件驱动架构

### 现状问题

- backtest/loop.py 是 380 行同步 for 循环：遍历交易日 → generate_signals → 查开盘价 → execute_signals → 记录权益
- _get_open_prices / _get_close_prices 每日期独立 sqlite3.connect，绕过 DataStore 连接复用
- 止盈止损、冷却期、除权除息全部手工 if/else，逻辑散落在主循环和 pipeline 之间
- ExecutionEngine 和 backtest 公用 BACKTEST_DB，TradeRepo 在 pipeline Step 0 重新初始化

### 改进方案

引入轻量事件队列，不引入 backtrader 重量级框架：

- Clock: TradingCalendar 迭代器（扩展现有 execution/calendar.py 的 is_trading_day）
- Broker 模拟: 封装 _get_open_prices / execute_signals 为 SimulatedBroker，复用 DataStore 连接
- 事件分发: StopLossEvent / CoolOffEvent / ExDividendEvent 各自实现 handle(portfolio)

工作量: ~200-300 行新代码，不改 pipeline 因子计算逻辑

## 2. 因子计算：向量化模式

### 现状问题

- _dispatch.py 串行循环，每个因子独立 _db_connect() 拉数据
- compute_margin_buy_ratio、compute_analyst_consensus、compute_dividend_yield 等各自独立 SQL 连接
- 已改进的: ztd 有 preload_ztd_cache 预加载

### 改进方案：「一次拉取，因子共享」

新增 factor/compute/_preload.py，批量拉取所有因子需要的辅助数据：

```python
aux = _preload_aux_data(symbols, date)
# aux 包含: margin_detail, analyst_forecast, fund_hold, dividend 等
for factor in factors:
    result = factor_fn(data, date, aux=aux)
```

因子函数签名加 aux=None，有 aux 就用，没有则回退到自己的查询（兼容单独调用）。

改动量: 新增 _preload.py (~60行)，修改 5-8 个基本面因子函数（各减 ~10 行）

## 3. 风险管理：VaR/CVaR 风险预算驱动

### 现状问题

- risk/var.py 基础实现规范（参数法 VaR/CVaR、压力测试、相关性崩溃检测）
- 问题: 只被 web dashboard 调，回测循环和执行引擎完全没用它
- update_daily_risk() 输出只进 broker 不进 DB

### 改进方案

在回测和执行链路中接入风险报告：

1. PortfolioConstructor 接入 VaR: construct() 后调用 compute_var()，VaR 超限则等比例缩减头寸
2. stress_test 加入回测报告: 回测结束后对最终持仓跑压力测试，写入 evaluation_runs
3. 日度 VaR 监控入库: update_daily_risk() 写入 daily_risk 表

config.yaml 新增: risk.max_var_pct (如 2%)

工作量: ~100 行新增 + 15 行 config

## 4. 数据管线：Lazy Loading + LRU 缓存

### 现状问题

- get_daily() 每次拉全量 7 列 OHLCV，很多因子只用 close 或 volume
- 裸 SQLite 连接绕过 DataStore: _get_open_prices、pipeline Step 2.5、多个基本面因子
- 无查询结果缓存: 同一日期/股票在同一次回测中被多次加载

### 改进方案：两级缓存 + 统一入口

Layer 1: LRU Query Cache (in-memory, per-DataStore-instance)
  key = (query_hash, symbols_hash, date), TTL = 本次 call 期间有效

Layer 2: DataCache (Redis/file, cross-process) — 已有，用于 stock_list、industry mapping

具体改动:
1. DataStore 加 _query_cache: lru_cache 装饰 get_daily() / get_fundamentals()
2. 废除裸 SQLite: _get_open_prices / _get_close_prices 改走 store.get_daily()
3. 延迟列加载: get_daily() 新增 columns 参数，按需只拉 close/volume

工作量: DataStore 新增 ~40 行，backtest loop 减 ~20 行，pipeline 减 ~15 行

---

## 实施记录

| 优先级 | 模块 | 状态 | 完成时间 |
|--------|------|------|----------|
| 1 | 数据管线 Lazy Loading | pending | - |
| 2 | 因子向量化 _preload | pending | - |
| 3 | 风险预算 VaR | pending | - |
| 4 | 事件驱动回测 | pending | - |

---

## 第二轮深层分析 (2026-07-13 18:00)

### 1. 事件驱动回测 — 三个缺位

| 缺口 | 现状 | 成熟做法 |
|------|------|----------|
| 市场冲击模型 | CostModel 固定滑点 0.1% | sqrt(order_size/daily_volume) 非线性滑点 |
| 除权除息事件 | get_daily() ffill 填停牌，不复权 | 复权因子调整前复权价格 |
| 订单类型 | 仅开盘市价单 | 限价单 / TWAP / VWAP |

### 2. 因子向量化 — 29 个独立连接 + 无共享中间结果

- _event.py 10 个因子 lhb/fund_flow/pledge 独立查库
- 价格因子间不共享 pct_change/rolling/rank 等中间结果
- fundamental.py 1272 行，33 个因子混在一起

### 3. 风险预算 — 参数 VaR 不够

- 需补: 历史 VaR bootstrap、ATR 动态止损、风险平价头寸管理

### 4. 数据管线 — IC/ZTD 数据复用、派生序列预计算

- compute_ic() 和 preload_ztd_cache() 的数据窗口有重叠
- covariance 的 log_ret 和 pipeline 的 close_df.diff() 重复计算

---

## 第二轮实施记录

| 方向 | 改动 | commit |
|------|------|--------|
| 2-深层 | _preload 扩展 lhb/fund_flow；_event.py 3因子(lhb_net_buy/main_flow_ratio/analyst_buy)接入aux | 44d9799 |
| 3-深层 | risk/atr.py 新建(ATR14/atr_stop_loss/atr_position_size)；var.py 补 historical_var/historical_cvar | 7544fad |
| 1-深层 | execution/cost.py 加 market_impact() sqrt模型 | affc550 |
| 4-深层 | preload_ztd_cache 接收 volume_data；pipeline.py 传入已加载 data | c1438d2 |

---

## 第三轮终极分析 (2026-07-13)

### 1. 事件驱动回测 — 第三层
- T+1 真实约束：sell-before-buy 执行顺序、同股同日不可买卖
- 限价单/订单簿：缺 Level-2 数据 → 硬边界
- 多资产组合执行：先卖后买释放现金

### 2. 因子向量化 — 第三层
- 共享中间计算图：所有滚动统计量一次算完，因子只做排序
- Numba JIT：不兼容 MultiIndex → 硬边界
- Apache Arrow 零拷贝

### 3. 风险预算 — 第三层
- 边际 VaR / 成分 VaR：∂VaR/∂w
- 风险平价：w_i ∝ 1/σ_i
- 波动率自适应头寸

### 4. 数据管线 — 第三层
- 物化派生序列：derived_daily 表存所有滚动统计
- DuckDB 迁移：并发写冲突 → 硬边界
- 增量数据加载

### 实施记录

| 项目 | 状态 |
|------|------|
| T+1 卖出优先执行 | pending |
| 风险平价 | pending |
| 边际 VaR | pending |
| 共享中间计算图 | pending |
| 物化派生序列 | pending |
