# 量化系统重新设计方案

日期: 2026-06-06  
来源: A股个人量化实盘指南 + 61参数矩阵工厂 + DeepSeek/QMT实战 + GitHub开源项目

## 核心理念

```
       ┌──────────────┐
       │ 1. 扫描全市场  │ ← 每天/每周跑一次
       │  历史数据+算法  │
       └──────┬───────┘
              ↓
       ┌──────────────┐
       │ 2. 排名选出    │ ← 1-3只最可能涨的
       │  1-3只股票     │
       └──────┬───────┘
              ↓
       ┌──────────────┐
       │ 3. 买入       │ ← 信号强全押, 信号近分散
       └──────┬───────┘
              ↓
       ┌──────────────┐
       │ 4. 持仓监控    │ ← 实时价格 + 止盈止损
       │  (已有)       │
       └──────┬───────┘
              ↓
       ┌──────────────┐
       │ 5. 卖出       │ ← ATR移动止损 / 止盈触发
       └──────┬───────┘
              ↓
         回到 1 (反复)
```

## 设计原则

1. **扫描→排名→买卖→重复** — 不搞一次预测467天不动
2. **入口=出口** — 每次扫描同时判断: 哪些该买, 哪些持仓该卖
3. **简单优先** — 去除IC筛选(保留为诊断工具), 去除过拟合风险
4. **风控分离** — 风控是独立层, 不在策略里硬编码

## 系统结构

```
quant/
├── scanner.py           ← [新] 全市场扫描引擎: 数据→因子→排名→输出1-3只
│                         参考: 61参数矩阵工厂 (61个条件并行, 700秒扫4400股)
│
├── executor.py          ← [新] 买卖执行: scanner输出→买入/卖出→记录
│                         参考: DeepSeek/QMT 自动化下单流程
│
├── monitor.py           ← [已存在] 持仓监控: 实时价格 + 止盈止损
│                         web/app.py 的 /api/trading/exit 已实现
│
├── scheduler.py         ← [新] 调度器: 每日08:00扫描→输出→09:25执行
│                         参考: A股个人量化实盘时间线
│
保留:
├── web/                 ← 不变: Web界面 + API
├── data/                ← 不变: 数据存储
├── factor/              ← 保留: 因子计算 (供scanner调用)
├── strategy/            ← 保留: 策略逻辑 (供scanner调用)
└── config/              ← 不变: 配置文件

简化或降级:
├── engine/              ← 降级: 只保留loader/screener(诊断用)/trainer(可选)
├── backtest/            ← 保留: metrics.py
└── execution/           ← 保留: broker.py + live_broker.py
```

## Scanner 设计 (核心)

```python
# scanner.py — 全市场扫描引擎

def scan(store, mode='daily') -> list[dict]:
    """扫描全市场 → 返回 1-3 只推荐股票。
    
    mode:
      'daily'  → 每日扫描 (快速, 用最近5天数据)
      'weekly' → 每周扫描 (深度, 用最近60天数据)
    
    返回: [{symbol, name, score, price, reason}, ...]
    """
    stocks = StockRepo(store).get_qualified(capital=current_capital)
    
    # 并行跑多个策略, 取共识
    results = []
    for strategy in [factor_strategy, first_board, aggressive]:
        picks = strategy.run_quick(stocks, store)
        results.extend(picks)
    
    # 排名: 按得分降序, 取 top 3
    results.sort(key=lambda x: x['score'], reverse=True)
    return results[:3]
```

## Executor 设计

```python
# executor.py — 买卖执行

def execute_recommendations(recs: list[dict], store) -> dict:
    """执行推荐: 卖出不在新推荐中的, 买入新推荐。
    
    参考: DeepSeek/QMT — 09:25集合竞价后下单, VWAP拆单
    """
    current = get_current_positions()
    
    # 1. 卖出: 不在新推荐中的持仓
    new_symbols = {r['symbol'] for r in recs}
    for pos in current:
        if pos['symbol'] not in new_symbols:
            sell(pos)
    
    # 2. 买入: 新推荐 (信号强全押, 信号近分散)
    to_buy = [r for r in recs if r['symbol'] not in current_symbols]
    if to_buy:
        allocs = allocate_concentrated(available_cash, to_buy)
        for a in allocs:
            buy(a['symbol'], a['shares'], a['price'])
```

## 改动量

| 操作 | 文件 | 说明 |
|------|------|------|
| 新建 | `scanner.py` | 全市场扫描+排名 |
| 新建 | `executor.py` | 买卖执行 |
| 新建 | `scheduler.py` | 定时调度 |
| 保留 | `web/`, `data/`, `factor/`, `strategy/`, `config/` | 不变 |
| 降级 | `engine/` | 只保留 loader + screener(诊断) + trainer(可选) |
| 删除 | `engine/backtest_runner.py`, `rebalance.py`, `walkforward.py` | 被 scanner+executor 替代 |

## 和现有系统的关系

```
现有系统:
  auto_run.py → RecommendationEngine → 回测 → 输出推荐

新系统:
  scheduler.py → scanner.py → 输出推荐 → executor.py → 记录
                                  ↓
                             已有持仓?
                           ├─ 是 → monitor.py → 止盈止损 → executor卖出
                           └─ 否 → executor买入
```

Web界面、API、因子库、数据层全部保留。变的是"怎么从推荐到买卖"这一环。
