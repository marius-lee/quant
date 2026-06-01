# Quant 项目第二轮全量代码审查报告

**审查日期**：2026-06-01  
**审查范围**：52 个 Python 文件，4 个审查子代理并行  
**审查人**：资深系统架构师 + 资深软件工程师

---

## 一、已实现功能总览

| 层 | 模块 | 功能 | 状态 |
|---|------|------|------|
| **数据层** | store.py | 6 数据源(zzshare/TickFlow/tushare/腾讯/akshare/Baostock)，多源回退，缺口分析，单位标准化 | ✅ |
| | repository.py | StockRepo/FactorRepo/PriceRepo，ST过滤，价格/成交额过滤 | ✅ |
| | fundamental.py | 腾讯财经 PE/PB/市值同步 | ✅ |
| **因子层** | compute.py | 因子全量/增量计算写入 factors 表 | ✅ |
| | technical.py | 28 个技术因子(4窗口×7类) | ✅ |
| | game_theory.py | 28 个博弈论因子(4窗口×7类) | ⚠️ 有隐藏崩溃风险 |
| | real_fundamental.py | 8 个真实基本面因子(已修复前视偏差) | ✅ |
| | demon.py | 妖股信号检测(5类信号) | ✅ |
| | alpha_factory.py | WorldQuant 风格 100 候选→20 保留 | ⚠️ 随机种子问题 |
| | screening.py | 向量化分块 IC 筛选 | ⚠️ 分块排名偏差 |
| | limit_up_pattern.py | 涨停板模式识别(按板块区分阈值) | ✅ |
| | dragon_tiger.py | 龙虎榜因子 | ⚠️ compute.py 调用有日期错位 |
| **引擎层** | backtest_runner.py | 向量化回测(佣金统一、参数修复) | ✅ |
| | rebalance.py | 周频/月频再平衡回测 | ⚠️ 多处逻辑问题 |
| | screener.py | 前向收益+IC筛选+前视防护 | ✅ |
| | trainer.py | 模型训练 | ✅ |
| | predictor.py | 分批预测 | ✅ |
| | ranker.py | Demon+涨停板+中性化排名 | ✅ |
| | builder.py | 结果组装 | ✅ |
| | tracker.py | 推荐追踪(命中率/平均收益/得分相关) | ✅ |
| | sim_broker.py | 模拟持仓+交易记录 | ✅ |
| **策略层** | ensemble.py | LightGBM+XGBoost+ET 集成 | ✅ |
| | signals.py | 信号生成(risk_parity已抛异常) | ✅ |
| | planner.py | 分阶段策略 | ✅ |
| **执行层** | broker.py | MockBroker + 工厂函数 | ⚠️ cash初始化不一致 |
| | order_manager.py | 信号→订单转换 | ❌ 资金分配有bug |
| | risk_checker.py | 风控检查(dict比较已修复) | ⚠️ 持仓上限检查缺陷 |
| | monitor.py | 实盘偏差监控 | ❌ 字段缺失 |
| **Web层** | app.py/pipeline.py/db.py | Flask 14 API | ⚠️ 默认值不一致 |
| **回测层** | metrics.py/event_engine.py | 绩效指标+事件引擎 | ✅ |
| **工具层** | dates.py/logger.py | 日期+日志 | ✅ |
| **配置** | config.yaml/loader.py | 配置驱动 | ⚠️ 两个值不合理 |
| **测试** | tests/ | 43 个测试 | ⚠️ 覆盖 <10% |

---

## 二、新发现的严重 Bug（本轮审查新增）

### P0 — 运行时崩溃 / 数据不可用

| # | 文件 | 行号 | 问题 | 状态 |
|---|------|------|------|------|
| 1 | `data/store.py` | 239-268 | **`_fetch_akshare_daily` OHLC 列顺序错乱**：传入 `_norm_row(开盘,收盘,最高,最低)` 但函数签名是 `(o,h,l,c)`。实际写入 high=收盘价, low=最高价, close=最低价。所有 akshare 来源数据不可用 | ✅ |
| 2 | `data/store.py` | 547-548 | **日志引用未定义变量** `zzshare_ok/tushare_ok/tencent_ok/akshare_ok` → `NameError` 崩溃 | ✅ |
| 3 | `data/store.py` | 222-223 | **`_fetch_tencent_daily` 日期比较失效**：`d < start_date` 因 `-` ASCII 45 < `0` ASCII 48 始终为 True。腾讯源从不产生数据 | ✅ |
| 4 | `factor/game_theory.py` | 21 | **`self.logger` 未初始化**：任何调用方不提供 OHLCV fallback 即触发 `AttributeError` 崩溃 | ✅ |
| 5 | `execution/order_manager.py` | 31→60 | **买入预算使用卖出前的旧 cash**：卖出后 broker 现金已增加，但 `capital_per` 仍用旧值，换仓时可能资金不足 | ✅ |
| 6 | `execution/monitor.py` | 75-83 | **`update_daily_pnl()` INSERT 缺少 `expected_return` 和 `actual_return` 列** | ✅ |

### P1 — 逻辑错误（回测结果不可靠）

| # | 文件 | 行号 | 问题 | 状态 |
|---|------|------|------|------|
| 7 | `config/config.yaml` | 58 | **`min_daily_amount: 100_000` 单位是千元 → 实际门槛 1 亿元**，过滤掉大多数妖股 | ✅ |
| 8 | `config/config.yaml` | 37/39 | **`max_stock_price: 50` + `max_weight: 0.5` 冲突**：2500 元头寸 < 5000 元一手*50 元股票 | ✅ |
| 9 | `data/repository.py` | 多处 | **空 symbols 列表 → `IN ()` SQL 语法错误** | ✅ |
| 10 | `factor/compute.py` | 108 | **龙虎榜因子日期错位**：`start_date` 快照广播到全部日期行，LHB 回测值全部错误 | ✅ |
| 11 | `factor/compute.py` | 142-148 | **DELETE+INSERT 非原子**：中间崩溃导致数据永久丢失 | ✅ |
| 12 | `engine/rebalance.py` | 128 | **等权目标计算错误**：`target_value = cash / n_new` 忽略已有持仓市值 | ✅ |
| 13 | `engine/rebalance.py` | 多处 | **佣金模型未使用共享函数**：无数低5元限制，无卖出千一印花税 | ✅ |
| 14 | `engine/rebalance.py` | 101 | **前视偏差**：一次性 `pred_series` 跨所有调仓日使用 | ✅ |
| 15 | `factor/alpha_factory.py` | 53-54 | **`random.choice` 代替 `rng.choice`**：因子生成不可复现 | ✅ |
| 16 | `factor/screening.py` | 15 | **分块排名非全局**：chunk 内排名 ≠ 全市场排名，IC 在分块模式下不准确 | ✅ |

### P2 — 口径不一致 / 边缘问题

| # | 文件 | 问题 | 状态 |
|---|------|------|------|
| 17 | `factor/real_fundamental.py` | `or 0` 对 np.nan 值无效，NaN 向下游传播 | ✅ |
| 18 | `engine/rebalance.py` | 无涨跌停过滤 | ✅ |
| 19 | `engine/sim_broker.py` | 佣金计算未使用共享函数，硬编码费率 | ✅ |
| 20 | `execution/risk_checker.py` | `check_buy` 中 `len(positions)` 使用本地副本，多次买入时可突破 max_positions | ✅ |
| 21 | `execution/broker.py` | 工厂函数 cash 初始化路径不一致(config vs 硬编码) | ✅ |
| 22 | `web/app.py` vs `planner.py` | 日收益率默认值不一致(0.02 vs 0.005) | ✅ |
| 23 | `web/db.py` | `save_result()` 无 `recommendations` key 守卫 | ✅ |
| 24 | `backtest/event_engine.py` | `_liquidate` 未取消 pending_buys | ✅ |

### P3 — 代码质量 / 技术债

| # | 文件 | 问题 | 状态 |
|---|------|------|------|
| 25 | `factor/alpha_factory.py` | IC 是 Pearson 非 Spearman，注释误导 | ✅ |
| 26 | `factor/screening.py` | _merge_stats 取交集丢失独有日期信息 | ✅ |
| 27 | `engine/ranker.py` | Demon 可能含前视偏差（detect 使用全序列） | ✅ |
| 28 | `engine/tracker.py` | `_get_benchmark_return` 在 pick 循环内重复调用 | ✅ |
| 29 | `strategy/ensemble.py` | `coef_` 分支死代码 | ✅ |
| 30 | `data/store.py` | `_fetch_tickflow_daily` 忽略 start_date 参数 | ✅ |

---

## 三、架构评估

### 当前架构（修复后）
```
数据层 → 因子层 → 策略层 → 引擎层 → Web层
 6源      152+       集成      8步       Flask
```

**评分: B+** — 分层清晰，多源回退完善，数据完整性 92.7%。

### 核心风险

1. **rebalance.py 是最大盲区** — 等权目标计算 + 佣金 + 前视偏差，三项 P1 bug 叠加，再平衡回测数字基本不能信任
2. **akshare 数据不可用** — OHLC 列顺序错乱（已写入的 akshare 数据全部不可用）
3. **腾讯财经从未产生数据** — 日期比较 bug 导致腾讯源静默跳过
4. **测试覆盖 <10%** — 引擎层零测试，无法自动捕获回归

### 数据质量最终评分

| 维度 | 评分 | 说明 |
|------|------|------|
| 数据完整性 | A- | 5123 只 ≥250天 (92.7%) |
| 数据正确性 | B+ | 量纲统一(手/千元)，189万条已修正 |
| akshare 数据 | D | OHLC 列序错乱，需重拉 |
| 腾讯数据 | F | 日期比较bug从未写入 |

---

## 四、与上次审查对比

| 上次审查(05-31) | 本次(06-01) |
|-----------------|------------|
| 31 个 Bug | 30 个 Bug（旧修31，新发现30） |
| P0: 4个 | P0: 6个(最后3个从未暴露的新bug) |
| 主要问题: 参数/类型/命名 | 主要问题: 数据流正确性/回测逻辑 |

优化明显但新问题被审查挖掘出来了——多源数据写入的边界条件（aksare列序、腾讯日期比较）、引擎层的资金分配逻辑在之前审查中被遗漏。
