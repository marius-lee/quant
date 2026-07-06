# Handoff: quant 项目状态 — 2026-07-06 22:15 CST

## 进入检查清单

| # | 检查项 | 命令/方法 |
|---|--------|----------|
| 1 | 先看日志 | tail -50 logs/app.log + tail -30 logs/quant.log |
| 2 | 服务存活 | lsof -i:8521 + launchctl list | grep quant |
| 3 | 界面 KPI | 总资产/PnL/PnL%/胜率/交易次数(买+卖)/可用资金/持仓市值 |
| 4 | 数据一致性 | total_asset = capital + pos_value? pnl = total_asset - base? |
| 5 | 现金缺口 | 跑 CLAUDE.md Data quirks 验证命令, 缺口 = 佣金+滑点, 不是 bug |
| 6 | scheduler | grep PHASE logs/quant.log | tail -5 |
| 7 | 退出日志 | grep EXIT logs/app.log |



## P59: engine.get_capital pos_value bug — eval stepwise Wealth=¥0 修复

### 问题
eval_stepwise.sh Layer 3 步进回测产出 `Wealth=¥0.00`，不是合理范围。

### 根因
`execution/engine.py:42` — `get_capital()` 中 `p.get('value', 0)` 永远返回 0。

`TradeRepo.get_positions()` 返回的 dict 键是:
  `symbol, price, shares, board_count, buy_time`
没有 `value` 键。所以 `pos_value` 恒为 0，`get_capital()` 只返回现金。

### 连锁误差
rebalance [1]: total_wealth = cash_only = ¥16,527  (实际: ¥99,847)
rebalance [2]: generate_signals 读到 cash_balance=¥16,527, 用这个做资本预算
rebalance [n]: 财富逐轮衰减到接近零 → Wealth=¥0.00

### 修复
- engine.py: p.get('value', 0) → (p.get('price',0) or 0) * (p.get('shares',0) or 0)
- eval_stepwise.sh: stderr=subprocess.DEVNULL → subprocess.PIPE + 正则失败时 debug 打印

### 验证
修复前: backtest wealth=¥16,527, pipeline total=¥99,847 (差 83%)
修复后: wealth=¥96,799, pipeline total=¥96,799 (完全一致)
烟雾: PASS

### commit
a97c73b P59 fix: engine.get_capital pos_value bug

## P58: 文档审计 + 策略隔离 + DB 锁 + dt_streak + 界面 + eval防护 + schema统一

### 完整改动清单 (14 commits)

```
6510b03 docs: HANDOFF — add eval guard to P58 commit list
6ac1f66 P58 refactor: unify sim_trades schema in TradeRepo — engine/web delegate all writes
95152f0 P58 refactor: DELETE FROM sim_trades instead of os.remove trades.db
b949a26 P58 fix: engine.py sim_trades schema missing created_at — align with trade_repo.py
9722091 P58 fix: ConstantInputWarning in spearmanr + backtest_jq.sh indent
b909ef9 P58 fix: eval_stepwise.sh guard for empty backtest result (KeyError: total_wealth)
311d010 docs: HANDOFF P58 section — all 7 commits + interface status confirmed
0e4207c P58 fix: status badge dynamic coloring — hot/warm/cold per trading period
89315f2 P58 fix: auto-migrate initialized column in strategy_config
35f1cd8 P58 final: dt_streak activated (IC=+0.039, IR=0.70) + backtest_jq.sh guard
36fc23f docs: update CHANGELOG + HANDOFF for P58 db lock fix + residual_momentum result
c9e0fb4 P58 fix: sqlite3 busy_timeout on all market.db write paths
a7d9b42 P58 fix: backtest.py strategy isolation — 6 hardcoded 'quant' → STRATEGY variable
405d503 P58: doc audit + residual_momentum_126d
```

### 核心变更

**因子**: 36 因子（27 price + 9 fundamental），2 active (zt_streak, dt_streak)
**策略隔离**: backtest.py 全部 6 处硬编码 `"quant"` → `STRATEGY="backtest"` 变量
**DB 锁**: 所有 market.db 写路径加 `timeout=30`（daily_sync, stats_cache, factor compute, eval_stepwise）
**界面**: status 徽章动态着色 — hot(交易中)/warm(盘前/午休/盘后)/cold(休市)
**文档**: 14 文件审计，因子数统一为 36，ADR 状态更新，CHANGELOG 补全

**DB 层**: sim_trades schema 统一到 TradeRepo._ensure_tables() — engine.py 不再持有 DDL
**写路径**: engine.execute(), web.api_add_trade, web.api_trades 全部通过 TradeRepo 写入
**清空策略**: backtest 不再 os.remove 文件, 改用 DELETE FROM 保留 schema

### 界面状态确认

- 买入时间列: ✓ P57 已实现
- 日期格式: ✓ 后端 [:19]，前端无 slice(0,16) 残留
- 交易次数标签: ✓ HTML 已写 "交易次数（买/卖）"
- status 徽章: ✓ 6 种状态对应 3 种颜色 (hot/warm/cold)
- 实时报价: ✓ 5s 轮询 + 新浪 quotes
- 盘后回退: ✓ 三级 fallback (实时→收盘价→成本价)
- 风险暴露图: ✓ /api/risk 端点


## P57: 界面审计修复 + 实时报价 + 风险暴露 + 时间格式 + 文档

### 完整改动清单 (13 commits)

```
7467c69 Docs: 交易成本备注 + cost.py comment
c919523 Cleanup: ops/ legacy + CLAUDE.md Files to remove
4509422 Rule: read before design -> CLAUDE.md
ee2850f /api/state 实时报价 overlay -> 盘中概览 KPI 随市价变动
b45be36 概览 KPI 使用 state 数据 + /api/performance 估值 fallback 修复
0d0ffc1 HANDOFF: P57 complete
2ff2299 盘后现价用最新收盘价 (daily.close)
238722c 持仓 tab 5s 轮询实时刷新
4258e1e 交易时间 HH:MM:SS + 买入时间列
8551988 /api/risk 风险暴露(vol+dd)
220af00 /api/positions name lookup
889ed15 state_broker pnl/metrics/get_trading_period + 前端胜率/PnL% + 日志清理
```

### 架构变更

state 估值三级回退:
  盘中(9:30-15:00) -> 新浪实时报价 (5s throttle)
  盘后/休市        -> market.db daily.close
  极端             -> 成本价

前端数据流:
  概览 tab -> 5s poll /api/state -> state (实时报价 or 收盘价) -> KPIs
  持仓 tab -> 5s poll loadPortfolio -> /api/quotes -> 持仓表实时刷新
  切离 tab -> 停轮询

新增端点: /api/risk (年化波动率+最大回撤, 60日滚动)

### 代码规则 (CLAUDE.md)
- 先读完目标方法, 确认已有模式, 最小改动贴合进去。不读完不开工
- Data quirks: cash balance 缺口 = 佣金+滑点, 附验证命令

### 当前运行
- Web: launchd com.quant.webapp, port 8521
- Scheduler: launchd com.quant.scheduler, 三时段 (08:30/09:30/15:30)
- 持仓: 002072凯瑞德x200 + 002767先锋电子x100
- 总资产: ~5121 (盘中随市价) | 现金: 1017

### 启动
```bash
cd /Users/mariusto/project/quant && ./restart.sh
```
