# ADR 024: Pipeline 两阶段分离 + 三时段调度

## 日期
2026-07-06

## 状态
Accepted

## 背景
原有 pipeline 在 15:30 盘后一步完成信号生成+交易执行，存在两个致命问题：
1. 用当日收盘数据产生信号并在收盘价"成交"——未来函数，因果关系颠倒
2. 单一 15:30 触发器无法覆盖盘前准备→开盘执行→盘后归因的完整交易日节奏

## 决策

### 1. Pipeline 拆成两阶段
- `generate_signals()`: 用 T-1 数据在盘前产出目标持仓（只算不执行）
- `execute_signals()`: 09:30 用开盘价对比当前持仓→计算 delta→模拟成交
- `run()` 保留为向后兼容包装器

### 2. Scheduler 三时段
- 08:30: Phase 1 — generate_signals()
- 09:30: Phase 2 — execute_signals()
- 15:30: Phase 3 — daily_sync + 绩效归因

### 3. 策略隔离
- `strategy='quant'`: pipeline 自动纸盘交易
- `strategy='manual'`: 前端 `/api/trade` 手动录入，券商户实盘追踪

### 4. 盘中止损
- 挂在 `/api/quotes` 5s 报价刷新路径上
- 每次拉取报价时扫描 quant 持仓，跌破止损线立即卖出
- 不额外增加定时器，与现有报价刷新同频

## 后果
- 信号生成和执行时间解耦，因果链正确
- 手动交易不污染策略 PnL
- 止损延迟不超过 5 秒
- 因子测试仍走独立脚本（compute_factor_stats / eval_stepwise），不受影响
