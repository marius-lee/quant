# CLAUDE.md

This file provides guidance to Claude Code when working with code in this repository.

---

## Project overview

A股量化选股系统。基于 Grinold & Kahn Fundamental Law 的 7 层架构：数据 → 因子 → Alpha → 风控 → 优化 → 执行 → 监控。¥5,000 起步，目标 ¥100 万。

## Commands

```bash
cd /Users/mariusto/project/quant

# Web 服务 (端口 8521)
PYTHONPATH=. python3 web/app.py

# 手动触发全流程
PYTHONPATH=. python3 pipeline.py

# 因子评估
PYTHONPATH=. python3 -c "from factor.evaluate import factor_report; print(factor_report())"

# 运行测试
PYTHONPATH=. python3 -m pytest tests/ -v
```

## Architecture (7 layers, ~25 files)

### Layer 0: Infra (`config/` + `utils/` + `execution/calendar.py`)
- `config/loader.py` — YAML 配置热加载，`get("key.path", default)` 取值
- `utils/logger.py` — `get_logger("module.name")`
- `utils/date.py` — `to_str()`, `to_compact()`, `today_str()`
- `execution/calendar.py` — `is_trading_day()`, `is_market_open()`, `get_trading_period()`

### Layer 1: Data (`data/`)
- `store.py` — **DataStore**: 多源日线增量同步（tickflow→新浪→腾讯→tushare→akshare），速度自适应轮转
- `trade_repo.py` — **TradeRepo**: `sim_trades` 统一读写，消除重复 SQL

### Layer 2: Factor (`factor/`)
- `base.py` — **Factor** 抽象基类: `compute(data) → Series`, `evaluate(values, returns) → dict`
- `compute.py` — 因子计算：动量(5/10/20/60d)、反转(5d)、波动率(20d)、量比(5/20d)、Amihud(20d)、偏度(20d)
- `evaluate.py` — 截面 Rank IC + IC_IR + 衰减分析 + 相关性矩阵
- `synth.py` — 因子合成：`equal_weight()` / `ic_weighted()`

### Layer 3: Alpha (`alpha/`)
- `model.py` — **AlphaModel**: 因子合成 → 收益预测 → 截面分位数排名

### Layer 4: Risk (`risk/`)
- `neutralize.py` — `industry_neutralize()`, `size_neutralize()`: 截面回归取残差
- `covariance.py` — `ledoit_wolf_cov()`: 收缩协方差估计 (Ledoit & Wolf 2004)
- `constraints.py` — **RiskLimits**: 单票仓位上限、行业暴露上限、流动性门槛、ST 过滤

### Layer 5: Optimizer (`optimizer/`)
- `portfolio.py` — **PortfolioConstructor**: 资本自适应 (<2万等权 / 2-10万得分倾斜 / >10万均值-方差) + 整手约束
- `rebalance.py` — `compute_trades()`: diff 目标持仓 vs 当前持仓 → 买卖订单列表

### Layer 6: Execution (`execution/`)
- `engine.py` — **ExecutionEngine**: 订单执行 → trades.db + capital_after
- `cost.py` — **CostModel**: 统一成本模型（佣金万三 + 最低 5 元 + 印花税千一）
- `quote.py` — `fetch_quotes()`: 新浪批量实时行情
- `calendar.py` — 交易日历

### Layer 7: Monitor (`monitor/`)
- `attribution.py` — Brinson 归因: 总收益 = 因子收益 + 选股收益
- `report.py` — `generate_report()`: 日报 → JSON → Web 推送

## Data flow

```
scheduler.py (交易日 15:30)
  └─ pipeline.py.run(date)
       ├─ Step 1: DataStore.update_daily()
       ├─ Step 2: factor/evaluate.py → IC/IR report
       ├─ Step 3: alpha/model.py → predict → cross_sectional_rank
       ├─ Step 4: risk/neutralize.py + risk/constraints.py → filter
       ├─ Step 5: optimizer/portfolio.py → construct → TargetPortfolio
       ├─ Step 6: execution/engine.py → execute → trades.db
       └─ Step 7: monitor/report.py → push to web/shared.py
```

Each step has independent try/except — failure in one layer does not block later layers.

## Key design decisions

- **截面 Rank IC**: Spearman 秩相关评估因子预测力，对异常值鲁棒
- **Ledoit-Wolf 收缩**: 协方差估计优于样本估计，适合高维截面（~5000 股票 × 60 日）
- **资本自适应优化**: <2万等权 → 2-10万得分倾斜 → >10万均值-方差 + 整数约束。方法随资金增长自动升级
- **统一成本模型**: `CostModel` 是所有模拟交易的唯一成本入口，确保绩效可比
- **配置驱动**: 所有阈值从 `config.yaml` 读取，无硬编码
- **独立策略多 track**: `strategy_config` 表允许多策略并行运行，各自独立资金核算

## Config access pattern

```python
from config.loader import get as cfg
value = cfg("factor.min_abs_ic", 0.02)
```

Config sections: `data`, `factor`, `alpha`, `risk`, `optimizer`, `execution`, `backtest`, `web`

## Logging convention

```python
from utils.logger import get_logger
logger = get_logger("module.name")
```

## Files pending implementation

| File | Status |
|------|--------|
| `factor/base.py` | Interface only |
| `factor/compute.py` | To implement |
| `factor/evaluate.py` | To implement |
| `factor/synth.py` | To implement |
| `alpha/model.py` | To implement |
| `risk/neutralize.py` | To implement |
| `risk/covariance.py` | To implement |
| `risk/constraints.py` | To implement |
| `optimizer/portfolio.py` | To implement |
| `optimizer/rebalance.py` | To implement |
| `execution/engine.py` | To implement |
| `execution/cost.py` | To implement |
| `monitor/attribution.py` | To implement |
| `monitor/report.py` | To implement |
| `pipeline.py` | To implement |
| `scheduler.py` | To implement |

## Files to remove (legacy, post-migration)

`intraday_runner.py`, `execution/sell_chain.py`, `archive/*`, `strategies/*`, `ops/*`, `backtest/__init__.py`
