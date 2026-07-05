# ADR 015: config.yaml 默认值从开发测试值切换为生产标准值

**日期**: 2026-07-05
**状态**: 已采纳
**关联**: P39 — 参数生产标准化

---

## 背景

config.yaml 中的组合构建参数（max_positions, max_single_position, default_capital 等）
此前为开发测试值——回测仅选 1 只股票全仓持有，资本 5000 CNY。
这些值适合因子快速筛选，但作为 config.yaml 的默认值是危险的：
任何未显式指定参数的 backtest / pipeline 调用都会在单股票模式下运行，
产出的 Sharpe/IR 不是组合策略指标，而是单股票选择命中率。

代码 fallback 值反而比 yaml 更接近生产标准（max_positions: 5-20 vs 1），
证明当初编写 yaml 时未将开发值与生产值分离。

## 问题

| 参数 | 旧值 (dev) | 实际效果 | 业界标准 |
|------|-----------|---------|---------|
| max_positions | 1 | 单股票选择 | 20-50 (多因子组合) |
| max_single_position | 1.0 | 全仓单票 | 0.05-0.10 (5-10%) |
| pe_max | 1000 | 包含极端噪声 | 200-500 |
| default_capital | 5000 | 无法分散 | 100k+ |
| default_start | 2026-01-01 | 仅半年数据 | 2023-01-01 (≥3年) |
| train_window | 252 | 最低可接受线 | 500+ |

旧参数下 eval_stepwise.sh 的回测结果为单股票命中率指标，
而非多因子组合收益。之前的 Sharpe=0.96 / +24.1% 不应被解读为策略收益。

## 决策

config.yaml 的默认值改为生产标准。因子筛选脚本（eval_stepwise.sh）
继续显式传 capital=5000，但组合构建参数（max_positions/max_single_position）
从 config 读取——单因子评估不需要组合分散，
但也不应该用单股票选择冒充组合评估。

### 新默认值

| 参数 | 旧值 | 新值 | 依据 |
|------|------|------|------|
| max_positions | 1 | **20** | Grinold & Kahn (1999): 多因子组合需 20-50 只 |
| max_single_position | 1.0 | **0.10** | 行业标准: 单票 ≤10% |
| pe_max | 1000 | **200** | PE>200 为极端值/亏损股, 无 alpha 价值 |
| default_capital | 5000 | **100000** | 100k = 20×5000, 最低可分散规模 |
| default_start | 2026-01-01 | **2023-01-01** | Lo (2002): T≥3年 CI 才可接受 |
| train_window | 252 | **500** | Grinold & Kahn: 60月月频→日频等价 500+ |

### 开发/生产分离

config.yaml 默认值 = 生产标准。需要开发测试值时显式传参：

```python
# 生产回测 (使用 config 默认值)
run_backtest()

# 因子快速筛选 (显式覆盖)
run_backtest(capital=5000)  # 小资金, 但组合参数仍用 config 的 max_positions=20
```

## 影响

- 默认回测从单股票选择变为 20 只等权组合
- 预期 Sharpe 低于旧值 (分散化降低极值), 但更反映实盘
- eval_stepwise.sh 的 Layer 3 回测受 max_positions=20 影响:
  capital=5000 时实际持仓 ~4 只 (资金不足以支持 20 只),
  这比旧版 1 只更接近实盘但仍偏集中

## 参考文献

- Grinold, R. & Kahn, R. (1999). *Active Portfolio Management*. McGraw-Hill.
- Lo, A.W. (2002). "The Statistics of Sharpe Ratios." *Financial Analysts Journal*, 58(4), 36-52.
- Bailey, D.H. & Lopez de Prado, M. (2014). "The Deflated Sharpe Ratio." *Journal of Portfolio Management*, 40(5), 94-107.
