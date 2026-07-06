# Handoff: quant 项目状态 — 2026-07-06 13:04 CST

## 进入检查清单（每次会话必过）

| # | 检查项 | 命令/方法 |
|---|--------|----------|
| 1 | 先看日志 | tail -50 logs/app.log + tail -30 logs/quant.log |
| 2 | 服务存活 | lsof -i:8521 + launchctl list | grep quant |
| 3 | 界面 KPI 逐项核 | 总资产/PnL/PnL%/胜率/交易次数(买+卖)/可用资金/持仓市值 |
| 4 | 数据一致性 | total_asset === capital + pos_value? pnl === total_asset - base? |
| 5 | scheduler 状态 | grep PHASE logs/quant.log | tail -5 |
| 6 | 退出日志 | grep EXIT logs/app.log |


## P57: 界面审计修复 + 风险暴露 + 时间格式 + 实时刷新 + 盘后收盘价

### 问题 & 修复

**1. state_broker._init_state() 缺失字段**
- status 写死"休市" -> 改为 get_trading_period() (6种: 盘前/上午交易/午休/下午交易/盘后/休市)
- 无 pnl/metrics -> 从 trades.db 实时计算: pnl={realized,total,unrealized}, metrics={total_return_pct,win_rate,buy_sell_counts,initial_capital}
- 持仓 name 为空 -> 从 market.db.stocks lookup (002072->凯瑞德, 002767->先锋电子)

**2. 前端显示**
- 胜率: 0卖时显示"—"而非"0.0%" (概览+绩效两个tab)
- PnL%: 改用 initial_capital(5000) 作为基数
- /api/performance 新增 initial_capital, total_return_pct

**3. 风险暴露**
- 新增 /api/risk 端点: 60日滚动计算年化波动率(log returns * sqrt(252)) + 最大回撤
- 前端: Plotly grouped bar chart (红=波动率, 黄=回撤) + 仓位权重
- 替换硬编码 "待 pipeline 实现"

**4. 交易时间格式**
- sim_trades 新增 created_at 列 (09:35:12, 10:15:38)
- /api/trades: date -> created_at, 格式 YYYY-MM-DD HH:MM:SS
- /api/positions: 表头新增「买入时间」列, 格式同上
- trade_repo.get_positions(): buy_time=MIN(created_at)

**5. 持仓实时刷新**
- 持仓 tab 激活时 5s 自动轮询 loadPortfolio() -> /api/quotes
- 切离 tab 时停轮询
- 盘中(9:30-15:00): 新浪实时报价
- 盘后: market.db daily.close 最新收盘价 -> 真实浮盈
- 极端回退: 成本价

**6. 日志清理**
- 删除旧 quant.log.1-5(50MB+), server.err(25MB), push.log, web.log, server.log
- 保留: app.log(日轮转10天) + quant.log(10MB*5)

### 审计结果 (4 tab * 24 项全部通过)

概览 tab (10项): KPI*7 + status + gauge + signals OK
因子 tab  (6项): KPI*3 + IC bar + decay + corr OK
持仓 tab  (5项): 表 + 饼图 + 风险(vol+dd) + 实时刷新 + 收盘价 OK
绩效 tab  (3项): 交易表 + 统计 + waterfall OK

### commits (P57: 889ed15 -> 2ff2299)
```
2ff2299 盘后现价用最新收盘价 (daily.close)
238722c 持仓 tab 5s 轮询实时刷新
4258e1e 交易时间 HH:MM:SS + 买入时间列
8551988 /api/risk 风险暴露图表
220af00 /api/positions name lookup
889ed15 state_broker pnl/metrics/get_trading_period + 前端胜率/PnL%
```

### 当前运行
- Web: launchd com.quant.webapp, port 8521
- Scheduler: launchd com.quant.scheduler, 三时段 (08:30/09:30/15:30)
- 持仓: 002072凯瑞德*200 + 002767先锋电子*100
- 总资产: 4986 | PnL: -14 (-0.28%) | 现金: 1017

### 启动/重启
```bash
cd /Users/mariusto/project/quant && ./restart.sh
```

### 关键文件
| 文件 | 用途 |
|------|------|
| web/app.py | Flask API (state/positions/trades/factors/quotes/risk/stream/health) |
| web/state_broker.py | 跨进程状态 (Redis pub/sub, fallback 内存) |
| web/static/app.js | 前端仪表盘 (SSE + poll) |
| data/trade_repo.py | sim_trades 数据访问层 |
| data/trades.db | 交易记录 + strategy_config |
| data/market.db | 日线 + stocks + 因子快照 |
| execution/calendar.py | 交易日历 + 交易时段判定 |
| utils/logger.py | 统一日志 (app.log 日轮转 + quant.log JSON DEBUG) |
| scheduler.py | 三时段调度器 |
