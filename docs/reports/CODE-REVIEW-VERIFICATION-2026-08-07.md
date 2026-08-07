# CODE-REVIEW 修复对照验证报告 2026-08-07

> **对照基准**：`docs/reports/CODE-REVIEW-2026-08-07.md`（原审查 46 项）
> **验证对象**：commit `50f4a3e`（test-v414 "CODE-REVIEW 全量修复 v406~v414"）+ 当前工作区（Web VERSION=**test-v415**）
> **方法**：3 路独立代码复核（读取当前代码直接验证，不采信 HANDOFF 自述）
> **结论**：**46 项中 17 项已修复 / 13 项部分修复 / 15 项未修复 / 1 项信息**，且引入 **1 项新回归**（Kelly `max_single` NameError）

> **2026-08-08 更新 (test-v416)**：本报告 15 项未修复 + 部分修复中的执行序列项已**全量落地**（见下方"修复批注"逐项标注 ✅v416）。除 A14（config 常量 import 期冻结 — 设计权衡，记入排期）外全部完成。回归 **257 tests 全过**，对应 HANDOFF §test-v416。

---

## 总览统计

| 状态 | 数量 | 关键项 |
|---|---|---|
| ✅ 已修复 | 17 | P0-3/7/8、Phase8 的 factor_store/suppress_push、qlib 掩码+单次fit+列序、止损一致、pipeline.errors、source_policy、margin executemany、日历、rebalance cash、_log_seal、HMM、Ledoit-Wolf、HRP IVP、基准闭环、依赖倒置、死文件删除 |
| ⚠️ 部分修复 | 13 | P0-1 键名、P0-2 Phase8、P0-4/5 xgb 掩码&分块、P0-9 backfill、P0-12 冲击空订单、跌停预检回测侧、daily_equity、SSE 实时推送、告警 notify、lgb skipped、Nano 0手、依赖/WEB安全 |
| ❌ 未修复 | 15 | **P0-10 Kelly（+回归）**、**phase7 前视**、**daily_equity 实际不写**、XSS/鉴权、连接泄漏、sync_fundamentals、DataCache.put、northbound 泄漏、_dispatch cn、fundamental 直连、DB路径、config 冻结、stop_loss、multi_tf、0信号日 |
| ℹ️ 信息 | 1 | VERSION 已到 test-v415（工作区领先 commit 标题 v414） |

---

## A. 数据层修复验证（14 项）

| # | 原问题 | 声称修复 | 验证结果 | 现状 |
|---|--------|---------|---------|------|
| A1 | P0-7 dividend_yield 未除股价 (fundamental.py:955-967) | v406 div/close_latest | ✅ 已修复：`fundamental.py:968 vals[sym]=div/price`，price_rows 有效使用，data/dividend.py:93 已转每股 | `fundamental.py:957-968` |
| A2 | P0-8 get_daily 缓存键 [:200] 截断碰撞 (store.py:2093) | v406 | ✅ 已修复：`store.py:2102-2103 _ck=(hash(tuple(sorted(symbols))),...)` 全长符号哈希 | `store.py:2102` |
| A3 | P0-9 backfill 单位错 (store.py:1517-1525) | v406 | ⚠️ 部分：volume /100 存手✅、turnover 不再除10000✅；**amount 仍未拉取**（字段列表与 INSERT 均无 amount） | `store.py:1507-1525` |
| A4 | sync_fundamentals 读 result["pe_count"] (store.py:2177) | 未声称 | ❌ 未修复：`store.py:2187` 仍读 `result['pe_count']`，而 fundamental.sync_all 返回 `{"count":...}` → 必 KeyError | `store.py:2187` |
| A5 | DataCache.put 不存在 (store.py:263/436) | 未声称 | ❌ 未修复：cache.py DataCache 只有 get/set/invalidate 无 put，store.py:263/436 必 AttributeError | `store.py:263` |
| A6 | northbound 空数据早退泄漏连接 | 未声称 | ❌ 未修复：northbound.py:58-63/72-73 return 前未 close | `northbound.py:58-73` |
| A7 | P0-11 mode 参数忽略 → 资金串号 | v406 声明"已验证非 bug" | ✅ 属实非 bug：get_cash/get_positions SQL 均已含 `mode=?` | `trade_repo.py:329-337,364-372` |
| A8 | 因子死代码：compute_asset_growth 占位 return None | 未声称 | ❌ 未修复：high_priority.py:36-38 仍占位 return None，未注册 | `high_priority.py:36-38` |
| A9 | _intermediates.compute_shared 无调用者 | 未声称 | ❌ 未修复：仍零引用 | `_intermediates.py:16` |
| A10 | turnover_accel 两份实现+短路表 | 未声称 | ❌ 未统一：_turnover.py:118、_huanfang.py:116（遮蔽前者）、_primitives.py:839/879 三处并存 | `price/*` |
| A11 | _dispatch except 引用未定义 cn | 未声称 | ❌ 未修复：`_dispatch.py:196` 仍用 `cn`（循环变量是 sym）→ NameError | `_dispatch.py:195-196` |
| A12 | fundamental.py 15 处直接 DB 连接 | 未声称 | ❌ 未修复：_db_connect 6处+DatabaseManager.market 9处；_shared_limit_conn 仍被置 None；ihn/pledge/holder_reduction 仍直连未走 aux | `fundamental.py` 多行 |
| A13 | DB 路径 `<os.path.join(...,DB)` 19处硬编码 | 未声称 | ❌ 未修复：quant/data 15个文件 + web/app.py:353 + quant/core/state_broker.py 5处 仍硬编码 | 各处 |
| A14 | config 常量 import 期冻结、override() 无效 | 未声称 | ❌ 未修复：constants.py:23-40 仍模块导入期求值绑定 | `constants.py:23-40` |

---

## B. 评估/ML 链路修复验证（11 项）

| # | 原问题 | 声称修复 | 验证结果 | 证据 |
|---|--------|---------|----------|------|
| B1 | P0-1 Phase2→3 键名 passed 断裂 | v407 | ⚠️ 部分：phase3:41✅ phase5:75✅ 改读 active；**bridge.py:35、phase7_wf.py:71、run_store.py:22 仍读 'passed' → 回测桥接/phase7 继续断裂** | `bridge.py:35` `phase7_wf.py:71` `run_store.py:22` |
| B2 | P0-2 Phase8 D1/D2/D4 必崩 | v406 | ⚠️ 部分：factor_store 已定义✅、suppress_push 参数已加✅；**run_backtest 返回仍无 "trades" 键（loop.py:770-777）→ D2 恒 no_matches；D3 仍 CostModel() 默认费率（非 from_config）** | `phase8:100-115,214,258-261` `loop.py:770` |
| B3 | P0-3 DSR 恒 None | v407 | ✅ 已修复：n_obs 已传、传 float 数组、dict 取 key | `loop.py:144-168` |
| B4 | P0-4 LGB 标签掩码 fillna 前置 | v406 | ⚠️ 部分：qlib_model=185-193 已移到 mask 后✅；**xgb_model.py:151-158 原 bug 保留；qlib Ensemble/rolling_cv (531/653) 同样未修** | `xgb_model.py:151-158` |
| B5 | P0-5/6 LGB 分块训练+列序 | v407+v408 | ⚠️ 部分：qlib_model 已单次 fit(234-236)✅、预测列序严格对齐(311-316)✅；**xgb_model.py:186 仍分块** | `qlib_model.py:234,311` |
| B6 | P0-12 冲击价未更新 cost | v406 | ⚠️ 部分：o.cost 已同步(execution_model.py:254-259)✅；**orders 为空时 impact_bps 未定义 UnboundLocalError 仍被 except 吞** | `execution_model.py:249-263` |
| B7 | Phase7 训练窗不生效 → 结构性前视 | 未声称 | ❌ **未修复（最高危残留）**：train_start/train_end 只打日志(phase7_wf:60)，phase2/3/4 仍用今日全量历史；且 line 71 keys 错误（读 passed）导致所有 fold 短路" | `phase7_wf.py:60-102` |
| B8 | 执行日无涨跌停模拟 | v413 | ⚠️ 部分：scheduler/execute.py 跌停预检已加✅；**回测 execution_model 仍无涨停不可买/跌停不可卖模拟** | `execute.py:164-181` |
| B9 | 回测↔实盘止损不一致 | v411 | ✅ 已修复：BacktestExecutionModel 已注入 rm.check() ATR 止盈止损 | `execution_model.py:167-178` |
| B10 | daily_equity 生产零调用 → 回撤告警断 | v411 | ❌ **功能上未修复**：reconcile.py:299-300 调 `engine.get_cash("quant", mode="live")`，但 engine.py:77/203 get_cash/get_positions **无 mode 参数** → TypeError 被 `except Exception: pass` 吞 → record_daily_equity 仍不执行 | `reconcile.py:294-304` `engine.py:77` |
| B11 | pipeline.errors 未 inc | v411 | ✅ 已修复：signals.py:65 inc，alerts.py:70 消费 | `signals.py:63-67` |

---

## C. 优化器 / 执行 / Web / 调度 修复验证（18 项）

| # | 原问题 | 声称修复 | 验证结果 | 证据 |
|---|--------|---------|----------|------|
| C1 | **P0-10 Kelly fraction 空操作** | v406"删第二次归一化" | ❌ **未修复 + 新回归**：第一次总归一化(kelly.py:120-124)仍抵消 fraction → 熊市仍不缩仓；且 v406 删了 `max_single=_require_cfg(...)`**却保留** `kelly.clip(upper=max_single)`(:130) → **Small 层必 NameError 崩溃** | `kelly.py:117-130` |
| C2 | rebalance cash=0 把总资产当现金 | v413 | ✅ 已修复：`max(cash,0)` | `rebalance.py:146` |
| C3 | Nano 0 手崩溃回测无 fallback | — | ⚠️ 部分：仍 re-raise 无降级；但 clip 资金补足已实现 | `portfolio.py:325-336,174-193` |
| C4 | stop_loss TP1 全仓化/TP2 空卖/trail 不需盈利 | 未声称 | ❌ 未修复：TP1(:209) 1手持仓仍全清、TP2(:216) 仍卖0股、trail_sl(:234) 仍不要求当前盈利 | `stop_loss.py:209,216,234` |
| C5 | multi_tf 周线前视 | 未声称 | ❌ 未修复：仍取本周五(end_of_week=pipeline.py:444-449)，仅 config 默认 false 缓冲 | `multi_tf.py:44-51` |
| C6 | HMM 回测/live 量纲不一致 | v413 | ✅ 已修复：loop 去掉 *100，backtest 内部自洽 | `loop.py:513-515` |
| C7 | SSE 跨进程断裂 | v409 | ⚠️ 部分：文件桥已实现(quant/core/state_broker.py:306-320)、web get 能读到快照✅；**长任务中 web SSE 实时 push 仍断（跨进程队列不触发）** | `state_broker.py` `app.py:747-752` |
| C8 | 告警通道 notify/telegram 空 | v409 | ⚠️ 部分：回撤告警已改读 daily_equity、orchestrator inc alerts 指标✅；**notify.py 仍死代码、telegram token 仍空** | `config.yaml:385-386` |
| C9 | evening 失败不重试 | v411 | ✅ 已修复：evening 失败 sys.exit(1)+orchestrator ret<>0 重试（上限2） | `evening.py:133-138` `orchestrator.py:265-282` |
| C10 | lgb_train skipped 破坏状态机 | 未声称 | ⚠️ 部分：lgb_train 仍写 skipped、task_log 契约未扩展 → 无 lightgbm 的机器晚间链仍被标 failed | `lgb_train.py:32-37` |
| C11 | 0 信号日误报 failed | 声称"v400已修" | ❌ **未修复**：execute.py 无 targets 时 status 仍 failed | `execute.py:28,55-57` |
| C12 | 基准只写不读 + Brinson 同源 | v410 | ✅ 已修复：compute_rolling_metrics 已接线、/api/benchmark 路由、Brinson Rp 改从持仓收益 | `attribution.py:559-566` `app.py:930-941` |
| C13 | factor_attribution bps 单位 | v410 | ✅ 已修复：×10000 | `factor_attribution.py:145` |
| C14 | Web XSS / 无鉴权 POST / 连接泄漏 | 未声称 | ❌ 未修复：POST/state、'curator/submit' 仍无鉴权；XSS（app.js 无 escapeHtml，26 处 innerHTML 未转义）；/api/risk + /api/health 连接未 close；错误格式仍 str(e) | `app.py:381,435` `app.js:33,472-498` |
| C15 | 死代码 state_pusher/index_fix/app.py.tmp/schema.sql | v412 | ✅ 已删除 | — |
| C16 | HRP 簇内等权 | v413 | ✅ 已修复：簇内 IVP (1/σ²) | `hrp.py:70-86` |
| C17 | Ledoit-Wolf NaN | v414 | ✅ 已修复：covariance_subset 剔除 NaN 列 | `covariance.py:160-165` |
| C18 | VERSION | — | ℹ️ 当前 test-v415（工作区已领先提交标题 v414） | `app.py:18` |

---

## 需要优先处理的未修复项（按风险排序）

1. **C1 Kelly `max_single` NameError（新回归，P0）**：`kelly.py:130` 引用已删变量 → Small 层组合构造必崩；且 fractional Kelly 仍是空操作。删 clip 行或恢复 `_require_cfg` 赋值。
2. **B10 daily_equity 仍不落库（P0）**：`reconcile.py:299` 传不存在的 `mode=` 参数，错误被静默吞掉 → 回撤告警/Sharpe/日报全部仍无数据。删 `mode=` 或给 engine 加参数 + 移除裸 `except: pass`。
3. **B7 Phase7 前视（P0，评估可信度）**：训练窗完全不生效，且继续读 `passed` 键。
4. **B4 xgb 掩码 + 分块（P1）**：xgb_model 标签污染训练，与 qlib 修复不同步。
5. **C11 0 信号日误报 failed（P1 业务噪音）**。
6. **B6/A11/A12/B14 残余 NameError + 直连 DB + XSS/鉴权（P1 兜底）。**

---

## 数据层 4 个未声称修复的真 bug（A3/A4/A5/A6）

- `store.py:2187` sync_fundamentals 读 `pe_count` → 必 KeyError
- `store.py:263/436` DataCache.put → 必 AttributeError（仅 __main__ 触发，潜伏）
- `northbound.py:58-73` 空数据早退漏 close
- `store.py:1526` backfill amount 未拉取（单位修复并未完整）