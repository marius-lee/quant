# HANDOFF — 盈迹 (quant) 项目变更日志

> **修改前**: `grep -rn "关键词" HANDOFF.md docs/adr/` 联动搜索，避免重复踩坑。

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
