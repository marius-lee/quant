# 系统架构 V5 设计方案

日期: 2026-06-05  
来源: Freqtrade (30k+ 用户验证) + Zerodha KISS 哲学 + solo-dev 共识

## 核心洞察

**Freqtrade 30,000+ 用户证明了：不需要四层分离。一个 Strategy 基类 + 三个方法足够。**

```
QuantConnect: 5 层分离 → 给 27.5 万用户、多资产、多策略混搭
Freqtrade:    1 个 Strategy 类 → 给 30k 用户、加密货币单品种
我们:         1 个开发者、A 股单品种、3 个策略
```

我们更接近 Freqtrade 的规模，不应照搬 QuantConnect。

## V5 方案

```python
# strategy/base.py — 重构后的基类

class BaseStrategy(ABC):
    """Freqtrade 模式: 三个方法定义一个策略"""
    
    def compute_factors(self, store, stocks) -> DataFrame:
        """Step 1: 数据→因子 (子类可覆盖)"""
        
    def generate_signals(self, factors, model) -> Series:
        """Step 2: 因子→信号 (子类可覆盖)"""
        
    def build_portfolio(self, signals, capital, prices) -> list:
        """Step 3: 信号→仓位 (子类可覆盖)"""
```

现有三个策略的改动：

```python
# strategy/factor_strategy.py
class FactorStrategy(BaseStrategy):
    def compute_factors(self, store, stocks):
        # 全部因子计算 (已实现, 移到基类方法)
    def generate_signals(self, factors, model):
        # ML预测 + Demon + 排名 (已实现)
    def build_portfolio(self, signals, capital, prices):
        # allocate_concentrated (已实现)

# strategy/first_board.py  
class FirstBoardStrategy(BaseStrategy):
    def compute_factors(self, store, stocks):
        # 涨停检测因子 (已实现)
    def generate_signals(self, factors, model):
        # 4因子评分 (已实现)
    def build_portfolio(self, signals, capital, prices):
        # 回调买入逻辑 (已实现)

# strategy/aggressive_strategy.py
class AggressiveStrategy(BaseStrategy):
    # ...同上模式
```

## 删掉什么

```
engine/backtest_runner.py   ← 逻辑移入 BaseStrategy.build_portfolio
engine/rebalance.py         ← 逻辑移入 BaseStrategy (作为 run(mode='rebalance'))
engine/walkforward.py       ← 逻辑移入 BaseStrategy (作为 run(mode='walkforward'))
engine/risk_filter.py       ← 逻辑移入 BaseStrategy (内嵌风控)
```

## 不改什么

```
engine/loader.py            ← 不变: 数据加载
engine/screener.py          ← 不变: IC 筛选
engine/trainer.py           ← 不变: 模型训练
engine/predictor.py         ← 不变: 预测 (被 generate_signals 调用)
engine/ranker.py            ← 不变: 排名 (被 generate_signals 调用)
engine/sim_broker.py        ← 不变: 模拟交易
factor/                     ← 不变: 全部因子模块
strategy/ensemble.py        ← 不变: 模型集成
strategy/position_manager.py ← 不变: 仓位分配 (被 build_portfolio 调用)
strategy/trading_rules.py   ← 不变: 退出规则 (保留供 API 使用)
```

## 改动量

| 操作 | 文件 |
|------|------|
| 重构 | `strategy/base.py` (三个抽象方法) |
| 重构 | `strategy/factor_strategy.py` (适配新基类) |
| 重构 | `strategy/first_board.py` (适配新基类) |
| 重构 | `strategy/aggressive_strategy.py` (适配新基类) |
| 删除 | `engine/backtest_runner.py` |
| 删除 | `engine/rebalance.py` |
| 删除 | `engine/walkforward.py` |
| 删除 | `engine/risk_filter.py` |
| 修改 | `auto_run.py` (调用方式简化为 strategy.run()) |
| 修改 | `web/pipeline.py` (同上) |

## 对比前五版

| | V1 扁平 | V2 嵌套 | V3 Backtrader | V4 编排器 | V5 Freqtrade |
|------|:--:|:--:|:--:|:--:|:--:|
| 新建文件 | 5 | 10 | 4 | 5 | **0** |
| 删除文件 | 3 | 3 | 4 | 4 | **4** |
| 接口数 | 4个函数 | 5个函数 | 4个函数 | 4个函数 | **3个方法** |
| 耦合点 | 分散 | 少 | Cerebro | Pipeline | **Strategy类** |
| 来源 | 随手 | QC | Backtrader | Nautilus | **Freqtrade** |
| 实现复杂度 | 中 | 高 | 低 | 中 | **最低** |

## 来源

- Freqtrade: 30k+ GitHub stars, `IStrategy` 三方法接口, 生产级验证
- Zerodha: KISS 哲学, "从不为 5000 万用户构建, 只构建可调整的系统"
- Solo-dev 共识 (2024-2025): 2-3 组件架构, 不是 5 层
- VectorBT vs Backtrader vs Freqtrade 对比 (2025): Freqtrade 配置驱动+模板方法模式最适合单人项目
