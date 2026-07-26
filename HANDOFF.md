# HANDOFF — 盈迹 (quant) 项目变更日志

> **修改前**: `rg "关键词" HANDOFF.md HYPOTHESES.md docs/adr/` 三文件联动搜索，
> 避免重复踩坑、重新讨论已否决方案、遗漏已有设计。

## test-v306 (进行中, P0+P1-6/7 已提交): 独立代码审计修复 — 数据层防腐 + PIT 收口

**断点 (2026-07-26 晚, 上下文重启备份)**: 接续口令 "继续 test-v306"。
已提交: 2c11b3f (P0 批量) + ce88060 (P1-6/7)。当前进度与待办:
1. ⏳ 用户终端回填 (网络活, 命令见下): ① 估值 `jq_valuation 2026-07-06
   2026-07-24` (限流自动退避 ~14min); ② fund_flow sync_all (~20min);
   ③ margin sync_range 2026-07-09 起 (~4min); ④ 完成后
   `bash scripts/run_task.sh factor_cache 2026-07-24` 增量重算;
   ⑤ `scripts/verify_v305.py` 终验 (7 窗口因子 + 4 估值因子恢复)。
2. ✅ P1-6: idx_fv_factor_date 索引 (真实库已建, 查询 60s→0.02s)。
3. ✅ P1-7: ideal_amplitude 8s→0.009s/日期, ztd preload 91s→秒级。
4. ⬜ P1-5 turnover 全历史回填 (backfill_turnover, baostock 0.3s/只,
   5208 股 × 缺口日 — 大任务, 建议分批; 命令: `PYTHONPATH=.
   .venv/bin/python -c "from quant.data.store import DataStore;
   s=DataStore(); print(s.backfill_turnover(0)); s.close()"`)。
5. ⬜ P2-9 hyperopt purged-CV 审计 (optimizer/hyperopt.py 是否过拟合源)。
6. ⬜ P2-8 broker 抽象界定 / P2-10 DataStore 收口界定。
7. ⬜ 新发现 (审计五节): financial_* 无 ann_date + 停滞 2025-12-31
   (需 jq 凭据 JQDATA_USER/PASS, 否则财务因子集体吃 7 月前年报)。
8. ⬜ push: 本地 ahead 25+ (v305/v306 提交), 网络慢曾被中断, 择时重推。

**背景**: 用户要求抛开旧文档独立审计 (docs/analysis/2026-07-26-independent-code-audit.md
为归档件, 含核对表)。结论: 数据层是最大风险源, 本条目跟踪 P0 修复。

**P0-1 _cs_zscore inf 防腐** ✅: registry.py 统一 isfinite 过滤 +
reindex 回原索引 (保 "输出索引==输入索引" 契约, test_nan_handling 实证)。

**P0-2 数据新鲜度 watchdog + 两源修复** ✅代码:
- 新增 `quant/data/freshness.py`: 5 表 SLO (daily/fund_flow/margin_detail/
  daily_valuation/adj_factor), check_freshness + unavailable_factors;
  daily_data 晚间链接入 fund_flow/margin/daily_valuation 增量同步 +
  stale → ERROR + send_alert CRITICAL (telegram/wechat/本地)。
- 根因修复 ×3:
  ① 东财封 python-requests 指纹 (curl 同参数 200/0.47s 实证) →
    fund_flow._http_get_json 模块级探测降级 curl 子进程;
  ② `to_compact` 漏 import ×2 (margin.py:115, daily_sync.py:41) —
    **margin 07-09 起停滞真凶**: 每晚 daily_sync 静默 NameError;
  ③ jq_valuation: jq 异常 → tushare 兜底 (原只在返回空时回退, auth
    失败直接中断); tushare daily_basic free tier 1次/min → 62s 退避 ×6。

**P0-3 物化池按数据可用性裁剪** ✅: freshness.TABLE_TO_FACTORS +
unavailable_factors; factor_cache._run 物化前裁剪源停滞因子
(fund_flow↦{fund_flow_3m, main_flow_ratio}, margin_detail↦{margin_*,
short_interest}); 源恢复自动回池由 per-date missing 过滤补算。

**P0-4 基本面 PIT 收口** ✅代码:
- **坐实前视**: get_fundamentals(date) 在 daily_valuation 覆盖外
  (2026-07-03 起停滞 23 天) 回退 stocks 快照 → 07-06..07-24 物化的
  ep_ratio/bp_ratio/size/roe_ratio 全部拿 07-26 快照 = 前视。
  已删污染行 259965 行 (78120+77490+78120+26235)。
- **修复**: 严格 PIT — 只认 `MAX(daily_valuation.date ≤ date)`,
  覆盖外 → NaN (诚实缺数据, 不静默前视); stocks 快照仅 date=None
  实盘路径可用; roe 同列清空后由 PIT pb/pe 推导。
- **误判纠正**: high52w_dist 原已 PIT (store.py 在传 date 时从 daily
  重算 52 周高 + 当日收盘), 审计初判有误, 代码验证后翻案。
- **遗留 (P1)**: financial_* 无 ann_date 列 (stat_date≤date-60d 近似,
  年报 120d 法定期 → 2-4 月窗口 ≤2 月前视) + 数据停滞 2025-12-31
  (2026Q1 未同步, 需 jq 凭据)。
- daily_valuation 接入晚间链 (14 天增量窗口, 已同步自动跳过)。

**测试**: test_v306_data_freshness (6 项) + test_v307_pit_fundamentals
(3 项) + test_factor_compute inf (3 项); 全套 175 绿。

**待用户终端执行 (网络活)**: ① 估值回填 `jq_valuation 2026-07-06
2026-07-24`; ② fund_flow 全量 sync_all; ③ margin sync_range 07-09 起;
④ run_task.sh factor_cache 2026-07-24 增量重算; ⑤ verify_v305.py 终验。

---

## test-v305 (已完成 2026-07-26): factor_cache 0 行重算死循环 — 7 因子永不物化

**实施结果 (2026-07-26)**:
1. ✅ `quant/factor/store.py` materialize: `data_full.loc[[ts]]` → `data_full.loc[:ts]`
   (trailing slice, 无前视; iloc[-1]=ts 语义不变)。
2. ✅ 次生 bug range_20d: 删 696131 错行 (107s) + 正常物化重算; 新口径实证
   000001@2026-07-24 raw 1.6887(旧错值) → 1.7262 (20d 窗口口径)。
3. ✅ **诊断外第二根因 — ctr_20d inf 截面污染**: trailing slice 修好后 ctr_20d
   仍 0 行。实证: market.db turnover 2026-07-10 前近全零 (零填充段, 07-10
   起 ~5192/日), 0→x 跳变 → `pct_change` 产 inf → `_cs_zscore` 不过滤 inf →
   std=NaN → 全 universe NaN → 0 行。修复 (compute_ctr): to_chg 剔除 ±inf +
   有效观测按 finite 计数 + 结果 isfinite 兜底。修后 ctr_20d@07-24 = 3988 行。
4. ✅ 测试 test/test_v305_factor_cache_trailing_slice.py 5 项全绿, 全套 163 绿:
   trailing slice 语义 / 无前视 (range_20d full vs truncated 一致) / amihud 1行→NaN
   vs trailing→有值 / ctr inf 防腐 / 集成 materialize 7 因子全产行 (40 股 mock)。
5. ✅ 全量补算: `run_task.sh factor_cache 2026-07-24` (134 日期增量, 仅算缺失)。
   07-24 覆盖 58 → 65/67。

**残留 (不阻塞, 非本任务范围)**:
- `fund_flow_3m` (fund flow 数据停滞 2026-02-27) + `short_interest` (margin 数据
  停滞 2026-07-09) 数据管道缺口 → 最新日 65/67, is_materialized 仍 False,
  每晚增量重算 3 个缺失因子 (有界成本 ~每分钟级, 非原全量死循环)。根治 = 修
  两条 aux 数据管道。
- 历史日 (< 2026-07-10) turnover 零填充 → ctr_20d 那些日期 NaN 残留;
  zt/dt_streak 全市场零涨/跌停日 std=0 → NaN 残留。均随数据累积自愈/可接受。
- `_cs_zscore` 不过滤 inf 是共享隐患 (现仅 ctr_20d 在因子侧防住); 其他因子若
  上游产 inf 同样截面全 NaN。建议后续在 registry._cs_zscore 统一 isfinite 过滤。

**症状 (2026-07-26 用户实跑日志)**: 每次 factor_cache run 均为
`materialized 134 dates × 67 factors × 5208 symbols → 0 rows in 1171s`
(materialization_log 实锤: run 4=2163s/0行, run 6=3304s/0行, run 7=1171s/0行;
仅 run 5 (07-24 21:22) 写过 443675 行)。

**根因链**:
1. 物化池 67 因子 (backtesting ∪ using), factor_values 仅 60 distinct →
   `is_materialized()` 查最新日 67 因子齐全 → 永 False → 每次全量循环;
2. 缺失 7 因子: `abn_turnover, amihud_250d, ctr_20d, dt_streak,
   hl_volume_20d, ideal_amplitude, zt_streak` — 全部是**非 shortcut 价因子**;
3. 机制: 物化循环 `day_data = data_full.loc[[ts]]` (quant/factor/store.py
   ~line 200, **1 行切片**) 喂 compute_all_factors; 而这 7 个函数签名契约是
   "给全历史+date, 内部自己 `iloc[start:idx+1]` 切窗口" → 1 行 = 无历史 →
   全 NaN → 0 行 → 永缺失 → 回到 1, 死循环。
4. 对照: 19 个 shortcut 因子吃全历史 prims → 正常; 12 个"工作中"非 shortcut
   (analyst_buy/fund_change/margin_*/seal_* 等) 吃 aux 表或纯当日 → 正常。

**次生 bug — range_20d 已写值是错的**: compute_intraday_range 同为窗口因子
但 1 行输入不产 NaN 而是退化成"1 日振幅"写库。实证: 000001@2026-07-24
cache raw=1.6887, 而 1d=0.008108 / 20d_avg=0.019720, 两边都不匹配
(疑似更早版本口径)。**须删行重算**: `DELETE FROM factor_values WHERE
factor='range_20d'` (重算走正常物化, 无需 force 全量)。其余 11 个工作中的
非 shortcut 因子改完 trailing slice 后需抽查口径是否也受 1 行输入影响。

**修复方案 (已全部实施, 见上方实施结果)**:
1. ~~`quant/factor/store.py` 物化循环 trailing slice~~ ✅
2. ~~删 range_20d 行 + 重跑 factor_cache~~ ✅
3. ~~测试 3 项~~ ✅ (扩为 5 项, 含 ctr inf 防腐)
4. ~~更新本文件 + 核对表~~ ✅

**复现陷阱 (勿踩)**: `_cs_zscore` 有 `zscore_min_count_dense` 下限 — 3~5 只
小样本直调必全 NaN, 与物化无关; 验证须 mock zscore 或用全 universe。
zt/dt_streak 对未涨停股 fillna(0) 后 zscore — 正常日 std>0 可写; 极端
"全市场零涨停"日 std=0 → NaN → 该日永不齐 (可接受的残留, 不阻塞)。

**会话重启提示**: 本条目即断点备份。接续口令 "继续 test-v305"。
相关文件: quant/factor/store.py (materialize 循环), _dispatch.py,
price/{_momentum,_turnover,_alternative,_event}.py (7 因子实现)。

---

## test-v304: tickflow 日K 本地因子转 qfq — 堵死 fallback 混写 (B-08 收口)

**背景 (数据源链审计 2026-07-26)**: OHLCV 链 = tushare→tickflow→zzshare→
pytdx→tencent→akshare, first-non-empty-wins。tushare 路 B-08 v2 起用本地
adj_factor 表转 qfq; 但 **tickflow 日K 未复权, fallback 接盘历史回填直接
落库混入 qfq 表** — v303 改完故障转移后 tickflow 实际是高频承接者
(tushare 200call/min 限流耗尽时), 混写风险从理论变现实。

**修复** (`store.py::_fetch_tickflow_daily`):
- 拉到 dfs 后先查本地因子: `_local_qfq_ratio(conn, symbols)` (复用 tushare
  同一函数, 单一口径源);
- 无覆盖股票跳过不写; **全缺 → return None 交下一源** (与 tushare 段同语义,
  不写口径不一致数据);
- 逐股 df 排序后 ratio = factor(date)/latest, 停牌日该股内 ffill/bfill,
  乘 OHLC 四列; YYYYMMDD 紧凑日期归一化 ISO 匹配因子键;
- `pd.to_numeric(errors="coerce")` 防全 None 边界 object/除法 TypeError
  (tushare 段同款隐患一并加固);
- 当日 quotes 实时价 = 最新因子价 (ratio=1), 不受影响。

**审计附带纠偏 (注释级, 行为不变)**:
- `_source_speed` EMA 只记录不参与排序 (`ordered = all_sources` 固定优先级),
  删"动态轮转, 最快的排前面"误导注释;
- baostock 0.9.20 实测兼容 py3.14, 纠 sync_industry 过时注释
  ("不支持 3.14"→"≥0.9.20 已实测兼容")。

**不改 (审计结论可接受)**: chunk 级短路语义 (50 只/批内任一源成即停);
baostock 不进 OHLCV 链 (只回填换手率 turn 字段+行业分类, 见 store.py:1185)。

**测试**: test_tickflow_failover.py +5 项 (除权日缩放/全缺→None/部分覆盖/
紧凑日期/停牌 ffill)。158 passed, smoke 12 段全绿。

**变更文件**: `quant/data/store.py`, `test/test_tickflow_failover.py`

---

## test-v303: tickflow 历史K线改走注册版 API

**背景**: 用户发现 daily_data 拉取走 `TickFlow.free()` 免费层 (打印免费
banner), 但项目有注册 key。原设计: 历史 K 免费层、仅当日行情
(_fetch_tickflow_quotes) 用注册 key。

**实测结论 (用户终端 2026-07-26)**: 注册 key 套餐**无批量K线权限** —
`PermissionError: 无日/周/月K线查询批量查询权限`。原"历史K走免费层"
不是疏忽, 是权限现实。

**终版设计 — 权限感知故障转移** (`store.py::_fetch_tickflow_daily`):
- 先试注册版批量K (单次尝试, 不过 retry 装饰器 — 权限错误重试 4×15s
  纯属浪费);
- `tickflow.PermissionError` → 模块级 `_TICKFLOW_BATCH_NO_PERM=True`,
  进程内不再白试, 显式 warning 落免费层 (免费层仍过 datasource_retry);
- 未配置 key → 免费层 + 显式 info 日志;
- 升级套餐后新进程自动走回注册版 (flag 仅进程内)。

**如要注册版提速 daily_data**: 需 tickflow 套餐开通"日/周/月K线批量查询"
权限 — 开通后无需改代码。

**测试**: test_tickflow_failover.py +3 项 (权限拒绝→落免费+置 flag;
flag 置位→直走免费; flag 复位→重试注册版)。153 passed。

---

## test-v302: 晚间调度重排 — 依赖链替代固定时刻 (断点 3/4 排程根因)

**设计判断**: v301 断点 3 (G1 窗口) /断点 4 (G4 倒挂) 不是单点 bug — 固定
时刻表在 daily_data 耗时不定 (实测 10min~2h) 下必然错位: attribution
20:00 永远跑在 factor_cache 21:00 物化之前, 而 G1/G4 都要当日因子缓存。
修补单任务只是续命, 须重排依赖拓扑。

**新拓扑 (cron 单入口依赖链)**:
```
19:00 evening_chain: daily_data --ok--> factor_cache --ok--> attribution
  阶段已 ok → 跳过; 任一失败 → 链中断 fail-loud; 各阶段仍各自记 task_runs
```
- 新模块 `quant/scheduler/evening.py`: _CHAIN 声明式依赖序, ok 门控,
  grace=4h (_TIMEOUTS["evening_chain"]=14400, 僵尸检测同源)
- crontab: 删 19:00 daily_data / 20:00 attribution / 21:00 factor_cache
  三条固定时刻, 并成 `0 19 * * 1-5 evening` 一条
- orchestrator daemon 同步: 门从 "尝试过" (in status) 改 "成功" (== ok),
  顺序对调 factor_cache→attribution (daemon 成 cron 兜底, 去重不冲突)
- v301 的 G4 warning 修复降级为纵深防御 (手动单跑 attribution 仍安全)

**附带发现 (周日实跑漏网)**: 周日验证 attribution 时 G1 过了是因为
周日最后交易日=周五已物化; 真交易日 20:00 跑 G1 同样必崩 (trading_days
含当日) — 固定时刻无救, 只有依赖链能解。

**测试**: test_evening_chain.py +8 项 (顺序/失败中断/已 ok 跳过/去重/
末段失败/超时注册/依赖序守卫)。150 passed, smoke 12 段全绿。

**变更文件**: `quant/scheduler/evening.py` (new), `quant/scheduler/orchestrator.py`,
`scripts/run_task.sh`, `test/test_evening_chain.py` (new), crontab 重排

---

## test-v301: 日频业务闭环审计与修复 (盘前→盘中→盘后全链路)

**审计方法**: task_runs 全量运行记录 + 逐任务实跑 (2026-07-26 周日)。
用户问题: 盘前信号→盘中执行→盘中风控→日终对账→数据拉取→盘后归因→
因子物化→因子评估, 是否逻辑闭环且流畅。

**结论: 审计前不闭环, 4 个断点全修**:

**断点 1 — reconcile 史上零执行**: crontab 无条目 + run_task.sh 无
reconcile case + orchestrator daemon 非常驻 (task_runs 零 reconcile 行
实锤)。修: run_task.sh 加 case, crontab 装 `5 15 * * 1-5`。
实跑 `reconcile._run('2026-07-24')` OK (pos checked=2 drifted=0)。

**断点 2 — cron+daemon 双调度误杀**: `_tk_start` grace ≪ 合法运行时长
(factor_cache 默认 120s vs 实际 2743s) → 第二触发把活任务标 aborted
(07-24 factor_cache 145s 被误杀), 僵尸进程继续持 market.db 写锁 →
daily_data 19:30 "database is locked"。修: grace 全面对齐
orchestrator._TIMEOUTS (signals/execute 1800, daily_data 7200,
attribution 3600, factor_cache 5400, weekly_eval 7200, monitor 21600);
monitor._run_continuous 补 rid=None 守卫 (原忽略返回值, 双触发
_tk_finish 必抛 "no running row found")。

**断点 3 — attribution G1 窗口冲突 (必崩)**: v296 因子缓存裁剪 244
日历日 vs oos_verify 需求 train 250+test 20+5=275 日历日 → 永久缺 31 天
→ "factor_cache miss for 2025-10-24" 每天必崩, 且 Step A ic_daily 断粮
(v300 CPCV L2 的数据源)。修: oos_verify.train_window_days 250→170
(需求 195cd < 244 保留窗, IS ≈113td 采样 1/5 ≈23 点 ≥ min_is_points 20)。
备选方案 (retention 280+回填 50 交易日) 因磁盘/时长成本否掉。

**断点 4 — attribution G4 排程倒挂 (每个交易日必崩)**:
factor_pnl_attribution 要求当日因子缓存, 但 attribution 20:00 跑在
factor_cache 21:00 物化之前 → RuntimeError 拖垮全任务 (Step E/基准记录
陪葬; 07-23 能 ok 纯属 22:56 重跑碰巧在物化后)。修: cache miss 改为
warning + 返 {} (与函数内其他"算不了"分支同级), G4 当日跳过。

**验证**: attribution 周日全链路实跑 OK (G1 23/183 采样日通过, CPCV L2
D3 判定生效, Step E 37 因子同步, G2/G3/R3/R4/基准记录全到);
142 passed + smoke 12 段全绿。07-24 的 attribution _require_cfg 失败
已由 test-v299 detector/deflated_sharpe import 修复覆盖。

**遗留观察 (未改)**:
- D3 退役路径: Step E 每日 sync 刷 factor_registry.updated_at → 20d
  buffer 永不超, 退役实际不触发 (既有缺陷)。
- G1 仍硬依赖 factor_cache 全窗口, 单交易日缺缓存即 raise (fail-loud,
  符合零 fallback; 窗口已对齐不会再缺)。
- orchestrator daemon 与 cron 并存是设计冗余 (grace 对齐后互不误杀);
  reconcile 现由 cron 兜底, daemon 跑也无冲突。

**变更文件**: `scripts/run_task.sh`, `quant/scheduler/task_log.py` (注释),
`scheduler/{signals,execute,daily_data,attribution,factor_cache,weekly,monitor}.py`,
`quant/monitor/factor_attribution.py`, `quant/config/config.yaml`, crontab +1 行

---

## test-v300: §8.4 CPCV+DSR 驱动替代短窗判定 (L2 重写)

**动机**: 核对表 §六 #4。旧 L2 用 G1 单切分 train 250d/test 20d 的 OOS/IS
IR 比率判降级/恢复 — 统计功效低且强依赖切分点 ("短窗判定")。

**新模块 `quant/evaluation/cpcv_dsr.py`**:
- `cpcv_oos_series()`: factor_ic_daily live IC 序列 → PurgedWalkForward
  切 fold → 各 fold 只取 OOS 段拼接 (首 fold 无训练段自动排除)。
- `evaluate_factor()`: OOS 序列 → **日频** ICIR → DSR 多重检验校正
  (M=评估因子总数, 偏度/峰度实测) → verdict:
  degraded (DSR<0.5) / significant (≥0.95) / neutral / insufficient
  (OOS<40 天, 不罚, 对齐 Phase 3 语义)。

**单位坑 (实测抓到)**: DSR/PSR 公式内 E[max_SR]=√(2lnM/T) 与标准误
均为 per-period 口径 — observed SR 传年化值会膨胀 √244≈15.6 倍,
噪声序列也判 significant。必须传日频 ICIR; 年化值仅作展示字段。

**attribution.py Step C 重写**: L1 滚动 IC 保留; L2 数据源换成
factor_ic_daily live 序列 (attribution.cpcv_lookback_days=120 窗口),
逐因子 evaluate_factor; D1/D2/D3 状态迁移 reason 改记 DSR/OOS_ICIR。
死键 `oos_warning_decay`/`oos_recovery_threshold` 从 config 删除
(oos_verify.decay_warn_threshold 属 G1, 保留)。

**config 新增** (attribution.*): dsr_degraded_threshold=0.5,
dsr_recover_threshold=0.95, cpcv_min_days=40, cpcv_lookback_days=120。

**验证**: test_cpcv_dsr.py +13 项 (fold 切分/强 IC→significant/噪声→
degraded/不足→insufficient/单调性/M 校正生效/config 接线+死键删除)。

**上线影响 (真实数据 dry-run 2026-07-26)**: 当前 registry active=0/
monitoring=37 → D1 无降级对象; 37 因子判定 28 degraded/2 neutral/
1 significant (dt_streak, dsr=0.96, 唯一恢复候选)/6 insufficient。
M≈40/T≈55 时运气门槛 ≈ 日频 ICIR 0.37 (年化 ~5.7) — DSR 固有保守,
T 累积后放宽。注意: D3 退役路径因 Step E 每日 sync 刷新
factor_registry.updated_at 导致 20d buffer 永不超 (既有缺陷,
本次未改), 不会批量退役。

**变更文件**: `quant/evaluation/cpcv_dsr.py` (new), `quant/scheduler/attribution.py`,
`quant/config/config.yaml`, `test/test_cpcv_dsr.py` (new)

---

## test-v299: §8.2 — 接线 HRP / Regime / Optuna

**动机**: 核对表 §六 #2。三个模块代码齐全但零调用方:
`optimizer.method: equal_weight` 是死配置; combine_regime 无人调;
hyperopt 修好后无入口脚本。

**HRP**: `optimizer.method` 变真实分发 — `hrp` (新默认, De Prado 2016)
| `risk_parity` (旧行为)。仅 small 层 (capital≥micro_cap) 有协方差时生效,
Nano/Micro 不读 → 实盘 (¥5000 Nano) 行为不变, 影响回测大资金层。
portfolio.py 新增 `_hrp_lot()` (hrp_weights→_iterative_clip→整手离散化,
与 _mean_variance_lot 同范式), PortfolioConstructor 读 method 分发,
0 仓位回落 Kelly/MV 链不变。

**Regime**: `alpha.regime_combine: true` (新键)。pipeline Step 3 接线
`am.combine_regime()`; generate_signals 新增 regime_label/regime_probs
参数 — 实盘 (scope=live) 缺省自动 get_current_regime() (pickle 缓存),
**回测禁止自动拉取** (全量历史训练 = 前视), 由 loop.py point-in-time
注入: 起始日前训练 HMM, 逐调仓日用截止当日 returns 前向滤波。
`regime.train_start: '2024-01-01'` 新键 (原 detector 两处硬编码),
实盘/回测共用。store.get_benchmark 小数 → ×100 对齐实盘 percent 口径。

**Optuna**: run_task.sh 新增 `hyperopt [trials]` 条目 (默认 200);
顺带修 help 文本 factor_cache 行未闭合引号吃掉 weekly 行的旧 bug。

**附带真 bug 修复**: `regime/detector.py` 与 `evaluation/deflated_sharpe.py`
模块级缺 `from quant.config.constants import _require_cfg` (后者 import
写在 docstring 里), RegimeDetector()/compute_dsr_for_strategy 直接调用
必 NameError — 此前无调用方所以未暴露。

**验证**: test_portfolio.py +5 (hrp 权重性质/分发/risk_parity 保持/无协方差
报错链), test_regime_wiring.py +10 (PIT 训练+截断 predict/权重偏置/签名守卫
+回测源码禁 get_current_regime() 调用)。既有 mean_var 测试 pin
method=risk_parity。smoke 回测确认 "regime combine: bull (confidence=1.00)"
真实触发。

**变更文件**: `quant/optimizer/portfolio.py`, `quant/regime/detector.py`,
`quant/pipeline.py`, `quant/backtest/loop.py`, `quant/config/config.yaml`,
`scripts/run_task.sh`, `test/test_portfolio.py`, `test/test_regime_wiring.py` (new)

---

## test-v298: 待办 #9 — hyperopt 空转修复 (Optuna 参数真实注入)

**Bug**: hyperopt.py 把 11 个搜索参数写进 `OPTUNA_*` 环境变量, 但全项目
无任何代码读取这些变量 (rg 全仓搜索确认) — Optuna 的 200 次 trial 全部
跑同一套 config, 目标函数是常数, 搜索完全无效。

**修复** (按核对表建议的"直接覆盖 config 单例"路线):
- `quant/config/loader.py`: 新增 `override(mapping)` contextmanager —
  深拷贝单例 → `_set_nested` 就地写 → 退出恢复; key 必须已存在于
  config.yaml (fail-fast 防笔误); 覆盖期间 mtime 钉 inf 防热重载冲掉。
- `quant/backtest/loop.py`: `run_backtest()` 新增 `combine_mode=None` 参数
  (原 warmup 后硬编码切 ic_weighted, 该维度搜不动)。
- `quant/optimizer/hyperopt.py`: 删全部 OPTUNA_* env 写入; config 参数经
  `loader.override()` 注入 (8 个运行时读取点逐一核实, 全部即时生效),
  universe_size / combine_mode 走 run_backtest 显式参数。

**搜索空间死维度清理**:
- `lookback_days` 范围 60-365 → 400-800: 旧范围全低于
  `max_factor_calendar_days()=378`, 被 pipeline `_eff_days=max()` clamp 成常数。
- `max_single_position` 删除: Nano 层 (capital=5000) _rank_concentrated
  不用该参数, 死维度。

**验证**: test_config_override.py +8 项 (override 生效/恢复/未知 key 拒绝/
异常后恢复; param map key 存在性; objective 注入接线含 kwargs+恢复断言;
backtest error→0; MDD>30% → sharpe×0.5)。全量 106→114 passed。
smoke 回测 `run_backtest(combine_mode='sleeve')` 端到端跑通 (22.6s);
注: smoke 窗口全在 warmup 内, sleeve/ic_weighted 信号相同属预期,
kwarg 有效性由单测的 monkeypatch 断言覆盖。

**变更文件**: `quant/config/loader.py`, `quant/backtest/loop.py`,
`quant/optimizer/hyperopt.py`, `test/test_config_override.py` (new)

---

## test-v297: §8.3 交易成本感知组合优化 (Grinold α − λ·TC 无交易区间)

**动机**: 核对表 §8.3 / 待办 #10。¥5000 Nano 账户一次全仓换股成本 ≈ ¥21
(最低佣金 ¥5×2 主导) ≈ 0.47%，周频无差别换手的年化拖累 ~24% ——
对微账户比因子本身更重要。

**方案**: 各层优化器产出理想目标后统一过成本带：持仓 A→候选 B 的换股仅在
`E[Δr] = Δz × IC_eff × σ_daily × horizon × 金额 ≥ λ × 实际成本` 时执行，
否则恢复原持仓。z = Blom 分位正态逆累积 (对中性化变换稳健)；
IC_eff 优先取运行时 ic_map 的 |IC| 均值，缺失回退 config `tc_ic_ref`；
成本由 CostModel 实算 (含 ¥5 最低佣金)。只拦截"以卖养买"的换仓配对
(满仓账户换手成本主导来源)；纯现金加仓、跌出候选集的持仓卖出不设门槛。
贪心配对: 最大买入金额 × 最弱 alpha 持仓, 逐手 chunk 判定。

**接线**: `generate_signals` 从 engine 读当前持仓 (`db_path` 回测=BACKTEST_DB /
实盘=TRADE_DB，同一口径) 传入 `construct()` — 回测/实盘自动同时生效。
被拦截留仓的持仓在 daily_signals.reason 标 `tc_hold` 前缀；optimizer step
暴露 `tc_suppressed` 计数。

**新增 config** (`optimizer.*`, 来源注释见 yaml):
`tc_lambda: 1.0` (G&K 单期全额成本门槛) / `tc_horizon_days: 5` (=weekly 调仓间隔) /
`tc_ic_ref: 0.015` (校准: factor_registry active+monitoring 平均 |ic_mean|=0.0148, 2026-07-26 实测)。
σ_daily 复用 `execution.default_daily_vol`。

**验证**: test_portfolio +11 项 (小 gap 拦截/大 gap 放行/无持仓/无 cost_model 兼容/
跌出候选卖出/仍最优不动/纯加仓不设槛 + 辅助函数 4 项)；全量 95→106 passed；
smoke 12 段全绿；真实回测 (2026-06-01→07-24, ¥5000, 80 股) 成本带实际触发:
`688549→601899 benefit=¥2.35 < λ×cost=¥18.59 拦截` (ic_eff=0.015 回退值,
该回测 warmup 内 ic_map 为空属预期)。

**变更文件**: `quant/optimizer/portfolio.py` (construct 单出口重构 + `_apply_tc_band`
+ `_alpha_to_z` + `_ic_effective`), `quant/pipeline.py` (持仓接线+reason 标注),
`quant/config/config.yaml` (+3 key), `test/test_portfolio.py` (+11 项)

---

## test-v237~239: 因子状态机重构 + 3项预存bug修复 (2026-07-24)

### 因子状态机重构 (test-v237)

**动机**: 因子状态由两个模块竞争管理 (phase5_monitor + attribution), 互不可见对方决策,
monitoring 语义混用, retry_count 仅一边维护。

**方案**: ADR-026 — 引入 `FactorStateManager` 作为唯一状态写入者。

**新增**:
- `docs/adr/026-factor-state-machine.md` — 完整 ADR 文档 (5状态 vs WorldQuant 对标)
- `quant/factor/state_manager.py` — FactorStateManager 类
  - 8 条状态转换, 事件驱动 `transition(name, event, reason)`
  - 非法转换→InvalidTransitionError (零 fallback)
  - `batch_transition()`, `get_pool()`, retry_count 自动管理

**状态机变更**: 6→5 状态 (registered 合并入 candidate, ADR-026).

**修改**:
- `quant/data/repos/factor_repo.py`: VALID_STATUSES 移除 registered; update_status 加 retry_count
- `quant/factor/compute/_registry.py`: backtesting 池→('candidate','monitoring','retired')
- `quant/evaluation/phase5_monitor.py`: 4 处 raw SQL→fsm.transition()
- `quant/scheduler/attribution.py`: 3 处 f_repo.update_status()→fsm.transition()
- DB 迁移: 4 行 status='registered'→'candidate'

### 预存 portfolio 测试修复 (test-v238)

price_buffer(+5%) 使 Nano 层 lot_cost > capital → 0 仓位。

| Bug | 修复 |
|-----|------|
| test_single_lot_single_stock: ValueError | construct() Nano 层 catch→原始价格重试 (portfolio.py:218-228) |
| test_rank_concentrated_alpha_ordering: 3手→2手 | 断言动态计算 |
| test_mean_var_with_covariance: numpy.index | array→pd.Series (portfolio.py:459-462) |

### 预存 execution 测试修复 (test-v239)

历史测试残留 sim_trades(3行旧记录)→get_cash() 多扣 ¥2,012.

**修复**: 7 测试加 _cleanup_strategy() 前置清理. 断言→精确 48994.0
(1000本金+5佣金+1滑点=1006; 50000-1006=48994. 来源: config.yaml execution.*)

### 本次变更文件

| 类型 | 文件 |
|------|------|
| 新增 | `docs/adr/026-factor-state-machine.md`, `quant/factor/state_manager.py` |
| 修改 | `quant/data/repos/factor_repo.py`, `quant/factor/compute/_registry.py`, `quant/factor/stats_cache.py`, `quant/evaluation/phase5_monitor.py`, `quant/scheduler/attribution.py`, `quant/optimizer/portfolio.py`, `test/test_portfolio.py`, `test/test_execution.py`, `web/app.py` |
| 数据 | `quant/data/market.db` (4 rows: registered→candidate) |

**测试结果**: 71/71 passed


---
## test-v267: 全项目数据库连接泄漏修复

**问题背景**: 数据库锁 (database is locked) 根本原因 — 多文件 sqlite3.connect() 开连接不关闭，
attribution/factor_cache 运行时累积未关闭连接。

**全盘检查结果** (`python3` 脚本逐一比对 connect vs close):
- 项目共 38 个文件含 `sqlite3.connect`, 7 个文件 GAP > 0
- `repos/` 目录已规范 (每次 connect 后 close), 其余文件大量未对齐

**修复的泄漏文件** (6 个文件):

| 文件 | GAP | 修复方式 |
|------|-----|----------|
| `quant/execution/impact.py` | +2 | `get_stock_volume_snapshot` / `get_stock_volatility_snapshot` 加 `conn.close()` |
| `quant/factor/compute/price/_sentiment.py` | +2 | `_get_news_series` / `compute_news_abnormal_20d` 加 `finally: conn.close()` |
| `quant/data/fund_hold.py` | +1 | `sync_quarter` 加 `if close_conn: conn.close()` |
| `quant/data/northbound.py` | +1 | `sync_single_stock` 加 `if close_conn: conn.close()` |
| `quant/data/limit_up.py` | +1 | `sync_date` 加 `if close_conn: conn.close()` |
| `web/app.py` | +1 | `_tr = sqlite3.connect(TRADE_DB)` 加 `_tr.close()` |

**非泄漏说明**:
- `crowdedness.py` (+1): 使用 `with sqlite3.connect() as conn:` 上下文管理器, 自动关闭, 假阳性
- 负 GAP 文件: 多个文件 conn 变量复用多次 `.close()` (如 `order_manager.py`, `task_log.py`), 无泄漏

**变更文件**: `web/app.py`, `quant/execution/impact.py`, `quant/factor/compute/price/_sentiment.py`, `quant/data/fund_hold.py`, `quant/data/northbound.py`, `quant/data/limit_up.py`

---
## test-v267 补充: repos 语法修复 + 脚本清理 + fund_flow 重写

**repos 孤立 try/finally 修复** (web 启动失败根因):
- `quant/data/repos/universe_repo.py`: 2 处孤立 `try:` 移除 (无匹配 except/finally)
- `quant/data/repos/trade_repo.py`: `_ensure_schema` 和 `record_daily_equity` 的孤立 try/finally 修复
- `quant/data/repos/evaluation_repo.py`: `save_run` 的 try 块缩进修复 (commit 未在 try 内)
- 根因: 之前编辑遗留的半成品 try 块, .pyc 缓存掩盖, web 重启时暴露

**脚本文件语法修复** (Python 3.14 兼容):
- `scripts/init_data.py`: shebang 位置 + 函数内 import 缩进 + 非 ASCII 字符
- `scripts/generate_factor_cards.py`: 模块级代码前导空格
- `quant/core/version.py`: `__version__` 赋值前导空格
- `test/test_registry_smoke.py`: 双重 `as fc as fc`

**fund_flow.py 重写** (东方财富 API 修复):
- akshare `stock_individual_fund_flow` → `requests` 直连 `push2his.eastmoney.com`
- 加 Referer+完整 User-Agent 头绕过服务端断连
- 加指数退避重试 (4 次, 1s→2s→4s)
- 注: 东方财富 API 仍不稳定, 偶尔 RemoteDisconnected, 大市值银行股尤其严重

**变更文件**: repos 3 个文件 + scripts 4 个文件 + `quant/data/fund_flow.py`

---
## test-v268: 删除 backtest/diagnostics.py — 废弃的因子"快照"模块

**删除**: `quant/backtest/diagnostics.py` (30 行)

**原因**:
- `compute_pre_backtest_ic()` 自 test-v215 起已被禁用 — `backtest/loop.py:361` 改为复用
  evaluation/ 的 walk-forward IC, 不再调用 diagnostics
- diagnostics.py 与 evaluation/ 七阶段管线功能重复: evaluation 已提供完整的因子 IC 评估
  (phase2_single → phase3_oos), diagnostics 的"快速快照"定位被替代
- 保持两个独立系统带来混乱: 同一个因子在 diagnostics 和 evaluation 中有两套 IC 指标,
  但只有 evaluation 的结果被实际使用
- 项目中 "diagnostics" 术语从此特指 evaluation/ 管线的 phase 诊断阶段,
  不再指 backtest/diagnostics.py

**清理的引用**:
- `quant/backtest/loop.py`: 移除 `from quant.backtest.diagnostics import compute_pre_backtest_ic`
- `quant/backtest/analyze.py`: 更新模块分工文档 (移除已废弃的 diagnostics.py 分工说明)
- `quant/factor/ic.py`: 更新注释 (backtest/diagnostics → evaluation/)

**变更文件**: `quant/backtest/diagnostics.py` (删除), `quant/backtest/loop.py`,
`quant/backtest/analyze.py`, `quant/factor/ic.py`

---
## test-v269: 6项业界标准差距修复

### Item 1 — 冲击成本模型 (Almgren-Chriss 接入)
- **现状**: `cost.py` 已内置 `slippage_with_impact()` — 有 volume 时调用 `impact.py` 的
  Almgren-Chriss 模型, 无 volume 时回退固定千一滑点 (已完成, 此次仅确认)
- 影响: 大单交易 (>日均量 1%) 时回测收益不再偏乐观 0.5-1.5% 年化

### Item 2 — 市场微观结构 (Roll 1984 bid-ask spread)
- **新增**: `quant/execution/market_microstructure.py`
- `estimate_roll_spread(prices)` — 从日线序列协方差反推有效价差
  公式: 2 × √(-cov(Δp_t, Δp_{t-1}))
- `batch_roll_spread(symbols, date)` — 批量估算, 用于回测诊断
- 来源: Roll (1984) + Harris (2003) Trading & Exchanges

### Item 3 — 幸存者偏差 (退市股数据完整性)
- **新增**: `quant/data/quality.py` — `check_delisting_completeness()`
- 检测退市股在 delist_date 前是否有完整的 daily 数据
- 标准: CSMAR/CRSP 退市追踪标准

### Item 4 — 前视偏差修复 (财报发布延迟)
- **修改**: `quant/data/store.py` — `get_fundamentals()` SQL 加 60 天发布延迟
- `stat_date <= date` → `stat_date <= date(date, '-60 days')`
- 模拟年报/中报/季报的实际发布时间线 (45-120天延迟, 保守取 60 天)
- **新增配置**: `universe.fundamental_publication_delay_days: 60`

### Item 5 — 风格因子模型 (BARRA 多因子)
- **新增**: `quant/risk/neutralize.py` — `style_neutralize()`
- 支持多元截面回归: alpha ~ value + momentum + volatility + quality + ...
- `neutralize()` 函数新增 `style_exposures` 参数 (向后兼容)
- 来源: Fama-French 5 因子 (2015) + Carhart 4 因子 (1997) + BARRA USE4

### Item 6 — 数据质量管线
- **新增**: `quant/data/quality.py` — 完整数据质量检测模块
- `check_price_anomalies(date)` — 日内涨跌幅 >20% 异常检测
- `check_delisting_completeness()` — 幸存者偏差 (同 Item 3)
- `run_quality_checks(date)` — 统一入口
- **新增配置**: `data.quality.price_anomaly_pct` / `data.quality.volume_spike_ratio`

**变更文件**: `quant/execution/market_microstructure.py` (new), `quant/data/quality.py` (new),
`quant/risk/neutralize.py`, `quant/data/store.py`, `quant/config/config.yaml`


## test-v270: PR2 修复 — validate_orders 裁剪时资金扣除遗漏股价

**问题**: `execute.py:202` — 修剪买单时 `available -= o.cost` 只扣了交易佣金（~¥5），
未扣除 `o.shares * px` 的股价成本。导致后续买单可用资金虚高，可能超买。

**修复**: 改为 `available -= o.shares * px + o.cost`

**PR3 验证结论**: 不存在问题。`pipeline.py:338` 已对 `target_positions` 按 score 降序排序，
`_rank_concentrated` 按 `alpha_series.index` 顺序处理，索引顺序 = score 降序。
Score 本身是 pipeline 中性化+排名后的 alpha 值，与 `_rank_concentrated` 的语义一致。

**变更文件**: `quant/scheduler/execute.py`

---



---




## test-v272: Engine.get_trades 参数位置 bug — limit 误传为 mode

**根因**: `engine.py:174` 调用 `TradeRepo(self.db_path).get_trades(strategy, limit)`
时，`limit`（int）作为第二位置参数传入，但 `TradeRepo.get_trades()` 的签名是
`(self, strategy, mode, limit)`，导致 `limit=500` 被赋值给 `mode` 参数，
SQL 变成 `WHERE mode=500`，查不到任何交易。

**影响**:
- attribution DSR 永远跳过（trades=0）
- attribution R3 turnover 永远跳过（trades_today=0）
- 凡是调用 `engine.get_trades(strategy, limit=N)` 的地方均受影响

**修复**: 改为 `TradeRepo(self.db_path).get_trades(strategy, limit=limit)`

**变更文件**: `quant/execution/engine.py`


## test-v273: 修复 task_runs 僵尸 running 行根因

**根因**: `orchestrator._run_task()` 的 except 捕获异常后仅记录日志，未调用
`_tk_finish()`。任何 task 的 `_run()` 抛异常后，`_tk_start` 创建的 running
行永远留在数据库中，形成僵尸。

**修复**: `orchestrator.py:_run_task` except 中新增 `_tk_finish(name, today, "failed", ...)`，
确保任何未捕获异常都被记录为 task 失败状态。

**修复范围**: 一处修改覆盖所有 6 个 scheduler 任务
(attribution/daily_data/execute/factor_cache/signals/weekly)。

**变更文件**: `quant/scheduler/orchestrator.py`


## test-v274: pytest-benchmark 性能回归检测

**新增**: `test/benchmark_performance.py` — 5 项性能基准测试

覆盖率 (Template 5 性能基线):
  B1. 因子 primitives 预计算 — 500 stocks × 100 days, 基线 ~3.3s
  B2. IC 统计计算 — 20 factors × 500 stocks, 基线 ~10ms
  B3. AlphaModel 组合 — 20 factors × 500 stocks, 基线 ~23ms
  B4. 行业中性化 — 500 stocks, 基线 ~3.6ms
  B5. 优化器排名集中 — 500 stocks, 基线 ~450μs

使用方式:
```bash
# 运行并保存基线
PYTHONPATH=. .venv/bin/pytest test/benchmark_performance.py -v --benchmark-only --benchmark-save=baseline

# 对比基线检测退化
PYTHONPATH=. .venv/bin/pytest test/benchmark_performance.py -v --benchmark-only --benchmark-compare
```

**变更文件**: `test/benchmark_performance.py` (new)


## test-v275: 修复 benchmark_tracking alpha=None + 降级 task_log finish 警告

**Fix 1 — benchmark_tracking alpha=None**:
`record_daily()` 在 `yesterday_equity=None`（daily_equity 表无数据）时，
无法计算 `strat_ret`，导致 alpha 也为 None。新增兜底: 从 `benchmark_tracking`
表自身查找最近一个交易日的 strategy_equity 作为前日权益。

**Fix 2 — task_log finish WARNING 降级**:
`task_log.finish()` 找不到 running 行时发出的 WARNING 改为 DEBUG。
原因: test-v273 在 orchestrator 异常路径增加了 _tk_finish 调用，正常路径
任务自身也调用 _tk_finish，双写时第二次必然找不到 running 行（已被第一次更新），
这是良性条件，不应告警。

**变更文件**: `quant/benchmark/tracker.py`, `quant/scheduler/task_log.py`

## test-v271: Phase 8 — 回测vs实盘一致性验证框架

**新增**: `quant/evaluation/phase8_live_consistency.py` (Phase 8)

四维验证:
  D1. 信号一致性 — 同日 backtest vs live 的 generate_signals() 输出对比
  D2. 成交一致性 — 相同 (date, symbol, side) 的 fill price 对比
  D3. 成本一致性 — CostModel 估算 vs 实盘 sim_trades.cost
  D4. 权益一致性 — backtest equity curve vs live daily_equity

**新增 TradeRepo 方法** (供 Phase 8 查询):
  - `get_daily_signals_range(start, end, mode)` — 按日期范围读 daily_signals
  - `get_daily_equity_range(start, end)` — 按日期范围读 daily_equity
  - `get_strategy_config(strategy)` — 读 strategy_config 配置行

**设计原则**:
  - 渐进退化: 实盘数据不足时返回 insufficient_data, 不报错
  - 信号对比用 Spearman rank 相关 (对 score 非线性变换不敏感)
  - 加权评分: D1 信号 30% + D2 成交 30% + D3 成本 20% + D4 权益 20%
  - 阈值从 config.yaml 读 (暂无, 用模块级常量)

**使用**:
```bash
PYTHONPATH=. .venv/bin/python3 -c "
from quant.evaluation.phase8_live_consistency import validate_consistency
result = validate_consistency()
print(result['status'], result['dimensions'])
"
```

**变更文件**: `quant/evaluation/phase8_live_consistency.py` (new),
`quant/evaluation/__init__.py`, `quant/data/repos/trade_repo.py`

## test-v212: Alpha 候选池 UI 优化

**变更**: `web/static/app.js`, `web/static/style.css`

- renderSignals: 候选池从 8 行缩至 5 行，得分从 3 位小数缩至 2 位
- reason 列截断：超过 2 个因子贡献时只显示前 2 个 + "N more"，hover 显示完整
- CSS: `.status-badge` → `.badge` 基类重命名，对齐 JS 中的 `class="badge badge-red"`
- 新增 `.trunc-reason em` 样式



---

## test-v215: 实盘执行全链路修复 — 涨停预检 + alpha 优先级裁剪 + 价格缓冲

**Phase A — execute.py 三处修复**:
1. `fetch_quotes` 加 `include_ask_bid=True` (获取五档盘口)
2. 新增 Step 3.5 涨停封死预检: 用 ask_volume==0 + 涨停价判断开盘封死, 跳过不挂单, 写入 exec_notes
3. 裁剪逻辑从「成本升序」改为「alpha 得分降序 + 实时开盘价重算股数」: top1 优先分配资金, 剩余给 top2

**Phase B — 价格缓冲**:
- pipeline 分配时预留 5% 价格波动空间 (Nano 层)
- config.yaml: `execution.price_buffer: 0.05`
- 用昨收价 × 1.05 估算成本, 减少 execute 阶段 reopen 价差导致的 validate_orders 裁剪

**变更文件**: `quant/scheduler/execute.py`, `quant/optimizer/portfolio.py`, `quant/config/config.yaml`

---

## test-v286: FactorStore 全量接入 — 5 个剩余文件

**背景**: `oos_verify.py` 已在 test-v284 接入 FactorStore，但还有 5 个文件直接调用 `compute_all_factors` 未走缓存。

**接入模式**:
```
try:
    _fs = FactorStore(db_path=FACTOR_CACHE_DB)
    fv = _fs.load(date_str, symbols=symbols, factor_names=factor_names)
    _fs.close()
    if not fv:
        fv = compute_all_factors(...)  # cache miss, compute fresh
except Exception:
    raise  # 无静默 fallback，DB 损坏直接上抛
```

**接入清单**:

| # | 文件 | 优先级 | 改动 |
|---|------|--------|------|
| 1 | `quant/factor/ic.py` | HIGH | `_compute_one_day()` 逐日 IC 计算 → FactorStore.load() 优先 |
| 2 | `quant/evaluation/parallel.py` | MEDIUM | `_evaluate_batch()` 批量评估 → FactorStore.load() 优先 |
| 3 | `quant/evaluation/phase5_monitor.py` | LOW | `_check_crowding()` 拥挤度 → FactorStore.load() 优先 |
| 4 | `quant/monitor/factor_attribution.py` | LOW | `factor_pnl_attribution()` 暴露归因 → FactorStore.load() 优先 |
| 5 | `quant/scheduler/crowdedness.py` | LOW | `compute_crowdedness_score()` 拥挤度 → FactorStore.load() 优先 |

**已确认无需接入**:
- `quant/pipeline.py` — 已有 `factor_store` 参数，`compute_all_factors` 仅在 `factor_store=None` 时作为 fallback
- `quant/factor/compute/_dispatch.py` — `compute_all_factors` 的调度实现，不是消费者
- `quant/core/phase_tracker.py` — 仅 docstring 引用
- `test/benchmark_performance.py` — 基准测试，非业务路径

**影响**: IC 计算、评估、监控全链路速度提升 ~10x（cache hit 时跳过逐日 compute）

---

## test-v287: pipeline.py 去 fallback — factor_store=None 直接抛错

**问题**: `generate_signals()` 中 `factor_store=None` 时静默 fallback 到 `compute_all_factors`，
违反系统"零 fallback"原则。

**修改** (`quant/pipeline.py`):

1. `factor_store is None` → `raise RuntimeError` 提示先跑 `factor_cache` 物化
2. `factor_store.load()` 返回空 → `raise RuntimeError`，不再静默 fallback
3. 删除 `from quant.factor.compute import compute_all_factors` — 调用方全部移除，已无引用
4. FactorStore.load() 异常不再 try/except 吞掉，直接上抛

**设计原则**: 因子值来源唯一 — FactorStore 缓存。缓存缺失说明物化任务未跑，应抛错而非静默降级计算。

---

## test-v288: FactorStore 5 文件去 compute_all_factors fallback

**原则**: 系统严禁 fallback。缓存缺失说明物化任务未跑，应抛错而非静默降级计算。

**修改** (5 个文件):

| 文件 | 改动 |
|------|------|
| `quant/factor/ic.py` | `except Exception: pass` → 删除；`compute_all_factors` fallback → `raise RuntimeError`；删 `compute_all_factors` import |
| `quant/evaluation/parallel.py` | `try/except: raise` → 删除；fallback → `raise RuntimeError`；删 `compute_all_factors` import |
| `quant/evaluation/phase5_monitor.py` | 同上 |
| `quant/monitor/factor_attribution.py` | 同上 |
| `quant/scheduler/crowdedness.py` | 同上 |

**统一行为**: FactorStore.load() 返回空 → `raise RuntimeError("factor_cache miss for {date}, run materialization first")`

---

## test-v289: 终端单行动态进度条

**新增**: `quant/utils/progress.py` — `progress_bar()` 函数，单行 `\r` 原地刷新。

**接入**: `quant/factor/ic.py` — `compute_ic()` 逐日 IC 计算循环 (`compute_days`) 内显示进度。

**输出格式**: `[=========>              ] 45/132  34%  |  IC: 2026-05-15`

**pytest-benchmark 注意**: 基准测试默认捕获 stdout，进度条不可见。需加 `-s` 标志：
```bash
PYTHONPATH=. .venv/bin/pytest tests/benchmark_critical.py -v --benchmark-only -s -k "slow"
```

---

## test-v290: oos_verify 进度条 + 去 fallback

**进度条接入**: `run_oos_check()` 的 `for ds in trading_days:` 逐日循环 → `progress_bar()`.

**去 fallback**: `except Exception: continue` → 直接上抛。`compute_all_factors` import 已删。

**受影响调用链**: `compute_backtest_ic` → `run_oos_check` — 基准测试 `test_bench_compute_backtest_ic` 现在有进度刷新。

**进度条位置汇总**:
| 文件 | 循环 | 状态 |
|------|------|------|
| `quant/factor/ic.py` — `compute_ic()` | `for ds in compute_days` | ✅ |
| `quant/scheduler/oos_verify.py` — `run_oos_check()` | `for ds in trading_days` | ✅ |

---

## test-v291: run_oos_check 参数化 — 去掉内部 yaml 回退

**问题**: `run_oos_check()` 内部读取 `_require_cfg("oos_verify.train_window_days")` 等 3 个值，
忽略调用方传入的 `n_train_days`。`fac_cal=378` 导致回溯到 2025-04-23，远超出缓存覆盖范围。

**修改**:

| 文件 | 改动 |
|------|------|
| `quant/scheduler/oos_verify.py` | 签名改为 `run_oos_check(today, status_filter, train_days, test_days, decay_warn_threshold)`，5 个参数全部必传。删除内部 `_require_cfg` 调用。删除 `fac_cal` 参与 `total_lookback`（因子值来自缓存，不再需要 warmup）。删除 `_require_cfg` 和 `max_factor_calendar_days` import |
| `quant/scheduler/attribution.py` | 调用前读 yaml，5 个参数显式传入 |
| `quant/factor/stats_cache.py` | `n_train_days` → `train_days`，`test_days` / `decay_warn_threshold` 显式传入 |

**设计原则**: 参数全部由调用侧负责，函数内部零 yaml 读取、零默认值。

---

## test-v293: 回退进度条 — pytest-benchmark 不可见

`progress_bar` (stdout → stderr) 在 pytest-benchmark 校准阶段仍不可见。
移除 `quant/utils/progress.py`、`ic.py`、`oos_verify.py` 中所有进度条代码。

进度条方案留待后续统一设计（终端安装风格进度线 + 百分比）。

---

## test-v294: oos_verify 三模块分离 — 对齐 Alphalens/DolphinDB

**动机**: `run_oos_check()` 六层耦合 (配置→数据→股票池→因子→IC→统计), 不可单元测试,
不可复用, FactorStore 循环内每日期新建/关闭。

**重构**: 参考 Alphalens (utils → performance → tears) 和 DolphinDB (preprocess →
singleFactorAnalysis → plot), 拆为三层:

| 函数 | 对标 | 职责 | 依赖 |
|------|------|------|------|
| `compute_ic_series()` | Alphalens `factor_information_coefficient()` | 纯数学: 逐日 Spearman IC | 零: 不读 config/DB |
| `analyze_is_oos()` | DolphinDB `singleFactorAnalysis()` | 纯统计: IS/OOS→IR→衰减 | 零: 不读 config/DB |
| `run_oos_check()` | Alphalens `create_full_tear_sheet()` | 编排: 加载→计算→分析→返回 | DataStore, FactorStore, UniverseRepo |

**改动**:
- `oos_verify.py`: 217行 → 纯函数 (~80行) + 编排函数 (~100行)。`n_symbols=0`=全量。
  5 个算法常量从 yaml 模块级读, 不再写死。
- `attribution.py`: `n_symbols=_require_cfg("oos_verify.attribution_n_symbols")`
- `stats_cache.py`: `n_symbols=_require_cfg("oos_verify.backtest_n_symbols")`
- `config.yaml`: 8 个 `oos_verify` key 带详细来源注释
- FactorStore 循环外创建一次 (不再每日期新建 close)

**可单元测试**: `compute_ic_series()` 和 `analyze_is_oos()` 接受 dict/DataFrame, 零外部依赖.
---

## test-v295: signals/run_task/phase8 接入 FactorStore — 修复信号生成崩溃 + 全链路影响排查

**问题**: `signals.py`、`scripts/run_task.py`、`phase8_live_consistency.py` 调用 `generate_signals()`
时未传 `factor_store`。pipeline.py test-v287 去 fallback 后，`factor_store=None` → RuntimeError。

**修改**:
- `quant/scheduler/signals.py`: 导入 FactorStore，`generate_signals(..., factor_store=fs)`，用完 close
- `scripts/run_task.py`: 同上
- `quant/evaluation/phase8_live_consistency.py:108`: 补充 `factor_store=factor_store`

**全链路影响排查 (14 模块导入 + 6 类调用方)**:

| 检查项 | 结果 |
|--------|------|
| 导入链: 14 个受影响模块 import | 全部通过 |
| `generate_signals()` 调用方 (除 backtest/loop 用 **kwargs) | signals ✅ run_task ✅ phase8 ✅ |
| `run_oos_check()` 调用方 (attribution, stats_cache) | 6 个必传参数全部正确 |
| `compute_all_factors` 引用 | 8 处: 4 处注释/导出, 4 处合法使用 (factor_ic Mode A, FactorStore 写侧, stats_cache Mode A) |
| `oos_verify.py` 函数体内 `_require_cfg` | 0 (全部模块级) |
| Web 路由 (11 个 API 端点) | 不调用 generate_signals/compute_all_factors, 只读 DB + shared_state |
| 调度器任务: attribution | run_oos_check 调用正确, 其他逻辑不变 |
| 调度器任务: factor_cache | FactorStore.materialize() 正常, 不经过 pipeline |
| 回测链: loop.py | kwargs 含 factor_store, compute_backtest_ic 参数正确 |

**Web 页面"无数据"根因**: signals 任务崩溃 → shared_state 为空 → positions/performance 空表。
调度页面 (task_runs) 读 DB 应正常显示 (已确认 DB 有数据)。
修复后 signals 任务重新执行即可恢复。



---

## 已确认修复 (无需再排查)

以下问题曾出现在审计报告中，已由之前版本修复，当前代码无问题：

| 问题 | 修复版本 | 说明 |
|------|---------|------|
| **benchmark_tracking alpha=None** | test-v270+ | `record_daily()` 接收 `yesterday_equity` 参数，`strat_ret = strategy_equity / yesterday_equity - 1` 正常计算。DB 中旧的 None 值是修复前的残留数据，下次回测自动写入正确 alpha。 |
| **"no running row found" 双写警告** | test-v273 | `orchestrator.py:76` 已委托 `_tk_finish` 给任务自身的 finally 块，不再重复调用。最近日志零出现。 |
| **B2: 252→244 年化天数** | test-v279 | `loop.py` 使用 `np.sqrt(244)` 和 `len(returns) / 244`，非 252。 |
| **L2: next_ret 收益率计算** | test-v279 | `(next_close[s] / today_close[s] - 1)` 正确计算前向收益率。 |
| **PR2/PR3: execute.py** | test-v279 | `execute.py` 已删除，拆为 `broker.py` + `bridge.py`。原问题自然消除。 |
| **factor_repo.py SyntaxError** | test-v287 | Line 119 `list[str]` 语法在 Python 3.14 合法，该错误为旧版本残留。 |
| **parallel.py IndentationError** | test-v287 | Line 41 缩进已修复。 |

**注意**: 下次会话如遇到上述问题，请先检查是否为旧 DB 数据残留或代码版本过旧，不要再重新排查。

---

## test-v296: 因子缓存自动裁剪 — factor_cache_max_days: 244

**动机**: 因子缓存无限膨胀 (7.6GB for 133 天), 3 年全量回测仅需 244 天窗口。

**修改**:
- `config.yaml` — `backtest` 下新增 `factor_cache_max_days: 244`, 带来源注释
- `quant/factor/store.py` — 新增 `trim_to_max_days(max_days)` 方法: 以最新物化日期为锚点, DELETE WHERE date < cutoff
- `quant/scheduler/factor_cache.py` — 物化成功后调用 `fs.trim_to_max_days()`, 裁剪失败不阻塞 (非致命 warning)

**设计约束**:
- 裁剪锚点为最新物化日期, 非系统时间 (避免调度间隔内误删)
- max_days ≤ 0 → 跳过 (保留全部, 开发调试用)
- 因子值 = 衍生数据, 丢失后可 force 重建
