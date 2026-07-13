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
