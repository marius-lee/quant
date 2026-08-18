# 全量代码 Review 报告与修复清单 — 2026-08-18

> 状态：**修复中** — 本文件同时作为修复清单（checkbox），全部修复完成后置为"已完成"。
> 约束：只 review 代码，未 review 文档。修复遵循零 fallback 硬约束，所有新参数入 config.yaml（带来源注释）。

## 结论摘要

架构分层清晰、工程意识上乘（注释密度、PIT 约定、状态机、文档化修复历史）。三类系统性问题：
1. **静默降级约 20 处**，与"零 fallback"硬约束系统性冲突 — 风控层最危险（缺数据即放行）
2. **结构约定未强制**（物化按日切片）→ 前视类 bug 复发
3. **多条自愈/验证链路"写了未驱动"**（超时检查、重试预算、phase8、metrics、highfreq）

## 七、Bug 汇总（36 个）

8 critical / 11 high / 17 medium-low。修复状态打钩。

### P0 资金安全

- [x] **B3 [critical] execution_model.py:172-203** — ATR 止损卖出后 `current_lots`/`target_lots` 未刷新（硬止损分支刷新了，ATR 分支没有），且 target_lots 过滤发生在 ATR 检查之前 → 已清仓 symbol 生成卖出单 → 负持仓/券商拒单。
- [x] **B7 [high] execution_model.py:74-79** — `trim_orders_by_alpha` 的 `max_shares = int(available//(px*LOT_SIZE))*LOT_SIZE` 未 `min(o.shares)` → 资金不足裁剪时首单（最高 alpha）被放大到耗尽全部资金，超目标股数。
- [x] **B24 [high] order_manager.py:102-118 + execute.py** — `place()` 无 (date,symbol,pending) 去重 × failed 无限重试（`_should_run` 用 aborted 计数恒 0）→ 重复下单。
- [x] **B13 [high] constraints.py:99,125,197** — 三道风控过滤器全部 fail-open：缺 stock_names 时 ST 过滤原样返回；缺 amount/close 列静默跳过流动性/股价过滤；缺 seal 数据直接放行涨停封板过滤。

### P0 数据正确性

- [x] **B1 [critical] store.py:416-422 get_universe** — `list_date` 实际存 ISO（DB 实证 5556 行 ISO），查询用 `list_date <= strftime('%Y%m%d', ?)` 做 ISO vs 8 位字典序比较，`'-'(0x2D)<'0'(0x30)` 同年恒真 → 实测 2024-06-15 查询错误包含 46 只未来上市股票（前视）。delist 分支同理。
- [x] **B2 [critical] alpha101.py:51,64-65,82-83,96-97,116-118,164-170 + _alternative.py:193,281 + intraday.py:139,222-223 + _event.py:485-508** — 物化路径整 chunk 传 data 不按 date 切片，因子用 `.iloc[-1]` 取 chunk 末行（未来数据）且同 chunk 内各日期返回相同值。alpha002/055 在 probation 实盘池。另 _preload.py:381-383 analyst 按 chunk 末日期加载 → 前视。
- [x] **B5 [critical] qlib_model.py:151-156 vs 275-279（xgb 同款）** — 训练喂 rank→正态分位 z 特征，推理喂原始因子值 → 树分裂阈值尺度错位。

### P1 统计可信度

- [x] **B4 [critical] portfolio.py:186-201,875** — `w_raw = inv_Sigma@alpha/lam` 后 `w_raw/w_raw.sum()`，λ 常数标量在归一化中抵消 → 恒返回网格首项 λ=0.5（最激进），均值-方差层实际是无约束最大 Sharpe 组合。
- [x] **B6 [critical] covariance.py:146-157→88-89** — `dropna(thresh)` 保留部分 NaN 列，中心化叉积全矩阵 NaN → 污染优化器/风控/VaR。
- [x] **B11 [medium] covariance.py:337-360** — `_incremental_update` 的 `new_cov` 从未赋值，else 恒执行 `_full_recalc()` → 每步双重 O(N³)，O(N²) 设计失效。
- [x] **B26 [P0] phase7_wf.py:108-125,193-209** — 测试折叠用 `status_filter="active"` 混入全注册表 active 因子 + 其它 fold 选中因子且不复原状态 → walk-forward 隔离破坏，OOS 虚高。
- [x] **B27 [P1] loop.py:725-726** — OOS 段 `combine_mode` 键缺失回退 sleeve，IS 段用 ic_weighted → OOS 验证比较的是两个不同策略。
- [x] **B28 [P1] deflated_sharpe.py:68-70 + loop.py:163** — E[max_SR] 用 √(2lnN) 近似（N=30 高估 42%）叠加正修正 → 高估 ~60%；loop.py n_trials 误用股票数(500)而非因子数(~30) → DSR 系统性偏严。
- [x] **B15 [high] xgb_model.py:158-171** — OOS 尾部样本同时用作早停 eval_set 与 OOS ICIR 报告 → 模型选择泄漏，OOS 指标虚高。

### P1 运营可靠性

- [x] **B22 [critical] orchestrator.py:316-318** — `_check_timeouts` 仅 `not is_trading_day()` 分支调用 → 交易日挂死任务/僵尸子进程无人清理；晚间链 `_wait_subprocess` 仅 spawn 时 poll 一次，重试分支死代码。
- [x] **B23 [high] runners.py:299-314 + orchestrator.py:341-350** — monitor 崩溃仅 log 不 `_tk_finish("failed")` → 行卡 running、风暴保护 `_get_monitor_failures` 恒 0、永不重启；`MonitorRunner.stop()` 空操作（`_monitor_stop` 无人 set）。
- [x] **B10 [high] broker_adapter.py:493** — `main_engine.get_exchange(vt_symbol)` 在 vnpy 不存在 → 实盘下单必 AttributeError，被 except 吞 → 实盘下单不可用。
- [x] **B19 [high] fund_hold.py:91** — `quarters = ['20241231',...'20251231']` 硬编码且永不推进 → 2026 年数据不再同步，静默过期。

### P2 清理

- [x] **B18 [critical] highfreq.py:501,504-580,626** — 6 个算法方法未定义 + `CostModel.total_cost` 不存在 + 引用未定义 `request` → 一跑即崩且 `_execution_loop` 吞异常 → 订单静默丢失。
- [x] **B34 [medium] stats_cache.py:733-740,760,796,724,175-177** — `_full_recalc` 空实现、IC 日期戳用 now()、get_ic_map 硬编码 120 无视 lookback、IC 衰减 0.8/0.5 捏造系数、forward returns 死代码。
- [x] **B35 [medium] factor_curator.py:431-434** — 方向强签：声明 negative 就强制 -abs(IC)，实测符号相反仍注册 → 统计造假。
- [x] **B36 [medium] state_machine.py:231,239,304,386 + golden_test.py:44** — 3 处 NameError + 1 处 AttributeError + `date_results` 未初始化 → curate/evaluate/golden_test 必崩。

### 其余 high/medium

- [x] **B8 [medium] stop_loss.py:228-233** — TP1 时 `max(100, shares//2)` → 100 股持仓 TP1 全仓卖出，止盈语义错误。
- [x] **B9 [high] execution_model.py:217 + trade_repo.py:388-417** — `_tp1_hit` 从不从 position_meta 回载 → TP1 每天重复触发减半出清；`_peak` 每日重置 → 移动止损跨日失效。
- [x] **B12 [high] var.py:82-84,103-105,121-122** — weights 按位置截断不按 symbol 对齐 → VaR 算错持仓；截断后权重和≠1。
- [x] **B14 [medium] multi_tf.py:51** — 周五用当日收盘价算"周线信号"→ 盘中调用即前视。
- [x] **B16 [medium] kelly.py:101-118,75-78** — σ² 取均值后归一化抵消 → Kelly 退化为 alpha 比例；ic_map 缺失时跳过 fraction 与 max_single clip → 熊市满仓。
- [x] **B17 [medium] hrp.py:92-93** — 簇方差用等权而非 IVP 权重 → 高波动簇方差高估，α 分配偏斜。
- [x] **B20 [medium] news.py:30** — PK(symbol,date) 下每日多条新闻互相覆盖 → news_count 恒≤1，因子失真。
- [x] **B21 [medium] store.py:955,1296** — `adj_factor.ffill().bfill()` 用未来复权因子回填历史 → 既前视又错价；`fillna(1.0)` 混入未复权价。
- [x] **B25 [high] scheduler/attribution.py:174-182** — 基准市值 SQL `ORDER BY date DESC LIMIT 1` 取单只股票而非行业汇总 → Brinson allocation 失真。
- [x] **B29 [P1] phase8_live_consistency.py:214** — 读 `run_backtest` 不存在的 `trades` 键 → D2 恒空转，死代码。
- [x] **B30 [P1] tear_sheet.py:114** — dict 构造 Series 的 index 是 object，`groupby(index.year)` → AttributeError，报告必崩。
- [x] **B31 [medium] evening.py:79** — `now().weekday()` 而非 today → 晚间链跨午夜时 lgb/xgb 门控错位。
- [x] **B32 [medium] pipeline.py:434-435,466-467** — 中性化/multi_tf 失败 warning 降级为带偏置因子继续。
- [x] **B33 [medium] crowdedness.py:38** — `get_symbols(exclude_market='BJ')[:300]` 按 universe 顺序切片非流动性排名。

## 归档记录

- 归档时间：2026-08-18
- 归档人：opencode review 会话
- 后续修复记录：见 HANDOFF.md 变更日志
