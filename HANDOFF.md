# Handoff: quant 项目状态 — 2026-07-06 15:03 CST

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


## P58: 文档审计 + 策略隔离 + DB 锁修复 + dt_streak 激活 + status 徽章

### 完整改动清单 (7 commits)

```
b909ef9 P58 fix: eval_stepwise.sh guard for empty backtest result (KeyError: total_wealth)
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
