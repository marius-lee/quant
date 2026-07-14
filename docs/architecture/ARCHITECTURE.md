# 量化选股系统架构设计 v3.0

## 设计原则

1. **分层解耦**: 每一层只依赖下层接口，层间通过明确的协议通信
2. **参数可追溯**: 每个阈值保留来源注释（数学恒等式 | 文献 | 数据校准 | 用户确认）
3. **可回测优先**: 所有信号生成逻辑必须支持在历史数据上独立运行，不依赖实盘环境
4. **零冗余**: 每个模块有明确的调用方，无调用方立即删除
5. **配置驱动**: 阈值、窗口、权重均从 config.yaml 读取，不硬编码
6. **北极星**: 所有决策围绕 ¥5,000 → ¥100 万目标，禁止无 alpha 贡献的建设

## 分层架构

```
+====================================================================+
|  Layer 7: Monitor      监控层 — 绩效归因 + 风险归因 + 报告          |
+====================================================================+
|  Layer 6: Execution    执行层 — 订单生成 + 成本估算 + 交易记录       |
+====================================================================+
|  Layer 5: Optimizer    优化层 — 组合构建 + 调仓 + 约束求解          |
+====================================================================+
|  Layer 4: Risk         风控层 — 中性化 + 协方差估计 + 暴露约束       |
+====================================================================+
|  Layer 3: Alpha        Alpha层 — 因子合成 + 收益预测 + 截面排名      |
+====================================================================+
|  Layer 2: Factor       因子层 — 因子计算 + IC/IR 评估 + 衰减分析     |
+====================================================================+
|  Layer 1: Data         数据层 — 日线增量同步 + 交易记录持久化        |
+====================================================================+
|  Layer 0: Infra        基础层 — 配置 + 日志 + 日期 + 交易日历        |
+====================================================================+
```

数据流向：**Infra → Data → Factor → Alpha → Risk → Optimizer → Execution → Monitor → Web**

每个 Layer 上方是一个独立的状态视图，下方是依赖。信号自底向上流动，订单自顶向下执行。

## Layer 0: 基础层 (config/ + utils/ + execution/calendar.py)

**职责**: 配置加载、日志系统、日期工具、交易日历。被所有上层模块依赖。

### 模块清单

| 文件 | 职责 | 变更 |
|------|------|------|
| `config/loader.py` | YAML 配置热加载 + ${ENV} 替换 | 保留 |
| `config/config.yaml` | 集中式参数配置 | 更新：移除陈小群专用参数，新增 factor/alpha/optimizer 段 |
| `utils/logger.py` | 模块级 logger + RotatingFileHandler | 保留 |
| `utils/date.py` | 日期格式统一 (YYYY-MM-DD) | 保留 |
| `execution/calendar.py` | A股交易日历 + 时段判断 | 保留 |

### 对外接口

```python
# config/loader.py
def get(key: str, default=None) -> Any  # 点号路径取值，自动热更新
def reload() -> dict                     # 强制重读

# utils/logger.py
def get_logger(name: str) -> logging.Logger

# utils/date.py
def to_str(d) -> str              # 任意输入 → YYYY-MM-DD
def to_compact(d) -> str          # → YYYYMMDD
def today_str() -> str

# execution/calendar.py
def is_trading_day(d: date = None) -> bool
def is_market_open(now: datetime = None) -> bool
def get_next_trading_day(from_date: date = None) -> date
def get_trading_period(now: datetime = None) -> str
```

## Layer 1: 数据层 (data/)

**职责**: 全 A 股日线数据的增量和全量同步，交易记录的持久化和查询。对上提供统一的数据访问接口。

### 模块清单

| 文件 | 职责 | 变更 |
|------|------|------|
| `data/store.py` | DataStore — 多源日线增量同步（tickflow→新浪→腾讯→tushare→akshare） | 保留，微调 |
| `data/trade_repo.py` | TradeRepo — sim_trades 统一读写 | 保留 |
| `data/__init__.py` | 公开导出 | 更新 |

### 对外接口

```python
# data/store.py
class DataStore:
    def __init__(self, db_path: str = "data/market.db")
    def sync_stock_list(self) -> int
    def update_daily(self, symbols=None, start=None) -> int
    def get_daily(self, symbols: list, start: str, end: str = None) -> pd.DataFrame
    def get_stock_count(self) -> dict
    def close(self)

# data/trade_repo.py
class TradeRepo:
    def get_capital(self, strategy: str) -> float
    def get_positions(self, strategy: str) -> list[dict]
    def record_trade(self, ...)
    def get_trades(self, strategy: str, limit: int) -> list[dict]
    def get_pnl(self, strategy: str) -> float
    def get_counts(self, strategy: str) -> tuple
```

## Layer 2: 因子层 (factor/)

**职责**: 计算时序/横截面因子，评估因子的预测能力（IC/IR/相关性/衰减），合成复合因子。因子状态由 factor_registry 管理：active 参与实盘交易 (P1: using=active only), monitoring 仅 15:30 归因观察不交易, retired 永不再用。因子上报 Alpha 层。




















| `factor/registry.py` | 因子状态机 + 共享连接 + z-score 标准化 | `get_factor_names(status_filter)` |
| `factor/compute.py` | 57因子计算（41 price + 16 fundamental，纯函数、向量化） | `compute_momentum(close, window) → Series` 等 |
| `factor/ic.py` | 统一 IC 计算（Spearman Rank IC + IR + 衰减分析） | `compute_ic(factor_names=) → ic_means, ic_irs` |
| `factor/synth.py` | 因子合成（等权 / IC加权） | `equal_weight(factors) → Series` |

### 核心接口协议

```python
from abc import ABC, abstractmethod
import pandas as pd

class Factor(ABC):
    name: str          # 例: "momentum_20d"
    category: str      # 例: "momentum"

    @abstractmethod
    def compute(self, data: pd.DataFrame) -> pd.Series:
        """在给定日期截面上计算因子值。
        data: MultiIndex (date,symbol) DataFrame, 至少含 close, volume
        返回: index=symbol 的 Series
        """
        ...
```

### 配置依赖

```yaml
factor:
  windows:  # 各因子独立窗口 (volatility:126d, amihud:250d, skewness:60d 等)
  evaluation: {n_symbols: 800, lookback: 120, n_days: 120}
  decay_horizons: [1, 5, 20]
  # 详见 config/config.yaml 完整因子配置
```

## Layer 3: Alpha 层 (alpha/)

**职责**: 将多个因子合成为单一 alpha 向量（预期收益），做横截面排名。

Alpha 层回答：「在当前截面上，哪些股票最值得持有？」

### 模块清单

| 文件 | 职责 | 对外接口 |
|------|------|---------|
| `alpha/model.py` | AlphaModel — 因子合成 + 截面排名 | `AlphaModel.predict(date) → pd.Series` |

### 核心接口

```python
class AlphaModel:
    def __init__(self, factors: list[Factor], method: str = "ic_weighted"):
        self.factors = factors
        self.method = method
        self._weights = {}

    def calibrate(self, factor_values: pd.DataFrame,
                  forward_returns: pd.DataFrame):
        """用历史数据校准因子权重。"""
        ...

    def predict(self, date: str, store: DataStore) -> pd.Series:
        """在指定日期截面上计算 alpha 得分。
        返回: index=symbol, value=score（高分=值得买）
        """
        ...

    def cross_sectional_rank(self, alpha: pd.Series) -> pd.Series:
        """截面分位数标准化 → [0, 1]"""
        return alpha.rank(pct=True)
```

### 配置依赖

```yaml
alpha:
  method: ic_weighted
  train_window: 252
  retrain_freq: 20
  top_fraction: 0.30
```

## Layer 4: 风控层 (risk/)

**职责**: 对 alpha 得分做风险调整，估计协方差矩阵，计算暴露约束。

风控层不对 alpha 加分，只做减法和约束。

### 模块清单

| 文件 | 职责 | 对外接口 |
|------|------|---------|
| `risk/neutralize.py` | 行业中性化、市值中性化 | `industry_neutralize(scores) → Series` |
| `risk/covariance.py` | 协方差估计（Ledoit-Wolf 收缩） | `ledoit_wolf_cov(returns) → DataFrame` |
| `risk/constraints.py` | 单票/行业暴露上限、流动性门槛 | `RiskLimits.filter(candidates) → DataFrame` |

### 核心接口

```python
# risk/neutralize.py
def industry_neutralize(scores: pd.Series, industries: pd.Series) -> pd.Series:
    """行业内部排名 → 消除行业 beta 的影响。"""
    ...

# risk/covariance.py
def ledoit_wolf_cov(returns: pd.DataFrame, shrinkage: float = None) -> pd.DataFrame:
    """Ledoit-Wolf (2004) 收缩协方差。"""
    ...

# risk/constraints.py
class RiskLimits:
    max_single_position: float
    max_positions: int
    min_daily_amount: float
    exclude_star_st: bool

    def filter(self, candidates: pd.DataFrame) -> pd.DataFrame:
        """应用所有筛选条件。返回通过的 subset。"""
        ...
```

### 配置依赖

```yaml
risk:
  covariance_method: ledoit_wolf
  covariance_window: 60
  max_single_position: 0.05
  max_positions: 20
  min_daily_amount: 500000
  exclude_star_st: true
```

## Layer 5: 优化层 (optimizer/)

**职责**: 将 alpha 得分和风险约束转化为目标持仓权重。

优化方法随资本规模自适应：

| 资金规模 | 优化方法 | 原因 |
|----------|---------|------|
| < ¥20,000 | 得分排序 + 等权 + 整数手约束 | 整手约束极度刚性，严格等权是唯一稳定解 |
| ¥20,000 ~ ¥100,000 | 得分倾斜 + 整数舍入 | 每只可买 10-20 手，按得分倾斜权重后用整数规划修正 |
| > ¥100,000 | 均值-方差 + 整数约束 | 单只手数占比 < 0.5%，连续权重近似有效 |

三种方法共享同一输入接口（alpha 得分 + 风控约束 + 动态资金），输出均为整数手持仓向量。上层调用无感知。

### 模块清单

| 文件 | 职责 | 对外接口 |
|------|------|---------|
| `optimizer/portfolio.py` | PortfolioConstructor — 资本自适应组合构建 | `construct(alpha, limits, capital) -> TargetPortfolio` |
| `optimizer/rebalance.py` | 调仓计算 — 目标 vs 当前 -> 买卖清单 | `compute_trades(target, current, cost_model) -> list[Order]` |

### 核心接口

```python
from dataclasses import dataclass

@dataclass
class TargetPortfolio:
    weights: pd.Series    # index=symbol, values=target_shares (100的整数倍)
    cash_reserve: float
    method: str           # equal_weight | score_weighted | mean_variance

@dataclass
class Order:
    symbol: str
    side: str             # buy | sell
    shares: int           # 100 的整数倍
    price: float
    cost: float           # 预估成本

class PortfolioConstructor:
    def __init__(self, config: dict):
        self.equal_weight_cap = config.get("equal_weight_cap", 20000)
        self.weighted_cap = config.get("weighted_cap", 100000)

    def construct(self, alpha, limits, capital) -> TargetPortfolio:
        """资本自适应分配:
        if capital < equal_weight_cap: _equal_weight_greedy()
        elif capital < weighted_cap: _score_weighted_rounding()
        else: _mean_variance_lot()
        """
        ...

    def _equal_weight_greedy(self, ...) -> TargetPortfolio:
        """Top N 等权。贪心: 每轮给得分最高的未满仓股票加 1 手。"""
        ...

    def _score_weighted_rounding(self, ...) -> TargetPortfolio:
        """按得分比例分配资金 -> 整手舍入 -> 修正余数。"""
        ...

    def _mean_variance_lot(self, ...) -> TargetPortfolio:
        """均值-方差优化 -> 连续权重 -> 整数规划 -> 逐手分配。"""
        ...

def compute_trades(target, current, cost_model) -> list[Order]:
    """diff 目标持仓 vs 当前持仓 -> 买卖订单列表。考虑 T+1 约束。"""
    ...
```

### 配置依赖

```yaml
optimizer:
  rebalance_freq: weekly              # daily | weekly | monthly
  min_holding_days: 5                 # 最小持仓天数
  turnover_limit: 0.50                # 每日换手<=50%总资产
  equal_weight_cap: 20000             # 等权分配最大本金
  weighted_cap: 100000                # 得分加权最大本金 (超过则用均值-方差)
  risk_aversion: 2.0                  # 均值-方差风险厌恶系数 lambda
```

## Layer 6: 执行层 (execution/)

**职责**: 根据调仓清单生成模拟订单，记录交易，计算成本。

### 模块清单

| 文件 | 职责 | 变更 |
|------|------|------|
| `execution/engine.py` | ExecutionEngine — 订单记录 + 成本估算 + 状态持久化 | **新建** |
| `execution/cost.py` | 统一成本模型（佣金 + 印花税 + 滑点估计） | **新建** |
| `execution/quote.py` | 新浪批量行情拉取 `fetch_quotes()` | 保留，删除 BoardTracker |
| `execution/calendar.py` | 交易日历 | 保留 |

### 核心接口

```python
# execution/cost.py
@dataclass
class CostModel:
    commission_rate: float = 0.0003
    min_commission: float = 5.0
    stamp_tax_rate: float = 0.001

    def buy_cost(self, price: float, shares: int) -> float:
        return price * shares + max(price * shares * self.commission_rate, 5.0)

    def sell_proceeds(self, price: float, shares: int) -> float:
        val = price * shares
        return val - max(val * self.commission_rate, 5.0) - val * self.stamp_tax_rate

# execution/engine.py
class ExecutionEngine:
    def execute(self, orders: list[Order], date: str, strategy: str = "quant"):
        """执行模拟交易: 成本计算 → 写入 trades.db → 更新 capital_after"""
        ...
```

### 配置依赖

```yaml
execution:
  commission: 0.0003       # 万三
  stamp_tax: 0.001          # 千一(仅卖出)
  slippage: 0.001           # 滑点千一
```

## Layer 7: 监控层 (monitor/)

**职责**: 盘后绩效归因和风险归因，生成报告，更新 Web 前端状态。

### 模块清单

| 文件 | 职责 | 对外接口 |
|------|------|---------|
| `monitor/attribution.py` | 绩效归因 + 风险暴露分解 | `factor_attribution(returns, exposures) → dict` |
| `monitor/report.py` | 日/周报告生成 → JSON + 前端推送 | `generate_daily(date, repo) → dict` |

### 核心接口

```python
# monitor/report.py
def generate_report(date: str, repo: TradeRepo) -> dict:
    """日报结构:
    {
      "date": str,
      "pnl": {"realized": float, "unrealized": float},
      "positions": list[dict],
      "exposure": {"sectors": dict},
      "metrics": {"sharpe_rolling_20d": float, "max_drawdown": float}
    }
    """
    ...
```

## 数据流

```
quant/scheduler/ (日频 orchestrator + 周频 weekly)
  │
  └─ pipeline.py.run(date)
      │
      ├─ [Step 1] DataStore.update_daily()          → data/store.py
      │   增量同步今日日线
      │
      ├─ [Step 2] FactorEvaluator.run()             → factor/
      │   ├─ 遍历 Factor 列表 → compute()
      │   ├─ 截面 Rank IC（滞后 1d/5d/20d）
      │   └─ 产生 factor_quality_report
      │
      ├─ [Step 3] AlphaModel.calibrate() → predict() → alpha/model.py
      │   ├─ 用历史 IC 校准因子权重
      │   ├─ 合成 alpha 得分向量
      │   └─ cross_sectional_rank() → Top 30% 候选池
      │
      ├─ [Step 4] RiskManager.apply()               → risk/
      │   ├─ industry_neutralize(alpha, industries)
      │   └─ RiskLimits.filter(candidates)
      │
      ├─ [Step 5] PortfolioConstructor.construct()  → optimizer/portfolio.py
      │   ├─ 资本自适应: <2万等权 / 2-10万得分倾斜 / >10万均值-方差
      │   ├─ 整数手约束分配
      │   └─ → TargetPortfolio
      │
      ├─ [Step 6] ExecutionEngine.execute()         → execution/engine.py
      │   ├─ compute_trades(target, current) → orders
      │   ├─ 执行模拟交易 → trades.db
      │   └─ 更新 capital_after
      │
      └─ [Step 7] Monitor.generate_report()         → monitor/report.py
          ├─ attribution → 绩效归因
          └─ push → web/shared.py
```

## 数据 Schema

### market.db (保留现有)

```sql
stocks (symbol TEXT PK, name, market, list_date, industry)
daily  (symbol, date, open/high/low/close/volume/amount/turnover, PK(symbol,date))
meta   (key, value)
```

### trades.db (保留现有 + 新增 strategy_config)

```sql
sim_trades (id, date, symbol, side, price, shares, pnl, pnl_pct, capital_after, strategy)
signals   (id, date, time, symbol, mode, price, score, reason, is_executed)
strategy_config (strategy PK, initial_capital)   -- 新增
```

## 调度系统

### quant/scheduler/（调度器包）

管理 pipeline 的执行时机。非交易时间休眠，交易日 15:30 自动运行。

### pipeline.py（新建）

串联 7 层，每层独立错误处理。任何一层异常不中断后续层。

```python
def run(date: str):
    store = DataStore()
    repo = TradeRepo()
    try: store.update_daily()
    except: pass
    try: factors = factor_pipeline.run(date, store)
    except: pass
    try: alpha = alpha_model.predict(date, store)
    except: pass
    try: alpha = risk_neutralize(alpha, store)
    except: pass
    try: target = optimizer.construct(alpha, limits, capital)
    except: pass
    try: engine.execute(compute_trades(target, current), date)
    except: pass
    try: shared.update_state(monitor.generate_report(date, repo))
    except: pass
    store.close()
```

## 迁移计划

### 保留（22 文件）

| 文件 | 说明 |
|------|------|
| `config/loader.py` | 不变 |
| `config/config.yaml` | 重构配置段 |
| `utils/date.py`, `utils/logger.py` | 不变 |
| `data/store.py`, `data/trade_repo.py` | 不变 |
| `execution/calendar.py` | 不变 |
| `execution/quote.py` | 仅保留 `fetch_quotes()` |
| `web/app.py`, `web/shared.py` | 更新路由 |
| `web/static/*`, `web/templates/*` | 更新前端 |
| `requirements.txt` | 更新依赖 |

### 删除（18 文件）

| 文件 | 原因 |
|------|------|
| `intraday_runner.py` | 替换为 scheduler + pipeline |
| `execution/sell_chain.py` | 陈小群卖出体系 |
| `archive/*` (5) | 死代码 |
| `strategies/*` (5) | 陈小群 + ETF/小市值 |
| `ops/*` (7) | 硬编码 stub |

### 新建（16 文件）

| Layer | 文件 |
|-------|------|
| Factor | `factor/base.py`, `compute.py`, `evaluate.py`, `synth.py` |
| Alpha | `alpha/model.py` |
| Risk | `risk/neutralize.py`, `covariance.py`, `constraints.py` |
| Optimizer | `optimizer/portfolio.py`, `rebalance.py` |
| Execution | `execution/engine.py`, `cost.py` |
| Monitor | `monitor/attribution.py`, `report.py` |
| 编排 | `pipeline.py`, `quant/scheduler/` |

## 配置结构 (config.yaml)

```yaml
data:        # 数据层（保留）
factor:      # 因子层（新增）
alpha:       # Alpha 层（新增）
risk:        # 风控层（新增）
optimizer:   # 优化层（新增）
execution:   # 执行层（保留核心参数）
backtest:    # 初始资金 + 基准（保留）
web:         # Web 端口（保留）
```


---

> **注意**: 本文档为 v3.0 架构设计快照。运行时配置值（如窗口参数、仓位上限等）以 `config/config.yaml` 为准，设计文档中的示例配置可能与当前实际值有差异。因子数量已从设计时的 11 个发展到 35 个。
