# Quant 项目全代码审查报告（2026-08-18）

- 范围: 全项目代码 ~50,000 行 / 182 个 Python 文件 / 5 大模块并行审查
- 审查方式: 5 个并行审查代理分模块深入 + 关键发现人工实锤验证
- 内容: 技术栈评估、功能盘点、业务闭环、架构、代码逻辑、算法、bug 清单（7 问）
- 结论: 架构分层与工程纪律属个人量化项目高水平；存在 1 个实盘资金安全级缺陷 + 6 个"修复了但没接线"的死路径
- 改进状态: 第 1 节"需改进"4 项已于归档当日完成 (test-v531, 全量测试 409 passed)，见第 8 节

---

## 1. 技术栈评估与改进建议

**现有技术栈**: Python 3.14 + SQLite(WAL) + DuckDB + pandas/numpy/scipy + LightGBM/XGBoost + hmmlearn + optuna + Flask + vnpy + akshare/tushare/baostock/pytdx + Prometheus。

**评估**:
- **合适**: 数据量级（6 年 A 股、~5000 只）下 SQLite+DuckDB 组合正确；LGB/XGB 对小样本截面训练足够；optuna 已用；5 万本金规模 vnpy+模拟双适配器合理。
- **需改进**:
  - **PIT 数据框架**: `get_financials` 用 `stat_date <= date(?, '-60 days')` 判定（store.py:2853），年报披露时滞最长 120 天 → 基本面因子回测存在最长 2 个月前视。这是当前回测可信度最大单点，应改用 pub_date/ann_date 维度 + 全量重物化。
  - **统一 REPLACE→ON CONFLICT**: 全库 5+ 处 `INSERT OR REPLACE` 造成列级静默清空（trade_repo:379、em_valuation:105、jq_valuation:220、store.py:393、stocks_snapshot）。
  - **日期/市场格式强约束**: B1 修复只落在 store.py，universe_repo.py:94 同构代码漏改（新股排除恒不生效）；market 字段中英文混存。应收敛进 `validate_date_format` 入口强制。
  - **可选增强**: polars 加速因子回放（当前 135s 全量测试可接受，非紧迫）；CI/CD + 类型标注覆盖；实验跟踪（MLflow）；实盘网关扩展（vnpy 白名单缺 ctp/xtp，broker_adapter.py:304）。

## 2. 已实现 vs 待实现功能

**已实现**: 104 因子物化缓存 + 表达式编译器；8 阶段因子评估（CPCV+DSR）；状态机（active/probation/evaluating/backtesting）；sleeve/ic_weighted/LGB/XGB/Ensemble alpha；multi_tf 确认；HMM regime；Nano/Micro/Small 三层优化（Kelly/HRP/成本带）；Almgren-Chriss 成本模型；TP1/ATR/trailing 止损；T+1/一字板校验；walk-forward 回测；manifest 声明式调度 + 晚间链；归因/告警/日报；web 面板；金丝雀/影子部署（**但未接线**）。

**关键缺口**（按优先级）:
1. **实盘止损真正走券商**（见 P0-1，功能"存在"但实盘路径直写模拟账本）
2. 因子 evaluating→active 晋升通道（phase5_monitor DSR 数据源不匹配，**自引入起从未生效**）
3. 晚间链失败自动恢复（子进程轮询/重试死代码 + 08:00 无因子物化兜底）
4. 盘中 VaR 用持仓市值权重（当前等权，monitor.py:190）
5. 换手率约束真实生效（rebalance.py 保底 1 手绕过预算）
6. 实时盘口数据驱动限价单（QUOTE_TTL 死配置，order_manager.py:45）

## 3. 业务逻辑清晰度与闭环

**清晰**: Grinold & Kahn 7 层架构落地明确，数据流单一方向（scheduler→pipeline→FactorStore→AlphaModel→neutralize→PortfolioConstructor→ExecutionModel），task_runs 单一真相源，config 单一真相源执行度高。

**闭环断裂点（6 处）**:
- 晚间链失败 → 次日 signals 无因子缓存失败，恢复依赖已失效的重试机制
- 对账 equity_cross 与"昨日快照"比较 → **每个有交易的交易日必报 break**（reconcile.py:114-127），告警疲劳
- 归因基准量纲错误（历史均值 vs 当日收益直接做 Brinson）→ 每日给虚假分解
- 回测止损状态每日重置 vs 实盘历史残留 → 行为方向相反（loop.py:604）
- 实盘止损"模拟成交" → 账本认为已清仓、券商实际仍持仓 → **可致券商持仓翻倍**
- 因子池不一致：phase8 D1 回测侧 backtesting 池 vs 实盘 using 池，恒 divergent

## 4. 架构优化与重构建议

**优化**:
1. **执行层职责分离**: engine.execute 应拆模拟/实盘双路径，实盘止损/风控必须经 BrokerAdapter（当前 broker_adapter 注入后从未被引用，ADR-036 未实现）
2. **双指标系统合并**: monitor/metrics.py 与 monitoring/prometheus.py 并存，后者多指标定义后从未 set
3. **死代码清理**: highfreq.py 全模块无调用方（6 引擎已补齐但无入口）、model_serving shadow 流量 `pass`、`_adjust_for_redundancy`（P3a 冗余降权从未生效）、piotroski aux 序错误等
4. **verify_strict 双路径校验从未真正生效**（preload_aux_data NameError 瘫痪 golden_test）→ 修好后才能防"修复但没接线"系统性复发

**重构**: alpha/model.py 的 sigmoid 单调变换是 no-op（对排名选股无效）；strategy/__init__.py:215 的 engine property 必崩 + 单位错乱（手数当市值）。

## 5. 代码逻辑错误（需重构/修复）

见第 7 节清单。重点：卖单 PnL 注释称 FIFO 实为全历史加权（engine.py:222）；回测 IR 公式用 beta 残差而非超额收益（loop.py:248）；attribution n_stocks 写入因子数而非股票数；win_rate 被买单稀释；probation IC 衰减浅拷贝复利化（**回测约 2 周内 probation 因子权重归零，与实盘单次减半背离**）。

## 6. 算法改进建议

1. **Kelly 已完全退化为 alpha 比例分配**: cov 从未沿 `_kelly_greedy→compute_lot_allocation→compute_kelly_fractions` 接线（portfolio.py:590），且 λ 校准网格恒选左边界 0.5（目标函数单调，portfolio.py:134-216）。两个"自适应"机制实际输出常数——要么打通 cov 链路，要么诚实删除改为 config 参数
2. **XGB 早停集与 OOS 评估集共用** → OOS IC 乐观偏倚（xgb_model.py:160-179）
3. **训练/推理 z-score 截面口径不一致**: 训练在全市场 rank，推理在候选子集 rank（qlib_model.py:280）
4. **LW π̂ 对 NaN 行系统性低估**（covariance.py:114），`covariance_subset` 整列剔除与 B6 pairwise 矛盾
5. **训练数据幸存者偏差**（退市股被剔除，qlib_model.py:722）
6. **phase7 训练窗口含未来因子池**（2020 年 fold 用 2023 年注册的因子）
7. **年化口径 244 vs 252 并存**（tear_sheet vs deflated_sharpe），ICIR 双重年化高估 phase4 毛 Sharpe

## 7. 全部 Bug 清单（按严重度，关键项已人工实锤）

### P0（资金安全/必崩）

| # | 位置 | 问题 |
|---|------|------|
| 1 | execution_model.py:123-137 → engine.py:158 | 实盘止损只写 sim_trades 不经券商，账本/券商错位可致持仓翻倍（**已实锤**） |
| 2 | alpha/model_serving.py:232 | `set_canary` 引用未定义 `version` → NameError，金丝雀 100% 崩 |
| 3 | alpha/strategy.py:258-280 | lgb/xgb 注册为 AlphaStrategy 但无 combine() → combine_mode="lgb/xgb" 必 AttributeError |
| 4 | strategy/__init__.py:215-218 | `config.capital.db_path` 字段不存在 → 访问必崩 |
| 5 | factor/store.py fundamentals | 市值/股本静态快照前视，污染 2020-2026 全部物化缓存 |
| 6 | backtest/loop.py:604-605 vs stop_loss.py:317 | 止损跨日状态回测每日重置、实盘历史残留，方向相反 |

### P1（数据/回测可信度/静默失真）

- data: trade_repo.py:379（REPLACE 清空风控参数）、:601（重买持仓成本漏算）、:697（exec notes 无策略过滤）；store.py:2853（财务 60 天前视）；universe_repo.py:94（新股排除恒失效）；duckdb_store.py:492（factor_registry 同步 0 行静默）；sina_financials.py:119（2019-2023 缺口永不补齐）；stocks_snapshot.py:71（market 中文混存）；store.py:393（退市股 REPLACE 清空财务列→幸存者偏差）
- factor: market_beta_60d 恒死（benchmark_ret 后注入）；insider_cluster 恒死（direction 值域不匹配）；analyst 快照 chunk 复用前视；preload_aux_data NameError（golden_test 瘫痪）；piotroski ASC 当 DESC
- pipeline.py:385: probation IC 衰减浅拷贝复利化（回测/实盘背离）
- scheduler: runners.py:407（子进程重试死代码）；attribution.py:134（Brinson 量纲错）；reconcile.py:114（每日必报 break）；crowdedness.py:47（连接泄漏+失败静默降级"无拥挤"）
- evaluation: phase5（晋升通道不可达）；phase8 D1（因子池不一致）；phase7（未来因子池）；phase4（硬编码+双重年化）
- execution: 止损单绕过一字板封板检查；engine.py:222（卖单 PnL 非 FIFO）；rebalance.py:146（封板卖单丢弃后现金为负）
- risk: neutralize.py:338（批量中性化失败静默直通，与 B32 政策冲突）；covariance.py:114（LW NaN 低估）、:226（整列剔除过激）
- alpha: kelly.py:180（B16 cov 未接线，Kelly 退化为 alpha 比例）；synth.py:273（常数因子 z-score 除零→全 NaN）；strategy.py:289（手数当市值传风控）
- monitor: report.py:117（total_return 不含未实现盈亏）；loop.py:248（IR 公式错）；notify.py:34（telegram 无 try/except）

### P2（约 40 项，择要）

hrp.py:145（corr>1→NaN→静默等权）；multi_tf.py（weekly_weight 未用 + 每 symbol 开连接）；rotation.py（宏观缺失 fail-open 恒判 recovery）；var.py:72（权重重归一化忽略现金）；rebalance.py:87（保底 1 手绕过换手约束）；qlib_model.py:565（Ensemble 缺列过滤）；snapshot.py:105（批量失败静默跳过）；weekly.py:260（uuid 当日历日期）；task_log.py:161（finish 不校验归属）；loader.py:63（YAML 非原子读）；macro.py:94（中文日期混存）；quality.py:71（自然日涨跌停，周一全漏）；config 244/252 双口径；prometheus 指标未 set；highfreq Kyle's Lambda 恒 1.0；vnpy 白名单缺 ctp/xtp；margin/limit_up NaN 脏行；analyst 跨年 ALTER 缺失；benchmark.py last_date 硬编码 2020-01-01。

---

## 总体结论

架构分层和工程纪律（config 单一真相源、task_runs、状态机）在个人量化项目中属高水平；但存在 6 个"修复了但没接线"的系统性死路径（Kelly cov、P3a 降权、lgb/xgb 注册、canary、shadow、verify_strict）和 1 个实盘资金安全级缺陷（止损不走券商）。

建议修复顺序: P0-1（实盘止损）→ 数据前视三件套（financials/市值快照/新股排除）→ 调度恢复闭环 → 晋升通道 → 归因/对账 → 死代码接线或清理。

- 归档时间: 2026-08-18
- 归档人: opencode review 会话（5 模块并行审查 + 人工验证）

---

## 8. 改进落实记录 (test-v531, 2026-08-18)

审查第 1 节"需改进"4 项全部完成：

1. **PIT 财务数据框架** ✅ — `get_financials` (store.py) 改双分支: 真实公告日行 `pub_date <= date`; sina 代填行 (占 85%, 无公告日) 用 `stat_date + 披露滞后`（年报 120 / 半年报 62 / 季报 45 天，证监会披露规则，入 config `data.financials.disclosure_lag_days`）。实测 2025-04-15 代填股回退 Q3、真实公告日股（招行 3-25 公告）可用 — 严格 PIT。
2. **REPLACE→ON CONFLICT 统一** ✅ — 5 处: trade_repo.set_initial_capital（PK strategy,mode 原子更新）、em_valuation / jq_valuation（两源共表不再互相清空列）、store.sync_delisted_stocks（退市股保留市值/PE/行业列）、fund_hold。
3. **日期/市场格式强约束** ✅ — universe_repo 新股排除 cutoff 8 位 vs 库内 10 位混比恒不命中 → 同格式；akshare 中文板块名与 tushare market 字段归一化 SH/SZ/BJ（原恒误标 SH）；存量 32 行中文 market 清洗为 0。
4. **vnpy 网关白名单** ✅ — `_VALID_GATEWAYS = {CtpGateway, XtpGateway}` connect() 校验。

**明确不做**（可选增强，非紧迫）: polars 加速（全量测试 135-148s 可接受）、CI/CD、MLflow 实验跟踪。
