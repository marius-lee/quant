# HANDOFF — 2026-07-22 test-v231

## 本次改动 (v225 → v226 → v227 → v228 → v229 → v230 → v231)

### v226 — daily_data turnover 回填改 baostock
- `daily_data.py`: `backfill_turnover_quotes`(tickflow,5只/批) → `backfill_turnover`(baostock,0.3s/只)
- tickflow 5457只需109min vs baostock 2622只需27min，来源: 2026-07-21 实测

### v227 — backfill_turnover 日期范围包含今天
- `store.py`: `gap_end_dt = _today` (废止 `_today-1`)，盘后当天 turnover 也能回填

### v228 — baostock 断连自动重登
- 重登阈值 5000→200 (实测 session ~240 只断连)
- Broken pipe 时检测并 `logout → login` 重连，不跳过后续股票

### v229 — 移除所有调度任务超时限制
- signals/execute/attribution/weekly_eval 全部 → None
- `_TIMEOUTS` 全线归零，每个任务自有逐阶段埋点日志判断死活

### v230 — monitor 午休跳过
- `monitor.py`: 循环内加 `11:30 ≤ t < 13:00 → sleep → continue`
- 时段标注统一为 `"09:35-11:30, 13:00-14:55 (午休跳过)"`
- 范围: monitor.py, orchestrator.py, __init__.py, status.py, CLAUDE.md

### v231 — attribution.py 全部 except Exception 吞错移除
- 11 个 `try/except Exception (non-fatal)` 全部删除，异常自然上抛
- 覆盖: Brinson, IC snapshot, promotion, OOS, G1-G4, R3-R4, benchmark
- 此前 `bc034e9` 声称消除但只加了 `raise` 未移除吞错，本次彻底清理

## 当前交易日业务流程
```
08:30  signals     Pipeline→因子→排名→_rank_concentrated分配→daily_signals
09:30  execute     读信号→涨停预检→封板重分配→compute_trades→挂限价单
09:35-11:30,13:00-14:55  monitor  每30s: 订单管理+止盈止损+集中度/VaR/流动性 (午休跳过)
19:00  daily_data  update_daily(OHLCV,tushare50只/批) → backfill_turnover(baostock,补turnover)
20:00  attribution Brinson→IC衰减→OOS→DSR→PnL→换手率→信号衰减 — 无超时无吞错
21:00  factor_cache 因子缓存刷新
```

## 关键文件

| 文件 | 最近改动 |
|------|----------|
| `quant/scheduler/orchestrator.py` | 全部超时 → None |
| `quant/scheduler/monitor.py` | 午休跳过 (11:30-13:00) |
| `quant/scheduler/daily_data.py` | turnover 回填 tickflow→baostock |
| `quant/scheduler/attribution.py` | 11 个吞错移除 |
| `quant/data/store.py` | gap_end_dt→_today; 断连重登; 进度+ETA |
| `web/app.py` | VERSION = "test-v231" |

---
# HANDOFF — 2026-07-22 test-v225

## 本次改动 (v224 → v225)

### v225 — daily_data 超时移除 + 进度日志优化

**背景**: daily_data 19:00 运行时超时 (1800s) 被 orchestrator kill。根因是 turnover 回填
(backfill_turnover_quotes) 用 tickflow 5 只/批×6s, 4157 缺口需 83 分钟, 远超 30 分钟超时。

**修改 1: orchestrator 移除 daily_data 超时**
- `orchestrator.py`: `_TIMEOUTS["daily_data"]` 从 `1800` → `None`
- 盘后 turnover 回填完成时间不可预测, 不应硬性 kill

**修改 2: update_daily 进度日志加 ETA**
- `store.py` `update_daily()`: 在 for 循环前加 `_t_loop` 计时器
- 每批进度日志加入 `elapsed=XXs ETA=XXs`, 可判断是正在拉取还是卡死

**修改 3: backfill_turnover_quotes 进度日志改密集**
- 添加 `_progress_interval = max(50, len(all_syms) // 20)` — 最少每 50 只打印一次
- 进度日志加入实际速率 `{rate:.1f}stocks/s` 和基于实际速率的 ETA
- 日志含 `today=N` 字段, 可观察是否在持续写入

## 当前交易日业务流程
```
08:30  signals    Pipeline→因子→排名→_rank_concentrated分配→daily_signals
09:30  execute    读信号→涨停预检→封板重分配(_rank_concentrated实时价)→compute_trades→挂限价单
09:35-14:55 monitor  每30s: 订单管理+止盈止损+集中度/VaR/流动性
19:00  daily_data  update_daily(OHLCV) → backfill_turnover_quotes(turnover) — 无超时限制
20:00  attribution 盘后归因
21:00  factor_cache 因子缓存刷新
```

## 关键文件

| 文件 | 最近改动 |
|------|----------|
| `quant/scheduler/orchestrator.py` | daily_data 超时 → None |
| `quant/data/store.py` | update_daily 进度加 ETA; backfill_turnover_quotes 进度加密 + 速率 ETA |
| `web/app.py` | VERSION = "test-v225" |

---
# HANDOFF — 2026-07-22 test-v223

## 本次改动 (v220 → v221 → v222 → v223)

### v220 — 修复 monitor daemon 每30秒崩溃
- `trade_repo.py`: `position_meta` 表加入 `_ensure_tables()` 自动建表
- `trade_repo.py`: `save_position_meta`: `self.conn.commit()` → `c.commit()` + `c.close()`
- `monitor.py`: 加 `TradeRepo`/`TRADE_DB` 导入, C6 load/save 加 try/except

### v221 — execute 封板重分配
- `execute.py`: sealed block 后，剩余候选用 `PortfolioConstructor.construct()` 重分配

### v222 — 重分配用实时价，对齐业界标准
- `execute.py`: 重分配改用 `_rank_concentrated()` 直接按实时价算，不走 `price_buffer`

### v223 — 撤回 post-fill residual sweep
- **撤回**了 v223 的 monitor sweep 逻辑
- 原因：残余现金是 lot-size 取整的正常残留，不是 bug
- 股价下跌补仓 = 无信号追跌，破坏优化器纪律
- 文档详见下方

## 残余现金处理策略

**决策：不在信号外追加买入。**

| 场景 | 处理 |
|------|------|
| 手数取整残余（¥668 < 一手 ¥722） | 正常现象，等下次 rebalance |
| 股价下跌，残余现金够买了 | **不追**。无 alpha 信号支持的买入 = 追跌，破坏纪律 |
| 第二天信号再次推荐同一只 | optimizer 会基于最新资本重新分配，自然补仓 |

业界标准（BlackRock/AQR/Two Sigma）：信号时刻决定仓位，残余现金不事后追投。
如果确实想用满资金，改进方向是优化器层面（允许碎股、降低手数粒度），不是事后 sweep。

## 当前交易日业务流程
```
08:30  signals    Pipeline→因子→排名→_rank_concentrated分配→daily_signals
09:30  execute    读信号→涨停预检→封板重分配(_rank_concentrated实时价)→compute_trades→挂限价单
09:35-14:55 monitor  每30s: 订单管理+止盈止损+集中度/VaR/流动性
19:00  daily_data  拉日线+换手率
20:00  attribution 盘后归因
21:00  factor_cache 因子缓存刷新
```
