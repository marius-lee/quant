# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

A股量化选股系统。全A股 ~5500 只日线，63 手工因子 + 100 自动生成因子 (WorldQuant 风格)，5 模型集成 (LightGBM+XGBoost+RF+ET+Ridge)，向量化回测，Flask Web 可视化。

🚨 **北极星目标：5000元初始资金，6个月实现盈利100万（200倍回报）。所有技术决策、功能优先级、优化方向必须围绕此目标，禁止无关优化、过度工程。**

已验证 Sharpe 1.24，年化收益 38.7%。

## Commands

```bash
cd /Users/mariusto/project/quant

# Web 服务 (端口 8521)
PYTHONPATH=. python3 web/app.py
PYTHONPATH=. python3 run.py              # 同上

# 全链路定时分析
PYTHONPATH=. python3 auto_run.py

# 测试
PYTHONPATH=. python3 -m pytest tests/ -v

# 启动/停止/查看 launchd 定时任务
launchctl load ~/Library/LaunchAgents/com.quant.analysis.plist    # 启用 (每天 8:00 + 16:00)
launchctl unload ~/Library/LaunchAgents/com.quant.analysis.plist  # 停用
launchctl load ~/Library/LaunchAgents/com.quant.server.plist      # Web 服务自启动 (KeepAlive)

# 查看日志
tail -f logs/quant.log                   # 应用日志 (滚动 10MB*5)
tail -f auto_run.log                     # launchd stdout
tail -f auto_run.err                     # launchd stderr
cat data/auto_status.json                # 最近一次定时分析的运行状态 + 告警
```

## Architecture

```
config/
  config.yaml          所有配置（数据源/因子/策略/回测/风控）
  loader.py            点号路径取值 get("backtest.commission")，惰性加载单例

data/
  store.py             DataStore — SQLite 核心（market.db: stocks/daily/meta 三表）
                        sync_stock_list (akshare→tushare 回退), update_daily (tushare 批量),
                        sync_fundamentals (腾讯财经 PE/PB/市值), get_benchmark (沪深300)
  fundamental.py       腾讯财经批量拉取基本面 → stocks 表 (60只/批, 3次重试)
  repository.py        StockRepo (合格股票筛选, 去ST/退市, min 250天历史)
                       FactorRepo (因子缓存读写, 自动分块避 SQLite 999参数限制)
                       PriceRepo (close/OHLCV 宽表查询)

factor/
  base.py              BaseFactor(ABC) + winsorize_mad + normalize_zscore 后处理管线
  technical.py         技术因子 — 5/10/20/60日窗口 × 7类 (动量/反转/波动/换手/均线偏离/量比/回撤)
  game_theory.py       博弈论因子 — 5/10/20/60日窗口 × 8类 (Amihud/PIN/羊群CSSD/价差HL/Kyleλ/信息到达/资金流/Nash偏离)
  fundamental.py       FundamentalCrossSection — 代理基本面 (规模/价值/质量/成长, 不需外部数据)
  real_fundamental.py  真实基本面 — 从 stocks 表读 PE/PB/市值/股息/现金流/52周位置
  demon.py             DemonSignals — 妖股检测 (量突/突破/异常波动/连涨/动量加权, 0-1分)
  alpha_factory.py     WorldQuant 风格自动因子生成 — 随机组合基础算子(rank/delta/ts_mean/…),
                       IC 筛选保留 |IC|>0.01, 取前 n_keep 个, 固定种子 RandomState(42) 可复现
  screening.py         向量化 IC 筛选 — 分块累加充分统计量(sum_x/sum_y/sum_xy/sum_x²/sum_y²/n),
                       合并后计算 Spearman Rank IC, 精确等价全量, 阈值 |mean_IC|≥0.01, |IC_IR|≥0.05
  cache.py             update_cache(store) — 因子缓存增量更新入口, 只算新日期, 无新数据跳过

strategy/
  ensemble.py          EnsembleModel — 5模型按 abs(验证集IC) 加权 (floor 0.03), scaler 先拆后归一化
  signals.py           generate_signals (top_pct 分位数) + generate_weights (equal/prediction)

backtest/
  metrics.py           compute_metrics — 夏普/年化收益/最大回撤/Calmar/胜率/盈亏比/Alpha/Beta/信息比率
  event_engine.py      EventDrivenBacktest — 事件驱动回测 (T+1/手续费/滑点/成交量约束/风控熔断),
                       当前未用于生产管线 (向量化回测更快更可靠)

engine/
  loader.py            → 加载 stock list + close_df (不加载因子)
  screener.py          → 前向收益计算 + 日期 7:3 拆分 + 分块 IC 筛选
  trainer.py           → 只加载 train_window(504天) 因子, 训练 EnsembleModel
  predictor.py         → 分批预测全量 (500只/批), 取每批最新日期切片
  ranker.py            → ML 预测 + demon 信号 5:5 混合 → 行业+市值中性化 (lstsq 残差)
  backtest_runner.py   → 向量化回测: 用预测排名选 top N 等权持有, pandas 秒出, 对比基准
  builder.py           → 组装最终结果 (推荐列表/指标/因子重要性/IC报告/行业分布)

web/
  app.py               Flask (0.0.0.0:8521), 路由见下方 API 表
  pipeline.py          RecommendationEngine — 串联 engine 7步管线
  db.py                results.db (runs + picks 表), save_result/get_history
  templates/index.html 单页 SPA (ECharts 5.5), 红涨绿跌 A股配色
```

## Data flow

```
auto_run.py (launchd 每天 8:00 + 16:00 触发)
  └─ RecommendationEngine.run()
       ├─ store.sync_stock_list()     # akshare 全A股 (tushare 免费版 1次/小时限制)
       ├─ store.update_daily()        # tushare 日线增量 (10只/批, 0.4s间隔)
       ├─ store.sync_fundamentals()   # 腾讯财经 PE/PB/市值 (60只/批)
       ├─ factor/cache.update_cache() # 增量计算因子 → factors_cache (SQLite)
       ├─ engine 7步管线:
       │    loader  → stocks + close_df
       │    screener→ IC筛选 + 日期拆分
       │    trainer → 504天窗口训练
       │    predictor→ 分批预测
       │    ranker  → demon混合 + 中性化
       │    backtest→ 向量化回测
       │    builder → 组装结果
       └─ save_result() → data/results.db
       └─ _write_status(alerts) → data/auto_status.json
```

## Web API routes

| 路由 | 方法 | 用途 |
|------|------|------|
| `/` | GET | 渲染 index.html |
| `/api/latest` | GET | 最新分析结果 (从 results.db) |
| `/api/run` | POST | 触发后台分析 (互斥锁防并发) |
| `/api/auto-status` | GET | 定时任务状态 (从 auto_status.json) |
| `/api/track` | GET | 推荐股票跟踪 (推荐价 vs 现价) |
| `/api/kline/<symbol>` | GET | 个股 120 日 OHLCV (K线图数据) |
| `/api/history` | GET | 最近 10 次运行历史 |

## Key design decisions

- **因子缓存**: 预计算入 SQLite (factors_cache 表), 增量只算新日期, YYYYMMDD 整数比较, 支持断点续算
- **内存控制三层**: (1) loader 不预加载因子 (2) IC 分块累加统计量 (3) 训练只加载 train_window 天 (~930MB→~230MB)
- **SQLite 自动分块**: FactorRepo.load_batch 超 900 参数自动拆批, 避免 SQLITE_MAX_VARIABLE_NUMBER
- **集成权重**: 按 abs(验证集 IC) 加权 (floor 0.03), 不因负 IC 排除模型 — 金融数据信噪比低, 偏负是常态
- **Scaler 先分后归一化**: train/val 拆分后再 fit_transform, 无数据泄露
- **前视偏差防护**: 重训练时 close_df.loc[:date] 只用到当前日期及以前的历史数据
- **向量化回测**: 用管线预计算预测排名, top N 等权持有, 纯 pandas 秒出结果。事件引擎 (EventDrivenBacktest) 保留但未用于生产
- **NULL 安全**: real_fundamental 全部字段用 `or 0` 防 None 崩溃
- **因子键格式**: (factor_name, stock_symbol) 双元素元组, stack/unstack 正确工作
- **异常处理**: 不允许静默吞异常, 至少 logger.warning; 热路径 (predictor batch) catch 后 continue 不中断全量
- **A股配色**: 红涨绿跌 (up=#ff6060, down=#38b860)
- **数据源优先级**: 股票列表 akshare (tushare 免费版限制太严) → 日线 tushare 批量 (10只/批) → 基本面 腾讯财经 (60只/批)
- **因子注册**: 无中心注册表, cache.py 中显式 import 各计算器并 concat 结果, 新增因子需在 cache.py 中挂载

## Config access pattern

```python
from config.loader import get as cfg
value = cfg("backtest.commission", 0.0003)  # 点号路径, 第二参数默认值
```

## Logging convention

```python
from utils.logger import get_logger
logger = get_logger("module.name")  # 自动在 quant. 命名空间下, 同时输出控制台(INFO+)和文件(DEBUG+)
```

## Debugging rules

- **零冗余铁律**: 创建任何代码必须全盘考虑项目——写之前确认"该不该存在"，写的同时明确"被谁调用、调用谁"，写完检查"有没有新冗余或死代码"。不写未接线的代码，不留未使用的 import。简洁、高效、专业。
- **10 分钟铁律**: 任何 bug 排查 10 分钟没出结果, 立即停手搜方案。不盲加日志, 不反复重启等待
- **搜索带链接**: 所有方案引用必须附来源 URL
