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

**🔍 铁律（2026-06-17 回溯教训）**:
0. **禁止误报**: 分析前先验证，不确定就说不确定。禁止把if/elif分支计数当bug，禁止未测试就报问题。每个结论必须有数据支撑。
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

陈小群体系量化交易系统。A股 ~5400 只，新浪实时行情 + Sina/TickFlow 日线。弱转强/首阴反包/连板接力三种买点，三层卖出体系，Flask Web 可视化。¥5,000 起步，目标 ¥100万。

## Commands

```bash
cd /Users/mariusto/project/quant

# Web 服务 (端口从 config 读取, 默认 8521)
PYTHONPATH=. /opt/homebrew/bin/python3 web/app.py

# 月度板块扫描
PYTHONPATH=. python3 ops/sector_scan.py

# 复盘报告
PYTHONPATH=. python3 ops/review.py

# 启动/停止 launchd 定时任务
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.quant.server.plist
launchctl bootout gui/$(id -u)/com.quant.server
launchctl list | grep quant
```

## Architecture (22 files, ~4500 lines)

### 数据层 (data/)
| 文件 | 功能 |
|------|------|
| store.py | DataStore — market.db: stocks/daily, 多源日线(sina→tickflow) |
| repository.py | StockRepo (ST/次新/价格过滤) |
| schema.sql | 所有表结构归档 |

### 因子层 (factor/)
| 文件 | 功能 |
|------|------|
| market_mood.py | 情绪周期 — smooth_stage 3日平滑, 冰点/复苏/扩张/高潮/退潮 |

### 核心 (intraday_runner.py)
陈小群日内监控 — 6 模块 (A情绪/B板块龙头/C买点/D卖点/E执行/F复盘)

### 执行层 (execution/)
| 文件 | 功能 |
|------|------|
| quote.py | BoardTracker — 四种买点扫描+板块龙头+实时行情(Sina) |
| calendar.py | 交易日历 |
| live_broker.py | 实盘交易接口 (未启用) |
| monitor.py | 实盘偏差监控 (未启用) |
| risk_checker.py | 风控检查 |

### Web层 (web/)
| 文件 | 功能 |
|------|------|
| app.py | Flask (端口 config 读取), 8 个 API 路由 |
| db.py | results.db 共享连接 |
| shared.py | 线程安全内存状态 (替代 JSON IPC) |
| templates/index.html | SPA 前端 |
| static/style.css | 暗色主题 |
| static/app.js | 前端逻辑 |

### 运维 (ops/)
| 文件 | 功能 |
|------|------|
| review.py | 盘后复盘 — 信号统计+交易分析 |
| sector_scan.py | 月度板块热度扫描 |

### 其他
| 文件 | 功能 |
|------|------|
| backtest/__init__.py | compute_commission 佣金计算 |
| config/loader.py | YAML 配置加载 |
| utils/ | logger, date 工具 |

## Data flow

```
launchd (每天 8:45)
  └─ web/app.py
       ├─ web/shared.py 从 trades.db 初始化状态
       └─ intraday_runner.run()
            ├─ 日线同步 (每天一次, Sina→TickFlow)
            ├─ 情绪周期检测 (smooth_stage)
            ├─ 持仓恢复 + A1-A5 条件卖出
            ├─ while 盘中:
            │    ├─ tracker.update() — Sina 实时行情
            │    ├─ tracker.scan_all_modes() — 弱转强/首阴反包/连板接力/首板试探
            │    ├─ 产业链加分 + 板块龙头加分
            │    ├─ 买入逻辑 (score 排序, 全仓最优, 限3只)
            │    ├─ B1-B6 盘中风控 + 移动止盈
            │    └─ update_state() → 前端轮询
            └─ 复盘 (ops/review.py)
```

## Web API routes (active)

| 路由 | 用途 |
|------|------|
| `/` | 前端页面 |
| `/api/state` | 实时状态 (capital/positions/signals/mood) |
| `/api/mood` | 情绪周期 |
| `/api/signals` | 信号列表 |
| `/api/sectors` | 板块热度 |
| `/api/positions` | 持仓 (from trades.db) |
| `/api/trades` | 交易记录 |
| `/api/review` | 盘后复盘 |
| `/api/performance` | 绩效统计 (已实现+未实现盈亏) |

## Key design decisions

- **状态机**: 休市→盘前→盘中→午休→已收盘，每段显式 update_state
- **信号分数**: 0.50 基线 + 时间加分(sina 45K样本) + 龙头加分(模块B) + 产业链加分
- **陈小群卖点**: A1-A5 开盘 + B1-B6 盘中 (硬止损/炸板/烂板/死亡换手/缩量加速/移动止盈)
- **T+1 合规**: 今日买入盘中不卖
- **不补仓**: 陈小群"做错就割，绝不补仓摊成本"
- **持仓 ≤3 只**: config 读取
- **ST 过滤**: 买入前查名称
- **产业链映射**: industry_chains 表，6 链 54 条，中游+0.10/上下游+0.05
- **日线同步**: 每天一次，持久化防重启重跑，盘中跳过
- **动态数据源**: 速度排序，Sina→TickFlow
- **配置驱动**: 所有阈值从 config.yaml 读取
- **A股配色**: 红涨绿跌

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
