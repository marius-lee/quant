# 架构问题修复记录 — 2026-05-30

基于全面审查报告的第三部分（架构问题 3.1-3.5）逐项修复。

---

## 3.1 删除死代码

| 文件 | 删除内容 | 减少 |
|------|---------|------|
| `factor/fundamental.py` | `FundamentalFactors` 类（无调用方） | 48行 |
| `data/store.py` | `sync_fundamentals_eastmoney()` + `_sync_fundamentals_tushare()` (仅定义，从未调用) | 94行 |
| `strategy/signals.py` | `generate_signals` 的 `quantile`/`threshold` 分支 + `method` 参数 | 21行 |

调用点适配：`engine/backtest_runner.py` 和 `strategy/signals.py` 的 `__main__` 去掉 `method=` 参数。

## 3.2 修复模块职责泄漏

- **新建 `factor/cache.py`** — 因子缓存增量更新逻辑从 `auto_run.py` 抽出为 `update_cache(store)` 函数，供 `auto_run.py` 和 Web app 共用
- **`auto_run.py`** — 删除内联的 `_update_factor_cache` 函数（171→88行），改为 `from factor.cache import update_cache`
- **`engine/builder.py:60`** — 去掉私有方法调用 `stocks_repo._query_symbols(...)` → 改用公开的 `stocks_repo.get_industry_mv(...)`
- **附带修复** `auto_run.py:115` 裸 `except: pass` → `except Exception: logger.warning(...)`

## 3.3 配置激活 — 回测参数

- **`engine/backtest_runner.py`** — `EventDrivenBacktest` 构造参数从 `config.yaml` 读取：`initial_capital`、`commission`、`slippage`、`max_weight`、`max_positions`
- **`backtest/event_engine.py`** — 新增 `max_positions` 参数，买入时按资金分配排序截断，当日持仓数 ≤ `max_positions`

## 3.4 Web 分析入口加因子缓存刷新

- **`web/app.py` `/api/run`** — `_bg()` 中在 `engine.run()` 前调用 `update_cache(engine.store)`，确保手动触发分析时使用最新因子数据

## 3.5 修复 /api/track 过度实例化

- **`web/app.py`** — 新增 `get_store()` 模块级惰性单例，`/api/track` 改用 `get_store().get_daily()` 替代 `get_engine().store.get_daily()`，避免为简单价格查询实例化完整推荐引擎
