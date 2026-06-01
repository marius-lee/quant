# 全面代码审计报告 — 2026-06-01

**审计人**: 系统架构师 / 资深软件开发工程师 / 量化软件开发专家
**审计范围**: 全部 ~45 个 Python 文件 + 前端 3 文件
**审计标准**: 北极星目标 ¥5000→¥100万 + 零冗余铁律
**测试**: 43 passed, 0 failed

---

## 审计发现总览

| 严重度 | 数量 | 分类 |
|--------|------|------|
| 🔴 致命 | 2 | 数据损坏风险 |
| 🟠 高 | 8 | 因子错误、死代码、数据漂移 |
| 🟡 中 | 12 | 重复逻辑、文档漂移、API 问题 |
| 🟢 低 | 14 | 代码质量、边界情况 |

---

## 🔴 致命 (CRITICAL)

### C1: `data/store.py:163-168` — MAX(date) 导致股票数据缺口

```python
batch_max = conn.execute("SELECT MAX(date) FROM daily WHERE symbol IN (...)", chunk).fetchone()[0]
```

10 只股票取一个 `MAX(date)`。如果股票 A 最新日期是 2026-01-20，股票 B 是 2026-01-10，那么 `batch_start=20260120`。股票 B 的 1月11-19日数据永久丢失，后续运行不会自愈。应该用每只股票的 `MIN(batch_max)`。

### C2: `factor/game_theory.py:40` — Nash distortion 因子被 kurtosis 常量淹没

```python
(f"nash_distortion_{w}d", ret.rolling(w).skew().abs() + (ret.rolling(w).kurt() - 3).abs())
```

`pandas.Series.kurt()` 返回 Fisher 超额峰度（正态分布=0），代码又减了 3。结果：每只股票获得 ~3.0 的固定偏移，完全淹没真实波动，4 个 Nash distortion 因子全部退化。

---

## 🟠 高 (HIGH)

### H1: `data/store.py:17-18` — `_ts_code` 将北交所股票映射到深交所

```python
def _ts_code(sym): return f"{sym}.SH" if sym.startswith(("6","9","68")) else f"{sym}.SZ"
```

北交所以 `4`/`8`/`92` 开头的股票被错误映射为 `.SZ`。Tushare 期望 `.BJ`。所有北交所股票的日线数据拉取失败。

### H2: `engine/tracker.py:160` — `all_stocks_avg` 求和而非平均，超额收益计算错误

```python
all_stocks_avg = close_data.pct_change().mean(axis=1).sum() * 100
```

对日均收益求和而不是计算复利回报。追踪表中的 `benchmark_chg` 和 `excess_return` 全部不对。

### H3: `factor/dragon_tiger.py` + `factor/limit_up_pattern.py` — 死代码

两个文件共 ~216 行，从未被任何生产模块导入。不在 `cache.py`、`pipeline.py`、`ranker.py` 中。龙虎榜和涨停板识别功能完全未接入。

### H4: `execution/broker.py` + `order_manager.py` + `risk_checker.py` — 死代码

三个文件共 ~300 行，从未被任何生产模块导入。仅 `monitor.py`（/api/monitor）有接线。

### H5: `data/fundamental.py:137` — 提交检查点永远无法到达

```python
if (i + batch_size) % 1000 == 0: conn.commit()
```

`batch_size=60`，i 值 0,60,120...,900,960→跳过1000→1020。1000 不能被 60 整除，中间提交永不触发。进程崩溃时所有基本面数据丢失。

### H6: `static/echarts.min.js` — 1MB 死文件

放在 `./static/`（项目根），Flask 从 `web/static/` 取静态文件。从未被 HTML 加载，但 CLAUDE.md 声称它存在。

### H7: `engine/builder.py:69` — `change_5d` 可能产生 NaN

```python
"change_5d": round(float(((1 + perf.iloc[-5:]).prod() - 1) * 100), 2)
```

`perf` 来自 `pct_change()`，首行为 NaN。如果股票数据不足 5 天，`(1+NaN).prod() = NaN`，JSON 无法序列化。

### H8: `engine/sim_broker.py:71` — 卖出侧缺少 `price <= 0` 防护

买入侧有 `if price <= 0: continue`（bugfix 已加固）。卖出侧无此防护，如果推荐中 `last_price` 为 0，仓位将零元卖出。

---

## 🟡 中 (MEDIUM)

### M1: `builder.py:25-30` ≈ `backtest_runner.py:39-45` — `_affordable_filter` 逻辑重复
### M2: `backtest_runner.py:121-138` ≈ `rebalance.py:206-224` — 基准对比逻辑重复
### M3: `web/pipeline.py:71-73` — `generate_signals`/`generate_weights` 在 default 路径中计算后丢弃
### M4: `web/app.py:140` — `/api/track` 无 try/except，DirectPrice 查询可能直接 500
### M5: CLAUDE.md — API 路由缺少 7 个新端点，架构缺少 4 个引擎模块
### M6: config.yaml — `strategy.model`/`factor.na_fill`/`factor.normalize`/`factor.winsorize` 死配置键
### M7: config 默认值漂移 — `max_stock_price`(50 vs 30)、`min_daily_amount`(100K vs 5M)、`start_date`(2023 vs 2020)
### M8: 多条路径连接泄漏 — 无 try/finally 保护（~6 个方法）
### M9: `dates.py` — 整个模块 67 行死代码，无人导入
### M10: `repository.py:139` — `_norm_date` 重复 `dates.py`，且 `pd.Timestamp` 输入会生成畸形日期
### M11: `/api/track` 和 `/api/tracking` — 两个不同的追踪端点返回不同数据源
### M12: `app.py:134` — `/api/track` 在请求处理里面 import pandas

---

## 🟢 低 (LOW)

14 项（代码质量、魔法数字、废弃语法、CSS 斑马纹颜色、JS 隐式全局变量等）。详见完整报告。

---

## 立即可修的高优项（6 项）

| 优先级 | 问题 | 预估 |
|--------|------|------|
| 1 | C2: `game_theory.py` kurtosis 减 3 | 1 行 |
| 2 | C1: `store.py` batch_max→per-symbol | 10 行 |
| 3 | H1: `_ts_code` 支持北交所 | 2 行 |
| 4 | H2: `tracker.py` 超额收益改为复利 | 3 行 |
| 5 | H8: `sim_broker.py` 卖出 price≤0 | 2 行 |
| 6 | H7: `builder.py` NaN fallback | 1 行 |
