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

## 9. 关键缺口 2-6 修复记录 (test-v532, 2026-08-18)

审查第 2 节"关键缺口"之 2-6 全部完成（第 1 项实盘止损走券商为资金安全级改造，留待实盘前专项实施）：

2. **晋升通道生效** ✅ — phase5 DSR 数据源 live→backtest：evaluating 因子未实盘，live IC 缺 58/94 → DSR 恒 None → 自引入起从未生效。backtest 6 年 IC 全覆盖，通道打通。
3. **晚间链失败自动恢复** ✅ — `_wait_subprocess` 重试死代码（计数不重跑 + 失败当成功）→ 预算内真正 respawn，orchestrator 按返回值决策；08:00 `_ensure_factor_cache` 增量物化兜底（原只修数据表，factor_cache 缺口无人接管）。
4. **VaR 持仓市值权重** ✅ — `sum(1 for ...)` 等权计数 → shares×现价市值权重。
5. **换手率约束真实生效** ✅ — 缩放后 |d|<0.5 手保底 1 手（绕过预算）→ 丢弃，与 alpha 分支同语义。
6. **QUOTE_TTL 落地** ✅ — 死配置 + `_chase` 无调用方 → 超 TTL 且 gap≤urgency 时追价 ask×(1-discount)，上限 MAX_CHASE=3（config）。

验证: 新增 test_v532_gaps_fix.py 6 项；全量 415 passed。

## 10. 闭环断裂点 2-6 修复记录 (test-v533, 2026-08-18)

审查第 3 节"闭环断裂点"6 处逐一闭环:

1. **晚间链失败 → signals 无因子缓存** ✅ — 实为 v532 修复: 08:00 daily_repair `_ensure_factor_cache` 增量物化兜底 + orchestrator 按返回值窗口内重试; v533 验证 signals 侧无残留缺口, 无需再改。
2. **equity_cross 每日必报 break** ✅ — 原比较"最近一条"快照 (= 昨日, reconcile 15:05 早于当日 equity 写入) → 有交易即 break; 改当日快照 `WHERE date=?`, 当日无快照 → skip。跨日漂移移交 daily_equity 曲线 + alerts Rule 1。
3. **Brinson 基准量纲错配** ✅ — 原 sector_returns 历史日均收益 (60 日均 vs 组合当日收益, 量纲不一致 → 每日虚假分解); 改最近交易日当日收益 (成分等权, 与 pos_returns 同日口径)。抽纯函数 `_sector_returns_from_df` 可测。
4. **回测止损每日重置 vs 实盘历史残留** ✅ — 根因: 回测 `_risk_manager` 每 run 新建实例 (meta 空 dict, 峰值每日归成本) vs 实盘 MAX 聚合 (清仓重买旧峰值残留 → trailing 立即触发 / TP1 失效), 行为方向相反。统一 "持仓周期跨日保留、清仓重买重置": 回测注入共享 `_rm`; 实盘 `_is_recently_rebought` (最近卖出早于本仓买入 → 跳过回载)。
5. **实盘止损"模拟成交"→ 券商持仓翻倍 (P0-1)** ✅ — 原 4 处止损直写 engine.execute (sim_trades), 账本清、券商留 → 翻倍; 新增 `_execute_stop_orders` 统一走 broker_adapter: 未连接 → RuntimeError 零 fallback (宁可留仓不双账); LiveExecutionModel.execute_sells 同样收紧。
6. **phase8 D1 因子池恒 divergent** ✅ — 回放 `status_filter="backtesting"` → `"using"` (与实盘 active+probation 同池), D1 匹配率恢复信息量。

验证: 新增 test_v533_closed_loop.py 7 项 (当日快照/当日基准/重买重置/止损三态/P0-1 三态/using 池); test_v532 追价测试钉死时钟 (时序敏感); 全量 422 passed。

注意: (a) 集成回测回归暂被因子缓存 data_hash 指纹失效阻断 (2023-01 起 ~10 因子需重算, v492 机制正常行为) — `bash scripts/materialize_full.sh` 后补跑; (b) P0-1 的 RuntimeError 属预期: 券商掉线时止损拒绝模拟, orchestrator 需人工恢复连接。

# 第 11 节: v534 优化/重构清单

## 优化1: engine.execute 模拟/实盘双路径 (ADR-036 落实)

原状态: v533 的 `_execute_stop_orders` 自管 broker_adapter — 券商成交成功后**漏写账本** (账本仍持仓 → 次日重复止损, 反向双账); 未连接时回退模拟执行 (v418 批判).

修复: `ExecutionEngine.execute()` 双路径 (仅 sell 单):
- adapter=None 或 SimulatedAdapter → 纯账本 (回测/模拟)
- 真实券商已连接 → `adapter.sell` 先成交, 成功后才写账本 (账本唯一真相源, 原子同步)
- 真实券商未连接 → RuntimeError 零 fallback (宁留仓不双账, 券商清账本留 = 事后不可逆)

收敛点: `execution_model._execute_stop_orders` / `LiveExecutionModel.execute_sells` / `monitor._execute_sell` 三处全部回归 `engine.execute` — adapter 逻辑不再散落 4 层, 双路径单点实现单点测试. buy 恒纯账本 (实盘买入由 OrderManager 限价流成交).

## 优化2: monitor/metrics.py ↔ monitoring/prometheus.py 双指标系统合并

僵尸指标 (19 个, 无人 set): TRADES_TOTAL/TRADE_VOLUME/TRADE_PNL/FACTOR_IC/FACTOR_ICIR/FACTOR_RANK/ALPHA_SCORE/VAR_95/VAR_99/MAX_DRAWDOWN/LEVERAGE/TURNOVER/CONCENTRATION/DATA_STALENESS/QUEUE_SIZE/TASK_DURATION/TASK_STATUS/SCHEDULER_POLL/BACKTEST_RUNS — 全部删除.

误删纠正: BACKTEST_SHARPE/CAGR/MAX_DD/DSR 曾被一并删除 — `_collect_backtest_metrics` 是活代码会 set → 恢复.

活指标保留 (10): POSITION_VALUE/CASH_BALANCE/TOTAL_EQUITY/DRAWDOWN/DATA_FRESHNESS/DATA_ROWS/CPU_USAGE/MEMORY_USAGE/DISK_USAGE/DB_CONNECTIONS.

其余: monitor_latency/monitor_count/monitor_gauge 装饰器无调用方 → 删; MetricType HISTOGRAM/SUMMARY → 删; `_collect_business_metrics` 新增本地指标动态导出 `quant_local_<name>` (GAUGE set 绝对值); AlertRuleBuilder 规则 + Grafana 3 面板引用同步.

## 优化3: 死代码清理

- `quant/execution/highfreq.py` (905 行): 全项目零调用方 → 删除
- `quant/alpha/model_serving.py` (439 行): 零调用方, ShadowDeploymentManager 246 行全 pass → 删除
- `alpha/model.py::_adjust_for_redundancy` (P3a): 定义于 2026-07 但 combine/combine_regime 均未接线, 从未生效 → 删除 + config `redundancy_corr_threshold` 一并移除
- **piotroski aux 序错误 (missing.py)**: `_preload.py` 以 `ORDER BY stat_date` 升序装载 aux, 而 `_last_two` 假设 DESC (rows[0]=最新期, DB 路径同款) → aux 单日路径 cur/prv 互换 → F-score 用错期. 修复: aux 分支构造后按 stat_date 降序排序, 与 DB 路径语义对齐. 测试: 升序/降序装载结果恒等 (修复前必不等)

## 优化4: verify_strict 双路径 NameError 瘫痪修复

根因: `_preload.py:146` 单日版 `preload_aux_data` 财务段误用 chunk 版变量 `date_from` (函数签名只有 `date`) → **单日 aux 预载必崩 NameError**. 影响: golden_test verify_strict 双路径校验从未真实执行; 物化走 chunk 版 (store 注入) 故掩蔽至今.

修复: `date_from`→`date`, `date_to`→`date`. 实测 verify_strict 0 mismatches (5 采样日期全通过).

## 重构1: alpha/model.py rank sigmoid 单调变换

`α' = α/(1+exp(-k(α-t)))` 是单调变换 — 入选集合只由序决定 (top_fraction 截断 + alpha 边际成本裁剪均按序), 任何单调变换不改变结果; `sigmoid_steepness=10` 无文献依据, 权重分配应按原始 alpha 相对差 (组合层 score_weighted). 删除变换 + config 项, rank 直接返回原分.

## 重构2: strategy/__init__.py engine property + 单位错乱

- engine property: `config.capital.db_path` — CapitalAllocation dataclass 无 db_path 字段 → AttributeError 必崩 → 改 TRADE_DB (路径由 config.paths 常量统一管理)
- check_risk_limits: 原 `lots × 100` 股数当市值传入 validate (单票占比/绝对限额全部失准) → 抽 `_position_market_value()` (价×手×100 = 市值元口径), update_positions 复用同源

## 验证

新增 test/test_v534.py 11 项:
- 双路径 6 态: 无 adapter 纯账本 / SimulatedAdapter 不双写 / 真实未连接 RuntimeError + 账本不写 / 已连接先券商后账本 / 券商失败 RuntimeError + 不写账本 / buy 恒纯账本
- preload_aux_data 单日无 NameError
- piotroski aux 升序/降序装载结果恒等
- strategy engine property 不崩 + 市值口径 (38.2×100 = 3820 元)
- alpha rank 恒等

test_v533_closed_loop.py 止损 2 测试更新为 v534 转发语义 (原断言 v533 中间态自管 adapter 行为).

全量: **433 passed (134s)**.

## 12. v535 审计 7 项修复 (2026-08-18)

### 优化1: Kelly 退化打通 + λ 校准删改 (kelly.py / portfolio.py)

**问题**: (a) covariance 从未穿透 _kelly_greedy → compute_lot_allocation → compute_kelly_fractions (portfolio.py:590 链), kelly 恒用常量 DEFAULT_RETURN_VAR; (b) calibrate_risk_aversion 在网格 [0.5,1,2,5,10] 上目标函数单调 → 恒选左边界 0.5, "自适应 λ" 为假。

**修复**: (a) _kelly_greedy 新增 covariance 参数并下传; compute_kelly_fractions 的 var 支持 DataFrame (reindex alpha.index 对齐) 与 ndarray, NaN 兜底 DEFAULT_RETURN_VAR; (b) 删 calibrate_risk_aversion/_CALIBRATION_GRID/_CALIBRATION_CACHE/_make_calibration_key, MV 层与 Kelly 层统一读 config `optimizer.risk_aversion: 2.0`。

### 优化2: XGB 早停集与 OOS 评估集分离 (ml_common.py / xgb_model.py)

**问题**: train() 的 eval_set=[(X_oos, y_oos)] — 早停集即 OOS IC 评估的同一段尾部数据 → 早停决策泄漏 → OOS IC 乐观偏差。

**修复**: split_train_oos 返回 (train, val, oos) 三元组 + val_frac; build_train_matrices 产出 X_va/y_va/val_dates + skipped["val"]; train() 用 eval_set=[(X_val, y_val)] (val_frac=config `alpha.val_frac: 0.10`)。

### 优化3: 训练/推理 z-score 截面口径统一 (qlib_model.py)

**问题**: 训练在全集 rank, predict 对候选子集 reindex 后 rank → 子集分布与训练分布错配。

**修复**: predict 先在全集 _sym_df rank 得 _z, 再 _z.reindex(symbols) 取子集。

### 优化4: LW π̂ NaN 系统性低估 (covariance.py)

**问题**: π̂ 原循环对含 NaN 的整行做 outer 差 → nan_to_num 置 0 → 低估; S 已 pairwise 而 π̂ 未对称。

**修复**: 逐对 (i,j) both-mask 有效样本累计, v=(d²).sum()/both.sum() 归一, 去掉 T 全局除法。

### 优化5: 训练幸存者偏差 — 退市名单 + PIT universe (store.py / qlib_model.py)

**问题**: (a) stocks.delist_date 全空 — sync_delisted_stocks 的 akshare fallback 返回中文列名, row.get("symbol") 恒 None → 367 条全写 symbol="000000" (INSERT OR IGNORE 只落 1 条); (b) build_forward_returns 全量 get_symbols — 训练起点后上市的股票混入早期标签。

**修复**: (a) 显式 _norm 列映射: SH 公司代码/公司简称/暂停上市日期, SZ 证券代码/证券简称/终止上市日期 (SH 终止列全 NaN); 重跑 367 只入库 (delist_date 非空 0 → 361); (b) get_symbols(start_date=训练起点) — PIT asof 仅含"当时已上市且未退市"。退市股 daily 历史由后续 update_daily 自动补拉。

### 优化6: phase7 因子池时间对齐 (factor_repo / _registry / phase2_single / phase7_wf)

**问题**: screen_factors 用全注册表 (status_filter="backtesting"), 2020 fold 使用 2023 注册的因子 — 未来因子池泄漏进训练窗口。

**修复**: factor_repo.get_factors_by_status 加 registered_before (created_at <= datetime(?)); _registry.load_active_*_factors 透传; get_factor_names(registered_before=); screen_factors(registered_before=train_end); phase7 注入 train_end。

### 优化7: 年化口径统一 (tear_sheet / stats_cache / deflated_sharpe / phase4)

**问题**: tear_sheet 硬编码 np.sqrt(244) vs stats_cache 硬编码 sqrt(252/lookback) vs deflated_sharpe docstring "A股=252" — 三处口径分裂。

**修复**: 全部收敛到 config market.annual_trading_days (244); phase4 gross_sharpe 补注释固化口径 — oos_ir 日频, √breadth (√240≈15.5) 数值上≈√244, 已≈完整 GK99 ICIR_annual, 严禁再乘 √annual_days (双重年化高估 ×242)。

### 验证

新增 test/test_v535.py 8 项; 全量 450 passed (132s)。详细见 HANDOFF.md。

## 13. v536 未接入功能接线 (2026-08-18)

### 扫描方法

4 个并行 agent 全仓 48.5k 行扫描 (data/scheduler/config/utils、alpha/factor、risk/optimizer/execution、monitor/backtest/evaluation/regime/benchmark/web)，判定标准: 公共函数/类/方法在 quant/+web/+scripts/ 全仓零调用方 (排除测试/CLI/注册表/self 调用)，产出 40+ 函数级候选。

### 接入 7 项

1. **web /api/stress 悬空导入** (app.py): `quant.risk.stress_test` 模块 v438 已删 → 端点必 500 → 改 `var.stress_test(positions, weights=持仓市值)` (backtest/loop.py:893 同源活代码)
2. **告警闭环** (orchestrator.py): `push_alerts` 零调用 → 主循环每 60s `check_alerts(broker.get(), metrics.snapshot())` → `push_alerts` (SSE 横幅, 去重内置); 原仅 /api/health 被动计算
3. **Metrics.persist** (orchestrator.py): metrics.db 恒空表 → 主循环每 6h 落盘 (docstring 声称 "scheduler 每次循环调用" 从未实现)
4. **sector_exposure_check** (pipeline.py): constraints.py:255 完整实现零调用, `risk.max_sector_exposure: 0.35` 无消费端 → construct 后市值权重检查, 超限 log + broker 状态字段; Nano 层豁免 (单票集中是设计), 不阻断
5. **web /api/benchmark** (app.py): 裸 SQL → `benchmark/tracker.py get_tracking_summary` (累计曲线 + latest_rolling 完整实现原零消费方)
6. **update_daily_risk** (scheduler/attribution.py): docstring 声称 "Called from scheduler.attribution" 从未实现 → 晚间链末尾复用 engine2 持久化 daily_risk 表, 失败不阻断
7. **phase8 CLI** (phase8_live_consistency.py): 511 行实现无任何入口 → 追加 `python -m quant.evaluation.phase8_live_consistency`

### 不接入清单 (判定为被替代/门控/无价值)

- **被替代旧实现**: daily_sync.py (裸 config. 导入即崩, 被 table_registry 取代)、jq_valuation (被 em_valuation 取代)、factor/orchestrator.py (自述不参与 import 链)、store_metadata.py、alpha/registry.py (与 strategy.py 双注册表)、EnsembleAlphaModel/rolling_train_cv、IncrementalCovariance/sample_cov/style_neutralize、scheduler/monitor.py _engine_sell (v534 双路径取代)
- **配置门控**: Micro/Small 层 (nano_cap ¥10K 门槛, ¥5000 起步设计)、tc_band (Nano 豁免)、turnover 999、multi_tf false、intersection/strict_intersection (无路径选中)、vnpy 族 (adapter: simulated)
- **数据源失效/已知事项**: northbound (API 2024-08 失效)、news/macro/alternative 自动同步 (源不可用)、analyst/fund_hold/holder_trade (CLAUDE.md 已知覆盖机制豁免)
- **因子注册**: compute/price/ 13 个未注册因子 (布林带×3/北向/主力/幻方×5 等) — 因子池 104 已固化, 注册需 8 阶段评估属业务决策
- **工具便捷 API**: tear_sheet/parallel/BacktestEngine/calendar 5 函数/order_summary/clear_cooloff/shap_explain/feature_importance — 低价值或无消费端

### 验证

新增 test/test_v536.py 10 项; phase8 CLI 实跑; 全量 453 passed (136s, 连续 3 次稳定)。详见 HANDOFF.md。

## 第 14 节: v537 UI 全量展示接入 (2026-08-18)

### 范围

上轮扫描后, "功能已实现但界面未展示" 分两类, 本轮全部接入:

- **A 组 (端点已存在, 前端从不调用)**: /api/benchmark、daily_risk 表、/api/signals/quality、/api/backtest/history、/api/strategy/<name> + /action、/api/metrics、/api/monitoring/datasources、/api/health
- **B 组 (数据已产生, 无端点)**: evaluation_runs 历史、phase8 报告、sector_exposure_alert 字段

### 改动

| 端 | 内容 |
|----|------|
| 后端 | 新增 3 端点: /api/risk/history (daily_risk 表, 空表幂等建表兜底)、/api/evaluations (run_store.list_runs, ?phase=)、/api/phase8 (默认读最新报告 / ?rerun=1 重跑 validate_consistency); flask request 补全局导入 |
| Performance tab | 基准追踪: 累计收益/Alpha/滚动 Alpha-60d/IR-60d/Beta/up-down capture KPI + 策略 vs 沪深300 Plotly 双线; 每日 VaR/CVaR Plotly; 回测历史 runs 表 |
| Strategies tab | 信号质量 KPI (今日 vs 20d 历史); 每行操作按钮 (调仓/详情), .action-btn 样式 |
| Systems tab | metrics 快照表 (counters+gauges); 评估历史表; phase8 四维报告面板 + 重跑按钮; Prometheus 区 datasources 摘要 |
| 横幅 | sector_exposure_alert 字段并入告警横幅 (SSE + pollOverview 双入口, withSectorAlert) |

### 验证

- 端点冒烟: 13 端点全 200 (flask test client); /api/phase8 实返回 divergent 报告 (15 runs 历史)
- 语法: node --check app.js + ast.parse app.py 通过
- 全量 453 passed — **前置条件: 停 web 服务**。服务进程 (api_state→_check_timeouts 写 task_runs) 与 pytest 写 market.db 竞争 → 随机 `database is locked` (实测 8→4 failed; 单测全过; 内嵌 pytest.main 同序复刻 18 passed, 停服务后全量恢复 453 passed)。非代码缺陷, 测试后 `bash scripts/restart.sh` 恢复

## 第 15 节: v538 回测默认区间接入 config (2026-08-18)

### 背景

用户确认"全量回测应从 2020-01-01 起"。查证: config.yaml `backtest.default_start: '2020-01-01'` (与 factor_cache_start 同源, v473 约定) 但 loop.py full 模式 start=None → end-12mo, **配置从未被消费** (全仓 grep 零引用)。

### 改动

- loop.py full 分支: end=None → `_require_cfg('backtest.default_end')`; start=None → `_require_cfg('backtest.default_start')`; 删 end-12mo 旧逻辑; smoke 分支不变
- 调用方审计: phase6/phase7 fold/phase8 live/BacktestEngine 全部显式传参 — 仅裸调 run_backtest() 生效
- 新增 scripts/run_backtest_full.sh: config 默认区间/自定义区间/--smoke, 结果落 backtest_runs

### 验证

test_v538.py 4 项 (源码断言×2 / 配置一致性 start==factor_cache_start / FakeEngine 拦截默认解析 — patch 源模块因 loop.py 函数体内 import)。全量 453 passed (停服务前提, 服务 api_state→_check_timeouts 写 task_runs 与测试写 market.db 竞争, 非代码缺陷)。

## 第 16 节: v539 因子缓存 data_hash 整库指纹误伤 (2026-08-18)

### 背景

全量物化 (07:33) 后回测报 "factor cache missing for 239 IC lookback dates (2025-06-06..2026-06-01)"。

### 根因 (双因叠加)

1. **data_hash 整库指纹误伤 (v492 设计缺陷)**: `_get_existing_factors` 要求 meta.data_hash == 当前整库指纹 (daily COUNT/SUM/MAX + daily_valuation + 财务三表) — 晚间链拉新数据 → COUNT/MAX(date) 变 → **全部日期误判缺失** (每日必发)。实测 meta 3f5dc83 vs 当前 574fa3f。
2. **source_hash 失效 15/99 因子**: 凌晨物化用 v533 代码, 白天 v534 (16:26, piotroski aux 序) / v535 (17:58) 落地改因子代码 → piotroski_fscore/alpha002/alpha055/alpha033/ztd/short_interest 等 15 因子缓存值过时 — **机制正确, 必须重物化**。

### 修复

- store.py `_get_existing_factors`: 删除 data_hash 判定 (局部信任: 日期已物化 + source_hash 匹配即有效); 指纹保留写入 meta 作审计字段
- 回填/因子代码变更 → force 全量重物化 (scripts/materialize_full.sh, v529 语义)

### 验证

test_v539.py 4 项 (源码断言 data_hash 判定删除 / source_hash 判定保留 / 行为: 旧指纹+已物化日期有效 / stale source_hash 判缺失 / 未物化日期判缺失), 4 passed。

## 第 17 节: v540 materialize_full.sh 终点写死修复 (2026-08-19)

### 背景

用户重物化被拦截: "factor_cache: DuckDB daily 落后 (2026-08-18 < 2026-12-31)"。脚本终点写死 `2026-12-31` (ee59fea 8-05 引入, "一次管到年底"意图 — 数据永达不到 → 必拦; 首次创建 e67e8af2 时终点为当时数据日 2026-08-03)。

### 修复

物化终点 = SQLite daily MAX(date) 动态取值 (数据真相源); DuckDB 落后于终点时守卫仍提示先 sync (scripts/duckdb_sync_all.sh)。起点 2020-01-01 保持 (v473 约定勿改)。

## 第 18 节: v541 写死日期全仓排查 (2026-08-19)

### 范围

scripts/*.sh|py + quant/ + web/ 全仓扫描日期字面量/路径/端口。

### 修复 (数据依赖终点 → 动态)

| 位置 | 原写死 | 改为 |
|------|--------|------|
| scripts/backtest_full.sh | 2026-08-03 | SQLite daily MAX(date), 起点对齐 config |
| scripts/run_backtest.sh | 2026-07-31 | 同上 |
| scripts/run_backtests.sh | 2026-07-27 | 同上 |
| scripts/full_backtest.sh | 2026-07-27 | 同上 |
| scripts/diag_lgb.sh | 2026-07-28 | 同上 |
| scheduler/{signals,execute,reconcile,snapshot}.py | "2026-08-10" CLI 默认日 | today_str() |
| data/holder_trade.py CLI | 2026-07-01 | today_str() |
| data/margin.py CLI | 2026-07-03 | today_str() 90 天窗口 |
| scheduler/factor_cache.py docstring | 2026-08-03 | 动态语义注释 |

### 保留 (业务评估窗口, 加注释)

eval_standard.sh 2023-2025 (完整年度评估)、phase7_wf --end 2025-12-31、backtest_full.sh oos_start_date 2025-06-01 — 业务窗口非数据终点。

### 不动 (合理写死)

测试用例日期 (smoke_verify.sh 等)、docstring 示例、expr_compiler demo、jq_valuation TRIAL (死模块)、config.yaml port 8521 (配置源正确)。

### 验证

8 脚本 bash -n + 8 文件 ast.parse; test_v538/v539 8 passed。

## §19 v542: 恒空结果因子排除物化池 (fund_change/financial_anomaly)

**触发**: 用户贴物化日志 — 两因子自 2020-01-22 起 blocked (计算为空结果), 追问三连: 是否缺失数据 / 能否计算 / 不能则排除。

**定位结论 — 非表级缺失, 但特定日期区间确实算不出**:
| 检查 | 结果 |
|------|------|
| fund_hold 27 期 change_ratio 覆盖 | 每期 2127-3909 只非空 ✓ 表齐全 |
| 财务三表 stat_date 覆盖 | 65-86 期 (2007-12-31 起) ✓ 表齐全 |
| blocked.json 分布 | fund_change 245 天 (2020-12-31 起, 2021-01 连续段+分散); financial_anomaly 184 天 (2023-03-31 起) |
| 500 只完整复刻 (chunk aux + fundamentals panel + compute_all_factors 全链) | fund_change/financial_anomaly **EMPTY**; 同路径 accruals 非空 → 物化环境独立复现空结果 |

**结论**: blocked 机制判定正确 (05:40 force 全量重算两轮独立复现空结果); 非补数可解 (长期日期区间缺口) → 按用户指令排除物化池。

**修复 (排除仅作用物化池, registry 状态不变)**:
- config.yaml `factor.materialize_exclude: ['fund_change','financial_anomaly']` (注释含实证 + 恢复条件)
- `get_factor_names` 加 `exclude` 参数 (默认 None 零破坏); 两处调用点: materialize_full.sh (backtesting 池 93→91) + scheduler/factor_cache.py (晚间链并集; 顺带顶 import `_require_cfg` 消除 53 行 NameError 隐患, 删 83 行冗余局部 import)
- blocked.json 清理 429 条记录 (1478 → 1049 日期)
- 影响: FactorStore.load 对缺因子目录天然跳过 (零副作用); 恢复 = config 移除即自动回池
- 验证: test_v542.py 4 项 (config/backtesting 池排除/默认兼容/using 池) + v536/v538/v539 18 项, 22 passed

## §20 v543: compute_fund_change symbol key 恒空 bug 根因定位 (回滚 v542 排除)

**用户质疑**: v542 结论"补数解决不了"与"数据补齐自动恢复"自相矛盾 → 要求代码分析 + 冒烟测试找根因。

**定位过程 (三步证据链)**:
1. **物化结果文件实证**: seg_0_25.pkl (05:40 force 轮) — fund_change 18 交易日全部 EMPTY (results 覆盖 0), 同路径 accruals 正常 → 物化环境真空确认 (非数据缺失)
2. **中间量调试**: `scores` 的 key = iterrows **行号** (0,1,2…), 非 symbol → `reindex(symbols)` 全 NaN → fillna(0) 全 0 → `_cs_zscore` std=0 → 全 NaN → **恒空**
3. **git 历史确认**: cece5a6 (07-17 aux 重构) 把 SQL 直查 (`SELECT symbol, change_ratio` → `scores[sym]`, key=symbol ✓) 改成 `for sym, row in fh.iterrows(): scores[sym]=…` → **iterrows 的 index 是行号, symbol 是普通列 → key 全错** — fund_change 07-17 起任何日期恒空; parquet 2020 年 242 天覆盖 = 07-03~07-17 SQL 版产物; 07-17 后被重算的日期 → blocked 245 天 (2020-12-31 起) — 全时间线吻合

**修复**: `scores[row["symbol"]] = float(row["change_ratio"])` — 500 只复刻 @2020-01-02/01-22 500/500 非 NaN (修复前 0), 非 0 值 286 与表内 290 只有值吻合; zscore 符号与原始 change_ratio 一致。

**v542 整体回滚**: config materialize_exclude 移除 / get_factor_names exclude 参数移除 / 两处调用点恢复 / test_v542 删除。

**financial_anomaly 定性 (无需排除)**: 2020-01 空 = 财务期数不足 (1 期 < YoY 需 2 期, 正常机制); 2023-03-31~2024 低覆盖 (663 只/天) → **2025 起 3099-5023 只/天已自愈** — "数据补齐自动恢复"实证发生。

**教训**: v542 时过早下结论 (10 只复刻被 min_count=30 门槛误导; 500 只复刻样本未查覆盖; 未用物化结果文件/中间量/代码历史三层验证)。根因定位必须到代码级, 而非 blocked 分布推断。

**验证**: test_v543.py 3 项 + 回归 21 passed。待物化轮结束后 force 重物化 fund_change 单因子恢复覆盖。

## §21 v544: financial_anomaly/gp_ta 恒空根因 = sina 数据源缺字段 + NaN 传染 (非代码 bug)

05:40 物化轮 blocked 洪水后用户质疑"全是这种错误, 还跑什么"。逐一根因:

| 因子 | 性质 | 根因 | 处置 |
|------|------|------|------|
| fund_change | 代码 bug | v543 已修 (05:40 轮用旧代码, 空属预期) | 轮后重跑 |
| financial_anomaly | 数据缺失 + 缺陷 | sina 接口 2020-2024 缺"营业成本/管理费用" → 表内全 NaN; 代码 NaN 传染 (z += NaN) → 恒空 | v544 修传染 |
| gp_ta | 数据缺失 | operating_cost 全 NaN → 无毛利可算 → 天然 blocked | 无需修 |

**数据实证** (financial_income): 2020-12-31 cost NaN 5441/5468 (99.5%), 2023-12-31 5554/5557, 2024-12-31 4644/5557 (84%); **2025-03-31 仅 100/5221** → "数据补齐自动恢复"实证发生 (2025 自愈), 与 CLAUDE.md 已知事项一致, 非补数流程触发点。

**v544 修复** (compute_financial_anomaly): 子因子 NaN 跳过不传染 + 归一化 z/count (原 z/count*4 在 count<4 时跨期放大, 平均偏差保证 3/4 子因子口径一致)。验证 2020-06-30 493/500 (修复前 0), 2025-06-30 500 只 std 2.14。

**gp_ta 定性**: `operating_revenue - operating_cost` 在 cost 缺失期无毛利可算, blocked 机制行为正确, 不修代码 (修了也无数据可用); 2025 起自愈。

**chunk 预载疑云排除**: 2025-06-30 chunk 预载实测含 9 报告期 (2023-06-30~2025-06-30), 同年期正常载入, 字符串比较无问题 (v523 实现正确)。

**教训**: 大规模 blocked 不恐慌, 按因子分类 (代码/数据/机制) 逐一钉死; 数据缺失类 blocked 在数据源补齐前不修代码、不排除因子, 等自愈。

## §22 v545+v546: sync 字段缺失根治 + blocked 自愈/聚合告警 (防再犯机制落地)

**v545 (sina_financials sync)**: "行已存在即跳过"是 2020-2024 缺字段永不补的另一半根因 (行在表里但列 NULL)。修复: financial_income 的 existing 判定要求 operating_cost/administration_expense 非空 + need_fetch 判定加 needs_cost — 缺失字段的股票重拉补齐, 根治。

**v546 (store.py)**: 防"bug 因子静默归档"机制:
1. `_unblock_recovered` — 成功重算自动解除 blocked (此前 blocked 只增不减, 恢复后残留导致增量轮跳过)
2. `_empty_factor_summary` — 本轮空结果按因子聚合 ≥50 天 → ERROR 告警"检查代码或数据源" — fund_change 案例 (245 天 blocked, 唯一暴露路径是人工查 parquet) 从此自动暴露

**补数**: scripts/backfill_financial_income.py v1.1 — sina lrb 重拉补齐 NULL 列 (COALESCE 幂等)。并发调优: 8 并发触发 sina 限流 (62s/请求) → 3 并发稳定 ~9.4s/请求, 5558 只 ≈ 5h。baostock 排除 (query_profit_data 仅 11 字段无 operatingCost)。银行股无营业成本科目 → 保持 NULL 合理。

**验证**: test_v545_v546.py 4 项 + v543/v544 (前置断言改为不依赖临时数据状态 — 补数生效后 2020 期 cost 已恢复 41%)。10 passed。

## §23 v548: 全表字段级体检 + margin_detail short_total 漏写修复

scripts/field_health.py 全表字段级扫描 (v547 事件后)。39 列超 5% 阈值, 分类:

1. **真实缺失 → 补数中**: financial_income 再缺 total_profit/income_tax_expense (74-75% NaN, 与 cost/admin 同源同模式) + financial_cashflow 4 列 (79% NaN) — 补数脚本 v1.2 扩展 (lrb+llb 双表, 5560 只 × 2 请求)
2. **代码 bug → v548 修复**: margin_detail short_total 100% NaN — SH 路径 (9 列 8 值错位, margin_total 恒 NULL) + SZ 路径 (8 值 9 槽错位) 均漏写融券余额; 修复 10 列 10 值对齐 (SH 加 rqye, SZ 加 short_total), test_v548 2 项 (mock SSE 响应 + 绑定形状)
3. **设计如此**: daily_valuation.turnover_rate (em_valuation 注释: turnover 在 daily 独立来源, 因子零引用); lhb post_Nd 最新期天然 NaN
4. **合理缺项**: 金融股无商誉/固定资产/长短期借款科目; fund_hold.change_ratio 部分期无变动; stocks 快照 6.5% (未上市/退市)

**待办**: 补数完成后 margin 历史回补 (SH 1849 天 + SZ 全量) → 复检 → 重启物化。

## §24 v549: market.db 写锁死根因 — backfill 长事务锁杀调度

**症状链**: 08:23 daily_repair 写 data_audit 锁失败 (60s busy_timeout 超) → 调度任务全灭 → 05:40 物化轮 seg_6+ 子进程全灭 (factor_cache 写等锁崩溃) → daily_repair 卡 running 25 分钟 → web 高 CPU 忙等。

**根因**: backfill_financial_income.py v1.0-v1.2 — 3 线程共享 1 sqlite3 连接 (check_same_thread=False) + 默认 deferred 事务 (首条 UPDATE 自动 BEGIN 持写锁) + 每 200 只才 commit → 锁窗口 ≈ 19 分钟。期间所有外部写者 (物化段/repair/task_runs) 全部 database is locked。

**证据**: 暂停 backfill (SIGSTOP, 冻结进程持锁 — 首次测试被误导判定 web) + 杀进程后写立即恢复 (IntegrityError 而非 locked)。

**修复 (v1.3)**: fetch 网络并行 (线程池), UPDATE 串行 (主线程单写者), 每 50 只 commit。实测: 补数运行中连续写 5/5 成功 (修复前 0/20)。

**通用约束**: 多线程共享 sqlite3 连接必须显式事务边界 (每批 commit ≤ 秒级); 禁止"共享连接 + 批量 commit"模式。

## §25 v550: signals 调度失败根因 — neutralize 单 None 崩溃 (B32 放大 v501 未实现语义)

**症状**: 09:01 orchestrator signals 两次失败: step 3 neutralize FAILED (B32): 'NoneType' object has no attribute 'dropna'。phase8 回测 07-30/07-31/08-03/08-06 同错 (08-18 18:21) — 非新回归。

**根因链**:
1. v501: fundamentals market_cap 仅来自 fund_val_piv PIT pivot; get_fundamentals (signals 路径) 从不含 market_cap 列; 注释承诺 "下游 neutralize 自动降级 (industry-only 或跳过市值)" — **从未实现**
2. neutralize._build_neutralize_projection:276: market_caps=None 时 None.dropna() AttributeError; neutralize_factors_batch:338 仅挡"两者都 None"
3. B32 (08-18): neutralize 失败 warning→阻断 — 旧静默降级变成硬失败

**修复**: _build_neutralize_projection 三态: 单 None → 只投影可用维度 (industry-only X=[1,dummies] / 市值-only X=[1,log_mcap]); 双有 → 原逻辑。test_v550 5 项; run_task.sh signals 实测 STATUS=OK (2 targets, 17.2s)。

## §26 v551: 纠正 v550 — neutralize 不降级 (B32), 真根因是列名不认 (PIT total_mv)

**§25 修正**: v550 实现的"单 None 降级投影"违背"不降级不静默"铁律, 已撤销。真根因不是数据缺失:

1. daily_valuation 2019-01-02~2026-08-18 全覆盖, 07-30/08-18 market_cap 100% 非空 — **数据齐全**
2. get_fundamentals (live/signals/phase8) PIT 路径将 market_cap 按 source 换算后写入 **total_mv** 列 (store.py:2946-2990)
3. pipeline.py:426 (因子层) + 513 (合成层) 只认 **market_cap** 列名 → live 路径市值恒 None → neutralize 崩 (B32) / industry-only 降级

**修复**: 426/513 认 PIT total_mv (同语义); neutralize 全家 (batch/_build/标量入口) 单 None、双 None、样本不足一律抛 ValueError — 风控硬要求不降级。验证: signals 日志 joint (industry+size), STATUS=OK。

## §27 v552: 全库写事务审查 — 锁死事故彻底解决 (7 处活体风险全修)

2026-08-19 事故 (backfill 长事务锁杀调度) 后, 对全部 market.db 写者做逐文件审查。发现 7 处与事故同构的活体风险 (deferred 事务 + 网络/CPU 在事务窗口内 + 批量 commit), 全部修复:

1. sina_financials.py: 每 200 只 commit + HTTP 在事务内 (回填 10-30 分钟锁) → 每 symbol commit
2. store.py ×4: _sync_industry_akshare (5-20 分钟)、_backfill_via_baostock (60-120s)、backfill_turnover (1-3 分钟)、_rebase_ex_dividend (分钟级) → 每股 commit
3. news.py: SnowNLP CPU 在事务内 (30s-3 分钟) → 攒批一次写
4. 受害加固: margin/limit_up/northbound/monitor/crowdedness 裸连接 (5s busy_timeout) → timeout=30

**审查方法**: 每个 connect 查 busy_timeout/WAL/isolation_level; 每个写路径查 commit 频率; 事务边界内查网络/CPU; 查共享连接。审查结论: runners/orchestrator/reconcile/task_log/factor_cache/industry_history/data_health 均无风险 (单语句即 commit 或网络在事务外)。

**验证**: 8 文件语法 OK; 全量 pytest 479 passed; 补数运行中 15/15 连续写成功 (锁窗口秒级)。

**通用规则**: sqlite3 默认 deferred 事务, 首条 DML 持写锁到 commit; 禁止网络/CPU 密集计算在事务内; 批量写每小批 commit; 所有写连接 timeout≥30。
