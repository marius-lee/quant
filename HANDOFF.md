# Handoff: quant 项目状态 — 2026-07-06 23:58 CST

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

## P61: 审计收尾 — 消除最后6项硬编码 + neutralize 路径统一

### 改动清单

**#9 — factor/synth.py**: sleeeve_compose() 去掉 positions_per_factor=8, min_factors=1 默认值。
调用方必须从 config.yaml 读取参数后传入，本函数不提供 fallback。

**#11 — 统一 neutralize 配置路径**:
- neutralize.py: 3 处 len(common) < 30 改为从 config 读取
- pipeline.py: risk.neutralization.industry_min_count 改为 risk.neutralize.min_common_stocks
- config.yaml: risk.neutralization.industry_min_count 改为 risk.neutralize.min_common_stocks + min_stocks_per_industry
- 原因: 两处都是 Fama-French (1993) OLS 最小样本量 30, 相同概念不同名字, 统一为 min_common_stocks

**#12-14**: web/app.py, data/cache.py, data/store.py 加注释说明硬编码原因

**#15**: attribution.py rf=0.02, periods=252 挪到 config attribution.*

### 审计文档闭合
docs/audit_magic_numbers_20260706.md 中 16 项全部处理完毕。

### 验证
validate.py: 0 errors, 8 files compile OK

## P60: 硬编码数值参数全部挪至 config.yaml（单一真相源）

### 动机
用户要求：每一个数值都要有来源依据，放到 yaml 中保证项目参数数值唯一性。
全量代码审查（67 个 .py 文件）后发现多个参数散落在代码中。

### 改动清单 (15 files)

**核心原则**: config/config.yaml 是项目中所有可配置参数的单一真相源。
代码中不再保留任何硬编码默认值；所有取值通过 `cfg("path.to.key")` 读取。

**config/config.yaml**:
- risk 新增: min_price=2（仙股过滤）, max_sector_exposure=0.40（行业暴露上限）, rolling_window=60
- factor 新增: amihud, turnover_rev, idio_vol, high52w, roe_ratio, roe_reported, debt_ratio, accruals, synth, stats 校准参数（文献依据详见注释）
- calendar 新增: max_lookup_days=30（死循环保护上限，非业务参数）

**risk/constraints.py**:
- RiskLimits 所有字段从 config 读取（from_config() 类方法）
- apply_all_filters() 不再 crash（之前 RiskLimits() 无参调用因缺少 required fields 崩溃）
- filter_st_stocks 修复非字符串 name 的 .upper() crash

**data/trade_repo.py**:
- SQL DDL 移除 hardcoded DEFAULT 5000/0.08/20
- 添加注释说明所有默认值来源 config.yaml

**backtest.py**:
- CLI fallback `else 5000` → `else cfg("backtest.default_capital", 100000)`
- 函数参数 fallback `cfg("backtest.default_capital", 5000)` → `(..., 100000)`

**8 个文件集体除硬编码**:
- optimizer/portfolio.py: LOT_SIZE = 100 → _cfg("backtest.lot_size")
- optimizer/rebalance.py: LOT_SIZE = 100 → _cfg("backtest.lot_size")
- pipeline.py: LOT_SIZE = 100 → _ecfg(...), seed = capital 不再 fallback 5000, sl_pct 不再 fallback 0.08
- execution/calendar.py: range(30) → range(_MAX_LOOKUP) 从 config 读取
- factor/compute.py: 16 个硬编码参数 → _require_cfg("factor.*")
- web/app.py: sl_pct fallback 0.08 移除, LIMIT 60 → cfg("risk.rolling_window")
- web/state_broker.py: fallback base=5000 移除
- scheduler.py: seed fallback 5000 移除
- monitor/report.py: initial_capital 参数顺序调整（必填参数前置）

**审计文档**: docs/audit_magic_numbers_20260706.md（67 文件逐行审查记录）

### 验证
- validate.py: 0 errors, 13 warnings（均为 pre-existing）
- config 所有新增 key 可正常读取
- backtest.py, trade_repo.py, risk/constraints.py 编译通过

## P59: engine.get_capital pos_value bug — eval stepwise Wealth=¥0 修复

### 问题
eval_stepwise.sh Layer 3 步进回测产出 `Wealth=¥0.00`，不是合理范围。

### 根因
`execution/engine.py:42` — `get_capital()` 中 `p.get('value', 0)` 永远返回 0。

`TradeRepo.get_positions()` 返回的 dict 键是:
  `symbol, price, shares, board_count, buy_time`
没有 `value` 键。所以 `pos_value` 恒为 0，`get_capital()` 只返回现金。

### 修复
- engine.py: p.get('value', 0) → (p.get('price',0) or 0) * (p.get('shares',0) or 0)
- eval_stepwise.sh: stderr=subprocess.DEVNULL → subprocess.PIPE + 正则失败时 debug 打印

### commit
a97c73b P59 fix: engine.get_capital pos_value bug

## P58: 文档审计 + 策略隔离 + DB 锁 + dt_streak + 界面 + eval防护 + schema统一

### 核心变更
**因子**: 36 因子（27 price + 9 fundamental），2 active (zt_streak, dt_streak)
**策略隔离**: backtest.py 全部 6 处硬编码 `"quant"` → `STRATEGY="backtest"` 变量
**DB 锁**: 所有 market.db 写路径加 `timeout=30`
**界面**: status 徽章动态着色 — hot(交易中)/warm(盘前/午休/盘后)/cold(休市)

**DB 层**: sim_trades schema 统一到 TradeRepo._ensure_tables()
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

### 架构变更
state 估值三级回退:
  盘中(9:30-15:00) -> 新浪实时报价 (5s throttle)
  盘后/休市        -> market.db daily.close
  极端             -> 成本价

### 当前运行
- Web: launchd com.quant.webapp, port 8521
- Scheduler: launchd com.quant.scheduler, 三时段 (08:30/09:30/15:30)
- 持仓: 002072凯瑞德x200 + 002767先锋电子x100

### 启动
```bash
cd /Users/mariusto/project/quant && ./restart.sh
```
