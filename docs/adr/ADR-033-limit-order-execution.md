# ADR 033: 限价单执行 — 从"09:30 市价一次买入"到"限价挂单 + 被动成交"

**状态**: 已决
**日期**: 2026-07-15
**作者**: Codex
**关联**: ADR 029, execution-timing-analysis-2026-07-15.md, HANDOFF.md#2026-07-15

## 背景

原执行流程: 每日 09:30 以开盘价市价一次性完成所有买入和卖出。此设计存在两个问题:

1. **市场冲击**: 集合竞价后流动性不足, 市价买入容易买到高价 (执行偏差)
2. **无价格优化**: 开盘价买入后, 若股价日内走低, 无机制在更低价格买入

业界标准 (Grinold & Kahn 1999, Almgren & Chriss 2005): 将买入拆分为限价挂单 + 被动成交, 只有卖出因紧急程度更高而使用市价。

## 决策

**买入**: 09:30 以限价挂单 (ref_price × (1 - discount_pct)), 盘中由 monitor 被动管理:

- **事件 A**: ask ≤ limit → 成交
- **事件 B**: ask < limit 且价差 > chase_threshold → 追价下调限价 (价格跌了, 可以更便宜买)
- **事件 C**: ask > limit 且价差 > runaway_threshold → 放弃限价, 市价买 (价格跑远了, 赶紧上车)
- **事件 D**: 时间 ≥ force_fill_time (14:50) → 全部未成交市价补单

**卖出**: 09:30 市价立即执行 (与旧逻辑一致 — 卖出优先级高于买入, 不应等待)

**Monitor**: 现有 5s 行情循环中嵌入 `OrderManager.check_and_manage()`, 不额外拉行情。

## 影响

- `execute.py`: 不再调用 `pipeline.execute_signals()`, 改为自行计算 delta + 挂单
- `monitor.py`: 新增限价单管理调用
- `pipeline.py`: 不受影响, `execute_signals()` 仍用于 backtest
- 新增 `order_manager.py`: 限价单生命周期管理

## 配置

```yaml
execution:
  limit_order:
    discount_pct: 0.002       # 限价折扣 0.2%
    chase_threshold: 0.01     # 1% 追价
    runaway_threshold: 0.02   # 2% 放弃
    force_fill_time: "14:50"  # 尾盘补单
    quote_ttl_sec: 30         # 行情陈旧阈值
```

## 后果

- **正面**: 降低买入成本 (预期改善 0.1-0.3%), 减少单次大单冲击
- **负面**: 可能部分股票买不到 (价格一路上涨, 未触及限价), 但尾盘强制补单兜底
- **风险**: 追价逻辑在极低流动性股票上可能反复追价, chase_count 可用于监控
