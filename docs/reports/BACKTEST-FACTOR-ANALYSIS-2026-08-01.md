# 回测策略与因子计算分析报告

**生成日期**: 2026-08-01  
**分析范围**: 项目全部 Python 代码（不含 README/文档/CHANGELOG 等文本说明）  
**分析重点**: 回测策略业务逻辑、因子计算正确性、与业界标准对齐度、重大/隐性 bug  
**结论摘要**: 项目已具备研究级量化平台骨架，但存在多处足以扭曲回测结果、导致实盘亏损的 bug，必须修复后方可考虑上线。

---

## 一、总体结论

项目回测框架已经具备**研究级量化平台的核心要素**：walk-forward、T+1、成本、风控、止损、冷却期、因子评估、PBO 过拟合检测等。但在**因子计算正确性、回测/实盘执行一致性、业务逻辑严谨性**上存在多处**实质性 bug 与重大隐患**，部分因子甚至方向错误或与名称不符。若直接用于实盘，存在较大风险。

---

## 二、回测策略业务逻辑分析

### 2.1 已对齐业界标准的部分

| 模块 | 实现 | 评价 |
|---|---|---|
| 7 层流水线 | 数据 → 因子 → Alpha → 风险 → 优化 → 执行 → 监控 | 与 Qlib/WorldQuant 研究框架一致 |
| 调仓频率 | `optimizer.rebalance_freq` 支持 daily/weekly | 符合量化策略常规 |
| T+1 执行 | 回测用次日开盘价成交 | 正确模拟 A 股 T+1 |
| 交易成本 | 佣金、印花税、滑点、冲击成本模型 | 覆盖主要成本项 |
| 风控 | 硬止损、ATR 止盈止损、行业/市值中性化、集中度 | 较完整 |
| 冷却期 | 止损后 N 天禁止买回 | 防止反复亏损 |
| Walk-forward IC | 按 `retrain_freq` 滚动重算 IC | 防前视 |
| PBO 门禁 | Phase 3 未通过则拒绝回测 | 防过拟合 |
| 复利 sizing | 用当日收盘 MTM 权益复利 | 比早期版本正确 |

### 2.2 回测业务逻辑中的重大 bug

#### 🔴 Bug 1: 回测 combine_mode 切换失效（严重）

**位置**: `quant/backtest/loop.py`

```python
if i >= warmup_days:
    kwargs["combine_mode"] = combine_mode or None
```

`None` 时 `generate_signals` 回退到 config 默认 `sleeve`，**永远切不到 `ic_weighted`**。代码注释声称 warmup 后用 walk-forward ic_weighted，实际未生效。

**影响**: 回测全程使用 sleeve 模式，因子组合方式与预期的 walk-forward 不一致，导致回测结果不能代表真实策略表现。

#### 🔴 Bug 2: 回测/实盘执行语义分裂

- 回测：`BacktestExecutionModel` 在开盘价 100% 成交。
- 实盘：`LiveExecutionModel` 挂限价单，盘中 `OrderManager` 按 ask/时间紧迫度/尾盘 force_fill 成交。

限价单可能全天不成交（尤其小盘股/涨停股），回测的 100% 成交假设会**系统性高估流动性差股票的收益**。

#### 🔴 Bug 3: 实盘成本模型未读配置

**位置**: `quant/scheduler/execute.py`

```python
cost_model = CostModel()          # 默认印花税 0.1%
# 应为 CostModel.from_config()     # config 中为 0.05%
```

直接影响实盘执行时的订单裁剪与资金估算，但回测路径（`pipeline.py`）是正确的。若用实盘参数优化后回测，结果会失真。

#### 🔴 Bug 4: 真实券商路径重复落账

**位置**: `quant/scheduler/order_manager.py` 的 `_fill()`

1. 先调用 `broker_adapter.buy()` 下真实单；
2. 无论是否成交，再调用 `engine.execute()` 写入 `sim_trades`。

真实券商订单未成交时，账本已记录买入，造成**现金/持仓虚高**。

#### 🔴 Bug 5: 监控止盈止损不落账

**位置**: `quant/scheduler/monitor.py` 的 `_execute_sell()`

- 优先走 `broker_adapter.sell()`；
- 成功后**没有同步写入 `sim_trades`**。

`ExecutionEngine` 是账本唯一真相源，此路径会导致持仓/现金与实际不一致。

### 2.3 回测中的隐性 bug / 逻辑缺陷

| 问题 | 位置 | 说明 |
|---|---|---|
| 未做 paper trading 桥接 | 全局 | 缺少用真实行情但不真下单的验证层 |
| 固定滑点 vs 动态冲击 | `execution/cost.py` | 回测用固定滑点，未用 Almgren-Chriss 动态冲击 |
| 涨停检测未分板块 | `factor/compute/price/_alternative.py` `compute_limit_touch_no_seal` | 统一用 10%，科创板/创业板/北交所错误 |
| 浮点相等判断涨停 | `_event.py` `compute_limit_up_streak`/`compute_dt_streak` | `close == high` 不可靠 |
| 熔断只在实盘 | `execution/execution_model.py` | 回测无熔断概念 |
| 行情源硬编码字段 | `execution/quote.py` | 腾讯/新浪 API 字段索引写死，接口变化即失效 |
| daily_risk 写错库 | `risk/var.py` `update_daily_risk` | 应写入 `trades.db`，实际写入 `market.db` |
| HMM regime 模型可能陈旧 | `regime/detector.py` | 实盘用 pickle 缓存，未验证重训练周期 |

---

## 三、因子计算分析

### 3.1 因子体系概况

项目实现了 **100+ 因子**，覆盖：

- 价量：动量、反转、波动率、偏度、Amihud、RSI、均线排列、量比、龙虎榜等
- 基本面：PE/PB、ROE/ROA、SUE、毛利率、资产增长、Piotroski F-Score 等
- 另类：新闻情绪、北向资金、融资融券、基金持仓、日内快照等
- Alpha101 / 幻方 Tier S / 缺失因子补充

### 3.2 重大因子计算 bug

#### 🔴 Bug A: `reversal_5d` 实际不是反转，而是动量

**位置**: `quant/factor/compute/price/_momentum.py`

```python
def compute_reversal(data, date, window=5):
    ...
    cum = log_ret.iloc[start:idx + 1].sum()
    return _cs_zscore(cum).rename(f"reversal_{window}d")
```

与 `compute_momentum(window=5)` **公式完全相同**：

```python
def compute_momentum(data, date, window):
    ...
    cum = log_ret.iloc[start:idx + 1].sum()
    return _cs_zscore(cum).rename(f"momentum_{window}d")
```

**影响**: `reversal_5d` 与 `momentum_5d` 完全共线，因子库存在冗余/错误信号。sleeve 模式下两者独立选 top-N，会选入同一批股票，放大动量敞口而非提供反转保护。

#### 🔴 Bug B: 特质波动率 beta 对齐错误

**位置**: `quant/factor/compute/price/_momentum.py` `compute_idiosyncratic_vol`

```python
beta = np.dot(ri_c, bm_c[:len(ri_c)]) / bm_var
resid = ri_c - beta * bm_c[:len(ri_c)]
```

用 `bm_c[:len(ri_c)]` 按位置截断基准收益，而非按日期对齐。若股票停牌，股票收益与基准收益日期错位，beta 估计错误。

#### 🔴 Bug C: 换手率反转 fallback 混用单位

**位置**: `quant/factor/compute/price/_momentum.py` `compute_turnover_reversal`

```python
# 正常路径
result = -(s / l.replace(0, np.nan) - 1)   # turnover 是百分比

# fallback 路径
result = -(vs / vl.replace(0, np.nan) - 1) # volume 是股数
```

`turnover` 与 `volume` 量纲不同，fallback 后因子含义完全改变。

#### 🔴 Bug D: 量价相关性用价格水平而非收益率

**位置**: `quant/factor/compute/price/_momentum.py` `compute_volume_price_corr`

```python
corrs = c_slice[sym].corr(v_slice[sym])   # close level vs volume level
```

通常应使用 **收益率 vs 成交量变化**，价格水平与成交量可能伪相关（如高价股自然成交量低）。

#### 🔴 Bug E: 涨停距离/触板未封未分板块

- `compute_limit_touch_no_seal` 统一用 `pre * 1.10`；科创板（20%）、北交所（30%）会被误判为未涨停。
- `compute_limit_up_streak` 虽通过 `aux["stocks"]` 分板块，但用 `close == high` 浮点相等判断，存在精度风险。

#### 🔴 Bug F: 幻方 MIF 因子实现与文档不符

**位置**: `quant/factor/compute/price/_huanfang.py` `compute_mif`

- 文档称：`MIF = |隔夜收益| × corr(turnover, mean_turnover, window)`
- 实现：`MIF = |隔夜收益| × |turnover_t - mean(turnover)| / std(turnover)`

用偏差替代了相关性，与声称的研报逻辑不一致。

#### 🔴 Bug G: 量价背离度使用价格水平

**位置**: `quant/factor/compute/price/_huanfang.py` `compute_vp_divergence`

计算 `close` 与 `volume` 的相关性，同样应使用收益率与成交量变化。

#### 🟠 Bug H: 基本面因子符号与理论相反

**位置**: `quant/factor/compute/fundamental.py`

```python
def compute_bp_ratio(...):
    bp = 1.0 / fundamentals["pb"]
    return _cs_zscore(-bp, sparse=True).rename("bp_ratio")  # 高 PB 得高分

def compute_size(...):
    return _cs_zscore(size, sparse=True).rename("size")      # 大盘股得高分
```

代码注释承认是“IC 实证”驱动，但这与因子名称/经济学含义相反。若 IC regime 变化，策略会迅速失效。应通过配置化方向管理，而不是硬编码反向。

#### 🟠 Bug I: 基本面因子大量直接查库

多数财务因子（如 `compute_gross_margin_diff`、`compute_financial_anomaly`、`compute_roe_trimmed`）在函数内部调用 `_get_financial_historical()` 查询 SQLite，而非使用 `compute_all_factors` 传入的 `preloaded_financials`。这在批量物化时性能极差，且 `_get_financial_historical` 未按 symbol 过滤，加载全表数据。

#### 🟠 Bug J: `fund_flow` aux 预加载丢失日期维度

**位置**: `quant/factor/compute/_preload.py`

```python
result["fund_flow"] = df.set_index("symbol")
```

但 `fund_flow` 是多日数据，按 symbol 索引后丢失 `date` 维度。`compute_main_flow_ratio` 按 `date` 过滤会失效或结果错误。

#### 🟠 Bug K: `_turnover_reversal` shortcut 可能 KeyError

**位置**: `quant/factor/compute/_primitives.py` `_turnover_reversal`

直接访问 `prims["approx_turnover"]`，若数据源不含 `turnover` 字段则崩溃。

#### 🟡 Bug L: 52 周高点使用 stale 数据

`compute_high52w_dist` TODO 注明 `close_latest`/`high_52w` 来自 `stocks` 静态表，可能过期。

#### 🟡 Bug M: Piotroski F-Score 字段索引硬编码

`compute_piotroski_fscore` 使用 `cur_fin[2]`、`cur_bal[3]` 等位置索引，若 SQL 列顺序变化即错误。应使用列名。

### 3.3 因子评估与合成

| 环节 | 现状 | 问题 |
|---|---|---|
| IC 计算 | Spearman IC，支持 1d/5d/20d 衰减 | 正确 |
| 因子状态机 | active/probation/evaluating/archived | 合理 |
| Alpha 合成 | sleeve / ic_weighted / intersection / lgb | `combine_mode` 切换 bug 使 walk-forward 失效 |
| 因子缓存 | gzip CSV | 新因子对旧日期返回 NaN，物化策略不检查完整性 |
| 中性化 | 行业/市值/风格 | 合理，但行业分类可能 stale |

---

## 四、与业界标准对齐度

### 4.1 已对齐

- 因子评估维度：IC、ICIR、half-life、t-stat、五分位、CPCV、PBO
- 组合优化：HRP、Risk Parity、Markowitz、Kelly、成本带
- 风险：VaR/CVaR、压力测试、行业暴露、止损止盈
- 回测：T+1、整手、复利、成本

### 4.2 未对齐 / 差距

| 维度 | 本项目 | 业界标准 |
|---|---|---|
| 行情粒度 | 日线 + 盘中 5s 快照 | tick / level2 |
| 执行仿真 | 开盘价 100% 成交 | 限价单/市场深度/订单簿 |
| 冲击成本 | 固定滑点 + 可选平方根 | 实盘校准 IS / TCA |
| 风险模型 | Ledoit-Wolf + 行业暴露 | BARRA/APT 多因子风险模型 |
| OMS | 简单状态机 | 独立 OMS，完整订单生命周期 |
| 数据基础设施 | SQLite + gzip CSV | 时序数据库/特征平台 |
| Paper Trading | 无 | 必须有 |
| 因子方向管理 | 硬编码反向 | 配置化/IC 监控自动翻转 |

---

## 五、Bug 分级汇总

### 🔴 严重（必须修复才能考虑实盘）

1. `reversal_5d` 与 `momentum_5d` 公式完全相同，方向错误。
2. 回测 `combine_mode` 切换失效，全程 sleeve。
3. 实盘 `CostModel()` 未读配置。
4. `OrderManager._fill` 真实券商下单后重复写入 `sim_trades`。
5. `monitor._execute_sell` 真实券商卖出后未同步 `sim_trades`。
6. `compute_idiosyncratic_vol` beta 按位置而非日期对齐。
7. `compute_limit_touch_no_seal` 未分板块（科创板/北交所错误）。
8. `update_daily_risk` 把风险数据写入 `market.db` 而非 `trades.db`。

### 🟠 高（影响回测可信度或性能）

9. `compute_turnover_reversal` fallback 混用 turnover 与 volume 单位。
10. `compute_volume_price_corr`、`compute_vp_divergence` 用价格水平而非收益率。
11. `compute_mif` 实现与文档（相关性）不符。
12. `compute_bp_ratio`、`compute_size` 符号与经济含义相反。
13. 财务因子大量直接查库，未用预加载数据。
14. `fund_flow` aux 预加载丢失日期维度。
15. `_turnover_reversal` shortcut 可能 KeyError。
16. `compute_limit_up_streak` 用 `close == high` 浮点相等判断。
17. `universe_repo.get_symbols` 连接未关闭（`conn.close()` 在 `return` 后）。

### 🟡 中低（代码债 / 隐患）

18. `compute_high52w_dist` 数据可能 stale。
19. `compute_piotroski_fscore` 字段位置硬编码。
20. 大量裸 `except: pass` 吞异常。
21. `alpha101` 等部分因子用 `sparse=True` 但有效样本可能不足。
22. 因子函数在 aux 缺失时返回 `None`，下游需健壮处理。

---

## 六、修复建议

### 立即修复（P0）

1. **统一修复 `reversal_5d`**: 改为 `-cum` 或根据最新 IC 实证调整，确保与 `momentum_5d` 正交。
2. **修复回测 `combine_mode` 切换**: warmup 后显式传入 `"ic_weighted"`。
3. **修复实盘 `CostModel`**: 统一使用 `CostModel.from_config()`。
4. **修复 OMS 落账**: 真实券商成交后通过回报事件同步 `sim_trades`，未成交不写。
5. **修复特质波动率**: 按日期对齐股票收益与基准收益。
6. **修复涨跌停板块判断**: 所有涉及涨停计算的函数统一使用 `_get_board_limit`。

### 短期优化（P1）

7. 建立 **paper trading** 层，用真实行情验证执行模型。
8. 将量价相关性改为 **收益率 vs 成交量变化**。
9. 统一财务因子使用 `preloaded_financials`，避免函数内查库。
10. 修复 `fund_flow` aux 预加载索引问题。
11. 实现因子方向配置化（根据滚动 IC 自动翻转或退役）。

### 中期重构（P2）

12. 引入 **BARRA/APT 风险模型**替代简单行业暴露。
13. 建立 **TCA（交易成本分析）** 模块，校准冲击成本。
14. 迁移到时序数据库 + 特征平台。
15. 拆分单体架构为 OMS / Risk / Strategy / Data 服务。

---

## 七、结论

该项目回测框架**方向正确、覆盖面广**，但当前代码中存在**多个足以扭曲回测结果、导致实盘亏损的 bug**，尤其是在：

- **因子方向/定义错误**（`reversal_5d`、特质波动率 beta、量价相关）
- **回测路径失效**（`combine_mode` 未切换）
- **实盘执行落账错误**（重复记账、卖出不落账）
- **成本模型不一致**

**建议**: 在将任何策略投入实盘前，必须完成 P0 修复，并建立 paper trading 验证层；同时引入更专业的风险模型与 OMS。否则回测绩效很可能无法复现到实盘。
