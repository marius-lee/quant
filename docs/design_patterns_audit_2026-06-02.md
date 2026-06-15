# QuantA 设计模式审计与重构方案

**日期**: 2026-06-02
**审计范围**: strategy/、factor/、engine/、execution/、data/、web/
**依据**: GoF 23 种设计模式 + SOLID 原则

---

## 一、已正确使用的模式

| 模式 | 位置 | 评分 | 说明 |
|------|------|------|------|
| **Strategy** | `strategy/base.py:10` — `BaseStrategy(ABC)` + `FactorStrategy` / `AggressiveStrategy` | A | 两个策略实现同一接口，`pipeline.py` 通过 `_get_strategy(force=)` 多态调度 |
| **Memento** | `engine/trainer.py:52-69` — `save_model()` / `load_model()` | B | joblib 序列化/反序列化模型状态 (models, weights, scaler) |
| **Singleton** | `config/loader.py` — `_config` 缓存；`web/app.py` — engine/store 懒加载 | B | 避免重复读盘和重复初始化 |

---

## 二、需要改进的设计缺陷

### P0 — 可能引发 runtime bug 的结构性问题

#### 1. Strategy 层与 Engine 层紧耦合（DIP 违反）

**位置**: `strategy/factor_strategy.py:5-10`

`FactorStrategy.run()` 直接导入 6 个 engine 模块并手动编排顺序：

```python
from engine.screener import screen_and_split
from engine.trainer import train_model
from engine.predictor import predict_all
from engine.ranker import apply_demon_and_neutralize
from engine.backtest_runner import run_backtest
from engine.builder import build_result
```

**问题**: 策略层（高层策略）直接依赖引擎层（底层实现）。新增一个处理步骤（如特征选择）需要修改策略代码。

**改进**: 引入 **Mediator** 模式。新建 `PipelineMediator`，封装引擎步骤的执行顺序，策略只传配置不关心内部步骤。

---

### P1 — 代码腐烂（改一处要改多处）

#### 2. 数据源格式转换未统一（Adapter 缺失）

**位置**: `data/store.py` — 5 个 `_fetch_*` 方法

tushare/akshare/tencent/zzshare/tickflow 各返回不同格式，`store.py` 内联处理所有转换（单位换算、列名映射）。加第 6 个源需直接改 store。

**改进**: 每个源实现 **Adapter** 接口，统一输出 `(symbol, date, open, high, low, close, volume, amount, turnover)`。

#### 3. IC 筛选模式 if/else 分支（OCP 违反）

**位置**: `engine/screener.py:59-81`

`screen_and_split()` 用条件分支在全局 IC / 分块 IC 间切换，加第三种筛选方式需改代码。

**改进**: **Strategy** 模式 — `ICScreeningStrategy` 接口，`GlobalIC` / `ChunkedIC` 两个实现。

#### 4. 模型权重公式硬编码（OCP 违反）

**位置**: `strategy/ensemble.py:158`

```python
combined = 0.7 * max(0, t_ic_val) + 0.3 * max(0, ic_val)
```

无法切换到等权/Bayesian/排名加权等替代公式。

**改进**: 提取 `WeightStrategy` 接口，`DefaultWeight` / `EqualWeight` / `RankWeight` 实现。

#### 5. 多个位置的 `row_factory` 设置（竞态隐患，已修复）

**位置**: `execution/live_broker.py`

`conn.row_factory = sqlite3.Row` 原散落在 6 个函数中，多线程同时设同一连接产生竞态。

**修复状态**: ✅ 已修复 — 移至 `get_conn()` 初始创建时一次设定。

---

### P2 — 长期可维护性

#### 6. `compute_factors()` 单体函数违反 SRP

**位置**: `factor/compute.py:21-193` (172 行)

一个函数处理：数据库连接、股票覆盖验证、日期范围、5 种因子计算、winsorization、SQL 写入。

**改进**: 拆为 `FactorPipelineBuilder` 的步骤式方法。

#### 7. `BaseFactor` 是死的抽象层

**位置**: `factor/base.py:29-53`

定义了 `compute(data: dict)` 抽象接口，但零个子类实现它。所有具体因子类（DemonSignals、LimitUpPatterns 等）有自己的不兼容签名。

**改进**: 删除 `BaseFactor` 或强制重构所有因子类实现同一接口。

---

## 三、不建议改的

- **Bridge（Polars/pandas）**: 两者在 `compute.py` 中已充分利用，纯架构隔离增加复杂度无收益
- **State（资金阶段）**: `planner.py` 5 个阶段纯数据驱动（dict 列表），State 模式过度设计
- **Observer（WebSocket）**: ¥5000 策略不需要实时推送，JSON 轮询足够
- **Template Method（策略 run 骨架）**: FactorStrategy 和 AggressiveStrategy 的执行逻辑差异太大（一个有 ML 训练，一个没有），强行统一会创建脆弱抽象

---

## 四、建议执行顺序

1. P0-#1：加 PipelineMediator，解耦策略和引擎
2. P1-#4：提取 WeightStrategy
3. P1-#2：Adapter 统一数据源格式
4. P1-#3：Strategy 替代 IC 筛选 if/else
5. P2-#6：拆分 compute_factors
6. P2-#7：处理 BaseFactor

---

## 五、关键评估指标

| 重构前 | 重构后 |
|--------|--------|
| FactorStrategy.run() 直接导入 6 个 engine 模块 | 通过 PipelineMediator 间接调度 |
| 加权公式无法切换 | 可插拔 WeightStrategy |
| compute_factors() 172 行单体函数 | 拆为 Builder 链 |
| IC 筛选硬编码分支 | 可插拔筛选策略 |
| BaseFactor 为死抽象 | 删除或规范化 |

> 所有绩效指标来自代码结构分析，不作为投资预测依据。
