# 系统架构 V4 设计方案

日期: 2026-06-05  
来源: NautilusTrader Actor Model + GitHub solo-dev 共识 (2024-2025) + Composable Pipeline 模式

## 核心模式: Composable Pipeline (编排器模式)

```
Alpha → Portfolio → Risk → Execution

每个组件只知道自己的输入输出。
Pipeline 是唯一耦合点——只负责按顺序调用。
```

## 组件接口

```python
# alpha.py — 预测→信号→排名
def generate(factors_repo, model, stocks, date) -> pd.Series
    # return: stock → score (越高越好)

# portfolio.py — 信号→仓位
def build(scores, capital, prices, max_positions=3) -> list[dict]
    # return: [{symbol, shares, cost, weight}, ...]

# risk.py — 仓位→风控审核
def check(targets, current_positions, capital, mood) -> list[dict]
    # return: approved targets (可能被缩减/拒绝)

# execution.py — 目标→成交
def fill(approved, current, prices, date) -> tuple[list, list]
    # return: (fills, new_positions)

# orchestrator.py — 事件循环 + 组装
class Pipeline:
    def __init__(self, alpha, portfolio, risk, execution): ...
    def run(self, dates, close_df, capital) -> dict
        # for date in dates:
        #   scores = self.alpha.generate(...)
        #   targets = self.portfolio.build(...)
        #   approved = self.risk.check(...)
        #   fills = self.execution.fill(...)
```

## 改动清单

新建 (5):
- engine/alpha.py        — 从 predictor.py + ranker.py 合并
- engine/portfolio.py    — 从 position_manager.py 迁移
- engine/risk.py         — 从 risk_filter.py + trading_rules.py 合并
- engine/execution.py    — 从 backtest_runner._execute_positions 提取
- engine/orchestrator.py — Pipeline 类, 唯一耦合点

删除 (4):
- engine/backtest_runner.py
- engine/rebalance.py
- engine/walkforward.py
- engine/risk_filter.py

修改 (2):
- auto_run.py — 引用改为 Pipeline
- web/pipeline.py — 引用改为 Pipeline

## 来源

- NautilusTrader: Actor Model + MessageBus 解耦 (GitHub 2k+ stars, 2024)
- ryannapp12/quant_trading_engine: 单人项目 core/strategies 分离 (2025)
- "Composable Pipeline": 2024-2025 回测引擎设计共识
- Solo-dev 共识: 15-40 文件用扁平结构, 文件名前缀分组
