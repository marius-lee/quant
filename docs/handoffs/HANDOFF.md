# HANDOFF — 2026-07-22 test-v222

## 本次改动 (v220 → v221 → v222)

### v220 — 修复 monitor daemon 每30秒崩溃

| 文件 | 修改 |
|------|------|
| `quant/data/trade_repo.py` | `position_meta` 表加入 `_ensure_tables()` 自动建表 |
| `quant/data/trade_repo.py` | `save_position_meta`: `self.conn.commit()` → `c.commit()` + `c.close()` |
| `quant/data/trade_repo.py` | `get_position_meta`: 加 `c.close()` |
| `quant/scheduler/monitor.py` | 加 `from quant.data.trade_repo import TradeRepo` |
| `quant/scheduler/monitor.py` | 加 `from quant.config.paths import TRADE_DB` |
| `quant/scheduler/monitor.py` | C6 load: `TradeRepo(db_path=DB)` → `TradeRepo(db_path=TRADE_DB)` + try/except |
| `quant/scheduler/monitor.py` | C6 save: `if _rm is not None` 守卫 + try/except |
| `web/app.py` | VERSION → test-v220 |

### v221 — execute 封板重分配

| 文件 | 修改 |
|------|------|
| `quant/scheduler/execute.py` | 加 `from quant.optimizer.portfolio import PortfolioConstructor` |
| `quant/scheduler/execute.py` | sealed block: 封板股被删后，调 `PortfolioConstructor.construct()` 重分配剩余候选 |
| `web/app.py` | VERSION → test-v221 |

### v222 — 重分配用实时价，对齐业界标准

| 文件 | 修改 |
|------|------|
| `quant/scheduler/execute.py` | 重分配改用 `_rank_concentrated()` 直接按实时价算，不走 `construct()` 的 price_buffer |
| `web/app.py` | VERSION → test-v222 |

## 当前交易日业务流程

```
08:30  signals    Pipeline: 加载数据→算因子→过滤→排名→_rank_concentrated分配→写daily_signals
09:30  execute    读信号→拉报价→涨停封死预检→重分配(封板释放资金)→compute_trades→挂限价单
09:35-14:55 monitor  每30s: 订单管理+止盈止损+集中度/VaR/流动性监控
19:00  daily_data  拉日线+换手率
20:00  attribution 盘后归因
21:00  factor_cache 因子缓存刷新
```

## 重分配逻辑 (v222)

execute 发现封板股 → 从 targets 删除 → 剩余候选用 `_rank_concentrated` + 实时价重算 → 无缓冲价 → 满仓贪心

示例: 001258(封板) 被跳过后，¥4,257 全部分给 600744 → 500 股(100旧+400新) → 对比旧版只买100股

## 待后续

- 信号层涨停预过滤（昨天封板的不推荐）
- 追高判断逻辑
- VaR 动态日期
