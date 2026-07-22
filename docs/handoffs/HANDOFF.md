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
