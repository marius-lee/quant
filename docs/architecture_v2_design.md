# 系统架构 V2 设计方案

日期: 2026-06-05  
来源: QuantConnect Algorithm Framework + D.E. Shaw Composable Strategy + BlackRock AIM + FinRL-X

## 核心模式

```
Data → Alpha Creation → Portfolio Construction → Risk Management → Execution
         ↑                    ↑                    ↑               ↑
    pd.Series            dict{sym: shares}   dict{sym: shares}  list[Fill]
    (stock→score)        (target positions)  (approved)         (executed)
```

每层只通过标准数据结构通信，不直接调用下一层。

## 目录结构

```
engine/
├── data/                    # 数据层 (不变)
│   ├── loader.py            # get_training_data() — 已存在
│   └── screener.py          # screen_and_split() — 已存在
│
├── alpha/                   # Alpha 创建层 [新建]
│   └── creator.py           # create_alpha(): factors + ML → scores
│                            #   合并 predictor.py + ranker.py 逻辑
│
├── portfolio/               # 组合构建层 [新建]
│   └── builder.py           # build_portfolio(): scores + capital → positions
│                            #   迁移 position_manager.py 逻辑
│
├── risk/                    # 风控层 [新建]
│   └── manager.py           # apply_risk(): positions → approved positions
│                            #   合并 risk_filter.py + trading_rules.py
│
├── execution/               # 执行层 [新建]
│   ├── simulator.py         # simulate_fills(): targets + current → fills
│   │                        #   从 backtest_runner.py 提取执行逻辑
│   └── broker.py            # 券商桥接 (BrokerInterface) — 已存在
│
├── backtest/                # 事件循环 [重写]
│   └── engine.py            # BacktestEngine: for date in dates → α→P→R→E
│                            #   合并 backtest_runner + rebalance + walkforward
│
├── trainer.py               # 模型训练 (不变)
└── tracker.py               # 推荐追踪 (不变)

strategy/                    # 策略定义 (保留)
├── factor_strategy.py       # 因子策略 → 配置 Alpha+Portfolio+Risk
├── first_board.py           # 首板策略
├── aggressive_strategy.py   # 激进策略
├── ensemble.py              # 模型集成 (不变)
├── signals.py               # 信号生成 (不变)
└── planner.py               # 阶段规划 (不变)
```

## 接口定义

### 1. Alpha Creation
```python
# engine/alpha/creator.py

def create_alpha(
    factors_repo: FactorRepo,
    stocks: list[str],
    model: EnsembleModel,
    passed_factors: list[str],
    store: DataStore,
    eval_date: str = None,
) -> pd.Series:
    """因子 + ML + Demon信号 → 排名分数。

    Input:  因子数据 + 训练好的模型 + 通过IC筛选的因子列表
    Output: pd.Series (stock → score, 0-1, 越高越好)
    """
```

### 2. Portfolio Construction
```python
# engine/portfolio/builder.py

def build_portfolio(
    scores: pd.Series,
    capital: float,
    prices: pd.Series,
    max_positions: int = 3,
) -> list[dict]:
    """分数 + 资金 + 价格 → 目标仓位。

    Input:  scores (stock→score), capital (¥), prices (stock→¥)
    Output: [{symbol, shares, cost, weight}, ...]  已按 score² 集中分配
    """
```

### 3. Risk Management
```python
# engine/risk/manager.py

def apply_risk(
    targets: list[dict],
    current_positions: list[dict],
    capital: float,
    mood_stage: str = "复苏",
) -> list[dict]:
    """目标仓位 → 风控审核 → 批准仓位。

    Input:  targets (Portfolio输出), current_positions (当前持仓)
    Checks: 涨跌停过滤, 单票上限, 总仓位上限, 日亏损熔断, 移动止损
    Output: approved targets (可能被缩减或拒绝)
    """
```

### 4. Execution
```python
# engine/execution/simulator.py

def simulate_fills(
    approved: list[dict],
    current: list[dict],
    prices: pd.Series,
    date: str,
) -> tuple[list[dict], list[dict]]:
    """批准仓位 → 对比当前持仓 → 生成买卖订单 → 模拟成交。

    Input:  approved (Risk输出), current (当前持仓), prices (当日价格)
    Output: (fills, new_positions)
      fills: [{symbol, side, shares, price, commission}, ...]
      new_positions: [{symbol, shares, cost_price}, ...]
    """
```

### 5. Backtest Engine
```python
# engine/backtest/engine.py

class BacktestEngine:
    """事件循环: for each rebalance date → α → P → R → E → 记录权益"""

    def run(self, ...) -> dict:
        """返回 {metrics, equity_curve, trades}"""
```

## 迁移计划

### Phase 1: 新建各层 (不改现有代码)
1. `engine/alpha/creator.py` — 从 predictor.py + ranker.py 提取
2. `engine/portfolio/builder.py` — 从 position_manager.py 迁移
3. `engine/risk/manager.py` — 从 risk_filter.py + trading_rules.py 合并
4. `engine/execution/simulator.py` — 从 backtest_runner._execute_positions 提取

### Phase 2: 重写事件循环
5. `engine/backtest/engine.py` — 用新分层重写

### Phase 3: 删除旧代码
6. 删除 `engine/backtest_runner.py`
7. 删除 `engine/rebalance.py`
8. 删除 `engine/walkforward.py`
9. 更新 `auto_run.py` 和 `web/pipeline.py` 引用

### Phase 4: 验证
10. 跑现有测试确保 78 项全绿
11. 跑 70/30 审计确保 Sharpe 不退化

## 对比

| 维度 | 当前 | V2 |
|------|------|------|
| Alpha/P/R/E 分离 | ❌ 全部耦合在 backtest_runner | ✅ 四层独立 |
| 新策略开发 | 改 backtest_runner 内部逻辑 | 换一个 Alpha 或 Portfolio 实现 |
| 单元测试 | 只能测整个回测 | 每层独立测 |
| 代码复用 | rebalance 和 walkforward 大量重复 | 共享 Portfolio+Risk+Execution |
| 回测变体 | 3 个不同文件 (runner/rebalance/walkforward) | 1 个引擎，不同参数 |
