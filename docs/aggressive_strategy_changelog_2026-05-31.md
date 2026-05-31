# 激进策略转向 — 变更记录

**日期**: 2026-05-31
**目标**: 5000元→100万，6个月，200倍回报
**变更文件**: 10个 | 测试: 43 pass, 0 fail

---

## 变更总览

| 文件 | 变更类型 | 核心改动 |
|------|---------|---------|
| config/config.yaml | 重写 | 全部默认值转向激进 |
| engine/ranker.py | 修改 | 中性化加开关(默认关), demon权重80% |
| engine/backtest_runner.py | 重写 | 涨跌停检测+5000元资金约束+真实佣金 |
| data/repository.py | 重写 | 可买性过滤+ST可选+次新120天 |
| strategy/ensemble.py | 重写 | 移除Ridge, 200树×深度15, 尾部IC加权 |
| engine/screener.py | 修改 | IC阈值0.01→0.03, 训练目标从config |
| factor/demon.py | 修改 | vol_window 20→10, 量比上限5→10, 动量上限0.3→0.5 |
| factor/alpha_factory.py | 修改 | IC筛选目标跟随config |
| factor/cache.py | 修改 | real_fundamental跟随use_fundamental开关 |
| engine/rebalance.py | 修改 | 默认周频调仓, 5000元/3只, 滑点千三 |

---

## 详细变更

### 1. config.yaml — 激进参数化

| 参数 | 旧值 | 新值 | 理由 |
|------|------|------|------|
| data.universe | "hs300" | "all" | 大盘股跑不出200倍 |
| data.start_date | "2020-01-01" | "2023-01-01" | 妖股模式变化快 |
| factor.use_fundamental | true | false | PE/PB/股息对短期极端收益是反向信号 |
| strategy.target | "return_5d" | "return_1d" | 直接预测明天涨跌，捕捉涨停 |
| strategy.train_window | 504 | 252 | 缩短到1年 |
| strategy.retrain_freq | 21 | 5 | 每周重训练 |
| ranker.ml_weight | 0.5 | 0.2 | 妖股信号占80% |
| ranker.neutralize | (无) | false | 关闭行业市值中性化 |
| backtest.initial_capital | 1,000,000 | 5,000 | 实盘资金 |
| backtest.max_positions | 30 | 3 | 5000元最多买3只 |
| backtest.max_weight | 0.10 | 0.50 | 集中押注 |
| backtest.slippage | 0.001 | 0.003 | 小盘股冲击成本更大 |
| risk.max_drawdown | 0.15 | 0.80 | 容忍大幅回撤 |
| risk.max_sector_exposure | 0.30 | 1.00 | 不限制行业集中 |
| risk.daily_loss_limit | 0.05 | 0.25 | 妖股日波动20%正常 |
| screening (新增) | 无 | min_abs_ic:0.03, min_ic_ir:0.15 | 提高IC门槛 |
| affordable (新增) | 无 | max_price:30, min_amount:500万, min_days:120 | 5000元可买性 |

### 2. ranker.py — 中性化可关闭 + demon权重

```python
# 旧: combined = ml_norm * 0.5 + demon * 0.5, 强制中性化
# 新: ml_weight=cfg("ranker.ml_weight", 0.2), 中性化可关闭
```

### 3. backtest_runner.py — 涨跌停+资金+佣金

新增函数:
- `_is_limit_up(price, prev)` / `_is_limit_down(price, prev)` — 涨停/跌停检测(9.5%阈值)
- `_real_commission(trade_value)` — 真实佣金(min 5元 + 千一印花税)
- `_affordable_filter(symbols, close_df, capital, top_n)` — 可买性过滤(股价×100≤资金/n)

回测流程改为:
1. 过滤买不起的股票
2. 在买得起的股票中选top N
3. 检查涨跌停状态(涨停的排除)
4. 按手数取整(100股为单位) + 真实佣金

### 4. repository.py — 可买性+股票池

`get_qualified()` 改为可配置:
- `affordable.exclude_st`: false (不排除ST，摘帽是弹性来源)
- `affordable.exclude_star_st`: true (仍排除*ST)
- `affordable.min_history_days`: 120 (不排除次新)
- `affordable.max_stock_price`: 30 (30元以上的股5000买不起)
- `affordable.min_daily_amount`: 500万 (过滤僵尸股)

### 5. ensemble.py — 激进模型

- **移除 Ridge**: 线性基线稀释非线性信号
- **树参数**: n_estimators 60→200, max_depth 6→15, min_samples_leaf 50→5, subsample 0.5→0.8
- **尾部IC加权**: 70%权重给模型在Top 5%收益股票上的排序能力，30%给全局IC
- LightGBM: num_leaves 31→63, learning_rate 0.05→0.08

### 6. demon.py — 窗口缩短+clip放宽

- vol_window: 20 → 10 (更灵敏)
- price_window: 60 → 30 (近期突破)
- 量比上限: 5 → 10倍
- 动量上限: 30% → 50%

### 7. screener.py — IC阈值+训练目标可配置

- 训练目标从config读取: return_1d(1天)/return_5d(5天)/return_20d(20天)
- IC阈值从config读取: min_abs_ic=0.03, min_ic_ir=0.15
- 前视偏差防护天数跟随目标周期

---

## 系统状态对比

| 维度 | 稳健版 (旧) | 激进版 (新) |
|------|------------|------------|
| 目标函数 | 夏普比率 | 绝对收益 |
| 持仓数 | 20-30只 | 2-3只 |
| 单票上限 | 10% | 50% |
| 初始资金 | 100万 | 5000元 |
| 调仓频率 | 月度 | 周度 |
| 股票池 | 去ST, 250天历史 | ST可选, 120天, 股价≤30元 |
| 因子侧重 | 价值+动量+质量 | 博弈论+妖股+量价 |
| 模型集成 | 5模型(LGBM+XGB+RF+ET+Ridge) | 4树模型(LGBM+XGB+RF+ET) |
| 模型评价 | 全局IC | 尾部IC(70%)+全局IC(30%) |
| 行业中性化 | 强制 | 关闭 |
| Demon权重 | 50% | 80% |
| IC阈值 | 0.01 | 0.03 |
| 风控 | 15%回撤止损 | 80%回撤容忍 |

## 验证

```
43 tests passed, 0 failed in 4.81s
All modules import OK
Limit detection: OK
Commission model: OK (min 5元)
Affordability filter: OK (过滤高价股)
DemonSignals: OK (窗口缩短)
Ensemble: OK (无Ridge, 尾部IC)
```
