# Quant 项目第三轮全量代码审查报告

**审查日期**：2026-06-01  
**审查范围**：全项目 Python 文件 + YAML 配置
**审查人**：资深系统架构师 + 资深软件工程师 + 量化开发专家

---

## 一、已实现功能总览

| 层 | 模块 | 功能 | 状态 |
|---|------|------|------|
| **数据层** | store.py | 6数据源多源回退、单位标准化、缺口分析 | ✅ |
| | repository.py | StockRepo/FactorRepo/PriceRepo | ✅ |
| | fundamental.py | 腾讯财经PE/PB/市值同步 | ✅ |
| **因子层** | compute.py | 152+因子计算写入 factors 表 | ⚠️ 连接管理冗余 |
| | technical/game_theory/fundamental/demon | 手工因子 | ✅ |
| | alpha_factory.py | WorldQuant风格100→20 | ❌ locked_names 崩溃Bug |
| | screening.py | 分块IC筛选 | ⚠️ 分块排名偏差未验证 |
| **引擎层** | backtest_runner/rebalance/ranker | 回测+排名 | ⚠️ 涨停阈值未区分板块 |
| | trainer/predictor/builder | 训练+预测+组装 | ✅ |
| | sim_broker/tracker | 模拟交易+追踪 | ✅ |
| **策略层** | ensemble/signals/planner | 集成模型+信号 | ✅ |
| **执行层** | broker/order_manager/risk_checker | Mock券商+订单 | ⚠️ 佣金重复实现 |
| | monitor | 偏差监控 | ⚠️ 止损未实现 |
| **Web层** | app/pipeline/db | Flask 14 API | ⚠️ event模式无效 |
| **配置** | config.yaml | 配置驱动 | 🔴 Token泄露 + 权重过高 |

---

## 二、严重/高危 Bug 清单

### 🔴 P0 — 运行时崩溃

| # | 文件 | 问题 | 状态 |
|---|------|------|------|
| 1 | `alpha_factory.py:153` | **`locked_names` 非空时 `ys` NameError 崩溃**。`locked_names` 跳过IC筛选后没定义 `ys`，后续兜底分支引用 `ys.index` 直接抛异常 | ⬜ |
| 2 | `alpha_factory.py:187` | **`kept` 为空时 `pd.concat([])` 崩溃**。无因子通过筛选时生成空列表传给 concat | ⬜ |
| 3 | `config/config.yaml:6` | **Tushare token 明文硬编码**。即使警告不要提交git，token仍直接暴露在配置文件中 | ⬜ |

### 🟠 P1 — 逻辑错误 / 数据库设计缺陷

| # | 文件 | 问题 | 状态 |
|---|------|------|------|
| 4 | `data/store.py:83` | **`_connect()` 缺少 `busy_timeout`**。WAL模式写者互斥，但连接秒抛锁。整个项目通过 `_connect()` 每次创建新连接写完后关闭，多操作顺序执行本不冲突，但缺少 busy_timeout 意味着任何并发写入（launchd重入/web并行触发）都立即失败 | ⬜ |
| 5 | `data/fundamental.py:25` | **`sync_all()` 绕过 `_connect()` 直接用 `sqlite3.connect()`**。缺少 busy_timeout、cache_size 等统一PRAGMA | ⬜ |
| 6 | `factor/compute.py:166-175` | **DELETE+to_sql 非原子**。append模式下 DELETE 隐式提交后 to_sql 若失败，数据永久丢失 | ⬜ |
| 7 | `engine/backtest_runner.py:14-26` | **涨停检测 9.5% 硬编码，未区分板块**。创业板/科创板/北交所涨幅限制不同，`limit_up_pattern.py` 已有 `_get_limit_pct` 但此处未复用 | ⬜ |
| 8 | `engine/rebalance.py:105` | **同上**。涨跌停过滤 9.5% 硬编码，跨板块误判 | ⬜ |
| 9 | `execution/broker.py:81+104` | **佣金公式硬编码**，未调用共享函数 `backtest.compute_commission()`。实盘执行与回测费用口径不一致 | ⬜ |
| 10 | `execution/risk_checker.py:30+57` | **同上**。`check_buy`/`check_sell` 内部重复实现佣金+印花税计算 | ⬜ |
| 11 | `execution/order_manager.py:62` | **买入资金分配 Bug**。`capital_per = cash / n_new` 在循环前算一次，后续买入消耗资金后 `capital_per` 不变，超支风险 | ⬜ |
| 12 | `web/pipeline.py:134-135` | **事件驱动 signal_fn 恒定**。忽略 `date` 参数返回固定权重，event模式完全等同于buy-and-hold | ⬜ |

### 🟡 P2 — 数据正确性 / 架构问题

| # | 文件 | 问题 | 状态 |
|---|------|------|------|
| 13 | `factor/alpha_factory.py:140` | **IC 计算用 Pearson 而非 Spearman**。与 `screening.py` 的 Rank IC 方法不一致，同一因子两种评估方式结果不同 | ⬜ |
| 14 | `factor/real_fundamental.py:60` | **`turnover_rate` 缺 NaN 防护**。使用 `or 0` 但对 `np.nan` 无效，而同一函数其他地方已用 `_val()` 正确防护 | ⬜ |
| 15 | `factor/screening.py:15` | **分块IC使用chunk内排名并非全局排名**。注释称N>200时偏差<5%，但未经验证 | ⬜ |
| 16 | `factor/compute.py` | **每批次 ~4 个独立连接**。get_daily读连接 + real_fundamental读连接 + lhb读连接 + 写连接，23批次 × 4 = ~92个顺序连接 | ⬜ |
| 17 | `execution/monitor.py:65` | **`check_position_limits()` 空实现**。止损检查完全未执行 | ⬜ |
| 18 | `config/config.yaml:39` | **`max_weight: 1.00` 允许5000元全仓单只**。配合 `max_drawdown: 0.80`，妖股策略遭遇跌停即本金腰斩 | ⬜ |
| 19 | `auto_run.py` | **缺少重入防护**。launchd触发间隔小于执行时间时，两个进程同时写 market.db | ⬜ |

---

## 三、数据库连接设计审查

### 当前架构

```
项目全局模式: 每次需要数据库时 → store._connect() → 用完 → conn.close()
```

| 操作 | 连接数 | 说明 |
|------|--------|------|
| sync_lhb_data | 2 (读max + 写) | 64K行INSERT |
| compute_factors | ~92 (23批×4) | get_daily读 + 2个因子读 + 写 |
| engine.run | ~20+ | loader/screener/trainer/predictor各开连接 |

### 核心问题

`_connect()` 缺少 `busy_timeout`。WAL 模式下第二个写者在第一个写者 commit 期间拿锁失败时，无 busy_timeout 的 SQLite 立即返回 `database is locked` 而非等待重试。单进程内顺序执行目前不触发此问题，但以下场景会：
- launchd 时间间隔 < 执行时间 → 两个 auto_run 同时跑
- Web `/api/run` 触发分析时与 auto_run 并发

### 建议

**最低代价修复**：`_connect()` 加一行 `PRAGMA busy_timeout=5000`

---

## 四、架构评分

**当前评分：B**

| 维度 | 评分 | 说明 |
|------|------|------|
| 分层架构 | A- | data→factor→engine→web 清晰 |
| 数据质量 | B+ | 5123只≥250天，单位统一(手/千元) |
| 连接管理 | C | 全局每次新开连接，无超时，无连接复用 |
| 佣金一致性 | C | 3处独立实现(backtest_runner用共享函数、broker硬编码、risk_checker硬编码) |
| 测试覆盖 | D | 43个测试，<10%覆盖率，引擎层零测试 |
| 安全 | D | token明文在config.yaml |

---

## 五、与上轮对比

| 上轮(06-01第二轮) | 本轮(06-01第三轮) |
|---|---|
| 30个Bug | 19个Bug(旧修+新发现) |
| akshare列序/腾讯日期/日志崩溃 | Alpha Factory locked_names崩溃/连接管理/佣金不一致/event模式无效 |
| 关注代码细节 | 关注系统级设计问题 |

---

## 六、修复优先级

**本周必须修（P0+P1）**：alpha_factory locked_names崩溃、busy_timeout、compute原子写入、fundamental走统一连接、佣金统一调用共享函数、order_manager资金分配、event模式修复、涨停阈值复用 `_get_limit_pct`

**本月修（P2）**：IC方法统一、turnover_rate NaN、分块IC验证、连接复用、止损实现、max_weight调低、重入防护
