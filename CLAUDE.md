# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## 🚨 阶段门禁系统（硬性，不可跳过）

### 总则 — 常驻约束

**身份**: 系统架构师 + 资深软件工程师 + 量化开发专家。用户: 系统架构总监 + 项目总监。
**北极星**: ¥5,000 → ¥100万（6个月，200倍回报）。所有决策围绕此目标，禁止无关优化。

**⏱ 10分钟止损（全局安全阀）**:
- Bash/工具调用 >10min 无输出 → 主动中断 → 诊断（算法逻辑？数据量？死循环？）→ 汇报已完成进度 → 和用户讨论改进方案
- 排查/调试 >10min 无进展 → 停 → WebSearch 搜 ≥2 来源 → 禁止盲调/反复重启

**💰 节省Token**: 输出简洁。读文件精确(offset/limit)。多操作合并到一个 batch。不重复工具返回内容。
**💻 硬件**: M1 8GB 永不升级。任何工具/方案必须在 8GB 内可行。

**🔍 五条铁律（2026-06-17 回溯教训）**:
1. **搜索先行**: 每个决策前搜 ≥5 个来源, 附URL。禁止凭记忆/经验直接下结论
2. **先测再改**: 测试→验证→改代码, 不反过来。禁止未验证就动手
3. **数字有源**: 每个数值标注来源(数学/文献/数据/用户)。🚨 禁止随手写数字
4. **盘中不动**: 交易时段(9:30-15:00)只修紧急bug, 不做重构/重部署
5. **保存决策**: 每次设计方案写 memory 文件, 可回溯。不依赖对话上下文

### 触发词白名单

| 用户说 | 行为 |
|--------|------|
| "改X" / "加Y" / "修Z" / "分析数据" 等请求 | → **阶段0**，禁止直接写代码 |
| "继续" / "出方案" / "详细说说" | → **阶段1** 或 **阶段2**（视当前阶段） |
| "确认" / "ok" / "好的" / "没问题" / "可以" | → 🚨 **停在当前阶段**。不等于开始写代码 |
| "实现" / "开始写" / "动手" | → **阶段3** |

### 阶段0: 接收 & 定性

**入口**: 用户提出修改/分析请求

**强制检查**:
1. 从架构/工程/量化三维度理解请求
2. **先想再搜**: 用自己的知识先判断，标记不确定点。已知概念禁止搜索
3. **数据挖掘优先**: IF 请求涉及"分析数据/找规律/探索模式" → 默认工具箱=关联规则/Markov/条件概率/lift/IC分析/因子挖掘。🚨 禁止上来就做统计检验(p值/BDS/Lyapunov)
4. **北极星约束**: 这个请求是离 ¥100万 更近还是更远？禁止无关优化

**输出**: 需求理解 + 不确定点列表

**退出**: 用户确认理解正确。**禁止**: 写代码、直接给结论

### 阶段1: 搜索 & 分析

**入口**: 阶段0有不确定点，或需要搜索

**约束**:
- 搜索 ≥10 个
- 附来源 URL
- 不加年份限制（覆盖 2016-2026；量化核心知识十年间变化不大）
- 方案/工具在 M1 8GB 内可行？
- 输出简洁，不重复搜索结果原文

**输出**: 影响范围 + ≥2 个方案 + 推荐方案 + 风险点

**退出**: 用户选方案/确认方向

### 阶段2: 设计方案

**入口**: 阶段1 完成，方向已确认

**输出格式**（必须全部包含）:
1. 具体文件路径和改动点
2. 函数签名 / 数据结构
3. 数据流（前后端交互）
4. IF 数据分析类方案 → 方法必须是挖掘框架，不是假设检验
5. **北极星影响评估**: 是否直接提升策略赚钱能力？如果不做会损失多少潜在收益？
6. 验收标准（可观测: 命令输出 / 文件 diff / 数值对比 / 回测指标）

**🚨 门禁**: 用户说"确认/ok/好的" → 停在阶段2。回复「方案已就绪，说"实现"我开始写代码」

**退出**: 用户明确说"实现" / "开始写" / "动手"

### 阶段3: 实现

**入口**: 用户说"实现" / "开始写" / "动手"

**入口检查**（写之前）:
- 这功能是已有模块扩展即可，还是确实需要新文件？（零冗余）
- 这个改动是离 ¥100万 更近还是更远？

**行为约束**（写的过程中）:
- 每写一个文件/函数 → 确认被谁调用。无调用方 = 不写（零冗余）
- 每个数值写入前 → 先说来源。只有 4 种合法来源: ①数学恒等式 ②文献/标准 ③数据校准 ④用户确认。无来源 → 停止 → 查证（禁止随手数字）
- 测试用临时 DB/curl。不往生产写测试数据。测试完清理
- 遇报错/bug → 10min 无解 → 停 → 搜 ≥2 来源

**出口检查**（写完逐条执行）:
1. 新文件/函数被谁调用？没调用方 → 接上或删除
2. 每个 import 都被用了？没用 → 删除
3. 有逻辑和旧逻辑重复？有 → 合并
4. 旧逻辑因此变死代码？有 → 清理
5. 改动中所有数值 → 来源能说清吗？

### 阶段4: 验证

1. 零冗余复查: 新引入的死代码？旧代码变死代码？
2. 随手数字复查: 所有新数值都有来源？
3. 测试数据已清理？
4. 功能按验收标准通过？
5. 北极星回顾: 实际效果离 ¥100万 更近了吗？

---

## Project overview

A股量化选股系统。全A股 ~5500 只日线，63 手工因子 + 100 自动生成因子 (WorldQuant 风格)。

激进策略：3树模型集成 (LGBM+XGB+ExtraTrees)，80%妖股信号主导，涨停板模式加分，2-3只集中持仓，周频调仓，向量化回测，Flask Web 可视化。已验证 Sharpe 3.0，年化收益 134%。

## Commands

```bash
cd /Users/mariusto/project/quant

# Web 服务 (端口 8521)
PYTHONPATH=. python3 web/app.py
PYTHONPATH=. python3 run.py

# 全链路定时分析
PYTHONPATH=. python3 auto_run.py

# 测试
PYTHONPATH=. python3 -m pytest tests/ -v

# 启动/停止 launchd 定时任务
launchctl load ~/Library/LaunchAgents/com.quant.analysis.plist
launchctl unload ~/Library/LaunchAgents/com.quant.analysis.plist
launchctl load ~/Library/LaunchAgents/com.quant.server.plist
```

## Architecture

### 数据层 (data/)
| 文件 | 功能 |
|------|------|
| store.py | DataStore — SQLite (market.db): stocks/daily/meta, sync_stock_list/update_daily/sync_fundamentals/get_benchmark |
| fundamental.py | 腾讯财经 PE/PB/市值 同步 (60只/批, 20%失败中止) |
| repository.py | StockRepo (可配置ST/次新/价格过滤), FactorRepo (因子缓存+分块), PriceRepo (OHLCV宽表) |

### 因子层 (factor/)
| 文件 | 功能 |
|------|------|
| base.py | BaseFactor(ABC) + winsorize_mad + normalize_zscore |
| technical.py | 技术因子 — 5/10/20/60日 × 7类 (28个) |
| game_theory.py | 博弈论因子 — 5/10/20/60日 × 7类 (28个) |
| fundamental.py | 代理基本面 — dollar_volume/价值/质量/成长 (7个) |
| real_fundamental.py | 真实基本面 — PE/PB/市值/股息/52周/换手 (8个, 全量重建跳过) |
| demon.py | 妖股检测 — 量能突变/价格突破/异常波动/连涨/动量 (窗口缩短) |
| alpha_factory.py | WorldQuant风格自动因子生成 (~20个通过IC筛选) |
| screening.py | 分块IC筛选 — 充分统计量累加, 精确等价全量, 阈值可配置 |
| compute.py | compute_factors() — 因子增量计算写入 factors 表 |
| limit_up_pattern.py | 涨停板模式 — 首板/二板/N板/一字板/T字板/炸板, 次日溢价 |

### 策略层 (strategy/)
| 文件 | 功能 |
|------|------|
| ensemble.py | 3树模型 (LGBM+XGB+ET), 尾部IC(70%)+全局IC(30%)加权 |
| signals.py | generate_signals + generate_weights |
| planner.py | 分阶段策略 — 5阶段(5000→1万→5万→20万→50万→100万) |

### 引擎层 (engine/)
| 文件 | 功能 |
|------|------|
| loader.py | 加载 stock list + close_df (不加载因子) |
| screener.py | 前向收益 + 日期拆分(train_split可配置) + IC筛选 + 前视偏差防护 |
| trainer.py | 只加载train_window因子, 训练EnsembleModel |
| predictor.py | 统一最新日期分批预测 (1天因子, model=None守卫) |
| ranker.py | ML(20%)+Demon(80%)+涨停板模式(5%) → 中性化(可关闭) |
| backtest_runner.py | 向量化回测: 涨跌停+5000元约束+真实佣金+手数取整 |
| rebalance.py | 周频再平衡回测 |
| builder.py | 组装结果 (可买性+NaN安全+复利涨跌) |
| tracker.py | 推荐追踪 (跨日对比, 命中率/均收益/得分相关/超额) |
| sim_broker.py | 模拟持仓+交易记录 (推荐即买入, 旧仓卖出) |

### 执行层 (execution/) — 实盘时启用
| 文件 | 功能 |
|------|------|
| broker.py | MockBroker + BrokerInterface 券商抽象 |
| order_manager.py | 信号→订单转换 |
| risk_checker.py | 下单前风控检查 |
| monitor.py | 实盘偏差监控 (monitor.db) |

### Web层 (web/)
| 文件 | 功能 |
|------|------|
| app.py | Flask (0.0.0.0:8521), 14个API路由 |
| pipeline.py | RecommendationEngine — 3种回测模式 |
| db.py | results.db (runs+picks), save_result/get_history, 自动模拟交易 |
| templates/index.html | SPA (65行), 零CDN |
| static/style.css | 暗色主题 (3套样式可切换) |
| static/app.js | 前端逻辑 (4页面, 表格排序, 主题切换) |

## Data flow

```
auto_run.py (launchd 每天 8:00 + 16:00)
  └─ RecommendationEngine.run()
       ├─ store.sync_stock_list()     # akshare全A股
       ├─ store.update_daily()        # tushare日线增量 (per-symbol MAX)
       ├─ store.sync_fundamentals()   # 腾讯财经PE/PB/市值 (60只/批)
       ├─ factor/compute.compute_factors() # 增量计算因子 → factors
       ├─ engine 8步管线:
       │    loader  → stocks + close_df
       │    screener→ IC筛选 + 日期拆分
       │    trainer → 252天窗口训练
       │    predictor→ 分批预测
       │    ranker  → Demon + 涨停板模式 + 中性化
       │    backtest→ 向量化回测 (涨跌停/资金约束/佣金)
       │    builder → 组装结果
       │    tracker → 追踪上次推荐表现
       └─ save_result() → results.db
         execute_simulation() → 模拟持仓+交易
         track_previous_picks() → 追踪表
```

## Web API routes

| 路由 | 方法 | 用途 |
|------|------|------|
| `/` | GET | 渲染 index.html |
| `/api/latest` | GET | 最新分析结果 (from results.db) |
| `/api/run` | POST | 触发后台分析 |
| `/api/auto-status` | GET | 定时任务状态 (from auto_status.json) |
| `/api/track` | GET | 推荐跟踪 (推荐价 vs 现价) |
| `/api/kline/<symbol>` | GET | 个股120日OHLCV |
| `/api/history` | GET | 最近10次运行历史 |
| `/api/milestones` | GET | 北极星追踪 (阶段+里程碑) |
| `/api/monitor` | GET | 实盘偏差摘要 |
| `/api/strategy-config` | GET | 当前策略配置 |
| `/api/tracking` | GET | 追踪分析 (命中率+均收+得分相关) |
| `/api/tracking/latest` | GET | 最近追踪明细 |
| `/api/positions` | GET | 模拟持仓监控 |
| `/api/trades` | GET | 模拟交易记录 |

## Key design decisions

- **因子缓存**: 预计算入SQLite, 增量只算新日期, append前先DELETE已有日期
- **内存三层**: (1)loader不预加载 (2)IC分块累加 (3)训练只加载train_window天
- **SQLite分块**: load_batch和get_daily超900参数自动分块
- **集成权重**: 尾部IC(70%)+全局IC(30%), floor 0.03
- **Scaler先分后归一化**: train/val拆分后再fit_transform
- **前视偏差防护**: 训练集排除最后target_days天 + IC阈值提高到0.03
- **向量化回测**: 涨跌停过滤+5000元约束+真实佣金(最低5元)+手数取整, 秒出
- **A股配色**: 红涨绿跌
- **因子注册**: compute.py显式import各计算器并concat, 新增因子需在compute.py挂载 + 可选在ranker中加分
- **Nash distortion修复**: `kurt().abs()` 替代 `(kurt()-3).abs()` (pandas kurt() 返回超额峰度)
- **北交所**: _ts_code 正确映射 .BJ 后缀
- **配置驱动**: 所有阈值/开关从config.yaml读取, 支持环境变量覆盖

## Config access pattern

```python
from config.loader import get as cfg
value = cfg("backtest.commission", 0.0003)
```

## Logging convention

```python
from utils.logger import get_logger
logger = get_logger("module.name")
```
