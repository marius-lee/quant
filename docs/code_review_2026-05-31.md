# Quant 项目全面代码审查报告

**审查范围**：52 个 Python 文件，418 行测试，1 个 YAML 配置
**审查日期**：2026-05-31
**审查人**：资深系统架构师

---

## 一、系统架构总评

### 当前架构

```
数据层 (data/)  →  因子层 (factor/)  →  策略层 (strategy/)  →  引擎层 (engine/)  →  Web层 (web/)
 SQLite           152+ 因子           集成模型             8步管线              Flask:8521
```

### 架构评分：**B+ (中上)**

**优点**：
- 分层清晰，单向依赖，模块职责分明
- 内存分层控制得当（三级：不预加载 → 分块 IC → 训练窗加载）
- 前视偏差防护意识好（训练集排除目标日 + IC 阈值提高）
- 配置驱动，所有阈值/开关可调

**缺点**：
- 回测、模拟交易、实盘执行三套资金/佣金模型口径不一致
- 引擎层 `backtest_runner` vs `rebalance` 功能重叠但逻辑不统一
- 数据库连接管理散落各处，DB_PATH 多处硬编码
- 缺少 CI/CD 和代码质量门禁（lint/type check）

---

## 二、已实现功能总结

| 模块 | 已实现 | 状态 |
|------|--------|------|
| **数据层** | SQLite 存储、tushare+akshare 双源自切换、股票列表/日线/基本面同步、龙虎榜数据 | ✅ 完成 |
| **因子层** | 技术因子 28 个、博弈论因子 28 个、代理基本面 7 个、真实基本面 8 个、妖股检测 5 类、自动因子生成 100 个(保留 20)、涨停板模式、龙虎榜因子、IC 筛选 | ✅ 完成 |
| **策略层** | 3 树集成(LGBM+XGB+ET)、尾部IC+全局IC 加权、信号生成、分阶段计划 | ✅ 完成 |
| **引擎层** | 8 步管线、IC 筛选、模型训练、批量预测、综合排名、向量化回测、周频再平衡、结果组装、推荐追踪 | ✅ 完成 |
| **执行层** | Mock 券商、订单管理、风控检查、偏差监控 | ⚠️ 仅 Mock |
| **Web 层** | Flask 14 API、SPA 前端、暗色主题、历史记录、K线图 | ✅ 完成 |
| **自动化** | launchd 定时任务、auto_run 全链路 | ✅ 完成 |
| **测试** | 418 行，覆盖 factor_base / screening / metrics / ensemble / signals / dates | ⚠️ 覆盖不足 |

---

## 三、严重 Bug 清单（必须修复）

### 🔴 P0 — 运行时崩溃

| # | 文件 | 行号 | 问题 | 状态 |
|---|------|------|------|------|
| 1 | `backtest_runner.py` | 66 | `affordable_filter()` 传入 4 个参数，函数只接受 3 个 → **TypeError 崩溃** | ✅ |
| 2 | `risk_checker.py` | 48 | `positions[symbol] < shares` — dict 与 int 比较 → **TypeError 崩溃** | ✅ |
| 3 | `limit_up_pattern.py` | 95 | `patterns.index.get_level_values(0)` 但索引是普通 DatetimeIndex → **AttributeError 崩溃** | ✅ |
| 4 | `data/store.py` | 17-22 | `_ts_code` 中 "92" 北交所股票被 "9" 先捕获 → **北交所数据全部缺失** | ✅ |

### 🟠 P1 — 逻辑错误（结果不可靠）

| # | 文件 | 行号 | 问题 | 状态 |
|---|------|------|------|------|
| 5 | `factor/cache.py` | 136-141 | 多 chunk 写入时后一批 DELETE 覆盖前一批数据 → **增量更新数据丢失** | ✅ |
| 6 | `factor/real_fundamental.py` | 59 | 当前 PE/PB 广播到所有历史日期 → **回测前视偏差，虚高收益** | ✅ |
| 7 | `factor/screening.py` | 15 | 分块模式下排名在 chunk 内而非全局 → **IC 计算在分块模式下不准确** | ✅ |
| 8 | `backtest_runner.py` | 110 | 手续费从每日市值扣减而非建仓日一次扣 → **收益率曲线整体被压低** | ✅ |
| 9 | `engine/sim_broker.py` | 71 | 卖出价 fallback 到成本价 → **交易盈亏记录失真** | ✅ |
| 10 | `execution/order_manager.py` | 58-60 | `capital_per = cash / (len(to_buy) + len(to_hold))` → **资金被已持有股票稀释** | ✅ |
| 11 | `data/repository.py` | 41-45 | `exclude_st=True, exclude_star_st=False` 时 *ST 仍被排除 → **过滤逻辑错误** | ✅ |
| 12 | `factor/limit_up_pattern.py` | 22 | 涨停阈值 9.5% 不区分板块(主板10%/创业板20%/科创板20%/北交所30%) → **跨板块误判** | ✅ |
| 13 | `factor/game_theory.py` | 15-21 | 缺失 OHLCV 时用 close 伪造数据 → **静默数据污染** | ✅ |
| 14 | `factor/alpha_factory.py` | 120-121 | 混合 IC 而非每日截面 Rank IC → **IC 高估** | ✅ |

### 🟡 P2 — 口径不一致 / 功能缺失

| # | 文件 | 问题 | 状态 |
|---|------|------|------|
| 15 | `execution/broker.py:75` | `get_price` 从不更新价格，永远返回买入价 | ✅ |
| 16 | `strategy/signals.py:29-47` | `risk_parity` 方法静默返回零权重 | ✅ |
| 17 | `web/db.py:66-71` | 模拟交易异常被 `pass` 完全吞没 | ✅ |
| 18 | `backtest/event_engine.py:87-89` | 卖出受成交量限制 | ✅ |
| 19 | 跨 5 模块 | 佣金模型不统一（最低 5 元/滑点处理方式/费率来源） | ✅ |
| 20 | `engine/rebalance.py:101` | 再平衡使用 pipeline 启动时的静态预测而非最新因子 | ✅ |
| 21 | `engine/tracker.py:160` | 基准收益使用等权复利而非沪深 300 | ✅ |

### 🟢 P3 — 代码质量 / 死代码

| # | 文件 | 问题 | 状态 |
|---|------|------|------|
| 22 | `data/repository.py:93-100` | `get_industry_mv()` 死代码（industry 列从未创建） | ✅ |
| 23 | `execution/broker.py:133-148` | xtquant/easytrader 声称支持但从未实例化 | ✅ |
| 24 | `strategy/ensemble.py:141-142` | `coef_` 分支在树模型下永远不执行 | ✅ |
| 25 | `strategy/planner.py:67` | 默认日收益率 2% 极不现实 | ✅ |
| 26 | `utils/dates.py:12` | `str | None` 语法在 Python <3.10 崩溃 | ✅ |
| 27 | `factor/limit_up_pattern.py:48-69` | 双重 Python 循环性能极差（550 万次迭代） | ✅ |
| 28 | `data/store.py:201-206` | `turnover` 换手率永远为 0（硬编码） | ✅ |
| 29 | `data/repository.py:93-100` | `get_industry_mv` 死代码 | ✅ |
| 30 | `data/fundamental.py:18-22` | 东方财富回退路径从未实现 | ✅ |

---

## 修复完成总结

**修复日期**：2026-05-31
**修复人**：资深系统架构师 + 资深软件工程师
**总计**：30/30 全部修复 ✅

### 关键变更摘要

| 优先级 | 数量 | 修复类型 |
|--------|------|----------|
| 🔴 P0 | 4 | 运行时崩溃修复（参数数量、类型比较、索引访问、北交所映射） |
| 🟠 P1 | 10 | 逻辑正确性修复（缓存覆盖、前视偏差、IC计算、资金分配、板块阈值等） |
| 🟡 P2 | 7 | 架构一致性修复（佣金统一、基准统一、异常处理、成交量约束等） |
| 🟢 P3 | 9 | 代码质量修复（向量化、类型注解、死代码标注、文档修正等） |

### 共享佣金函数

新增 `backtest/__init__.py` → `compute_commission()` 统一佣金模型，所有回测和模拟模块调用同一函数，确保绩效指标可比。

