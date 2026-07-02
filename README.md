# A股量化选股系统

## 架构概览

基于 Grinold & Kahn 的 Fundamental Law 框架搭建的 A 股量化选股系统。因子驱动、风险中性化、组合优化、模拟执行四阶段全流程。

```
基础层 → 数据层 → 因子层 → Alpha层 → 风控层 → 优化层 → 执行层 → 监控层 → Web
config    SQLite   6类12+   IC加权   行业中性  得分排序   成本模型   绩效归因   Flask
utils             因子               Ledoit-Wolf 整手约束            风险报告   8521
calendar                                         资本自适应
```

**核心设计**: 每一层只依赖下层接口，可独立回测。信号自底向上流动，订单自顶向下执行。

## 目录结构

```
quant/
├── config/                # Layer 0: 配置 + 日志 + 日期
│   ├── loader.py          #   YAML 热加载
│   └── config.yaml        #   集中参数
├── utils/                 # Layer 0
│   ├── logger.py          #   模块级 logger + 轮转
│   └── date.py            #   日期格式化
├── data/                  # Layer 1: 数据
│   ├── store.py           #   DataStore — 多源日线增量同步
│   ├── trade_repo.py      #   TradeRepo — 交易记录读写
│   └── market.db          #   SQLite 数据仓库
├── factor/                # Layer 2: 因子
│   ├── base.py            #   Factor 抽象基类
│   ├── compute.py         #   因子计算（向量化、纯函数）
│   ├── evaluate.py        #   IC/IR 评估 + 衰减分析
│   └── synth.py           #   因子合成（等权/IC加权）
├── alpha/                 # Layer 3: Alpha
│   └── model.py           #   AlphaModel — 收益预测 + 截面排名
├── risk/                  # Layer 4: 风控
│   ├── neutralize.py      #   行业/市值中性化
│   ├── covariance.py      #   Ledoit-Wolf 协方差估计
│   └── constraints.py     #   暴露约束 + 流动性过滤
├── optimizer/             # Layer 5: 组合优化
│   ├── portfolio.py       #   PortfolioConstructor — 目标组合生成
│   └── rebalance.py       #   调仓计算
├── execution/             # Layer 6: 执行
│   ├── engine.py          #   ExecutionEngine — 模拟交易执行
│   ├── cost.py            #   统一成本模型（佣金+印花税+滑点）
│   ├── quote.py           #   新浪实时行情
│   └── calendar.py        #   交易日历
├── monitor/               # Layer 7: 监控
│   ├── attribution.py     #   绩效归因（因子收益 vs 残差收益）
│   └── report.py          #   日报生成
├── web/                   # Web 仪表盘
│   ├── app.py             #   Flask API + 调度线程
│   ├── shared.py          #   线程安全内存状态
│   ├── static/            #   前端资源
│   └── templates/         #   HTML 模板
├── pipeline.py            # 全流程编排
├── scheduler.py           # 盘后调度
├── requirements.txt
└── README.md
```

## 数据流

```
交易日 15:30 → scheduler.py → pipeline.py.run()
  Step 1: DataStore.update_daily()        # 增量同步日线
  Step 2: Factor.compute() → rank_ic()    # 因子计算 + IC评估
  Step 3: AlphaModel.predict()           # 因子合成 + 截面排名
  Step 4: RiskManager.apply()            # 中性化 + 约束过滤
  Step 5: PortfolioConstructor()         # Top N + 整手分配
  Step 6: ExecutionEngine.execute()      # 模拟成交 → trades.db
  Step 7: Monitor.generate_report()      # 归因 → Web 推送
```

## 运行方式

```bash
cd /Users/mariusto/project/quant

# Web 服务（含调度线程）
PYTHONPATH=. python3 web/app.py
# 浏览器打开 http://localhost:8521

# 手动触发全流程
PYTHONPATH=. python3 pipeline.py

# 因子评估报告
PYTHONPATH=. python3 -c "from factor.evaluate import factor_report; print(factor_report())"
```

## 数据存储

| 数据库 | 用途 | 大小 |
|--------|------|------|
| `data/market.db` | 全 A 股日线（~5500 只，5 数据源） | ~400MB |
| `data/trades.db` | 模拟交易记录 + 信号 + 策略配置 | ~1MB |

## Key Design Decisions

| 决策 | 选择 | 原因 |
|------|------|------|
| 存储 | SQLite | 单用户本地，零配置，千万行无压力 |
| 频率 | 日线 | A 股 T+1，更高频无意义 |
| 因子评估 | 截面 Rank IC | Spearman 秩相关，对异常值鲁棒 |
| 协方差 | Ledoit-Wolf 收缩 | 优于样本协方差，适合高维 |
| 组合构建 | 资本自适应（等权→得分倾斜→均值-方差） | 方法随资金规模升级，不对初始本金过度优化 |
| 成本模型 | 统一 CostModel | 确保所有模拟交易的绩效可比 |
| 参数管理 | YAML + 热更新 | 零停机调参 |

## 北极星

¥5,000 → ¥100 万（6 个月，200 倍）。所有架构决策围绕此目标。

## 免责声明

本系统仅供学习和研究使用。股市有风险，投资需谨慎。系统输出不构成任何投资建议。
