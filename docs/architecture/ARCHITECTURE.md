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

**职责**: 计算时序/横截面因子，评估因子的预测能力（IC/IR/相关性/衰减），合成复合因子。因子状态由 factor_registry 管理：active 参与实盘交易 (P1: using=active only), monitoring 仅归因观察不交易 (归因在晚间链 attribution.py 运行), retired 永不再用。因子上报 Alpha 层。




















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

| 资金规模 | 层级 | 优化方法 | 原因 |
|----------|------|---------|------|
| < ¥30,000 | Nano | 贪心等权 (1-3 只) | 整手约束刚性; Kelly 离散化误差 >30%; 佣金占比 >0.3% |
| ¥30,000 ~ ¥100,000 | Micro | 得分倾斜 + 整数舍入 (3-8 只) | 基本分散化; 佣金可接受; 协方差估计不可靠 |
| > ¥100,000 | Small | Risk Parity / Kelly 均值-方差 (8-20 只) | 分散化充分; 协方差矩阵可用; Kelly 离散化 <10% |
| > ¥500,000 | Medium | 全 MV + 市场冲击 (预留) | 市场冲击成为主导成本 |

来源: docs/reports/capital-segmentation-analysis-2026-07-15.md

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
        self.nano_cap = config.get("nano_cap", 30000)       # (已弃用: equal_weight_cap -> nano_cap)
        self.micro_cap = config.get("micro_cap", 100000)    # (已弃用: weighted_cap -> micro_cap)

    def construct(self, alpha, limits, capital) -> TargetPortfolio:
        """资本自适应分配:
        if capital < nano_cap: _rank_concentrated()           # Nano: 排名集中 1-3只
        elif capital < micro_cap: _score_weighted_rounding()  # Micro: 得分倾斜 3-8只
        else: _mean_variance_lot() / _kelly_greedy()          # Small: MV/Kelly 8-20只
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
  nano_cap: 30000                     # Nano 层上限 (低于此: 贪心等权 1-3 只)
  micro_cap: 100000                   # Micro 层上限 (低于此: 得分倾斜 3-8 只; 高于: Small 均值-方差)
  # medium_cap: 500000                # 预留: Medium 层
  kelly_fraction: 4.0                 # Kelly 分数 (4=quarter-Kelly, 仅 Small 层启用)
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
quant/scheduler/ (orchestrator 主循环, manifest 驱动 — v428)
  │  30s 轮询 → _should_run(spec,...) 窗口内+依赖满足 → _dispatch
  ├─ signals   (08:00-15:30, inline)     → pipeline.generate_signals()
  │     ├─ [Step 1] DataStore.update_daily()      → data/repos/daily_repo.py
  │     ├─ [Step 2] UniverseRepo + 风险预筛        → investable universe
  │     ├─ [Step 3] FactorStore.load() → AlphaModel.combine()/rank()
  │     ├─ [Step 4] neutralize + covariance(Ledoit-Wolf) + VaR
  │     └─ [Step 5] PortfolioConstructor.construct() → target_positions
  ├─ execute   (09:20-14:56, inline, 依赖 signals 尝试过)
  │     └─ [Step 6] pipeline.execute_signals()
  │           ├─ ExecutionModel.run() → ExecutionEngine.execute() → trades.db
  │           └─ [Step 7] Monitor.generate_report() → push web
  ├─ snapshot_open  (10:00-14:55)  开盘30分钟快照
  ├─ monitor        (09:30-15:00)  盘中风控守护线程 (ATR止损/止盈/熔断)
  ├─ snapshot_close (15:00-15:05)  收盘快照 ← v428: 原14:55(收盘前)修正
  ├─ reconcile      (15:05-16:00, 依赖 monitor==ok)
  ├─ evening_chain  (19:00-23:59, subprocess)
  │     └─ daily_data → factor_cache → attribution
  └─ weekly_eval    (周六 06:00-12:00, subprocess, 在 is_trading_day 短路前检查)
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

> v428 (2026-08-08) 重构：任务声明表 (manifest) 单一真相源 + 单一编排器。
> 历史版本 (v2-v427) 的多线程直驱 (每任务独立 `_loop()` / `_weekly_loop` 线程、
> orchestrator `_TIMEOUTS` 超时表) 已全部删除 (`quant/scheduler/_base.py` 移除)。

### quant/scheduler/（调度器包）

```
quant/scheduler/
├── manifest.py        # ★ v428 单一真相源: TaskSpec 声明表 + ALL + spec() + _PLAN_ORDER
├── orchestrator.py    # 主循环: 30s 轮询 → _should_run(spec,...) 纯函数决策 → _dispatch
├── task_log.py        # 任务状态机 (running→ok|failed|aborted|skipped) + pid 记录
├── status.py          # 注册表/状态快照 (供 Web /api/scheduler)
├── signals.py         # 盘前信号 (Metrics+IC 门控) → pipeline.generate_signals()
├── execute.py         # 盘中执行 → pipeline.execute_signals()
├── snapshot.py        # 开盘/收盘快照 (实盘 min-bar 落库)
├── monitor.py         # 盘中风控 09:35-15:00 实时监控
├── reconcile.py       # 日终对账
├── evening.py         # 晚间链 subprocess (daily_data→factor_cache→attribution)
├── weekly.py          # 周六周度评估 (评估部并行 5 阶段)
├── daily_data.py      # 日线增量同步
├── factor_cache.py    # 因子缓存物化
└── attribution.py     # 归因 (晚间链内运行)
```

调度入口只有一条：**orchestrator 主循环**（独占进程，`scripts/restart.sh` 拉起）。
所有任务的时间窗口、依赖关系、超时统一声明在 `manifest.py`：

| 任务 | 时间窗 | 依赖 | 模式 |
|------|--------|------|------|
| signals | 08:00-15:30 | — | inline |
| execute | 09:20-14:56 | attempt[signals] | inline |
| snapshot_open | 10:00-14:55 | attempt[execute] | inline |
| monitor | 09:30-15:00（窗口期常驻） | — | daemon 线程 |
| snapshot_close | **15:00-15:05**（收盘后） | — | inline |
| reconcile | 15:05-16:00 | **ok[monitor]** | inline |
| evening_chain | 19:00-23:59 | — | subprocess |
| weekly_eval | 周六 06:00-12:00 | — | subprocess |

- 每天 30 秒轮询，`is_trading_day` 短路非交易日；**weekly_eval 触发检查置于该短路之前**（周六非交易日也能触发周度评估，v416 教训）。
- 依赖语义：`depends_attempt` = 今日尝试过即放行（如 execute 需 signals 尝试）；`depends_ok` = 今日完成 ok 才放行（如 reconcile 需 monitor ok）。
- 周期状态语义：`running` = 执行中；`ok` = 完成（绝不自动重跑）；`failed` = 异常（当日不自动重试）；`aborted` = 中断（可重试，预算 2 次）。
- 守卫：pid 存活检测（僵尸 running 自愈，v424+）、进程级 `start()` dedup（`_tk_start` grace）、DB 幂等（`check_today_ran`）。

### 调度触发（多重冗余，幂等由状态机保证）

1. **orchestrator**（主）：主循环，30s 轮询决策。
2. **cron**（兜底）：`scripts/setup_cron.sh` — 周六 06:00 weekly 兜底 + 每小时 adj_factor（因子复权基准）。
3. **手动**：`bash scripts/run_task.sh <task> [date]`，单任务重跑（如 factor_cache 补物化）。

### pipeline.py（两阶段，被 signals/execute 调用）

```python
# Phase 1 (盘前, signals 任务内): generate_signals()
# 1) DataStore.update_daily() → 2) UniverseRepo+risk 预筛 → 3) FactorStore.load()
# → AlphaModel.combine()/rank() → 4) 中性化+Ledoit-Wolf+VaR → 5) PortfolioConstructor
# Phase 2 (开盘, execute 任务内): execute_signals()
# 6) ExecutionEngine.execute() → trades.db → 7) Monitor report → push web
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
