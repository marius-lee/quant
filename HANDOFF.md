# HANDOFF — quant 项目当前状态

**最后更新**: 2026-07-07 10:35 CST

## 当前已完成

| 提交 | 内容 |
|------|------|
| 117b6d9 | fix: 除权除息检测 — ExecutionEngine._check_ex_dividend() |
| 39bb299 | fix: 3新因子函数签名添加 financials=None |
| 712a8f9 | fix: sim_trades created_at UTC→CST 时区修复 |
| 8ecd709 | P69 fix: 重建三阶段调度器 quant/scheduler.py |
| 06cf29e | P69: 修复 scheduler + data sync 漏股 bug |
| c454daa | P68: dividend 数据源切换 Tushare→akshare |
| ac5fee4 | P67 hotfix: dividend API + 限流 |

## 当前问题 / 待处理

1. **002072 除权导致错误买入** → 已修复: engine 层 `_check_ex_dividend()` 检测，gap > 10% 跳过
2. **execution 价格来源**: execute_signals() 在 market.db 无当日数据时回退到昨日收盘价。除权检测能拦截异常跳变，但正常交易日也应用实时报价（后续优化）
3. **git push 被 keychain 阻塞**: 待下次提交一并推送

## 关键架构

- **调度**: 三阶段 (08:30 信号 → 09:30 执行 → 15:30 归因)
- **状态通信**: web/state_broker.py (Redis pub/sub + fallback)
- **交易持久**: data/trade_repo.py → data/trades.db
- **市场数据**: data/store.py → data/market.db
- **执行引擎**: execution/engine.py → 含除权检测、T+1 检查、止损
- **实时报价**: execution/quote.py (新浪财经)

## 关键约束

- 所有数值参数放 config/config.yaml，不硬编码（涨跌停 10% 是交易所硬规则除外）
- 不遗留 fallback 默认值
- 修改后文档同步更新
