# Quant 项目全面审查报告

**身份：幻方量化系统架构师 | 日期：2026-05-30**

---

## 一、已实现功能清单

| 层级 | 功能 | 状态 |
|------|------|------|
| 数据层 | SQLite 存储（stocks + daily + factors_cache） | ✅ |
| 数据层 | tushare 批量日线拉取 + 增量更新 | ✅ |
| 数据层 | 腾讯财经批量基本面（PE/PB/市值/股息/52周，60只/批） | ✅ |
| 数据层 | Repository 模式（StockRepo/FactorRepo/PriceRepo） | ✅ |
| 因子层 | 技术因子 20个（动量/反转/波动/均线偏离/量比/回撤×4窗口） | ✅ |
| 因子层 | 博弈论因子 28个（Amihud/PIN/羊群/价差/Kyle/信息到达/Nash×4窗口） | ✅ |
| 因子层 | 真实基本面因子 8个（EP/BP/股息/CF/市值/52周位置/换手率） | ✅ |
| 因子层 | 基本面代理因子 7个（规模/价值/质量/成长） | ✅ |
| 因子层 | 妖股信号检测（量能突变/价格突破/波动异常/连续强势） | ✅ |
| 因子层 | 向量化 Rank IC 筛选（分组 rank 相关性，比逐日快27x） | ✅ |
| 策略层 | 5模型集成（LightGBM+XGBoost+RF+ET+Ridge），IC 加权 | ✅ |
| 策略层 | 信号生成（long_only/quantile/threshold）+ 仓位权重 | ✅ |
| 回测层 | 事件驱动回测（T+1/手续费万三/滑点千一/成交量约束5%） | ✅ |
| 回测层 | 绩效指标（夏普/年化/最大回撤/Calmar/胜率/Alpha/Beta/IR） | ✅ |
| Web UI | Flask 服务（KPI条/推荐表+sparkline/行业分布/回测面板/Tab） | ✅ |
| 自动化 | launchd 定时（08:00/16:00），因子缓存增量更新 | ✅ |
| 基础设施 | 日志轮转、YAML 配置加载、结果持久化 | ✅ |

---

## 二、未实现/待完成功能

### 2.1 配置未落地（config.yaml 是摆设）

config.yaml 定义了 50+ 个配置项，但实际被代码读取的不到 10 个。以下配置项**定义了但从未生效**：

| 配置项 | 设定值 | 实际代码行为 |
|--------|--------|-------------|
| `strategy.model` | `lightgbm` | 始终用全部5个模型 |
| `strategy.train_window` | `504` | 未使用，固定 70/30 划分 |
| `strategy.retrain_freq` | `21` | 未使用，无滚动重训练 |
| `backtest.max_positions` | `30` | 未使用，无持仓数限制 |
| `backtest.benchmark` | `000300` | 未使用，回测无基准对比 |
| `risk.*` (全部3项) | 15%/30%/5% | **完全未实现** |
| `data.frequency` | `daily` | 未使用 |
| `data.universe` | `hs300` | 未使用，始终全A股 |
| `factor.normalize` | `zscore` | 硬编码在 base.py |
| `factor.winsorize` | `mad` | 硬编码在 base.py |

### 2.2 缺失的核心功能

1. **风控完全缺失** — 止损、行业暴露限制、日亏损限制三项配置定义了但零实现
2. **滚动重训练** — 模型只在固定窗口训练一次，没有滚动更新
3. **基准对比** — 回测无沪深300基准，看不到超额收益
4. **自动因子生成（WorldQuant 风格）** — README 提到100个，实际未实现
5. **通知告警** — 新旧推荐变化、高分信号无推送
6. **K线图** — 之前讨论过但未实现
7. **实时价格刷新** — `/api/track` 已实现后端，但前端不自动刷新

---

## 三、架构问题

### 3.1 死代码

- **`factor/fundamental.py` — `FundamentalFactors` 类**：需要 `stock_info` DataFrame 入参，但整个代码库没有任何调用方。实际使用的是 `FundamentalCrossSection` 和 `real_fundamental.compute()`。
- **`data/store.py` — `sync_fundamentals_eastmoney()` 和 `_sync_fundamentals_tushare()`**：东方财富逐只 API 极慢且有硬编码日期 `"20260527"`，从未被调用。`sync_fundamentals()` 只委托到 `data/fundamental.sync_all()`。
- **`strategy/signals.py` — `generate_signals` 的 `quantile` 和 `threshold` 方法**：定义了但从未使用，只有 `long_only` 被调用。

### 3.2 模块职责泄漏

- **`auto_run.py` 里写死了因子计算逻辑**（第18-100行）：直接 import TechnicalFactors、GameTheoryFactors 等并拼装。这本应是 `factor/` 模块提供的统一接口，不应该让调度脚本知道因子计算细节。
- **`engine/builder.py:60` 调用私有方法** `stocks_repo._query_symbols(...)` — 应该给 StockRepo 加一个公开方法或让 builder 直接调 `get_industry_mv`。

### 3.3 配置未激活

`backtest/event_engine.py` 的 `__init__` 参数和 `engine/backtest_runner.py` 的回测创建都是硬编码的，完全不读 config。

### 3.4 Web 分析入口无缓存更新

用户点击 `/api/run` 时直接跑 pipeline，但**不先更新因子缓存**。如果数据库刚清空过或新增了交易日，会因缓存过期而产出错误结果。`auto_run.py` 是有这步的。

### 3.5 `/api/track` 过度实例化

```python
prices = get_engine().store.get_daily(symbols)  # app.py:89
```
`get_engine()` 创建完整的 `RecommendationEngine`（含 `DemonSignals()` 等），只是为了一次简单的价格查询。应该直接用 `DataStore`。

---

## 四、代码逻辑问题

### 4.1 异常静默吞没（3处未修复）

| 位置 | 代码 | 后果 |
|------|------|------|
| `engine/predictor.py:28` | `except Exception: continue` | 预测失败的分批静默丢弃，无日志 |
| `engine/backtest_runner.py:33` | `except Exception: return pd.Series(dtype=float)` | 某日模型预测失败→当日空仓，无日志 |
| `auto_run.py:112-115` | `except: pass` | 历史推荐解析失败静默跳过（甚至不是 `except Exception`） |

注：`engine/ranker.py` 的 `_neutralize` 异常吞没问题已在 2026-05-30 修复。

### 4.2 IC 计算类型判断脆弱

`factor/screening.py:56-57`：
```python
mean_ic = float(ics.mean().iloc[0]) if hasattr(ics.mean(), 'iloc') else float(ics.mean())
```
依赖 `hasattr` 检测 Series vs scalar，逻辑绕。应使用更直接的方式。

### 4.3 `factor/game_theory.py` 列构造冗余

内层循环对每个窗口都遍历 `close.columns`，但其实所有窗口的股票列是相同的。可以提到外面。

### 4.4 `strategy/ensemble.py` 模型训练异常无日志

```python
def _score(model, name):
    try:
        model.fit(X_tr, y_tr)
        ...
    except Exception:
        return None  # 哪个模型失败了？永远不知道
```
如果 LightGBM 或 XGBoost 训练失败，没有任何日志告知。

---

## 五、总结

**整体评价**：系统核心链路完整且设计合理（数据→因子→筛选→集成→回测→Web），代码质量在重构后大幅提升。当前的主要矛盾不是架构，而是**配置未落地导致大量设计意图没生效**和**风控完全空白**。

**优先级排序**：

| 优先级 | 事项 | 原因 |
|--------|------|------|
| P0 | 风控止损实现 | 实盘无止损等于裸奔 |
| P0 | 异常吞没加日志 | 排查问题的基础 |
| P1 | 配置激活（至少 model/risk/benchmark） | config 写了就要用 |
| P1 | Web `/api/run` 加因子缓存刷新 | 否则结果不准 |
| P2 | 删除死代码 | 减少维护负担 |
| P2 | factor 缓存逻辑抽出 `auto_run.py` | 模块职责清晰 |
| P3 | 基准对比、滚动重训练 | 增强功能 |
