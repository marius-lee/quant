# HANDOFF — 2026-07-23 test-v244

## 本次改动 (v240 → v241)

### v241 — 修复 B8: Python 3.13+ Formatter defaults 依赖

- `logger.py:73`: `logging.Formatter(defaults={"trace_id":""})` → `_TraceFormatter` 子类
- 自定义 `format()` 方法注入 `record.trace_id` 默认值，兼容 Python 3.10+

### v240 — 修复 B5: LHB sync 中 trade_date 未定义

- `store.py:1531`: 被删注释行导致 `trade_date` 变量丢失，下一行 `validate_date_format(trade_date)` 报 NameError
- 修复：从 row 提前提取 `trade_date = to_str(row.get("上榜日", ...))`，验证和 INSERT 复用同一值

# HANDOFF — 2026-07-23 test-v239

## 本次改动 (v238 → v239)

### v239 — 修复 B6 (sync_all 参数名) + B4 (JQ 缓存方法名)

- B6: `store.py:1486` — `sync_all(conn, max_pb_fetch=-1)` → `max_fetch=-1`，符合 `fundamental.py` 的签名
- B4: `jq_valuation.py:140,147` — `_cache.put()` → `_cache.set()`，符合 `DataCache` API

# HANDOFF — 2026-07-23 test-v238

## 本次改动 (v237 → v238)

### v238 — 修复 TradeRepo.__init__ 参数顺序 bug

v237 迁移时 `db_manager` 在前，导致 `TradeRepo(TRADE_DB)` (positional arg) 把字符串赋给 db_manager，
`self._db.get_connection()` 报 `'str' object has no attribute 'get_connection'`。
修复：`db_path` 放首位。

### v237 — B7: TradeRepo 完整迁移到 repos/ + DatabaseManager

**背景**: 两个 TradeRepo 并存（`data/trade_repo.py` 470行 vs `repos/trade_repo.py` 94行空壳），
schema 不兼容、连接方式不统一。审计报告 B7。

**迁移内容**:

| 操作 | 文件 | 说明 |
|------|------|------|
| 重写 | `quant/data/repos/trade_repo.py` (→418行) | DatabaseManager 单例获取连接；完整 26 方法；自动建表 |
| 删除 | `quant/data/trade_repo.py` | 旧实现，raw sqlite3.connect() per-call |
| 更新 | 12 文件 × 27 处 import | `from quant.data.trade_repo import TradeRepo` → `from quant.data.repos import TradeRepo` |
| 清理 | `quant/execution/engine.py` | 删除 `TRADE_DB_DEFAULT`/`MARKET_DB` 本地定义 → `from quant.config.paths import TRADE_DB, MARKET_DB`；移除临时 `_ensure_tables()` hack |

**设计要点**:
- `_conn()` → `DatabaseManager.get_connection(db_path)` — 连接复用，不再 per-call open/close
- 所有方法去掉 `c.close()` — DatabaseManager 管理连接生命周期
- `record_trade(conn=...)` 保留外部连接参数 — engine.py 事务分组语义不变
- `TRADE_DB` 唯一定义在 `quant/config/paths.py`

**删除了什么**:
- `data/trade_repo.py` — 470行旧实现（raw sqlite3 连接）
- `repos/trade_repo.py` 旧版 — 94行空壳（key-value schema）
- `engine.py` TRADE_DB_DEFAULT/MARKET_DB 本地定义 + _ensure_tables() hack
- 27处旧 import 路径

**验证**: smoke_test_v190.py 8/8 ✅

# HANDOFF — 2026-07-23 test-v236

## 本次改动 (v235 → v236)

### v236 — attribution.py L3 修复`active_names`未定义

- 删除旧 IC 块时 `active_names` 变量（旧块中定义，新块中引用）被误删
- 改为 `_all_monitored_names`，从 `FactorRepo.get_all_by_status(('active','monitoring'))` 实时获取
- 修复后 L1/L2/L3 三级检测全链路贯通：归因补跑成功完成，36/37 因子从 active → monitoring

## 验证确认

归因补跑 07-22（trace_id=a07b0db588da）:
- G1 OOS walk-forward: 12/37 因子衰减
- L1 (滚动 IC 突变): 7 因子
- L2 (OOS/IS 反转/衰减): 14 因子
- L3 状态变更: active → monitoring 正常触发
- factor_ic_daily: 2279 行写入
- ic_mean 同步到 factor_registry: 完成

# HANDOFF — 2026-07-23 test-v235

## 本次改动 (v234 → v235)

### v235 — attribution.py 修复 _require_cfg 未导入
- 删除旧 IC 块时 _require_cfg import 一起被移除，新三级检测块引用时报 NameError
- 补上 `from quant.config.constants import _require_cfg`

### v234 — 零 fallback: phase3_oos.py RuntimeError
### v233 — 因子健康监控闭环（三级检测体系）

---

# HANDOFF — 2026-07-23 test-v234

## 本次改动 (v233 → v234)

### v234 — 零 fallback: phase3_oos.py 数据不足时上抛 RuntimeError
- `phase3_oos.py`: 移除 silent fallback。Phase 2 未传入 IC 序列且 `factor_snapshot.ic_series` 不可用时
  不再静默跳过，改为 raise RuntimeError 明确指引用户先运行 factor_cache 或补充 factor_ic_daily 数据。
- 对齐零 fallback 原则（CLAUDE.md 硬约束）

### v233 — 因子健康监控闭环（见上一段）

---

# HANDOFF — 2026-07-22 test-v233

## 本次改动 (v232 → v233)

### v233 — 因子健康监控闭环（对标 AQR/WorldQuant 三级检测体系）

**背景**: 归因任务的 IC 衰减检测读取静态 `factor_registry.ic_mean`（永不更新），降级永远不触发。
G1 OOS Walk-Forward 正确计算了每个因子的 IS_IR/OOS_IR，但结果只记日志不做动作。

**修改概要**:

| 文件 | 改动 |
|------|------|
| `quant/data/repos/factor_repo.py` | 新建 `factor_ic_daily` 表 + `insert_ic_daily`/`get_ic_rolling`/`sync_ic_mean_to_registry` 方法；删除 `save_ic_snapshot`/`get_recent_ic_snapshots`/`delete_old_ic_snapshots` |
| `quant/scheduler/oos_verify.py` | `run_oos_check` 返回增加 `ic_daily` 字段（透出逐日 IC 序列） |
| `quant/scheduler/attribution.py` | 删除 L106-222（static ic_mean 整块）；新增三级检测：L1 滚动 IC → L2 OOS/IS 比率 → L3 稳定性校验 → 执行状态变更 |
| `quant/factor/stats_cache.py` | 删除 `ic_series` 写入 factor_snapshot |
| `quant/scheduler/factor_cache.py` | 刷新后同步 `ic_mean` 到 `factor_registry` |
| `quant/config/config.yaml` | 替换 attribution 配置：`oos_warning_decay`/`oos_recovery_threshold`/`monitoring_buffer_days` 20d |
| `quant/data/schema.sql` | 待更新：移除 factor_ic_snapshot，添加 factor_ic_daily |

**数据表变更**:
- `factor_ic_daily`: 新建，每因子每天一行，是因子绩效唯一真相源
- `factor_ic_snapshot`: 已删除（旧 JSON blob 表，存静态假数据）
- `factor_snapshot.ic_series`: 不再写入（已有字段不清除）

**三级检测体系**:
```
Level 1: 滚动 IC 监控 (factor_ic_daily) — 20日均值 vs 当前值
Level 2: OOS/IS 比率 (G1 per_factor) — OOS_IR<0 立即降级 / 比率<0.3 严重衰减
Level 3: 稳定性校验 (factor_ic_daily) — 连续 N 天稳定 → 升回 / 持续衰减 → 退役
```

## 当前交易日业务流程
```
08:30  signals     Pipeline→因子→排名→_rank_concentrated分配→daily_signals
09:30  execute     读信号→涨停预检→封板重分配→compute_trades→挂限价单
09:35-11:30,13:00-14:55  monitor  每30s: 订单管理+止盈止损 (午休跳过)
19:00  daily_data  update_daily(OHLCV) → backfill_turnover(baostock)
20:00  attribution Brinson→G1 OOS→L1+L2+L3 三级检测→状态变更→G2 拥挤度→G3 DSR
21:00  factor_cache 因子物化→factor_snapshot→同步 ic_mean
```

## 关键文件

| 文件 | 最近改动 |
|------|----------|
| `quant/data/repos/factor_repo.py` | factor_ic_daily 表 + 新方法 + 删旧快照方法 |
| `quant/scheduler/attribution.py` | IC 块替换为三级检测体系 |
| `quant/scheduler/oos_verify.py` | 返回增加 ic_daily 透出 |
| `quant/factor/stats_cache.py` | 删除 ic_series 写入 |
| `quant/scheduler/factor_cache.py` | 加 sync_ic_mean |
| `quant/config/config.yaml` | attribution 配置重构 |
| `web/app.py` | VERSION = "test-v233" |

---

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
