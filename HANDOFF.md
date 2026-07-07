# HANDOFF — quant 项目当前状态

**最后更新**: 2026-07-07 10:40 CST

## 最近两次提交 (今日)

| 提交 | 内容 |
|------|------|
| e74a00a | fix: execute_signals 执行价格 — Sina 实时开盘价替代 market.db fallback |
| 117b6d9 | fix: 除权除息检测 — ExecutionEngine._check_ex_dividend() |

## 执行价格链修复 (完整)

**修复前**: execute_signals → market.db 取当日 open → 无数据 → fallback 到昨日 close → 除权日买错价
**修复后**: 
1. `execute_signals()` 直调 `fetch_quotes()` 取 Sina 实时开盘价，拉不到就跳过不执行
2. `engine.execute()` 对每个 buy 做 `_check_ex_dividend()` 检测，gap > 10% 跳过

**原则**: 不 fallback，不降级。执行价格只有一个来源 — Sina 实时报价。拿不到就不交易。

## 当前问题 / 待处理

- **git push 被 keychain 阻塞**: 待解锁后推送 (117b6d9 + e74a00a 两个 commit 都没推)
- **002072 持仓残留**: 今天被除权检测前的旧逻辑买入的 100 股还在持仓里，需手动清理

## 关键架构

- **调度**: 三阶段 (08:30 信号 → 09:30 执行 → 15:30 归因)
- **执行价格**: fetch_quotes() → Sina 实时 open (无回退)
- **除权检测**: engine._check_ex_dividend() → market.db 前收对比 (10% 硬阈值)
- **状态通信**: web/state_broker.py (Redis pub/sub + fallback)
- **交易持久**: data/trade_repo.py → data/trades.db
- **市场数据**: data/store.py → data/market.db

## 关键约束

- 所有数值参数放 config/config.yaml (涨跌停 10% 是交易所硬规则除外)
- **永不 fallback 执行价格** — 拿不到实时报价就跳过
- 修改后文档同步更新
