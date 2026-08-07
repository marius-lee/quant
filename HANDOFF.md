# HANDOFF — 盈迹 (quant) 项目变更日志

> **修改前**: `grep -rn "关键词" HANDOFF.md docs/adr/` 联动搜索，避免重复踩坑。

## 当前状态 (test-v418, 2026-08-08)

### CODE-REVIEW-2026-08-07 遗留 R1-R11 全量修复 (2026-08-08, v418)

对照 `docs/reports/CODE-REVIEW-2026-08-07.md` ⚠️ 遗留项全部落地 (4 项判定已修/非 bug, 11 项完成)。**277 tests 全过 (263 + 14 新增)**。

| # | 修复 | 文件 |
|---|------|------|
| R1 | **synth factor_count 计数 bug** — 每轮重置计数导致多因子共振 bonus 永远失效。删 `factor_count[sym]=...` 重置行, `sleeve_compose` 正确累加 | `quant/alpha/synth.py` + `test_codereview_r1_synth.py` |
| R2 | **monitor/state_broker 吞错 → 可观测** — monitor var/liquidity/tradefreq 降级 debug→warning+metric; adapter fallback→critical; state_broker exec_notes/signals/regime/quote 线程告警 (`except: pass` 全清) | `quant/scheduler/monitor.py` `quant/core/state_broker.py` |
| R3 | **HRP 中点切分 → linkage 树切分** — `_count_leaves` 递归+ `_bisect` 按子树; ⚠️ 陷阱: scipy 对全等距离产病态树 → 零相关退化守卫改走逆方差加权 (1/var, 与旧等权测试兼容) | `quant/optimizer/hrp.py` + `test_codereview_r3_hrp.py` |
| R4 | **generate_signals 26→18 参数** — 14 个 preload 参数移入 BacktestContext (无 ctx 时默认 None), 保留 8 业务参数 + factor_store/primitives/regime | `quant/pipeline.py` + `test_codereview_r4_signature.py` |
| R5 | **JSON 文件桥 → SQLite 消息表** — `/tmp/quant_state_bridge.json` → `trades.db:state_bridge` 单行原子 upsert, 读写失败→warning (非静默) | `quant/core/state_broker.py` |
| R6 | **monitor 跨层 import 提模块级** — 原函数内 `from quant.X` (循环依赖假想) 全部为静态拓扑, 提顶 | `quant/scheduler/monitor.py` |
| R7 | **VaR PSD 修正** — eigen-clip 非 PSD 协方差 (eigh 对称化→负特征值归零→重建), 三个 VaR 家族函数共用; update_daily_risk log_ret 加列级 dropna | `quant/risk/var.py` |
| R8 | **删 regime sizing 资本乘数死角** — 删 `get_regime_sizing`(detector) + `_apply_regime_sizing`(portfolio) + config `regime.sizing` 块 (yaml.safe_dump 合规, 注释丢失已核对无误删键); 展示字段改 `regime_max_lots` (lot-based) | `quant/regime/detector.py` `quant/optimizer/portfolio.py` `quant/pipeline.py` `quant/core/state_broker.py` `quant/config/config.yaml` |
| R9 | **detector.py:20 硬编码路径** — `_MARKET_DB` 无任何引用, 直接删 | `quant/regime/detector.py` |
| R10 | **快照因子 60 天门控** — intraday 三因子 (intraday_reversal/open_volume_ratio/close_surge) 快照积累 < 60 日前显式返回 None (原来静默产出 NaN 因子); aux 路径零查询读 chunk 计数透传 (`intraday_snapshot_days`), DB 路径 per-进程 COUNT 缓存; Bug6 列名单位注释同步 (腾讯 volume=股非手) | `quant/factor/compute/intraday.py` `quant/factor/compute/_preload.py` `quant/scheduler/snapshot.py` + `test_codereview_r10_snapshot_gate.py` |
| R11 | 全量回归 277 passed + VERSION bump (本条目) | — |

**判定非 bug / 已修 (不落地)**: Bug1 monitor 缩进 ✅ 前序已修; Bug2/Bug3 非 bug (快照空值在 compute 端降级=设计); Debt7 benchmark ✅ v401 已修。⚠️ Gap2 (0-active-factors 反馈环) 为**业务问题非代码缺陷** — 独立排期。

**验证**: `test_codereview_r1/r3/r4/r10_*.py` 新增 14 测试; 全量 `pytest test/` **277 passed**; 语法校验全过。config.yaml 注释在 R8 safe_dump 中丢失 (值不变, 规则仍强制 safe_dump)。

### 周六周度评估调度断链修复 (2026-08-08, v417)

**现象**: Web 调度页「因子评估(总)」从未启动; 96 因子全停留在 evaluating 状态、永远无法晋升 active。

**根因 (三路触发源全断)**:
1. **orchestrator 周六分支不可达** — `orchestrator.py` 主循环顶部 `if not is_trading_day(): continue`, 而周六非交易日 → 循环体永远被短路, v301 引入的 `weekly_eval` 触发块 (位于 continue 之后) **从未执行过**
2. **独立 weekly 线程无人启动** — `scheduler/__init__.py::start_all()` 设计为双线程 (orchestrator + `_weekly_loop`), 但 `scripts/restart.sh` / `run_task.sh daemon` 均直接调 `orchestrator.start()` → `_weekly_loop` (正确实现, 不检查交易日) 是死代码
3. **cron 兜底为空** — `scripts/setup_cron.sh` 声明了 `0 6 * * 6` weekly 行但实际 `crontab -l` 只有注释; `.cron_installed` 标记 (7月16日残留) 让 web 误显示"已配置"
4. **附带**: 评估子进程无超时 (`_TIMEOUTS` 缺失) — 若卡死会永远占用 running 行, 后续调度永久阻塞

**修复 (五处, 低侵入)**:
1. `orchestrator.py` — 状态读取 + weekly_eval 触发块整体**前移到 `is_trading_day()` 短路之前**; 触发窗口由 `06:00-06:05` 放宽为 `06:00-12:00` (周六上午 restart 错过 06:00 窗口会漏掉整周评估; `_tk_start` dedup 保证三路触发不重复执行)
2. `scripts/restart.sh` + `run_task.sh daemon` 入口 → `from quant.scheduler import start_all` (orchestrator + `_weekly_loop` 双线程, 单进程)
3. **cron 重建** — `setup_cron.sh` 重写为仅装两条 (weekly 周六 06:00 + adj_factor 每小时); 并修 heredoc 变量未展开 bug (macOS bash 3.2 双引号 heredoc 不展开 `$PROJ`, 导致 crond 实际执行 `cd ` 空路径); 已实测 `crontab -l` 含完整路径
4. **超时防御** — `_TIMEOUTS["weekly_eval"]=43200` (12h); `weekly.py` grace 7200→43200 对齐
5. **可观测性** — web `/api/scheduler` 由于 `_next_scheduled_time` 已支持 "周六 HH:MM" 格式, 下周六 06:00 起正确显示 next_run; 周六日志含 weekly_eval window 诊断

**验证**: `test/test_weekly_sat_trigger_v416.py` 6/6 (触发块位于 is_trading_day 之前 / 窗口覆盖 12:00 / start_all 双线程 / restart.sh 入口 / cron 条目); **263 tests 全过 (257 + 6 新增)**。下一周六 (2026-08-15) 06:00 为首次真实执行验证点。

### CODE-REVIEW-VERIFICATION 全量 46 项修复 (2026-08-08, v416)

对照 `docs/reports/CODE-REVIEW-VERIFICATION-2026-08-07.md` 逐项落地, 已全量修复 (除 A14 config 冻结记入排期)。**257 tests 全过**。

**P0 修复 (3/3)**:
- **P0-10 Kelly fraction 空操作** (`quant/optimizer/kelly.py`): v406 删权重归一化导致 fraction、但保留 clip(upper=max_single) → Small 层崩溃 + 熊市不缩仓。修复: 恢复 `max_single` 参数注入 + 语义化测试 (均匀 alpha 下 w10=2×w05; 集中 alpha+fraction=1.0 → 单票≤cap 且 sum<1), `test_portfolio.py` 48/48
- **reconcile daily_equity 实际不写** — 去 `except Exception: pass` 吞错 + `engine.get_cash("quant", mode="live")` 的非法 mode 参数 (engine.get_cash/get_positions 无 mode 形参 → TypeError)。现在 `record_daily_equity` 真落库, 回撤告警恢复
- **phase7 前视** — `screen_factors(prefilter_from_diagnostics=...)` 不存在的关键字 → TypeError → 圆滑失败后空集。修复: `eval_start/eval_end` 窗口经 `stats_cache.compute_factor_stats` → phase2/phase3 全程注入 (PIT); phase7 失败路径返 `[]` 不再混沌 dict; run_store 读 `passed|active|kept`。`test_eval_chain.py` 4/4

**P1 修复 (10/10)**:
- **B4/B5 xgb/qlib 掩码**: 4 处 `fillna(0)` 前置 `notna` mask (主链/ensemble/rolling CV/OOS test); xgb 移除分块续训 → 单次 fit
- **B6/B8 执行链**: `_apply_cost` 缺 impact_bps 未初始化 (order 空时 UnboundLocal); 回测涨跌停封板 → `broker._day_ohlc` → `pipeline.execute_signals(ohlc=)` → `BacktestExecutionModel._sealed_orders` 阻断买/卖, 测试 28/28
- **C4 stop_loss 边界**: TP1 一手全卖→卖半手; TP2 残留→卖完(不卖 0); trail_sl 需 peak≥cost+2ATR 才激活 (防微利噪音), 7 项回归
- **C5 multi_tf 前视**: 周线取 ≤date 最近周五, 周一~周四只用上周五 (不用未来本周五), 3 项回归
- **C11 0信号日**: execute 调仓日无 target → status="ok"(业务空转) 非 failed, 保留 no_targets metric
- **C10 lgb_train skipped**: `_tk_finish` 契约落 skipped, web /api/scheduler 渲染"今日跳过"黄色徽标

**A 系列 (13/13)**:
- A3 backfill 补 amount 列 (baostock 'amount' 字段); A4 sync_fundamentals 读 `result['count']` (原 `pe_count` 必 KeyError); A5 DataCache `.put`→`.set` (无 put 方法必 AttributeError); A6 northbound 早退 close 连接 (try/finally); A8 删 compute_asset_growth 占位死码(未注册); A9 删 `_intermediates.py`(零引用); A10 `_huanfang._compute_turnover_accel` → `_turnover_accel_5_20` 消遮蔽, 华安版回归 _turnover.py; A11 _dispatch 日志 `cn`→`sym`; A12 fundamental 15 处直连统一 `_db_connect()`, 删 _shared_limit_conn 遮蔽; **A13 DB 硬编码 19 处 → `quant.config.paths` (15 data 文件 + state_broker + web/app.py + 新增/修复 import)**
- **C14 Web**: 3 个 POST(`/api/trade /api/state /api/curator/submit`)统一 `_require_token()` (QUANT_API_TOKEN + hmac); XSS: app.js 加 `escapeHtml()` 全接口转义 (reason/exec_note/scan/heatmap/stress-test/scheduler), scheduler status_label(服务端可信 HTML) 保留; /api/risk /api/health 连接 try/finally close; backtest_history 错误 `str(e)` → 结构化 error

**记录未修复 (排期)**: A14 config 常量 import 期冻结 — 属设计权衡 (config 静态校验), 待专项; C15-C18 已在 v412-414 完成。
### 关键指标: 263 tests, Web test-v417 (11 个新测试文件/类)

---

## 当前状态 (test-v415, 2026-08-07)

### 全量代码审查报告 (CODE-REVIEW-2026-08-07.md)

完整 6 维度审查，详见 `docs/reports/CODE-REVIEW-2026-08-07.md`。

**关键发现**:
1. **0 active factors** — 全量 96 注册因子均为 evaluating/probation/archived，评估管线存在但无因子毕升 active。系统架构完整但无可用 alpha 交易
2. **monitor.py 缩进错误** — `tp_key = f"{sym}:profit"` 应在 `if _is_profit:` 块内 (L275)
3. **`generate_signals()` 参数爆炸** — 16+ kwargs + ctx 双接口, 迥滥用 lazy import (50+ 处) 掩盖真实依赖
4. **JSON 文件桥跨进程 IPC** — `/tmp/quant_state_bridge.json` 无锁/无原子性, 存在竞态条件风险
5. **过度吞错** — monitor/reconcile 多处 `except Exception: _log.debug()` 违反零 fallback 原则
6. **HRP 二分不遵循树结构** — Naive `n//2` split 而非 linkage 树的合并点 (De Prado 2016 pp.68-72)
7. **regime sizing 双存** — `regime.sizing` (capital-based, 已废弃) + `optimizer.{nano,micro}.regime_max_lots` (lot-based) 并存, 存在死代码路径
8. **sleeve_compose 死代码** — L88 `factor_count[sym] = factor_count.get(sym, 0)` 被 L89 立即覆盖
9. **VaR 参数方法假设正态** — A 股厚尾/波动聚集, parametric VaR 在市场压力下低估尾风险
10. **技术选型缺口**: Redis (IPC), Airflow (调度), TimescaleDB (时序), Prometheus+Grafana (监控)

### §6 算法优化 3 项 (CODE-REVIEW-2026-08-07.md)

**HRP 簇内 IVP**:
- 原: 簇内等权 (alpha/len(left))
- 修复: De Prado (2016) 原文 IVP — 权重 ∝ 1/σ²_i

**Ledoit-Wolf NaN 上游修复**:
- 原: 含 NaN 的列直接传入 covariance → 全矩阵 NaN
- 修复: covariance_subset 入参前剔除含 NaN 的列

**HMM 回测量纲对齐**:
- 原: backtest `_bm_rets * 100`, live 路径不缩放 → HMM 特征量纲不一致
- 修复: 去掉 *100, 与 live 路径统一

### 关键指标: 233 tests, Web test-v414

---

## 当前状态 (test-v413, 2026-08-07)

### §5 逻辑错误修复 3 项 (CODE-REVIEW-2026-08-07.md)

**#4 rebalance cash=0**:
- 原: cash=0 时 `available_cash = capital` → 把总资产当现金, 超买
- 修复: `max(cash, 0)` — 没钱就不买

**#5 _log_seal 先使用后定义**:
- 原: for 循环内 `_log_seal.debug(...)` 在 L159 定义之前
- 修复: logger 定义移到循环之前

**#8 执行日跌停卖出预检**:
- 原: 只检查涨停(封板不买), 不检查跌停(封板不卖)
- 修复: execute.py 新增跌停预检, bid_vol=0 时跳过卖出

### 关键指标: 233 tests, Web test-v413

---

## 当前状态 (test-v412, 2026-08-07)

### §4 架构优化 4 项 (CODE-REVIEW-2026-08-07.md)

**1. 依赖倒置 — state_broker 移到 quant/core**:
- web/state_broker.py → quant/core/state_broker.py
- 所有 import 从 `from web.state_broker` → `from quant.core.state_broker`
- web/state_broker.py 保留为薄重导出 (兼容旧路径)
- web/shared.py 同样重导出

**2. 死代码清理**:
- 删除 web/index_fix.py (0 引用)
- 删除 web/state_pusher.py (0 引用)
- 删除 quant/data/schema.sql (DDL 全在 ensure_tables)

**3. 配置注释清理**:
- config.yaml 删除 5 行历史注释 (tier_*_cap, nano_cap, rebalance_freq)

**4. DB 路径统一 — 10 处**:
- 全部改为 `from quant.config.paths import MARKET_DB/TRADE_DB`
- 消除硬编码 os.path.join(..., "data", "market.db")

### 关键指标: 233 tests, Web test-v412

---

## 当前状态 (test-v411, 2026-08-07)

### §3 业务断点 6 项修复 (CODE-REVIEW-2026-08-07.md)

**1. 失败重试 — evening sys.exit(1)**:
- except 块 break + 最终 os.environ[_EVENING_SUBPROCESS]==1 时 sys.exit(1)
- orchestrator 设 _EVENING_SUBPROCESS=1, 检测 ret≠0 → 重试 (上限2次)

**2. 质量门禁 error→abort**:
- qr["overall"]=="error" → break 出链, status=failed

**3. daily_equity 写入**:
- reconcile._run 成功后调用 TradeRepo.record_daily_equity()
- 回撤告警 + Sharpe 计算从此有数据

**4. pipeline errors metric**:
- signals._run except 块加 _m.inc("pipeline.errors")

**5. 0 信号误报**: ✅ v400 已修复

**6. 回测↔实盘止损一致**:
- BacktestExecutionModel 注入 rm.check() (ATR 止盈止损)
- risk_only 路径同样加 ATR 检查

### 关键指标: 233 tests, Web test-v411

---

## 当前状态 (test-v410, 2026-08-07)

### §2 归因闭环 + 基准闭环修复 (CODE-REVIEW-2026-08-07.md)

**1. Brinson Rp 同源修复**:
- 原: Rp 和 Rb 都从 sector_returns 取值 → 选股效应恒为 0
- 修复: Rp 从实盘持仓日收益计算, 按行业加权; Rb 保持市值加权

**2. factor_attribution bps 单位修复**:
- 原: contribution_bps = abs(exposure × ic_mean) × 100 → 实际是%不是bps
- 修复: ×10000 (1% = 100 bps)

**3. 基准闭环接线 + Web 端点**:
- compute_rolling_metrics 加入 attribution 末尾 (每日更新 alpha/IR/beta)
- Web 新增 /api/benchmark 端点

### 估值连续性问题
- daily_valuation 2026-04~06 缺失 (仅4/1-4/2有数据)
- 需执行: PYTHONPATH=. .venv/bin/python3 -c "from quant.data.em_valuation import sync_range; sync_range('2026-03-15','2026-06-30')"

### 关键指标
- 因子: 100 注册, Web: test-v410

---

## 当前状态 (test-v409, 2026-08-07)

### §1 技术选型 6 项全量修复 (CODE-REVIEW-2026-08-07.md)

**1. SSE 跨进程 — JSON 文件桥**:
- state_broker.update() 写 `/tmp/quant_state_bridge.json`
- state_broker.get() 读文件桥取 pipeline progress/signals
- 财务数据仍从 trades.db 读取 (天然跨进程)

**2. 告警通道 — orchestrator CRITICAL 日志 + metrics**:
- 任务 abort 时 inc alerts.task_aborted.{task_name}
- 回撤 <-20% → CRITICAL, <-10% → WARNING
- 数据滞后告警 CRITICAL 日志

**3. 交易日历 — 2026 端午确认**:
- "2026-06-22"(保守) → "2026-06-19"(周五端午, 已确认)

**4. 数据源开关 — tencent/akshare 摘除**:
- config.yaml data.source_policy.enabled: tencent/akshare=false
- store.py 回退链检测开关, 跳过已封禁源节省 12-25s/批

**5. 批量 INSERT — margin.py executemany**:
- _sync_sse_raw + _sync_szse_wrapper 逐行 INSERT → executemany

**6. 测试基建 — smoke tests**:
- test_smoke_v408.py: web/qlib/benchmark 导入+基本调用
- 233/233 passed

### 关键指标
- 因子: 100 注册, Web: test-v409

---

## 当前状态 (test-v408, 2026-08-07)

### P0-5: LGB 分块训练恢复单次全量 fit

**OOM 历史**: v275 (7/30) 21.8M×75特征×float32 → >25GB → macOS OOM kill → 分块训练引入。

**v408 回归单次 fit**: v398 内存优化 + 特征数从75→29后, X仅 ~1.1GB,
fit() 峰值 ~3-4GB, 16GB M1 安全。即使100特征全量回测, 峰值 ~11.5GB 仍安全。
n_estimators 保持 200, 删除分块循环 + init_model 串联。

### 关键指标
- 因子: 100 注册, Web: test-v408

---

## 当前状态 (test-v407, 2026-08-07)

### 评估链路修复 (P0-1) + DSR (P0-3) + qlib 树膨胀 (P0-5)

**P0-1 Phase2→3 状态键名不匹配**:
- v346 已将 Phase2 输出键从 passed/monitoring/failed → active/probation/archived
  对齐 factor_registry 四态, 但 Phase3/Phase5 未同步更新, 仍读旧键 'passed'
- 修复: phase3 L39 `'passed'` → `'active'`, phase5 L34 `"passed"` → `"active"`
- 不改 Phase2 (它 v346 时已经是正确的)

**P0-3 DSR 三重 bug**:
- 传 numpy 数组当 float, 缺 n_obs 参数, 返回 dict 当 tuple 解包
- 三层错误全被 `except: return None` 吞掉 → DSR 永远 None
- 修复: 先计算年化 Sharpe, 正确传参, 从 return dict 取 dsr

**P0-5 qlib 分块训练树膨胀**:
- 分块训练每批 init_model 追加 200 棵树 → 3 批 = 600 棵树过拟合
- "数学等价"注释错误: 不同数据子集 extra trees 不等价于全量 boosting
- 修复: n_estimators 200→100, 删除分块循环, 单次 fit(X, y) (v398 内存已够)

### 关键指标
- 因子: 100 注册, Web: test-v407

---

## 当前状态 (test-v406, 2026-08-07)

### 代码审查 P0 修复 (8 项) — docs/reports/CODE-REVIEW-2026-08-07.md

- **P0-2** Phase8 NameError+TypeError: factor_store 定义, run_backtest +suppress_push
- **P0-4** qlib fillna(0) 移到 mask 之后 (无收益股不打 0 标签)
- **P0-6** qlib 预测列序严格按 training feature_names 对齐
- **P0-7** dividend_yield: div / close_latest (原是股息额未除股价)
- **P0-8** get_daily 缓存键: hash(full symbols) 替代 [:200] 截断
- **P0-9** backfill: volume 股→手(÷100), turnover 不除 10000
- **P0-10** Kelly: 删第二次归一化 (与第一次抵消, fractional 空操作)
- **P0-12** 冲击价后更新 o.cost (防现金为负)
- P0-11 已验证非 bug (mode 已传入 SQL)

### 关键指标: 100 注册因子, Web test-v406

---

# HANDOFF — 盈迹 (quant) 项目变更日志

> **修改前**: `grep -rn "关键词" HANDOFF.md docs/adr/` 联动搜索，避免重复踩坑。

## 当前状态 (test-v404, 2026-08-06)

### factor_cache 物化全链路 Bug 修复 (6 项)

**背景**: 全量审计物化流程发现 6 个 Bug, 导致新因子无法入库、旧因子残留、
代码变更不触发重算、文件清理不彻底。

**Bug 1 (P0) — `is_materialized` 用错检查函数**:
- 调 `_date_has_data()` (只看文件是否存在) 而非 `_date_has_all_factors()` (检查因子完整)
- → 新因子注册后, 旧缓存文件被误判为"已物化", materialize 全跳过
- 修复: L663 `_date_has_data` → `_date_has_all_factors`

**Bug 2 (P1) — Chunk 跳过也只检查文件存在**:
- materialize L201-213 快速跳过逻辑只检查 `os.path.exists`, 不看因子完整性
- → Bug 1 修复后 is_materialized 放行, 但 chunk skip 仍拦住
- 修复: 改为抽查首尾日期 `_date_has_all_factors()`

**Bug 3 (P1) — 旧因子缓存残留, merge 不 prune**:
- `_write_chunk_rows` 合并新旧行时只加不删, 已 archived 因子行永久残留
- → 3 个 archived 因子 (alpha012_vol_dir/ideal_amplitude/limit_touch_no_seal) 残留在缓存
- 修复: 合并时过滤 `factor_names` 外的因子行

**Bug 4 (P1) — manifest source_hash 变更不触发重算**:
- `_get_existing_factors` source_hash 不匹配时只 log, 仍返回旧 factors → missing=[] → 不重算
- → 因子代码修复后缓存值永久过时
- 修复: source_hash 不匹配 → return `set()` → 触发重算

**Bug 5 (P2) — trim 不清理 manifest**:
- `trim_to_max_days` 只删 CSV, 不删 `.manifest.json`
- → 孤立的 manifest 文件堆积
- 修复: 删除 CSV 时同步 `os.remove(manifest_path)`

**Bug 6 (P2) — merge 旧值优先, 新值被丢弃**:
- 旧逻辑 `if line not in existing_set: existing_lines.append(line)`
- → 同 (symbol, factor) 的旧值保留, 新值丢弃 (因子代码修复后缓存不更新)
- 修复: 改为 dict[(symbol,factor)] → 新值覆盖旧值

### 关键指标
- 因子: 100 注册 (0 active, 24 evaluating, 14 probation, 62 archived)
- _PRICE_FN_MAP: 72 条目
- Web: VERSION=test-v405

### 需要重跑物化
lhb_detail 修复 + limit_up/lhb 纳入晚间链后, 需重跑物化验证 lhb_reversal_5d:
```bash
PYTHONPATH=. .venv/bin/python3 -c "
from quant.scheduler.factor_cache import _run
_run('2026-08-05', '2026-08-05')
"
```
_run('2026-08-05', '2026-08-05')
"
```

---

## 当前状态 (test-v403, 2026-08-06)

### 数据完整性修复 — 因子注册 + 缺失实现 + prev_close 填充

**背景**: 全量数据审计发现 9 个因子存在注册/实现/数据源问题:
- 4 个在 `_PRICE_FN_MAP` 但未注册到 `factor_registry` (intraday_reversal/open_volume_ratio/
  close_surge/alpha033_gap) → pipeline 永远不加载它们
- 5 个在 `factor_registry` 注册为 evaluating 但 `_PRICE_FN_MAP` 无对应 compute 函数
  (abn_turnover_resid/overnight_gap_ratio/price_channel_position/qlib_vema/wq_alpha_006)
  → factor_cache 物化时跳过
- intraday_snapshot.prev_close 列永不为0 → intraday_reversal 即使注册也静默失败

**修改 1 — intraday_snapshot.prev_close 填充 (P0)**:
- `snapshot.py`: `_fetch_batch` 新增 prev_close 字段 (腾讯 fields[4]=昨收)
- `snapshot.py`: INSERT 语句新增 prev_close 列

**修改 2 — 注册 4 个未注册因子 (P0)**:
- `factor_registry`: intraday_reversal/open_volume_ratio/close_surge/alpha033_gap
  → 注册为 evaluating (等60天快照积累后评估)

**修改 3 — 实现 5 个缺失 compute 函数 (P1)**:
- `_alternative.py`: 新增 5 个函数:
  - `compute_abn_turnover_resid`: 换手率截面残差 (华泰2022)
  - `compute_overnight_gap_ratio`: 隔夜跳空比率 (华安2020)
  - `compute_price_channel_position`: Donchian价格通道位置 (Donchian 1960)
  - `compute_qlib_vema`: 量权EMA偏离 (Qlib 2020)
  - `compute_wq_alpha_006`: WorldQuant Alpha#6 价量相关 (WorldQuant 2015)
- `price/__init__.py`: import + `_PRICE_FN_MAP` 条目

**数据回填命令 (P1: limit_up_pool + factor_cache)**:
```bash
# limit_up_pool 回填 7/9 至今
PYTHONPATH=. .venv/bin/python3 quant/data/limit_up.py 2026-07-09 2026-08-06

# factor_cache 重物化 (含新注册的 9 个因子)
PYTHONPATH=. .venv/bin/python3 -c "
from quant.scheduler.factor_cache import _run
_run('2026-08-05', '2026-08-05')
"
```

### 已知待处理
- `lhb_reversal_5d` (probation) 7/31 后从缓存消失 — 等晚间链重物化后观察
- 3 个 archived 因子 (alpha012_vol_dir/ideal_amplitude/limit_touch_no_seal) 缓存残留
  — P2, 需增加物化清理步骤
- 0 active 因子 — 需推进评估管线 (evaluating→probation→active)

### 关键指标
- 因子: 100 注册 (0 active, 24 evaluating, 14 probation, 62 archived)
- _PRICE_FN_MAP: 72 条目 (新增 5)
- Web: VERSION=test-v403

---

## 当前状态 (test-v402, 2026-08-06)

### snapshot_open 触发时间 09:30→10:00 修正

**问题**: snapshot_open 在 09:30 执行, 拉取腾讯实时行情写入 `open_30min` 列。
但 09:30 市场刚开盘, 实际拉到的是开盘价, 不是"开盘30分钟后"的价格。
`intraday_reversal` 因子公式 `-(open_30min/prev_close - 1)` 因此退化为隔夜缺口因子,
缺少 30 分钟价格发现过程, 经济含义偏差。

**修改**:
- `orchestrator.py`: 触发条件 `hhmm >= time(9, 30)` → `time(10, 0)`
- `status.py`: 注册描述 `09:30 (execute后)` → `10:00 (execute后)`
- `snapshot.py`: 模块 docstring 更新

**尾盘快照 14:55 确认正确**:
A股 14:57 进入收盘集合竞价, 14:55 是连续竞价最后一笔有效成交价。
`close_surge` 因子计算 `-(收盘价 - 14:55价)/全天振幅` 衡量尾盘异动,
这是 AQR/WorldQuant 标准做法, 时点选择正确。

### 关键指标
- 因子: 96 注册 (0 active, 20 evaluating, 14 probation, 62 archived)
- Web: VERSION=test-v402

---

## 当前状态 (test-v401, 2026-08-06)

### Micro 层 regime sizing 统一为 lot-based cap (Critical)

**背景**: test-v400 将 Nano 层 regime sizing 从 capital-based 改为 lot-based，但 Micro 层
仍保留 `_apply_regime_sizing()` (capital × multiplier) + fallback 绕过 →
前后不一致: 同一 sideways 市场中, Nano 层限制每只 1 手, Micro 层因 fallback
可能完全不限手数。

**修改 — Micro 层统一为 lot-based (方案 A 的完成态)**:
- `config.yaml`: 新增 `optimizer.micro.regime_max_lots: {bull: 999, sideways: 5, bear: 2}`
- `portfolio.py`: 废弃 `_get_nano_regime_max_lots()`, 统一为 `_get_regime_max_lots(tier, regime_label)`
- `portfolio.py`: Micro 层 `construct()` 从 capital×regime 改为 `max_lots_per_stock` 手数上限
- `portfolio.py`: `_score_weighted_rounding()` 新增 `max_lots_per_stock` 参数, 计算后 `min(n_lots, cap)`
- `portfolio.py`: `_equal_weight_greedy()` 新增 `max_lots_per_stock` 参数, `max_lots_per = min(..., cap)`
- `portfolio.py`: Micro fallback 加 try/except ValueError — 防御 greedy 0-lots crash
- `portfolio.py`: `_apply_regime_sizing()` 保留函数体标记废弃, 供回测兼容
- `test_portfolio.py`: 新增 `TestMicroRegimeLotCaps` (6 个测试): 震荡5手/熊市2手/牛市不限/
  unknown不限/None不限/greedy fallback 防御

**三层 regime 策略对比 (最终态)**:

| Tier | regime 策略 | 配置来源 |
|------|-----------|---------|
| Nano | 每只股票最多 N 手, 不缩资本 | `optimizer.nano.regime_max_lots` |
| Micro | 每只股票最多 N 手, 不缩资本 | `optimizer.micro.regime_max_lots` |
| Small | Kelly fraction × regime | `_regime_kelly_fraction()` (v397) |

**决策依据**: Nano/Micro 层瓶颈是离散约束 (1手=100股), capital scaling
在资金低于 1手成本时直接空仓 → lot cap 是唯一可持续方案。
Small 层资金量充分 (≥¥100K), Kelly 公式的连续分配成立。

**测试**: 全量 227 passed, 新增 6 测试全绿。

### orchestrator `_run_task` 回读 DB 确认状态 (方案 D)

**背景**: `execute._run` 在 no-signals 分支 early-return (不抛异常), finally 写 status=failed,
但 orchestrator `_run_task` 只看异常 → 日志 STATUS=OK, 实际 DB failed, 前后不一致。

**修改**: `_run_task` 在 `fn()` 正常返回后回读 `task_runs` 表确认真实状态:
- DB="ok" → STATUS=OK
- DB="failed" → STATUS=FAILED (DB)
- DB="aborted" → STATUS=ABORTED (DB)
- 异常仍然走原 STATUS=FAILED 路径。

---

## 当前状态 (test-v400, 2026-08-06)

### regime sizing 挪入 construct() 内部 + 0-target 持久化 (Critical)

**背景**: 2026-08-04~06 连续 3 天 execute 任务报"今日失败"(error=no signals)。
根因链: HMM regime=sideways → v309 外部缩资 ¥5K×0.6=¥3K → ¥3K < nano_cap(¥10K)
→ nano tier → cheapest_lot≈¥3K+ → 买不起任何1手 → ValueError 被 v380 静默吞掉
→ 0 positions → save_signals 跳过 (guard: `if targets`) → daily_signals 仍是3天前数据
→ execute 读到的 date≠today → targets=[] → "no signals".

**修改 1 — regime sizing 挪入 construct() (方案 A)**:
- `pipeline.py`: 删除 `_sizing_capital` 外部缩资, 传原始 `total_capital` 进 `construct()`
- `portfolio.py`: `construct()` 按 tier 分别处理 regime:
  - Nano: 不缩资本, 改为限制每只股票手数 (`max_lots_per_stock`, config: `optimizer.nano.regime_max_lots`)。震荡/熊市→1手, 牛市→999(不限)
  - Micro: 调整分配资本上限 (`_apply_regime_sizing()`), 不改变 tier 判定
  - Small: 已有 `_regime_kelly_fraction()` (v397 Problem 7)
- `portfolio.py`: Nano 层 ValueError 恢复 re-raise (修复 v380 违反 ADR-032 反模式 #4)
- `config.yaml`: 新增 `optimizer.nano.regime_max_lots: {bull: 999, sideways: 1, bear: 1}`
- 新增辅助函数: `_get_nano_regime_max_lots()`, `_apply_regime_sizing()`

**修改 2 — 0-target 也写 daily_signals (方案 B)**:
- `pipeline.py`: `if targets and not suppress_push` → `if not suppress_push`, 写 `targets or []`
- 0-target 日写空数组 `[]`, execute 读到的 date=today, targets=[], 不会回退到3天前数据

**历史追溯**:
| 版本 | 日期 | 变更 | 影响 |
|------|------|------|------|
| ADR-032 | 07-15 | 三层资本分段, 反模式 #4: 0仓位必须暴露 | 基线 |
| v181 | 07-21 | `_rank_concentrated` 引入 | — |
| v309 | 07-31 | `get_regime_sizing()` sideways×0.6 外部缩资 | **引入 capital×regime 交互** |
| v380 | 08-03 | Nano ValueError 从 re-raise 改为静默吞掉 | **违反 ADR-032 反模式 #4** |
| v397 | 08-04 | nano_cap ¥30K→¥10K | 意图让¥5K可交易 |
| v398 | 08-04 | save_signals 加 `and not suppress_push` | — |
| v400 | 08-06 | **本次修复** | — |

### 关键指标
- 因子: 96 注册 (0 active, 20 evaluating, 14 probation, 62 archived)
- 数据: 2019-2026 日线 + daily_valuation 678万行
- Web: VERSION=test-v400

---

## 当前状态 (test-v399, 2026-08-05)

### factor_snapshot 缓存失效修复

**`get_cached_factor_stats` — 缓存感知数据变化 (High)**
- 原: 纯 24h TTL 判断, 不感知 factor_cache 文件变化, factor_cache 物化后仍返回旧空数据
- 改: 检查 factor_cache/*.csv.gz 最新 mtime, 比 snapshot 新则自动失效重算

**`compute_factor_stats` — 因子范围扩大 (Medium)**
- 原: 默认 status_filter='backtesting' 仅覆盖 evaluating+probation
- 改: 默认 ('active','probation','evaluating') 全量非归档因子

---

## 当前状态 (test-v398, 2026-08-05)

### 因子归因阈值对齐业界标准 (Critical)

背景: 全量数据回填完成后 96 因子 0 active, 审计发现三处偏离业界实践。
详见项目记忆 `project/factor-eval-full-audit-2026-08-05`。

**attribution.py L1 — 单日 IC → 5 日滚动均值 (High)**
- 原: `vals[-1]` 单日 IC vs 60d 均值, 偏离 >30% 告警 → 日噪声误杀 active→probation
- 改: 近 5 日滚动均值, 窗口由 config `attribution.l1_rolling_days` 控制
- 依据: Grinold & Kahn (1999) 月频; WorldQuant 101 Alphas 周频

**config oos_recovery_threshold — 1.5→0.7 (High)**
- 原: OOS_IR > IS_IR × 1.5 才能 probation→active (变量反超, 不现实)
- 改: 0.7 (AQR 20-for-20 2018: 淘汰条件 <0.2, 恢复 >0.5; OOS/IS>0.7 已属优秀)
- 依据: AQR, Two Sigma Factor Lens (2021)

**attribution.py L3 — t-test |t|<1.0→|t|<2.0 (High)**
- 原: |t|<1.0 (~68% 置信, p≈0.32) 即归档 (过于激进)
- 改: |t|<2.0 (~95% 置信, p≈0.05)
- 依据: De Prado (2018) Ch.7

**层面二评估管线阈值 — 已审计, 暂缓**
- pbo_max 0.20→0.10, dsr_degraded 0.50→0.80, net_sharpe 0.30→0.50, min_oos_points 5→20
- 暂缓原因: 当前 0 active, 加严评估管线会使新因子更难通过; 等有 active 后再对齐

### 关键指标
- 因子: 96 注册 (0 active, 20 evaluating, 14 probation, 62 archived)
- 数据: 2019-2026 日线 + daily_valuation 678万行 + lhb_detail 10万行 + financials 5万行
- adj_factor: 4924/5208 覆盖 (94.5%)
- 因子缓存: 物化 1841 日期 × 32 因子 × 5208 符号 → 1.99 亿行

---

## 当前状态 (test-v397, 2026-08-04)

### 回测策略全链路审计修复 (12 项问题)
详见 docs/reports/backtest-strategy-audit-2026-08-04.md。

**第二轮审查修复 (test-v397, 2026-08-04):**
- Bug: `get_regime_weights` 对 dict ic_map 做 `*=` 崩溃 — 入口展平 dic→float
- Bug: `_apply_turnover_constraint` 丢弃 diff=0 持仓 — no_change_syms 自动保留
- Bug: pipeline.py probation decay `_log` 未定义 — 改为 `logger`
- 优化: factor_cache 预加载移到初始 IC 之前, 首次 IC 也走内存

**性能优化 (P0/P1, test-v397):**
- P0: FactorStore.bulk_load() 回测启动时一次性加载全量因子值到内存 (~47MB), 消除逐日 gzip I/O + IC 重算时的 720 次文件打开
- P0: generate_signals / compute_backtest_ic / run_oos_check 支持 factor_cache 参数, 内存命中跳过 gzip
- P1: neutralize_factors_batch() — 预构建投影矩阵 P 一次, 30 因子共享, 替代逐因子 lstsq (中性化 ~30x 加速)
- 预期 1 年回测从 ~170s 降到 ~35s (5x)

**结构修复:**
1. **协方差 N>T** — portfolio.py Small 层改用 covariance_subset() 对 top-K(30) 股票子集计算, 保证 T(252) > K(30) 矩阵良态
2. **TC 后置→再分配** — _apply_tc_band 拦截后调用 _rebalance_after_tc() 按权重比例再分配闲置现金
3. **中性化前置** — pipeline.py Step 3: 因子级独立行业+市值中性化 (Barra USE4), 再合成, 全局中性化作二次保险
4. **Sleeve mean-rank** — sleeve_compose() 改用多因子入选计数+平均rank分位 (替代 max z-score)
5. **IC PIT 加固** — compute_backtest_ic() 日志显式标注 PIT 日期截止
6. **止损已验证** — 执行时序正确, 无需修改
7. **Kelly regime** — kelly.py 新增 `_regime_kelly_fraction()`, 根据 regime_label 动态调整 fraction (bull=0.8, sideways=0.5, bear=0.2), 通过 construct→_kelly_greedy→compute_lot_allocation 链路传入
8. **OOS 隔离** — run_backtest(oos_start_date=...) 冻结参数并分别报告 IS/OOS 指标
9. **换手率约束** — _apply_turnover_constraint() 全局换手超限时按成交量裁减
10. **σ_daily 差异化** — _stock_sigma() 从 log_returns 取单股波动率替代硬编码 0.02
11. **VaR 活代码** — 从 pipeline.py 死代码移至 portfolio.py Small 层 covariance 后实时检查
12. **参数对齐** — nano_cap 30K→10K, tc_horizon_days 1→5, 新增 max_turnover_ratio=999

### 关键指标
- 因子: 84 注册 (0 active, 20 evaluating, 25 probation, 46 archived — 8 个新注册待评估)
- 数据: 2019-2026 日线, 5208 只 (2019: 3551 只)
- adj_factor: 4924/5208 覆盖 (94.5%), 2020-01-02 起

## 当前状态 (test-v370, 2026-08-03)

### 关键指标
- 因子: 84 注册 (0 active, 20 evaluating, 25 probation, 46 archived — 8 个新注册待评估)
- 数据: 2019-2026 日线, 5208 只 (2019: 3551 只)
- adj_factor: 4924/5208 覆盖 (94.5%), 2020-01-02 起
- 因子缓存: 物化性能优化 A/B/C2 落地; C1(parquet 列存) 保留设计, 待评估后实施
- Alpha 模型: 新增 XGBoost 后端 (combine_mode='xgb'), 与 LightGBM 平行
- 回测: 冒烟通过 (CAGR=-23.5%, 0 errors, avg 2.2 信号/天), 全量待跑
- scheduler: orchestrator (16 tasks, ~85MB) + cron (清空)
- 测试: validate_factors.py 100/100 通过; pytest factor 相关 47/47 通过; pytest alpha/model 19/19 通过

### 晚间链流程
```
19:00 daily_data → adj_factor → factor_cache → attribution → lgb_train(Mon/Thu)
```

### test-v310→v366 变更总览

**v370**: 启动时自动清理僵尸行 — restart 后不再需要手工清 DB

| 问题 | 修复 | 文件 |
|------|------|------|
| restart 后旧进程残留行 (aborted/failed/running/lunch) 阻塞新进程 | _cleanup_zombie_tasks 重写: dead-PID 非ok行直接DELETE, 不标aborted | `orchestrator.py` |
| lunch 状态掩藏僵尸 (Bug B) | cleanup 覆盖所有非ok状态, 不再只查 running | `orchestrator.py` |
| _set_monitor_stage 覆写所有行 (Bug C) | 按 pid 精确更新当前实例的行 | `monitor.py` |
| aborted 消耗重试预算 (Bug D) | _get_monitor_failures 只计 failed; + cleanup直接DELETE死进程行 | `orchestrator.py` |

**v369**: 盘中风控 monitor 超时误杀修复 (limit 1800→21600 + stop_event + _tk_finish防御)

**v368**: 因子缓存物化分步计时埋点 (load/prim/aux/compute/write)

**v367**: 因子缓存物化性能优化 Round 1+2 — 预计全量 4-8h → 1.5-3.5h

| 编号 | 项目 | 文件 | 预计节省/chunk |
|------|------|------|---------------|
| R1.1 | ztd ×5 冗余: worker_init 删 preload_ztd_cache (fork 已继承) | `store.py` | 60-180s |
| R1.2 | 杀死 primitives: 删除 roll_high/low/min_pct/vol_ma/amt_ma (5族×~10窗=~50 rolling) | `_primitives.py` | 15-20s |
| R1.3 | market_beta 复用 prims benchmark_ret (消除 per-date SQL) | `_dispatch.py` | ~5s |
| R1.4 | fundamentals panel 复用 data_full close (消除 daily SQL+pivot+ffill) | `store.py` | 5-10s |
| R1.5 | mean_log 去重: 仅保留 w=20 (uret), cum_log 已覆盖动量/反转 | `_primitives.py` | ~5s |
| R1.x | close.pct_change() 去重: 3→1 (复用 pct_ret) | `_primitives.py` | ~3s |
| R2.1 | ctr_20d 全向量化: per-symbol for loop → DataFrame numpy 广播 (~100x) | `_turnover.py` | 400-600s |
| R2.2 | zt_streak/dt_streak 全向量化: per-symbol 双层循环 → pandas 布尔矩阵 | `_event.py` | 200-400s |
| R2.3 | _turnover_reversal 用 turnover_ma 原语替代每日期 to.rolling | `_primitives.py` | ~60s |
| R1.x | news 因子 aux 预加载基础设施 (news_daily_count → aux) | `_preload.py` | 后续激活 |

**v366**: P0 因子正确性修复 (Piotroski F-Score/turnover_anomaly/idio_vol/cf_roa/YoY/pe_ttm)

**v365**: (skip — VERSION bump only)

**v364**: 3 个存量问题修复

| 问题 | 修复 | 文件 |
|------|------|------|
| multiprocessing freeze_support (Python 3.14) | ProcessPoolExecutor 显式 `mp_context='fork'` | `store.py` |
| seasonality_12m_1m/tail_risk 多余 `aux` 参数 | 移除未使用的 `aux=None` | `high_priority.py` |
| worker OOM (factor_cache 多进程崩溃) | 因子>50 时 `max_workers` 4→2 + OOM 保护 | `store.py` |

**v363**: 全架构审计 8 项修复 (P0-P3)

| 编号 | 项目 | 文件 | 状态 |
|------|------|------|------|
| P0 | 因子注册签名校验 | `_registry.py` | ✅ |
| P1a | task_runs 装饰器 | `task_log.py` + `snapshot.py` | ✅ |
| P1b | 物化 source_hash 变更检测 | `store.py` | ✅ |
| P2a | 数据质量门禁 | `data/quality.py` + `evening.py` | ✅ |
| P2b | 成本模型接入模拟成交 | `execution_model.py` | ✅ |
| P3a | 因子协方差/冗余检测 | `alpha/model.py` | ✅ |
| P3b | 物化断点续传 | `store.py` | ✅ |
| P3c | 因子 golden 测试集 | `factor/golden_test.py` | ✅ |

**v362**: 4 个基本面因子物化静默失败修复
- 根因: `revenue_growth_yoy`, `earnings_growth_yoy`, `piotroski_fscore`, `cf_roa`
  函数体 `data["close"].columns` 只兼容 MultiIndex, 但 _FUNDAMENTAL_FN_MAP 调度
  传入简单 DataFrame (symbol index, 无 "close" 列) → KeyError → 静默吞错
- 修复 (保留 _FUNDAMENTAL_FN_MAP 归属):
  - 4 函数 `symbols` 提取改为 `isinstance(data.columns, pd.MultiIndex)` 双分支:
    MultiIndex → `data["close"].columns.tolist()`, 简单 DF → `data.index.tolist()`
  - 与同文件 `compute_market_beta_60d`/`compute_overnight_gap_5d` 一致模式
  - 保留 `window=None` 参数 (price dispatch 兼容, 不使用时无副作用)
- 附带: `_str` shortcut 文档标注市值中性化省略

**v361**: orchestrator monitor 崩溃后不重启修复
- `quant/scheduler/orchestrator.py`:
  - `monitor_done` 从 `("ok", "failed")` 改为仅 `"ok"` — monitor 是持续 daemon，"failed" 应重启而非放弃
  - 新增 `_get_monitor_failures()` 统计当日累计 failed+aborted 次数, 达 `_MAX_TASK_RETRIES` 上限后放弃
  - 根因: v313 引入局部 import 导致首次崩溃 → task_runs 写 "failed" → 原逻辑视 "failed" 为完成 → 全天不再重启

**v360**: Bug 修复 — monitor TradeRepo 作用域冲突 + snapshot 死循环
- `quant/scheduler/monitor.py` L253: 删除冗余局部 `from quant.data.repos.trade_repo import TradeRepo`
  - Python 编译时发现函数内局部 import 赋值 → 全函数 TradeRepo 视为局部变量
  - L104 `TradeRepo().get_flag("circuit_breaker")` 时尚未赋值 → UnboundLocalError
  - 顶层已有 `from quant.data.repos import TradeRepo`, 局部导入冗余
- `quant/scheduler/snapshot.py`: `snapshot_open`/`snapshot_close` 添加 `_tk_start/_tk_finish` 写入 task_runs
  - 修复: 未写 DB 导致 orchestrator 每 30s 无限重复触发 (日志确认重复 50+ 次)
  - 新增 `_snapshot_with_log` 包装函数, 含模板 9 日志埋点 (entry/exit/exception + elapsed)

**v359**: XGBoost Alpha 模型后端
- 新增 `quant/alpha/xgb_model.py`: `XgbAlphaModel` (train/predict/save/load/feature_importance)
- `AlphaModel.combine()` 支持 `combine_mode='xgb'`, 未训练/未安装时自动回退 `ic_weighted`
- `quant/config/config.yaml` 新增 `alpha.xgb` 参数块 (reg_lambda=1.0, max_depth=5, subsample=0.8 等量化专用超参)
- 复用 `build_forward_returns` 与因子缓存加载逻辑, API 与 `LgbAlphaModel` 一致
- 测试: pytest alpha/model 19 通过; 模块导入/配置解析正常
**v358**: ADR-043 layer2 — 8因子shortcut化, 消除non-shortcut对MultiIndex data的依赖
**v357**: ADR-043 layer1 — 10因子aux覆盖, 消除per-date DB泄漏
- A1: alpha035 `rolling.apply` → numpy 向量化 ts_rank, 单因子 4.8s/日 → 0.4s/整块
- A2: shortcut 因子预计算整块 zscore panel, 物化时直接取行
- A3: CSV 由逐日 gzip 解压-压缩改为 chunk 级批量合并写
- A4: precompute_primitives 按需计算窗口, 非全量 183 张表
- B1: chunk 内逐日计算支持 ProcessPoolExecutor(max_workers=4)
- B2: 基本面 PIT 由逐日循环改为向量化 panel (pivot + rolling)
- C2: 新增日期级 manifest.json, is_materialized 读清单而非扫描文件
- 新增脚本 `scripts/benchmark_factor_cache.py`: 用临时缓存目录跑 chunk 1 基准, 不污染生产缓存, 输出全量 9 chunks 估算
- 新增脚本 `scripts/run_factor_cache_chunk1.py`: 清空生产缓存并正式跑 chunk 1 (2019-06-03 -> 2020-03-26), 输出全量估算
- C1: **parquet 列存 — 经分析后决定不实施**
  - 基准 (22 factor × 5208 symbol / 日): gzip CSV 784 KB, parquet snappy 1266 KB (+61%), parquet f32+brotli 759 KB (-3%)
  - 写速度 parquet 快 9x, 但物化瓶颈仍是 compute (小时级), 写仅占分钟级
  - 读场景: pipeline 用 `factor_names=None` 全读, 条件读 pushdown 用不上
  - 迁移成本高 (1.8GB 现存缓存 + 双格式兼容), gzip CSV 零依赖、易调试
  - 结论: 当前 ROI 低, 保留为候选; 未来因子 >300 或 pipeline 改为部分因子读取时再评估
**v355**: (已并入 v356 统一归档)
**v310-v312**: 界面显示市场状态 + hmmlearn 依赖修复
**v313**: ATR 峰值持久化 position_meta
**v314**: 消除全部 except:pass (30+ 处)
**v315**: ADR-042 归档被拒绝的架构/算法建议 (24 项 + 3 决策原则)
**v316**: adj_factor 从 cron 迁入 orchestrator → 回退 (晚间链原则)
**v317**: adj_factor 迁入晚间链子进程, cron 清空
**v318**: fix _next_scheduled_time 非时间格式崩溃
**v319**: 调度描述链式依赖顺序修正
**v320**: 对账正常=绿色 异常=红色 (A 股红涨绿跌)
**v321**: 日志定期清理 (启动 7 天 + weekly 14 天)
**v322**: factor_curator 修复 — 日期 Timestamp/索引重复/source (22→4 注册)
**v323**: 4 核心缺失因子 (market_beta_60d, overnight_gap_5d, vol_price_sync_20d, revenue_growth_yoy)
**v324**: 日内反转因子 + 9:30 快照表 + snapshot 任务
**v325**: 成长因子 (earnings_growth_yoy) + Piotroski F-Score (9 项质量打分)
**v326**: Alpha101 7 个最高优先级因子
**v327**: 快照加成交量 + 开盘成交量占比因子
**v328**: 尾盘快照 (14:55) + 尾盘异动因子
**v329-v330**: 状态栏加日期+周几
**v331**: fix phase5 f_repo 未定义 + 手动补跑周评 (8 因子注册)
**v332**: 状态池重构 — 废弃 using/backtesting 间接层 → get_signal_factors/get_evaluable_factors
**v333**: curator 抑制 spearmanr ConstantInputWarning
**v334**: fix market_beta_60d common_dates 顺序 + 因子审计 (28 问题/1 真 Bug)
**v335**: 因子计算全面加固 — 11 raise→return None + trcf/fund_flow/ztd
**v336**: 重做: 13 raise→return None + ztd numpy + trcf/fund_flow
**v337**: Bug A — reversal_5d 改为真反转 (-cum)
**v338**: Bug B+D+E+G — beta 日期对齐/量价改收益率/板块涨停/vp_divergence
**v339**: Bug E 涨停分板块 (科创 20%/北交 30%)
**v340**: Bug C 删 turnover fallback + Bug F MIF 文档修正 + Bug A 注释清理
**v341-v343**: Python 3.14 broker 作用域修复
**v344**: P0 — pipeline broker suppress_push 保护 + 回测失败不持久化
**v345**: P1 — np.log 类型/状态机事件/Phase2+5 术语对齐
**v346**: Phase2+5 输出完全对齐状态机四态 (active/probation/archived)
**v347**: fix np.log — pd.to_numeric→astype(float) (DataFrame 兼容)
**v348**: fix update_daily batch_start — 历史回填 _explicit_start 标志
**v349**: backfill_range — 按日期范围精准补缺, 与 update_daily 解耦
**v350**: _explicit_start 标志 + conn 共享 + chunk 缺失修复
**v351**: backfill_range 改用 baostock (免费历史数据, qfq 自带)
**v352**: 因子冒烟 95/100 通过
**v354**: ADR-043 因子缓存物化性能 — aux 数据从每日期 12 次 SQL → 每块 12 次 (200x), financial 表加 symbol 过滤, 预计全量物化 3天→3-5小时
**v353**: abn_turnover numpy 数组→pd.Series — 100/100 通过

### 新增因子 (v322-v328)
| 因子 | 类型 | 状态 |
|------|------|------|
| market_beta_60d | low-beta | evaluating |
| overnight_gap_5d | T+1 隔夜动量 | evaluating |
| vol_price_sync_20d | 量价同步 | evaluating |
| revenue_growth_yoy | 营收增长 | evaluating |
| earnings_growth_yoy | 净利润增长 | evaluating |
| piotroski_fscore | 9 项质量打分 | evaluating |
| intraday_reversal | 日内反转 | evaluating (等快照) |
| open_volume_ratio | 开盘量占比 | evaluating (等快照) |
| close_surge | 尾盘异动 | evaluating (等快照) |
| alpha002_vol_div | Alpha#2 | evaluating |
| alpha012_vol_dir | Alpha#12 | evaluating |
| alpha033_gap | Alpha#33 | evaluating |
| alpha035_range_mom | Alpha#35 | evaluating |
| alpha041_geo_vwap | Alpha#41 | evaluating |
| alpha042_vwap_div | Alpha#42 | evaluating |
| alpha055_pos_vol | Alpha#55 | evaluating |

### 数据回填 (v348-v351)
- backfill_range: 2019-01→2022-12, baostock qfq, 3.87M 新行
- 2019: 854→3551 stocks, 2020: 3984, 2021: 4507, 2022: 4935
- adj_factor: 4924/5208 (94.5%), 2020-01-02 起
- tushare 历史数据因 adj_factor 缺失被丢弃 → 归档 ADR-042

### 关键 ADR
- ADR-042: 拒绝纳入的架构/算法 (24 项 + 因子阈值锁定 + 数据回填根因)
- ADR-041: 因子状态机简化
- 见 docs/adr/ 完整列表

### 待完成
- [ ] 全量回测 (2019-2026)
- [ ] 快照数据积累 (60 天后激活日内反转因子)
- [ ] paper trading 桥接验证
