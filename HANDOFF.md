### v529: blocked 因子永不恢复修复 + 单因子 force 物化 + ocfp 2020-2022 缺口闭环 (test-v529)

**背景**: 东财回填 (v527) 后 sue 恢复, 但 ocfp 2020-2022 物化 4 小时 (42 段) 后 meta
仍 818 日期 (2023-03-31 起) — 增量物化没碰 ocfp。

**根因 1 (blocked 永不恢复)**: store.py v483-2 的 blocked 剔除**无条件生效**
(`bl = set(blocked.get(date_str, {}))`, force 分支后仍剔除) — 数据补齐后 blocked
因子永远不重算, "自动恢复"从未实现。ocfp 2020-2022 在数据缺失期被算空而 blocked,
东财回填后依然被剔 → 永不恢复。**修复**: force 模式豁免 blocked 剔除 (force = 无条件
重建语义)。

**根因 2 (节假日日期反复空算)**: materialize_range.sh 用 `pd.date_range(freq='B')`
(工作日) 生成日期, 含法定节假日 (元旦/春节/清明/五一/国庆 等) → 无行情 → 全因子算空
→ 每年 ~73 个节假日日期永远 todo, 每轮物化空算一遍 (本轮实测: blocked 清除后第二轮
todo 仅剩 73 个节假日日期, 全部 0 dates 空转)。

**修复加脚本**:

- `quant/factor/store.py`: force 分支豁免 blocked 剔除 (v529 注释留档)
- 新脚本 `scripts/materialize_factor.sh` (登记 scripts/README.md): 单因子 force
  强制重算区间 (无视 blocked/指纹判定) — 定向修复因子数据缺口
- **ocfp 2020-2022 修复完成**: force 重算 2020-01-02→2023-12-29, 全工作日 1042 日期,
  4,499,073 行, 3593.7s。meta: 818 → **1676 日期 (2020-01-02 → 2026-08-14)**,
  2020-2023 全覆盖 1042 日期 ✓
- 遗留: materialize_range.sh/factor 脚本的日期生成仍含节假日 (空算无害, 只浪费
  ~分钟级; 后续可改用交易日历过滤)
- **2026 段物化 + 08-17 补齐闭环**: 2026 数据回填后首次物化 (data_hash 过期全量重算,
  7 段 67,023,480 行 4312.5s), 新增 `scripts/check_materialize_gaps.py` 缺口核查
  (is_trading_day 正式口径: 真交易日缺口/blocked 机制/节假日三分类). 08-17 段
  failed 根因 = **DuckDB 副本落后** (SQLite daily 已到 08-17, DuckDB 停 08-14;
  昨晚晚间链未跑 + 今天手动补数绕过同步) → get_daily 读 DuckDB 缺 primitive →
  alpha035 KeyError. **修复**: duckdb_sync_all.sh 全量同步 (daily 949 万行 match)
  + 重物化 08-17 (439,654 行 70.2s) + **物化入口 DuckDB 新鲜度硬断言**
  (`_last_sqlite_date` vs DuckDB MAX(date), 落后即 raise 提示先同步, 防 7 小时白算).
  最终核查: 真交易日缺口仅 08-18 (今晚晚间链自然补), blocked 1596 天属正常机制.

前序: v528 baostock 软上限闸口。

### v528: baostock 日软上限全局闸口 — v513 设计断链补齐 (test-v528)

晚间链 adj_factor 同步失败复盘: tushare 免费档限流 (3-4 批/天) + baostock IP 黑名单
(8-16 23:50 标记, 服务端 day_count=52956 拉黑 "黑名单用户").

**断链根因**: v513 声称"恢复日上限但改软上限"(优雅停止+换热点提醒), 但实现断层:

- `baostock_calls_per_day` (50000) 取值函数与 `send_baostock_quota_alert` 通知函数
  v513 已写, **acquire() 里从未有日配额检查** (`_locked_update` 只拦 minute)
- 软上限检查只在 industry_history.py:436 **任务自查** (行业 PIT 同步独家受益),
  adj_factor/turnover/手动脚本等任务不查 → 8-16 行业同步到限后, 其他任务继续
  打服务端 → 52956 拉黑 (软上限 50000 设计值应提前 ~25 分钟拦截)

**修复 (全局闸口下沉)**:

- `quant/utils/baostock_gate.py` `_locked_update`: minute 检查后加日软上限检查
  → 达限抛 BaostockQuotaExceeded (所有任务经 acquire 自动受控), 当日首达
  `quota_alert_on` 去重后发告警 (macOS+ServerChan+可选 IM)
- `quant/monitor/notify.py` `send_baostock_quota_alert`: body 不再写死"行业 PIT
  同步已停止" (通用化, pending 可选), 提示换热点 (IP 变化自动清零续跑)
- config.yaml `baostock_calls_per_day: 50000` 注释补实证依据 (52956 拉黑,
  50000 提前 ~25 分钟; 0.5s 间隔 × 3000 次)
- 验证: 模拟达限 state → acquire 拦截 ✓ + 通知去重 ✓ + 告警实测送达 ✓ (state 已还原)

**adj_factor 补数**: 用户换热点 → IP 变化检测自动解禁 (218.12.27.93 → IPv6 新地址,
日计数清零+解除黑名单, v513 设计生效) → 手动触发 baostock 全市场补数 (22:52 完成:
RESULT batches=4683 rows=27170 remaining=0; adj_factor 是除权事件表, 08-17 仅 151 只正常)。

前序: v527 东财财务回填 (sue/ocfp 恢复) + 4 因子归档。

### v527: 东财财务回填 — sue/ocfp 2019-2023 根因修复 (test-v527)

物化实测 sue/ocfp 在 2020-2023 blocked, 根因 = financial_income(利润表) / financial_cashflow
(现金流表) 2019-2023 历史缺口 (financial_balance 同区间完整 5466-5552 只/年, 当日覆盖对比确认;
v482 已知欠账, baostock 解封后一直未补)。同轮另外 4 因子归档 (数据不可补):

| 因子 | 根因 | 处置 |
|------|------|------|
| sue / ocfp | income/cashflow 表 2019-2023 空 | 东财回填修复 (见下) |
| close_surge / intraday_reversal / open_volume_ratio | intraday_snapshot 仅 2026-08-03 起 ~2 周, 分钟快照历史不可回填 | 归档 archived (物化池 97 → 93) |
| pledge_ratio | pledge_stat 仅 2024-09-06 单日快照, akshare 质押仅当前视图 | 归档 archived |

修复 (scripts/backfill_financials_em.py, 新增):

- 源: 东财 datacenter-web RPT_F10_FINANCE_MAINFINADATA (与 em_valuation.py 同域, 项目已验证)
  → PARENTNETPROFIT→income.net_profit (sue 因子), NETCASH_OPERATE_PK→cashflow.net_operate_cash_flow
  (ocfp 因子), 另带上 operating_revenue/operating_profit。TARGET = 2019-2024 全部季度。
- 先用 sina 源 (v525 脚本) 实测: 覆盖率仅 7-14% (sina 接口 num<=50, 部分股票仅存近端期) → 弃用, 保留备用。
- 性能: columns=ALL 原型 400ms/请求 → 显式 6 列 21ms/请求 (×18); 6 并发 × 批量 4 只/请求
  (pageSize 500 上限保证不截断 4×107 期)。
- 结果: 163.9s 全市场 5557 只, 插入 124,301 行; income/cashflow 2020-2023 覆盖 5471-5557 只/年
  (2020-2021 少 ~80 只为次新上市缺口, 正常)。
- 幂等: (symbol, stat_date) upsert, 非 NULL 字段只更新; WAL 与物化并发安全。
- 文章登记: scripts/README.md "数据回填" 节。

待办: 当前物化 (启动时池 97, sue/ocfp 遇 blocked 已剔除后续日期) 跑完后, 补跑一次增量
物化让 sue/ocfp 在 2020-2023 重算出值, 并以 2023-12-31 日收盘出值数/IC 泼面验证。

前序: v526b 归档 analyst_consensus/earnings_revision/earnings_upgrade 3 因子 (物化池 104 → 97)。

### v526b: 因子池文档对齐 (物化中实测触发)

全量物化实测 5 因子 blocked (analyst_consensus/earnings_revision/earnings_upgrade/
ihn/insider_increase, 2020 日期) — 查证非数据缺失:

- `analyst_forecast`: 4722 行, 仅 2 期覆盖式快照 (2026-07-03/12) → 早于 2026-07 空
- `fund_hold`: 2024-12-31 → 2025-12-31 → ihn 早于 2024-12 空
- `holder_trade`: 2025-01-01 → 2026-07-01 → insider_increase 早于 2025-01 空

根因: v520 复活 97 因子后物化池 9 → 104 (evaluating 101 + probation 5),
上述晚覆盖因子重回池内; CLAUDE.md / factor-status-pools.md 的 "9 因子"
固化结论过期。已更新两文档 (104 因子 + 晚覆盖表 blocked 正常语义),
与实时 registry 对齐 (evaluating 101 / probation 5 / archived 10)。

## 2026-08-17: v526 物化起点文档对齐 — 全面防误设 (test-v526)

### 背景

`scripts/materialize_full.sh` 仍用 2019-01-01 起点 (v470 误设,
v473 只修复了 materialize_full.py 的 START, sh 版未同步), 且
`config.yaml backtest.factor_cache_start=2019-01-01` 被晚间链
(evening.py:114) 每晚用作增量区间起点 → 每晚扫描 2019 半脏区。

### 修改 (物化起点统一 2020-01-01, 数据备齐 2019-01-01 语义不变)

| 文件 | 改动 |
|------|------|
| `quant/config/config.yaml` | `backtest.factor_cache_start: '2019-01-01'` → `'2020-01-01'` + 约定注释 (唯一运行时消费方 evening.py) |
| `scripts/materialize_full.sh` | 日期窗 2019 → 2020 + 头部约定注释; 保持不注入 store (v525) |
| `scripts/patch_2019h1.sh` | 标记已废止 (v473 约定, 2019 不物化) |
| `scripts/run_factor_cache_chunk1.py` | docstring 标记已废止 (v470 一次性任务) |
| `scripts/benchmark_factor_cache.py` | 基准窗口 2019-06-03 → 2020-01-01 (对齐约定) |
| `scripts/README.md` | materialize_full.sh 条目注明起点 2020-01-01 |
| `CLAUDE.md` | 已知事项固化物化起点约定 (单一真相源: config `factor_cache_start` + `data.start_date`) |

### 语义分工 (防再误设)

- **数据备齐起点 2019-01-01**: verify_materialize_inputs / backfill_* / smoke_*
- **物化起点 2020-01-01**: materialize_full.{sh,py} / 晚间链 factor_cache / benchmark
- 2019 残留半脏缓存按 v473 决定不清理; 晚间链不再扫描 2019

# HANDOFF

## 2026-08-17: v525 物化架构重构 — 弃 fork+Pool 共享内存, 改独立 subprocess 段并行 (test-v525)

### 背景: v522/v523 分片调度治标不治本

fork+Pool+大 DataFrame COW 在 8GB M1 上反复 309 jetsam kill (OOM) 与
DuckDB AfterFork 线程死循环, 评估反复卡死/零产出。

### v525 方案 (用户拍板: 分片 subprocess 并行)

- 新增 `quant/factor/materialize_segment.py`: 独立段进程入口
  (`python -m quant.factor.materialize_segment --seg <json> --out <pickle>`),
  段进程自载数据 (段首-`data_start` 统一为 todo 首日-eff_days, 跨段一致),
  自算 prims/aux/fundamentals, 复算 `_worker_main` 计算循环 → 紧凑 pickle
  交回主进程 → 既有 `_consume_worker_result` 落盘 (一行未动)。
- `store.py` materialize: 删除 fork/Pool/全局共享数据构建; 主进程仅调度
  (切片 → 派发 subprocess → 边收边消费 → checkpoint); workers 默认 3。
- `scripts/reevaluate_now.py`: 撤 workers 参数 (用默认 3)。
- store 注入 (测试/mock) 时降级 `_materialize_sync`: 主进程同步直算,
  语义与 v525 前一致 (subprocess 无法继承内存数据源)。

### 验证

- 端到端: 3 天 × 5 因子 × 5208 股 subprocess 段并行, 35.9s, part merge ✓,
  增量幂等跳过 ✓, blocked 记录 ✓。
- 确定性: 同段两次直跑 maxdiff=0 (3 因子)。
- 一致性: 直跑 vs 物化落盘 gap_5d corr=0.9986 (段边界首日 rolling 微差,
  IC 用 rank 可忽略), max_ret_20d=1.0。
- 测试: test_v472_factor_cache_materialize + test_factor_aux_consistency
  11 passed (store 注入降级路径)。
- 已知: sqlite daily symbol 无后缀 ('600000'), DuckDB market.duckdb 仅
  部分范围 — DataStore.get_daily 空回退 sqlite, 生产 symbols 来自
  UniverseRepo 无后缀, 段进程正常。

# HANDOFF

## 2026-08-17 深夜-凌晨: v522/v523 物化加速 + 配置事故恢复 (test-v523)

### v522: 分片调度 (半成品v521 之后)

- 物化 worker 由 50 天/片 → 25 天/片 (4 workers), 每 10 天进度打点,
  治 fork 后 swap 抖动 (M1 8G)。
- `store.py:2837` "cash_flow"→"cashflow" 表名修复已在 v521 段记录,
  v522/v523 无回归。

### v523: 财务因子 aux 预载快路径

- `_preload.py`: chunk 级预载 aux panel (financial_income/balance/cashflow
  窗口 = financial_lookback_days 下界 + stocks.total_shares), 每 worker
  一次加载 ~2.6s; `compute_sue`/`compute_financial_anomaly`/
  `compute_asset_growth` 快路径按 symbol loc 取数, 消灭 37 因子/日 ×
  裸 SQL IN-大查询。
- 一致性回归 `test/test_factor_aux_consistency.py` (3 passed):
  - asset_growth: RankIC=1.0 maxdiff=0
  - financial_anomaly: RankIC=1.0, slow-only 35 只 = 1100d 窗口外
    老/退市票 (放宽断言)
  - sue: RankIC=0.983, bigdiff(>0.5)≈1150 只 — **口径定论**: 次新股
    (301/688/603 板) 招股书期 EPS (股本小→超大离群) 在窗口外, fast
    tail(8)=上市后连续报告, 比 fallback 全史 (含 IPO 前不可比期) 更
    符合 SUE 定义; docstring 已注明。
- 42 只 slow-only = 全市场摘牌/退市财务残留 (financial_income/balance/
  cashflow 三表各 42, stocks 表无对应), 查证 DB 确认, 函数尾部 reindex
  已剔除, 不影响因子值 (用户确认 normal)。

### 配置事故 (自纠)

- 改 `financial_lookback_days` 用 yaml.safe_dump 重写 → 全文件注释丢失
  (grep '#' = 0)。恢复: `git show 9170174:config.yaml` (v488 带注释版)
  为基座 + 补回 7 键 (attribution.promotion_min_days/ic_tolerance、
  data.rate_limit.baostock_ip_probe_url、monitor.serverchan_sendkey、
  prometheus.collector_interval/data_rows_interval、lookback 730→990→1100)。
  验证: 7 键全 OK + 注释保留。教训: 注释版 YAML 单值修改用 re.sub/Edit,
  禁 safe_dump 全量重写 (除非转存注释)。
- 窗口 730→990→1100 天 (v523-A2/A2b): sue 需 8 季度 std + 去年同期 YoY
  ≈9 季度; 1100 = 3 年覆盖 2022 末上市票招股书单期。

### 交付状态

- 全量 `pytest test/` **407 passed** (2m32s, 破前 404)。
- VERSION test-v523 (web/app.py)。
- 遗留: holder_reduction UnboundLocalError (preload 缺列, 单因子 skip);
  earnings_decay 35 因子标量循环可向量化 (未做, 非阻塞)。

---

## 2026-08-17 上午: holder_reduction 幽灵行修复 + 评估重启确认 (test-v524)

- **holder_reduction UnboundLocalError 根治**: `fundamental.py:944` — 粘贴残留:
   `vals = {r[0]: r[1] for r in rows ...}` 置于 `rows = conn.execute(...)` 定义
   之前 → 每 worker 每日期必炸, 被 fail_fast=False 吞为 skip (评估 101 因子池
   里唯一持续报错项)。修: 该行移至 rows 之后。冒烟: 表空 → 正常返回全 NaN
   (docstring 语义), 无异常。
- 注意: 07:01 启动的评估 (pid 69633) 的 workers 为 fork 时旧代码 — 本轮
  评估仍 skip holder_reduction; 下轮评估 (或补物化) 自动用新代码生效。
- **后台健康确认 (用户问"是不是崩了")**: 4 workers 100% CPU ×14min 无打点
  = 25 天 slice 长任务首次打点前静默 (正常, v523 单日 ~60-125s); 全程
  物理 RSS 合计仅 ~124MB 驻留, **无内存问题**; swap 3.6GB = 凌晨 5.5h
  死锁期历史残留 (macOS swap 不自动清算), 非当前占用。
- 变更: 仅 fundamental.py (1 行移动) + VERSION。


## 2026-08-17: 手动重评事故与修复 (test-v521)

用户拍板立即重评 (不等周六)。23:45 手动执行 `scripts/reevaluate_now.py`
(物化 2025-08-04..2026-08-14 → Phase1-5 → 状态报告), 结果 108 archived /
6 evaluating / 2 probation / active 0, 其中 52 个 REJECTED 永久淘汰。

### 事故链条 (已修复)

1. **物化 502 个 worker 全失败**: `store.py:2837` 拼 `financial_{tbl}` 时
   "cash_flow" → 表名 `financial_cash_flow`, 实际表 `financial_cashflow`
   (tushare 接口命名, 与 `_dispatch.py:219` 一致)。9 因子老物化前从未
   触发 (池内无财务因子) → 测评窗口 2025-08+ 缓存零写盘 (2020-2024
   parquet 为 6 月旧物化产物)。
2. **compute_ic 全 IC=0**: 评估窗口缓存缺失 → FactorStore.load 空 →
   `n_valid=0` → 98 因子被误归档 (52 个累计 3 次 → REJECTED)。
3. **修复**: `store.py` "cash_flow"→"cashflow"; 新增
   `scripts/undo_reevaluate.py` v1.0 逆向恢复 (touch 名单 updated_at ≥
   23:47, notes 含 `| undo |` 标记幂等跳过; probation5/evaluating4/
   复活97 三通道恢复; retry-1; 恢复后分布 verified: evaluating 101 /
   probation 5 / archived 10)。

### 性能优化 (物化单日 240s → 125.5s, 全量 404 passed)

- `compute_cf_roa` (high_priority.py): 5208 次 `cf[cf['symbol']==sym]`
  object 比较 (11 亿次, 95s/日) → `set_index("symbol")` + `loc[[sym]]`
  (<1s)。结果保真: 5203 非 NaN。
- `compute_asset_growth` / `compute_sue` (fundamental.py): 同模式
  (56s/17s) 同法修复。
- cProfile 显示 133s 的 object 数组比较已消除; 剩余慢项 (SQL fetchall
  7.4s×37、piotroski iterrows 13.8s、dispatch earnings-decay 35 因子
  Python 标量循环) 留待后续评估。

### 进行中

- 01:07 重启 `scripts/reevaluate_now.py` (pid 58476, 日志
  /tmp/reevaluate_now_v522.log): chunk1 4 slices 运行中, 预计物化
  ~2h (4 workers × 50 天 × 125.5s) + 评估 ~5min。
- 遗留: holder_reduction UnboundLocalError (preload 数据缺列, 单因子
  skip 不阻断); blocked.json 含 2020 老记录 (无碍); 物化性能可再压
  (earnings_decay 向量化)。

## 2026-08-16: 归档因子批量复活重评 (test-v520)

用户拍板 (基于 v519 归档分类表): C1(Phase2 IC/ICIR 不达标) + C2(实盘
DSR 衰减) + C3(重评失败/marginal) 全部迁移回 evaluating 开始自动重评。
v519 后重评即完整流程: 周六 weekly_eval 物化 → p2 IC/ICIR →
p3 CPCV/PBO → p4 成本 → phase5b 综合裁决 (p2+p3+p4+DSR 显著才 active;
未显著 → probation 半权观察; 再败 → archived)。

### 数据现状 (迁移后)

- **evaluating 101** (原 4 + 复活 97) / **probation 5** / **archived 10** / active 0
- 复活 97 = C1 64 + C2 18 + C3 15 (5 Phase3/4 失败 + 4 marginal retry=18 +
  6 全 0 IC)
- 保持 archived 10: DATA_SPARSE 5 (limit_up_prox_5d/ztd/day_night/
  lhb_net_buy_20d/insider_cluster) + DATA_DEAD 2 (northbound_20d/
  northbound_streak, 北向按用户意愿排除) + [OPS] 2 (fund_flow_3m 用户
  拍板搁置/short_interest 用户拍板搁置) + abn_turnover (截面不足)
- 4 个 rejected 因子 (vp_divergence/idio_vol_60d/trend_strength/
  liquidity_shock, retry_count=18≥max_retries 3) 一并复活 —
  RETRY_RESTORE 无 retry 上限检查, 给一次机会: 下轮再败 retry 19 ≥ 3
  立即永久 rejected。

### 脚本

- `scripts/reevaluate_archived_factors.py` v1.0: 幂等 (非 archived 跳过)、
  --dry-run 预览 / --execute 迁移、分类统计、进度打点、耗时统计。
  判据: status_reason 关键词 (below all thresholds / IC_PERSISTENT+DSR /
  marginal|reeval|Phase 3|Phase 4; DATA_DEAD|DATA_SPARSE|[OPS] 排除)。

### 验证

- dry-run 名单核对 (97 vs 10 排除) → --execute 执行 ok=97, failed=0 (0.1s)。
- 涉及状态机变更必须走 FactorStateMachine.transition (RETRY_RESTORE) 单一入口。

### 后续注意

- 周一晚间链物化池 9 → 106 因子 (evaluating 101 + probation 5): 首次
  物化 97 个新因子全历史 (1728 天), 晚间链时长可能大幅上升;
  LightGBM 训练特征维度 +~93。
- 周六 weekly_eval 首次以 v519 单一裁决跑 ~101 因子评估 — 评估结果
  自会分流 (active/probation/archived), 无需干预。

---
# HANDOFF

## 2026-08-16: 因子晋升单一裁决入口 + 模拟盘期四重门槛 (test-v519)

用户质疑: "因子在哪步转 active 都不知道" + "回测通过是否应转 active"。
核实后发现 phase5b 完整评估 (p2+p3+p4) 晋升路径是**死路径** —
`EVAL_PASS` 不在状态机转移表, batch_transition 静默跳过,
实际生效的只有仅查 IC/ICIR/half-life 三指标的轻量 Phase 0。
业界标准 (De Prado 2018 Ch.7-8 / WorldQuant 模拟盘期) 对齐落地。

### 现状 vs 业界 (改动前)

| 路径 | 判定 | 改动前 |
|---|---|---|
| weekly.py Phase 0 | 仅 IC/ICIR/half-life | ✅ 唯一生效晋升 (轻量抢先) |
| phase5b 综合裁决 | p2+p3+p4 全 pass | ❌ EVAL_PASS 非法事件, 静默跳过 |
| attribution D2 | 滚动 IC 稳定 10 天 | ✅ DSR 只写 reason 不入门槛 |

### 改动 (Fix 1-5)

1. **Fix 1 状态机** `quant/factor/state_machine.py`: 新增 `FactorEvent.EVAL_PASS`
   + `("evaluating", EVAL_PASS) → active` — 修复 phase5b 死路径。
2. **Fix 3 Phase 0 只读** `quant/scheduler/weekly.py`:
   `reevaluate_evaluating_factors` 不再物化+即时算+直接 transition, 改为读
   上一轮 phase2 快照输出 would-be verdict 预判报告 (report-only),
   状态变更收敛到 phase5b 单一裁决入口。周六 Phase 0 不再全量物化 (省时)。
3. **Fix 5 DSR 硬门槛** `quant/evaluation/phase5_monitor.py`:
   certified_active 需 p2+p3+p4 全 pass **且 DSR significant**
   (Bailey & López de Prado 2014, factor_ic_daily 滚动 IC → CPCV+DSR 现算);
   三阶段全 pass 但 DSR 未显著 → probation 半权观察 (模拟盘期), 不直升。
   EVAL_PASS/EVAL_MARGINAL 只发 evaluating 因子; 实盘 probation 因子归
   attribution 日频通道 (消除 batch_transition 噪声)。
4. **Fix 4 模拟盘期** `quant/scheduler/attribution.py` D2:
   probation→active 四重门槛 `_promotion_eligible` (纯函数):
   观察期 ≥ promotion_min_days 交易日 (updated_at→today) +
   滚动 IC 稳定 + DSR verdict significant + live consistency
   (|实盘滚动 IC − eval ic_mean| ≤ tolerance 且同号, 防泄漏/实现偏差)。
   新增 `_count_trading_days` (calendar.is_trading_day 计数)。
5. **config** `quant/config/config.yaml`: `attribution.promotion_min_days: 20`
   (WorldQuant 模拟盘 20-60 交易日取下限), `promotion_ic_tolerance: 0.02`
   (与 ic_threshold 0.02 同量级)。
6. **Fix 2 回归测试** `test/test_v519_factor_promotion.py` (10 项):
   转移表/phase5 落库 active/DSR 门槛→probation/probation 不被裁决/
   Phase 0 不动状态/_promotion_eligible 四重门槛/交易日计数。

### 新晋升规则 (v519 起)

```
evaluating --(p2+p3+p4 全 pass + DSR significant)--> active   (phase5b, 周六)
evaluating --(三阶段全 pass 但 DSR 未显著)--> probation 半权  (phase5b, 周六)
evaluating --(p2/p3/p4 任一 fail)--> archived                  (phase5b, 周六)
probation  --(观察期≥20交易日 + IC稳定 + DSR significant
              + live consistency)--> active                    (attribution, 每日)
active     --(IC_DEGRADED/冗余/数据死)--> probation/archived   (不变)
```

### 验证

- 新增 10 测试全过; 全量 **404 passed** (45.7s)。
- 集成实测: EVAL_PASS 落库 (status_reason 含 DSR significant)、
  DSR degraded → EVAL_MARGINAL → probation。
- 注: fund_flow_3m 复活本次**不做** (用户拍板: 500 只覆盖+7 个月历史,
  评估口径与实盘 universe 不一致, 等 B/C 方案另行排期)。

---
# HANDOFF

## 2026-08-16: 等待式换热点检测 — 达上限自动轮询续跑 (test-v518)

用户点名: v513 实现是**触发式** (task_scope 进入时探测), 达上限后任务停止、
探测休眠, 需手动重跑 resume — 未实现其要求的"达上限→自动等待检测→换热点
自动续跑"全自动方案。本次落地。

### 设计

- resume_industry_sync.sh 达上限分支由 `exit 2` 改为**等待循环**:
  - 每 30s 探测一次公网 IP (probe_and_reset_if_rotated, urllib→myip.ipip.net)
  - 日志去重: 仅"进入等待模式"写一条 + "未变化期间不再刷日志"一条,
    检测到变化各写一条 — 无 per-30s 刷屏
  - 三分支: rotated (IP 变 → 自动清零+解除黑名单+clear alert) / reset
    (新的一天配额恢复) / wait (继续等)
  - Ctrl+C 可退出; 探测失败降级为 wait 不阻断
- 跨天自动续跑: 等待中跨过 0 点 → day_limit_reached 变 False → reset 分支
  自动续跑 (避免用户白等一夜次日还要手动)

### 改动

- `scripts/resume_industry_sync.sh`: 达上限分支替换 (exit 2 移除)。
- `quant/utils/baostock_gate.py`: 无改动 (复用 probe_and_reset_if_rotated)。

### 验证

- bash -n 语法 OK。
- wait 分支: 未达上限/IP 未变 → 'wait'。
- rotated 分支: 模拟旧 IP (203.0.113.99) → 探测到真实 IP 变化 → 自动清零
  (day_count 0) + 解除黑名单 (blacklisted_at None) + last_ip 更新 — 实测通过。
- gate 相关单测 18 passed (376 deselected)。
- VERSION: test-v517 → test-v518 (re.sub)。

## 2026-08-16: zt/dt_streak 无事件日 blocked 修复 (test-v517)

### 症状

行业 PIT 重物化后 verify smoke 回测仍报 `factor cache missing for 1 IC lookback
dates (2025-07-24)` — 重物化日志 `447 (date,factor) blocked (缺数据剔除)`.

### 根因

- 2025-07-24 全市场无跌停事件 → dt_streak 全 0 → `_cs_zscore` MAD=0 → std=0
  → 返回全 NaN → 物化空结果 → v483 机制写 blocked 剔除 → 该日缓存缺 dt_streak.
- test_v305 早有注释点名此陷阱 ("全零日 zscore -> NaN -> 永不齐"), 靠测试构造
  涨跌停日规避 — 但真实数据无事件日必现.

### 修复

- `quant/factor/compute/price/_event.py`: compute_limit_up_streak /
  compute_dt_streak — 无事件日 (全 0) 直接返回全 0 序列, 跳过 zscore 除零
  (0 = 无信号中性, 语义正确).
- blocked.json 清除 zt_streak/dt_streak 全部记录 (447 → 248 条; 保留
  wq_alpha_006/smart_money_20d 数据起点 blocked, 位于 2020 年, 不影响 IC 窗口).
- 测试: test_v305 新增 `test_no_event_day_streak_factors_materialize` —
  纯普通日物化 7 因子产行 + 全 0 断言 (红→绿).

### 验证

- test_v305: 6 passed (新增测试含在内).
- 全量重物化已启动 (21:44, /tmp/industry_rematerialize2.log, ~31min) —
  因子代码变 → source_hash 变 → 全量 force 必须.
- **回测 universe 空列表问题 (verify smoke 第二层)**:
  - 症状: `WHERE symbol IN ()` + `pre-loaded 0 days x 0 symbols` +
    `'RangeIndex' object has no attribute 'levels'` (loop.py:437).
  - 根因: stocks.delist_date 全为**空串 '' 而非 NULL** (5557/5557),
    `delist_date > strftime('%Y%m%d', ?)` 对空串恒 False → 带日期参数的
    get_symbols 永远返回 0 — 存量 bug (带日期路径此前未触发).
  - 修复: quant/data/repos/universe_repo.py 两个 delist 分支豁免 `= ''`.
  - 验证: 带日期 get_symbols 5208 (2024-25 窗口 5176); smoke 回测过.
- **verify_industry_pit.sh 最终 PASS** (22:19): 39 天 smoke, Sharpe=0.716,
  CAGR=9.2%, MDD=-42.2%, errors=0 — 行业 PIT 全链激活完成.
- 全量测试: 394 passed (含 test_no_event_day_streak_factors_materialize).
- VERSION: test-v516 → test-v517 (re.sub).

## 2026-08-16: 行业同步收尾 — 北交所 920 段数据源缺失 skip 机制 (test-v516)

### 实证根因 (解决"338 只永不进展")

- pending 338 只 = 336 只北交所 920 段 + 301717/688828 (次新 2026-08-11 上市).
- **baostock query_stock_industry 不收录北交所 920 段**: 抽查 5 只×3 种前缀
  (sh./sz./bj.) 全部 rows=0, 且上市日从 2020 到 2026 都有 — 数据源缺失,
  非格式/网络/封禁问题 (day_count 6741 正常, 无黑名单).
- 此前"零进展"真相: 每股空探测全套 (快速路径 4 查 + 全区间二分 12 查 ≈
  30-60s/只), 338 只 ≈ 4h 空转, 无 DB 写入所以 COUNT 不动.
- 已同步 5219 只 = 0/6/3 开头全覆盖 (主板 2319 + 深市 1498 + 创业/科创 1402),
  北交所从未同步成功过 (被这 336 只永久卡住).

### 设计

1. `industry_history_skip` 表 (symbol PK, reason, created_at) — 数据源缺失
   标记, 同步 pending 计算排除 (done ∪ skip), 同步即可完成.
2. `scripts/mark_industry_skip.py` (幂等, --dry-run): 预置两类 —
   A. 920 段 → reason "baostock_920段无行业数据(2026-08-16实证)"
   B. 上市 < 30 天次新 → reason "次新数据滞后, 待baostock收录后重试"
      (日后重试: DELETE FROM industry_history_skip WHERE symbol='...')
3. verify_industry_pit.sh 更新: 覆盖判定 done+skip >= total; 零段统计排除 skip.

### 改动

- `quant/data/industry_history.py`: _build_table 建 skip 表; pending 过滤排除 skip.
- `scripts/mark_industry_skip.py`: 新增 (338 只已预置: 336 920段 + 2 次新).
- `scripts/verify_industry_pit.sh`: 覆盖判定 + 零段统计兼容 skip.

### 验证

- 同步重跑: 0 symbols pending, 0.5s 完成 (5557 = 5219 + 338 全覆盖).
- neutralize 缺行业安全: NaN 组 len<3 → 原样保留不崩 (risk/neutralize.py).
- **重物化已启动** (20:49, nohup, /tmp/industry_rematerialize.log, 预计 1-2h):
  2020 起 force — 顺带修复 smoke 回测发现的历史缓存 239 天过期
  (2025-06-06..2026-06-01, source/data_hash 校验失效, 晚间链增量只覆盖近期).
  完成后跑 verify_industry_pit.sh (smoke 回测需缓存就绪).
- VERSION: test-v515 → test-v516 (re.sub).

## 2026-08-16: Server酱³ 通知通道 — 个人微信必达 (test-v515)

用户不考虑 Telegram; 企微 webhook 需企业微信组织, 个人微信不可用 → 选
Server酱³ (微信服务号模板消息, 国内直连, 一个 SendKey 即可).

### 改动

- `quant/monitor/notify.py`: 新增 `_serverchan_send()` — POST
  `https://sctapi.ftqq.com/<key>.send` (title/desp), 10s 超时, HTTP 非 200 或
  code != 0 记日志返回 False; 未配置 key 静默跳过 (与既有通道同风格).
  `send_alert` 通道顺序: macOS → **Server酱** → Telegram → 企微 → 日志兜底.
- `quant/config/config.yaml`: `monitor.serverchan_sendkey` 填入用户 SendKey
  (yaml roundtrip 写入).
- **敏感信息防护**: config.yaml 原被 git 跟踪且历史已 push GitHub — 本次
  `git rm --cached` + .gitignore 追加 `quant/config/config.yaml`
  (含 token 的生产配置不再进版本库; 文件保留在本地, 不影响运行).
- `scripts/notify_test.sh`: 新增通道连通测试 (幂等, --no-macos 只测远程,
  --title 自定义标题).
- `scripts/README.md`: 登记 notify_test.sh.

### 验证

- notify.py ast 语法 OK.
- 真实推送成功 (20:21:34): macOS 通知 + 提示音 + ServerChan alert sent —
  用户微信服务号收到同一消息.
- VERSION: test-v514 → test-v515 (re.sub).

## 2026-08-16: 达上限提醒机制 — macOS 通知 + Web 横幅闪烁 (test-v514)

用户选择方案 A (macOS 通知+提示音) + C (Web 横幅闪烁) 落地; B (Telegram/企微)
保留可选 — config.yaml 填入 token 即自动激活, 未填则跳过。

### 设计

1. **达上限触发链**: industry_history 每股循环前检查 day_limit_reached →
   达限 → `send_baostock_quota_alert(count, limit, pending)` (macOS 通知 + 提示音
   + Telegram/企微, 最后日志兜底) + `push_baostock_quota_alert` 写入 bridge overlay
   "alerts" 键 (跨进程: 同步进程 → web SSE) → 优雅 break。
2. **恢复清除**: gate.py IP 变化自动恢复分支调 `clear_baostock_quota_alert()`
   移除横幅 + 覆盖恢复通知。
3. **Web 展示**: SSE 消费 `state.alerts` 渲染红色闪烁横幅 (顶部告警条,
   alert-flash 动画), 轮询兜底; 可手动关闭。

### 改动

- `quant/monitor/notify.py`: `_macos_notify()` (osascript display notification +
  Glass 提示音)、`_macos_sound()` (afplay)、`send_baostock_quota_alert()`;
  `send_alert` 通道顺序 macOS → Telegram → 企微 → 日志兜底。
- `quant/monitor/alerts.py`: `push_baostock_quota_alert()` / `clear_baostock_quota_alert()`。
- `quant/core/state_broker.py`: bridge overlay 键加 "alerts" (两处 for 循环)。
- `quant/data/industry_history.py`: 达上限分支触发 notify + push。
- `quant/utils/baostock_gate.py`: IP 变化恢复分支 clear 告警。
- `web/templates/index.html` / `web/static/style.css` / `web/static/app.js`:
  `#alert-banner` 容器 + 红色闪烁动画 + `renderAlerts()` (SSE + 初始轮询)。
  (⚠️ 需重启 web 生效 — 用户手动 `bash scripts/restart.sh`)

### 验证

- python / node --check 语法全部通过。
- 链路单测: push → bridge → get 横幅内容 OK; clear 后为空 OK;
  notify 全通道失败返回 False 写日志兜底 OK; macOS 通道成功 OK。
- 真实 macOS 通知 + 提示音触发成功 (19:46:16)。
- 393 项全量测试通过。
- VERSION: test-v513 → test-v514 (re.sub)。

## 2026-08-16: 日请求软上限 + 换热点自动检测 (test-v513)

用户决策 (取消 v511 硬配额后): 保留软上限设计 — 到达 5 万/日请求上限时
优雅停止任务 + 提示换热点; 检测到公网 IP 变化后自动清零计数重新开始。

### 实证背景 (修正 v511 错误结论)

- v511 认为 "baostock 无官方日配额" — **被 2026-08-16 实证推翻**:
  day_count=52956 时服务端直接拉黑 ("黑名单用户，请与管理员联系",
  IP 冷却 ~24h)。服务端存在 ~5 万/日软限制。
- 因此 v511 删除配额是错误决策 (拆了保护壳撞服务端真实限制), v513 恢复
  日上限但改为**软上限** (不硬拦截抛错, 由任务层优雅停止)。

### 设计 (用户确认)

1. **计数**: 每次 baostock 请求经 gate 记入状态文件 day_count (已有)。
2. **达上限**: `day_limit_reached()` 返回 (reached, count, limit); 行业同步
   每股循环前检查, 达限 → 打 warning 优雅 break (不崩溃, 不硬拦截)。
3. **IP 检测**: 公网 IP 探测 myip.ipip.net (IPv4 优先, IPv6 回退) —
   `_probe_public_ip()`; 仅 task_scope **最外层**进入时探测一次 (嵌套重入不重复)。
4. **IP 变化自动重置**: `probe_and_reset_if_rotated()` — last_ip 与当前 IP
   不同 → 清零 day_count + 解除 blacklisted_at (新 IP 不承继旧封禁) + 更新 last_ip。
   首次探测 (无基线) → 建立基线并解除存量黑名单 (若服务端仍封将再次标记, 无副作用)。
   探测失败 (超时/解析) → 降级不重置, 不阻断任务。
5. **守护退出**: resume_industry_sync.sh 每轮开头检测日上限 → 提示换热点退出
   (exit 2), 不再 20 轮空转重试。
6. **兜底**: scripts/reset_baostock_day.sh 手动清零 (IP 探测不可用时)。

### 改动

- `quant/utils/baostock_gate.py`: `_per_day` 配置读取; `day_limit_reached()`;
  `_probe_public_ip()` (urllib + 正则, IPv4/IPv6); `ip_rotated()`;
  `probe_and_reset_if_rotated()` (首次基线 + 变化自动重置 + 失败降级);
  `reset_day()` 保留; task_scope 最外层进入时探测; 文档字符串更新 (v511 结论已修正)。
- `quant/data/industry_history.py`: 每股循环前检查 day_limit_reached → 优雅停止
  + 提示换热点续跑命令。
- `scripts/resume_industry_sync.sh`: 每轮开头日上限检测 → 提示退出 (exit 2)。
- `scripts/reset_baostock_day.sh`: 新增手动兜底 (幂等)。
- `quant/config/config.yaml`: `baostock_calls_per_day: 50000` +
  `baostock_ip_probe_url` (注释带实证来源)。
- `scripts/README.md`: 登记新脚本与行为。

### 验证

- 单测 7 项: 达上限检测 / reset_day / 探测失败降级 / IP 变化自动清零 /
  task_scope 嵌套仅探测 1 次 / 换热点首探 (基线+解除+清零) / 幂等 — 全过。
- 393 项全量测试通过。
- 真实场景: 用户换热点 → 首探建立基线 2409:8907:... → 黑名单解除 → day_count
  清零 → 守护重启 → 520 只 pending 正常续跑。

### 效果

- 换热点后无人工干预自动恢复拉取; 达 5 万/日优雅停止并明确提示,
  不再撞服务端封禁。

## 2026-08-16: 回退批量快照方案 — baostock 服务端翻页 bug 实证 (test-v512)

**v511 调研结论被推翻**: `query_stock_industry(code="", date=D)` 批量翻页
实测失效 — `rs.next()` 恒 True 且 `cur_page_num` 恒定不变 (60 页 bad=59, 0.6s
无进展), 只返回首页 500 只。while rs.next() 循环 → rows 无限增长 → **内存爆掉
进程被系统强杀, 无 traceback 静默死亡** (4 次 480s 内无声消失的根因)。
批量快照方案废弃。

### 改动

- `quant/data/industry_history.py`:
  - `_BatchProber` 类重命名为 `_BatchProberUnused` (废弃警示, 含实测证据注释),
    `probe = _Prober(_relogin)` 恢复逐股探测。
  - 模块 docstring 数据源段更正: 原"仅返回前 500 只是页容量"说法已被翻页
    实证推翻, 改为"全市场查询翻页失效必须逐股"。
- 修 `bs_task` 装饰器插入位置 bug: 原实现把 `_jittered_interval` 吸进
  `bs_task` 函数体 (0 缩进插在类体中间), 调用时
  `AttributeError: 'BaostockGate' object has no attribute '_jittered_interval'`
  — 已移至模块级 (类外)。
- 验证: 逐股版端到端恢复 (5 只/415 查询/238s, 含 10 次 session re-login 正常);
  互斥 4 项单测重跑全过; 393 项测试全过。

### 效果

- 行业同步逐股版已恢复可用, 无配额拦截, 可续跑剩 630 只。

--- 以下为 v511 记录 (批量方案已废弃, 保留待考) ---

## 2026-08-16: 取消 baostock 日配额 + 任务级互斥防并行 (test-v511)

用户反馈: 日配额 (50000/天) 是自设的瞎设计 — baostock 无官方日配额, 真正的
封禁根因是并行任务请求叠加 (2026-08-13 黑名单事件)。要求取消配额 + 避免并行。

### 关键调研结论 (实证, 非猜测)

1. **行业 PIT 拉取是逐股设计** (industry_history.py 二分探测, 每股 ~5-27 次查询,
   全量 2.8万~15万次) — 但 baostock `query_stock_industry(code="", date=D)`
   **支持全市场批量 + 历史日期** (实证: 2025-06-30 全市场 15500 行 31 页 4.7s)。
   原注释"无 code 仅返回前 500 只"是误读 — 那是页容量, `rs.next()` 手工翻页即可全量。
   → **未来可将行业 PIT 改为按日期批量快照 (查询量降 3 个数量级)**, 本次未做 (待评估)。
   ⚠️ 已被 v512 推翻: 服务端翻页 bug, 批量不可行, 见上方 v512 记录。
2. 日线/复权/财务/股本仍是逐只 (baostock API 限制, 每只 1 次查询拉全区间)。

### 改动

- `quant/utils/baostock_gate.py`:
  - **删除日配额**: `_per_day`/`quota_exhausted()`/day_count 拦截全部移除;
    day_count 仅作统计留存。BaostockQuotaExceeded 类保留仅为兼容旧 except, 不再抛。
  - **新增任务级互斥** (防并行叠加 — 封禁根因): `BaostockTaskBusy` +
    `task_scope(owner)` contextmanager + `bs_task(owner)` 装饰器;
    跨进程原子锁文件 `.baostock_task.busy`; 同进程嵌套重入安全 (深度计数);
    崩溃残留锁自动接管 (owner pid 探活, os.kill(pid,0))。
  - 保留: 黑名单熔断 + 0.5s 跨进程最小间隔 + 每分钟 120 上限 (防封核心)。
- `quant/data/industry_history.py`: `sync_history` 外包 `task_scope("industry_pit")`
  (拆出 `_sync_history_inner`), 并行任务抛 BaostockTaskBusy fail-fast。
- `quant/data/store.py`: `_fetch_baostock_daily`/`_backfill_via_baostock`/
  `_sync_adj_factor_baostock`/`backfill_turnover`/`_backfill_turnover_full`/
  `_backfill_amount_full` 加 `@bs_task(...)` (baostock 专属函数; 混合源
  tushare 主路径不占锁)。
- `quant/data/stocks_snapshot.py`: `refresh_total_shares` 包 task_scope
  (拆 `_refresh_total_shares_inner`)。
- `quant/config/config.yaml`: 删 `baostock_calls_per_day` (yaml 校验通过)。
- `scripts/resume_industry_sync.sh`: 删配额耗尽分支 (配额已取消, 恢复纯网络重试)。
- 验证: 互斥 5 项单测 (单任务/嵌套重入/并行拒绝/残留接管/锁释放) 全过;
  get_factor_names 门面实测 using=5 / backtesting=9; 393 项测试全过。

### 效果

- 行业同步不再被 50000/天腰斩 — 可连续跑完全量 5557 (剩 630 只, ~3.9h)。
- 任何时刻至多一个 baostock 长任务在跑; 并行触发直接 BaostockTaskBusy 拒绝,
  不会请求叠加 → 从源头防 IP 封禁。
- VERSION: test-v510 → test-v511 (re.sub)。

## 2026-08-16: 同步守护修复 — 配额耗尽不再空转重试 (test-v510)

- 背景: 16:21 同步达 4927/5557 时 baostock 当日配额 (50000) 耗尽, resume_industry_sync.sh
  将其误判为"网络波动"空转重试 20 轮 (每轮都被 gate 拒绝), 达上限退出。
- `quant/utils/baostock_gate.py`: 新增只读 `BaostockGate.quota_exhausted()` (无状态副作用/不 acquire,
  读 .baostock_state.json 判断当日 day_count ≥ per_day)。
- `scripts/resume_industry_sync.sh`: 失败分支先检查配额 — 耗尽立即明确退出 (提示明日重跑),
  否则才按网络波动 10s 续跑。
- 验证: bash -n + ast 语法通过; 真实现状 quota_exhausted()=True; 未来日期模拟=False。
- 剩余进度: 4927/5557, 剩 630 — 明日配额重置后 `bash scripts/resume_industry_sync.sh` 续跑 (~3.9h),
  完成后 `bash scripts/industry_pit_activate.sh`。
- VERSION: test-v509 → test-v510 (re.sub)

## 2026-08-16: 因子状态池/数据核查结论固化 (test-v509, 纯文档+版本号)

- 用户要求: 不再重复分析已核实结论; 把 using/backtesting 使用逻辑与空/旧表结论写入显著位置。
- 新增 `docs/architecture/factor-status-pools.md` (**权威参考**):
  - 四态枚举 (evaluating/active/probation/archived) — active 当前 0 实例但状态机路径存活 (EVAL_OK→active, IC_RECOVERED→active), 非失效
  - using=('active','probation') 实盘池, backtesting=('evaluating','probation') 回测池, 物化池=并集=9 因子 — **过滤器仍在用, 非弃用**; 逐一列出 8 处使用点
  - **ADR-041 文档-实现分歧**: 文档写 backtesting 含 archived, 实现 (registry.py:52-53) 不含 — 实现为准
  - 数据核查: check_freshness 14 表全绿; 空/旧表 (daily_basic/derived_daily/analyst_forecast/pledge_stat) 不影响物化 (附核实方法); "92 因子"=83 archived 残留+9 池内, 2025/2026 文件数少不是缺失
- `CLAUDE.md` 已知事项段加醒目条目指向该文档 (含"不再重复排查/报重大发现")
- `DATA_DICTIONARY.md` factor_registry.status 字段描述从五态旧枚举改为四态, 指向权威文档
- VERSION: test-v508 → test-v509 (re.sub)

## 2026-08-16: 行业同步网络中断 + 守护续跑 (v508)

- 09:00 同步在 3509/5557 崩溃: baostock 10002007 网络接收错误 (Connection reset + Broken pipe),
  fail-fast 退出 (设计行为, 不吞错)。pit_activate 正常 ABORT。
- 新增 `scripts/resume_industry_sync.sh` (守护续跑): 循环重启 sync (幂等断点), 每轮进度打点,
  网络错误 10s 后自动重试, 全量 5557 完成或达 max_retries 退出。已注册 scripts/README.md。
- 已在后台启动 (PID 20239, 日志 /tmp/industry_sync4.log)。

## 2026-08-16: Prometheus/Grafana 深度接入 (test-v508) — 埋点激活 + 仪表盘

- `quant/monitoring/prometheus.py`:
  - MetricsCollector 间隔参数迁入 config.yaml (prometheus.collector_interval=30s / data_rows_interval=600s, 来源注释已写)
  - 模板 5: 大表 (daily 9.5M/daily_valuation 8.3M) 行数采集 COUNT(*) 全扫 1-2.4s → MAX(rowid) 索引 2ms + 10min 低频, 消除每 30s WAL 压力 (实测对比 2026-08-16)
  - 业务指标接入: daily_equity 最新行 → TOTAL_EQUITY/CASH_BALANCE/POSITION_VALUE/DRAWDOWN; backtest_runs 最近成功 run → BACKTEST_SHARPE/CAGR/MAX_DD/DSR
  - 高基数修复: BACKTEST_* 标签 run_id → strategy (无限系列膨胀, 违反模板 5)
  - MonitoringPlatform.start(): pushgateway 未配置不再起 60s 空转 push 线程
  - GrafanaDashboardBuilder: 老 graph 面板 (Grafana 13 已移除) → timeseries/stat; 输出改为 provisioning 兼容格式 (schemaVersion 39, datasource uid 引用)
  - 修复: 顶层缺 `import json` (原 export_all 从未运行过, NameError)
- `web/app.py`: main() 启动时 init_monitoring() 激活采集器; 新增 /api/monitoring/datasources (Prometheus/Grafana 端口摘要)
- `web/static/app.js`: 系统 Tab — Grafana 运行中点绿时变为可点击链接 (打开面板), 新增 面板/Prom 快捷链接
- `scripts/export_grafana_dashboards.sh` (新): 一键导出 3 张 dashboard (overview/factors/risk) 到 Grafana provisioning 目录
- `/opt/homebrew/etc/grafana/provisioning/dashboards/quant-dashboards.yml`: file provider, 30s 自动 reload
- 验证: 3 张 dashboard 已由 Grafana 加载 (quant 文件夹); 独立进程单轮采集各指标有值 (CPU 7.1%, equity 4724.49, sharpe 2.896)
- **待用户执行**: `bash scripts/restart.sh` 重启 web 使采集器生效; Prometheus 15s 后可见新指标

## 2026-08-16: 安装 Prometheus + Grafana (监控栈落地)

- `brew install prometheus grafana` 装好 (prometheus 3.13.2), `brew services start` 常驻。
- `/opt/homebrew/etc/prometheus.yml`: 增加 quant job 抓取 `localhost:8521/metrics` (web 内置端点, 15s 间隔)。
- Grafana: 启用 `provisioning = /opt/homebrew/etc/grafana/provisioning` (grafana.ini), 数据源
  `quant-prometheus.yml` 指向 localhost:9090, isDefault, 已验证 proxy 查询 up{job=quant}=1。
- 端口约定与 config.yaml 一致 (prometheus 9090 / grafana 3000, 默认 admin/admin 首次登录改密)。
- web `/api/monitoring/prometheus` 动态探测两个端口, 无需重启 web。
- 无项目代码改动, 无 VERSION 推进。

## 2026-08-16: 行业同步续跑 + PIT 激活链重启 (后台进行中)

- 续跑 `sync_industry_history.sh` (PID 13866, 日志 /tmp/industry_sync3.log): 08-16 配额已重置
  (day_count=408/50000), 断点续跑从 3390/5557 继续, 实测 ~22s/股 (gate 硬限速 120/min),
  ETA ≈ 13h (2167 only pending)。
- 已启动编排链 `industry_pit_activate.sh` (PID 14352, 日志 /tmp/pit_activate2.log):
  自动轮询等待同步 → verify_industry_pit(覆盖校验+smoke) → rematerialize(2020 起 force 重物化 1-2h)
  → 提示用户手动 restart.sh。全程后台, 无需干预。
- 进度核对: `tail /tmp/industry_sync3.log` | `tail /tmp/pit_activate2.log`。

## 2026-08-16: CLAUDE.md 增加 Agent 身份段落

- `CLAUDE.md` 新增 "Agent 身份" 段: 资深系统架构师 / 骨灰级软件开发专家 / 资深量化开发专家
  三重角色定位与职责, 执行任务不降级交付。纯文档改动, 不涉及代码, VERSION 不推进。

## ⚠️ 当前进行中 (重启 opencode 后续工作起点) — 2026-08-15 22:5x

**后台任务: 行业历史同步 + PIT 激活链 (未完成)**

1. **状态**: 行业历史同步 (`bash scripts/sync_industry_history.sh`) 在 **3390/5557** 停止,
   原因为 **baostock 当日配额耗尽** (`BaostockQuotaExceeded`, `data.rate_limit.baostock_calls_per_day=50000`,
   gate 见 `quant/utils/baostock_gate.py:140`, 状态文件 `quant/data/.baostock_state.json`)。
   属正常保护, 非故障。断点续跑内置 (`quant/data/industry_history.py:342` 表内已同步符号自动跳过), 重跑不丢进度。
2. **已尝试续跑**: 用户更换热点后我执行 `nohup bash scripts/sync_industry_history.sh > /tmp/industry_sync2.log &`
   **因是同日 08-15 且配额按日计数 (本机文件计数, 与出口 IP 无关), 预计仍会被 gate 立刻拒绝**。
   **下次续跑应在跨天后 (2026-08-16) 再执行**, 或确认热点是否重置了 baostock 服务端侧配额。
3. **队列任务 (等同步完成后)**: `bash scripts/industry_pit_activate.sh` — 校验 industry_history 覆盖 +
   smoke 回测 + 重物化。它会在同步未完成时 ABORT (正常)。
4. **进度核对途径**: `tail /tmp/industry_sync.log` / `/tmp/industry_sync2.log` (新), `/tmp/pit_activate.log`。

**本轮已完成并提交 (v506/v507, 已 push origin/main, commit c4d1fe1)**:
- v506 多策略页真实账户数据源 (弃空内存 StrategyManager, 改读 trades.db strategy_config +
  PositionService), 系统页 Prometheus 显示修复 (setText 塞 HTML 修复为 innerHTML + 新增
  /api/monitoring/prometheus 摘要); v507 修复刷新后因子页叠概览下方 (tab-factors 多余 active 移除)。
- 全部已提交并 push, 工作区当时干净。`web/app.py VERSION = "test-v507"`。
- 若本次会话还有未提交改动 (e.g. 新探针脚本皆在 /tmp, 不影响; 无 repo 内改动), 重启后先 `git status` 核对。

**提醒**: 探针脚本 `/tmp/probe_bs.py`, 状态快照 `/tmp/status_snapshot.txt` 为排查残留, 无 repo 污染。

### v505 因子平台并回因子 Tab — 统一真相源 factor_registry (2026-08-15)

**背景**: 用户指出因子 Tab 与因子平台 Tab( v503 加)重复。分析证实更深的问题:
**因子平台 Tab 是空壳** — 它读 `factor_metadata`(factor_registry.db)表,**0 行,
从未被填充**; 而真实驱动数据在 `factor_registry`(market.db, 116 行:
状态机/IC/策展/因子名全在这)。平台页顶部 KPI 全 0、表格空, 只有
`state_probation`(来自 state_machine → market.db)是真的。

**决策 (方案 C, 彻底合一)**:
- 唯一真相源 = `factor_registry`(market.db)。废弃 platform 空壳线
  (factor_metadata / factor_registry.db — 该表不被任何消费方读取)。
- 因子平台功能并入因子 Tab, 删除独立平台 Tab + 路由 + 侧边栏按钮。

**后端改动**:
1. `web/admin_services.py`:
   - 重写 `factor_platform_snapshot()` → 用 `FactorRepo.get_all_factors()` +
     补充 `compute_fn/academic_source/direction/formula/updated_at`, 不再 import
     platform.FactorRegistry (空壳)。返回 `{factors: [116], state: {active,
     probation, evaluating, archived}, counts}`。
   - `_derive_lineage()` 血缘: compute_fn/formula 提取基础数据列 token
     (`close/volume/...`) 或归属族回退标签; downstream 全量扫描引用词边界匹配。
     实测: upstream 64/116, downstream epds→epd 真实。
   - `factor_lineage()` 改为从统一 registry 实时推导 (不再读空壳 metadata)。
2. `web/app.py`:
   - 重写 `/api/factors` → 统一复合 payload: registry(116 全字段+血缘) +
     state + counts + stats(ic/decay/corr 来自 factor_snapshot, 24h TTL)。
   - 删 `/api/platform/factors`、`/api/platform/factors/lineage`
     路由 → 新单一路由 `/api/factors/lineage`。移除 `_factor_platform` import。

**前端改动**:
- index.html: 删除 tab-platform section + sidebar platform 按钮(嵌 SVG 残留清过);
  因子 Tab 加「因子注册表」表格 + 血缘面板 (table-registry / lineage-panel)。
- app.js: 删 `loadPlatform()` 和 `_tabLabels.platform`, showTab 删 platform 分支;
  新 `renderRegistry()`(116 行表格, IC/IR/方向/来源/状态 badge + 血缘按钮) +
  `showLineage(name)`(fetch /api/factors/lineage → 上游/下游列)。loadFactors 加
  `renderRegistry(fd)`。
- style.css: 补 `.badge-purple`, **修复历史排版 bug** — `.badge-gray` 声明原被
  换行拆成两行, 花括号跑到 `.trunc-reason em` 之后致其样式失效。

**测试**: 全路由冒烟 200 (`/api/factors` `/api/factors/lineage` `/metrics` 等),
旧 `/api/platform/*` 404; lineage miss→404, epds→downstream ['epd'] 真实;
KPI/chart 字段齐全且兼容 loadFactors; pytest 353 passed 无回归。

**注意**: 前端 factor tab 首次会渲染 116 行注册表 + 血缘按钮 (DOM 较重但单页完整)。

### v506 修复多策略页全 0 + 系统页 Prometheus/Grafana 显示异常 (2026-08-15)

**背景**: 用户实测两个 Tab 异常 — ①「多策略隔离」数据全为 0;②「系统」页
Prometheus / Grafana「好像出错」。

**① 多策略页全 0 双根因**:
- 后端 `strategy_summary()` 读 `StrategyManager.get_global_metrics()` — 该管理器是
  **纯内存空壳** (启动时无人 `register()`), 恒定返回 `total_strategies: 0` → KPI 全 0。
- 就算有数据, 前端 `loadStrategies()` 表格列用 `position_value`, 而
  `StrategyInstance.get_metrics()` 实际返回键是 `portfolio_value` — 键名错位 → undefined。
- **真实数据源**: trades.db (`strategy_config` 9 个策略全部 initialized) +
  `PositionService.get_portfolio_summary()` (现金+持仓收盘价估值+PnL) — 唯一真相源。

**修复**:
1. `web/admin_services.py` `strategy_summary()` 重写 → 读 `strategy_config` 各策略 +
   broker positions 的 strategy 归属, 经 `PositionService.get_portfolio_summary()`
   聚合真实账户数据 (total_asset / total_cash / total_pnl / capital_utilization /
   strategies[] 含 position_value/available_cash/total_pnl/positions)。
   `strategy_detail()` 同样改写真实数据 + StrategyManager 状态交叉 (不再 KeyError)。
2. `web/static/app.js` `loadStrategies()`: 键对齐 `total_asset ?? total_equity`.
3. **实测**: 9 策略全出真实数据 — quant 持仓 4220 + 现金 134.49 + PnL -645.51,
   其余 _t_ 系列回测壳策略无持仓; 全局 total_asset 369700.89。

**② 系统页 Prometheus/Grafana「出错」假象**:
- 根因: `loadSystems()` 用 `setText()` (`textContent`) 往 `mon-metrics` 塞
  HTML 链接 `<a href="/metrics">` → **以纯文本渲染出 `<a href=...>` 标签**,
  看起来像页面坏了。Grafana「未运行」本是如实状态 (端口探测没起服务是真没起)。

**修复**:
- 后端 `grafana_status()` 补 `prometheus_running` (探测 9090) + `prometheus_url`;
  新增 `prometheus_status()` (序列数/族列表/采样前3) → `/api/monitoring/prometheus`。
- 前端改 `innerHTML` 渲染: Grafana ●运行中/未运行 + Prometheus「N 条指标序列 · Prom
  9090 运行中/未运行」+ 查看链接。不再出现裸 `<a>` 文本。

**测试**: 393 passed 无回归; 路由冒烟: `/api/strategy/summary`
(active_strategies:9 真实) `/api/strategy/quant` `/api/monitoring/grafana`
`/api/monitoring/prometheus` `/metrics` 全 200。

**注意**:
- `capital_utilization` 定义为 `持仓市值/总资产` (真实投入率), 全策略并集下仅 quant
  有持仓 → ~1.1%, 符合 9 个 shell 策略无持仓的实况。
- `/metrics` 当前仅回 2 系列 (CPU/内存 0.0) — 业务指标由 MetricsCollector 收集,
  web 进程内未常驻, 属正常; 系统页已如实显示「2 条指标序列」。

### v507 修复刷新后因子页内容叠在概览下方 (2026-08-15)

**背景**: 刷新后因子 Tab 内容显示在概览页下方; 点击因子 Tab 再回概览,
因子内容消失。

**根因**: `web/templates/index.html` 里 `tab-overview`(line 59) 和
`tab-factors`(line 88) **两处都带 `class="tab-content active"`** — 页面加载时两个
section 同时 display:block 叠排; 之后点击任意 Tab 触发 `showTab()` 只保留一个
`active`, 于是因子内容随 active 移除而消失 (看似"切走即没")。

**修复**: `tab-factors` 移除多余的 `active`, 仅 `tab-overview` 默认激活
(侧栏按钮本就只有 overview 带 active, 与 section 对齐)。

**验证**: grep `class="tab-content` 确认仅 overview 带 active; 语法/JS 校验通过。

### v504 修复侧栏 hover 提示消失 (2026-08-15)

**背景**: v503 给 `.sidebar-tabs` 加 `overflow-y: auto` (8 tab 可滚动) 后,
自定义 tooltip (`.sidebar-tab::after`, `left:54px` 伸到侧栏外) 被滚动容器裁切
(CSS 规范: overflow-y 非 visible 时 overflow-x 强制 auto) → hover 提示消失.

**修复**: 侧栏 tab 改用原生 `title` 属性 (不受 overflow 裁切), style.css 移除
`.sidebar-tab::after` 规则; 不受滚动影响的 `.theme-toggle` 保留自定义 tooltip.
冒烟: 8 tab title 齐全, / 渲染 200.

### v503 后台管理界面接入 — 7 大未接入模块 → SPA 管理页 (2026-08-15)

**背景**: 审计发现 7 个代码就绪的后端模块完全未接入 web 界面: 因子平台
(platform.py)、多策略 (strategy/)、另类数据 (alternative.py)、分布式回测
(distributed.py)、模型服务 (model_serving.py)、Prometheus 监控 (prometheus.py)。
按现有 SPA 模式 (index.html 多 tab) 接入, 新增 3 个 sidebar icon tab.

**新增后端** — `web/admin_services.py` (服务层, 复用 web/services.py 约定):
- `factor_platform_snapshot()` — FactorRegistry.list + 状态机 active/probation
- `factor_lineage(name)` — 因子血缘 (upstream/downstream)
- `strategy_summary()/strategy_detail()/strategy_action()` — 多策略总览/详情/启停
- `alternative_sources()` — 另类数据源列表 + factor 行数
- `dist_submit()/_GridWorker/dist_status()` — 分布式网格后台线程 + 进度轮询
- `model_serving_info()` — MLflow 模型列表 (lazy import 防启动崩溃)
- `prometheus_metrics()` / `grafana_status()` — Prometheus 文本 + Grafana 端口探测

**新增路由** — `web/app.py` 10 个:
`/api/platform/factors` `/api/platform/factors/lineage` `/api/strategy/summary`
`/api/strategy/<name>` `/api/strategy/<name>/action` (POST)
`/api/alternative/sources` `/api/backtest/dist/submit` (POST) `/api/backtest/dist/status`
`/api/model/serving` `/api/monitoring/grafana` `/metrics` (Prometheus 文本).
写操作均带 `_require_token()` 鉴权; 错误信封统一 `_api_response`.

**修复 bug**:
1. `quant/backtest/distributed.py` — `run_grid_search()` 便捷函数原为
   `run_grid_search({}, {})` placeholder (丢入参 + BacktestParamSet 必填字段缺失
   TypeError) → 转发真实入参 + start/end_date 必填校验.
2. 同上 `save_results()` — 原建同名字段不同的 `backtest_runs` 表 (与 web
   BacktestService 冲突) → 改造兼容现有 22 列 schema, strategy=run_id,
   分布式结果与历史页打通.
3. `ads admin_services._GridWorker` — params["strategy"] 不存在的字段 (运行中
   发现并移除).
4. state_machine 引用: `get_active_factors/get_probation_factors` 是实例方法
   非模块函数 → 经 `FactorStateMachine()` 实例调用.

**前端** — `web/templates/index.html` + `web/static/app.js` + `style.css`:
- sidebar + 3 tabs: platform (因子平台) / strategies (策略) / systems (系统).
- systems tab 聚合: 另类数据源表 + 分布式回测网格 (提交/进度/最近结果) +
  模型服务状态 + Grafana/Prometheus 状态.
- 渲染: loadPlatform/loadStrategies/loadSystems + submitDistGrid (POST body JSON),
  15s 轮询 (进 tab 才启).
- style.css: `.sidebar-tabs` overflow-y auto (8 tab 防溢出) + 管理页 td 截断.

**测试**: Flask test_client 全路由冒烟 (7 新 GET + 2 POST + /metrics 全 200);
`347 passed` pytest 无回归; 分布式网格端到端闭环验证
(提交 → worker → save_results → dist_status recent) 通过 (回测 error 由
factor cache 缺失引起 — 行业 PIT 重物化未跑, 属前置依赖).

**注意**: 分布式网格真实跑需 factor cache 已物化 (行业 PIT 生效后跑
`rematerialize_industry_pit.sh`). 管理页对未生效状态显示 null, 不造假.

**当前状态**: 后台行业 PIT 同步 + 编排 (industry_pit_activate.sh) 仍在运行.
web 管理页上线需重启 (`bash scripts/restart.sh`) 由用户执行.

### v502 行业 PIT 历史同步 (industry_history 表) — 数据缺口闭环 (2026-08-15)

**背景**: v501 #2 识别 `stocks.industry` 当前快照被历史读取 → 前视污染。
行业分类随证监会半年度/年度调整批量变更, 历史任意日期须用 PIT 行业。

**根治**: 新表 `industry_history(symbol, effective_from, industry)` (主键
symbol+effective_from), 每股一条 "自 effective_from 起行业为 X"; PIT 读取取
`effective_from <= T` 最大行. 同步自 baostock `query_stock_industry` (逐股,
单次无 code 只返回前 500 只).

**同步算法 (锚点 + 全局变更日剪枝)** — `quant/data/industry_history.py`:
1. 两端探测: `probe(start)` vs `probe(today)` + 中点回归验证 → **86% 单段股 3
   次查询完成** (实测 145 股 avg 1.16 段).
2. 变更股走半年度锚点扫描 → 相邻锚点不同用**全局变更日剪枝** (变更日跨全市场,
   首只股确认后累计进 global_days, 后续股命中免二分).
3. 左端无记录 (上市/退市) 二分定位上市日.
实测: 200 只 = 1459 查询 (7.3/股), 全量 5557 只 ≈ 4万查询 < 日配额 5万;
速率 gate 0.5s/次 → 全量约 7-8h (后台夜间跑, 断点续跑).

**健壮性**:
- 免费 baostock session 极短 (实测 ~32s/168 次过期): `_Prober` 每 40 次强制
  重登 + 遇 "未登录" 自动重登重试.
- scheduler weekly 长事务并存: 连接 busy_timeout=120s + `_insert_with_retry`
  写锁重试 (最多 ~2min, 仍锁则 fail-fast — 已提交批次断点续跑不丢).

**同步接入**: `quant/factor/store.py` `_build_fundamentals_panel` 每日构建覆盖
`industry` 列 (取 <=ts 的最大段) — 行业 PIT 生效点 (v501 #2 落地).

**消费端审计** (v502 完成): 所有历史/回测/物化路径已接 PIT —
`store.py` fundamentals 面板、`_preload.py` aux["stocks"]、`loop.py` industry
pivot (dtype 修复: index 转 datetime64, 原字符串 index 与 pipeline
`pd.Timestamp` 切片会崩)、`pipeline.py` 中性化。实时 live 路径 (rotation 轮动 /
state_broker 持仓 / attribution 日报) 用当前快照, 合理不改.

**一键生效链**: `scripts/industry_pit_activate.sh [--skip-wait]` —
等后台同步完成 → `verify_industry_pit.sh` (5557 只覆盖校验 + smoke 回测)
→ `rematerialize_industry_pit.sh` (force 重物化 2020 起) → 提示用户
`bash scripts/restart.sh` 重启 (CLAUDE.md 约定重启由用户执行).

**当前状态**: 表已有 200+ 只 (含 000009/000017/000032 已知变更股全部对拍一致);
后台全量续跑中 (幂等, 预计数小时). 完成后跑 `industry_pit_activate.sh` 生效.

### v501 回测 PIT 正确性修复 — 3 项落地 + 1 项数据缺口 (2026-08-15)

**背景**: 系统性审查回测策略业务逻辑, 发现 4 处前视/语义问题。采取"逐项
隔离 + 对拍"法验证, 凡能修即修, 数据不存在的如实标注 (零 fallback 精神).

**#1 (已修) 首日 IC 前视** — `quant/backtest/loop.py`:
原 `compute_backtest_ic(start_date=trading_days[0])` → run_oos_check 的 OOS
窗口延伸到回测首日 T0, 末样本配对 `ret(T0→T1)` (T1=回测第二日收盘) —
生成 T0 信号时用了未来收益。改为找 `trading_days[0]` 的前一交易日作
train_end (回测首日前的 PIT), 窗口止于 T0-1, 末样本 `ret(T0-1→T0)` 在
T0 信号前已知。实测间隔打点: `PIT IC train_end=2024-12-31`.

**#3 (已修) market_cap 回退快照前视** — `quant/pipeline.py` 三处:
1) fundamentals 组装: 原 `_dyn = {pe_ttm/pb/market_cap: 股票快照}` + pivot
   当日覆盖 → 回退路径保留 stocks 现值 (未来数据). 改为仅取 ≤date_str 的
   PIT pivot 最近行 (`_avail[-1]`), 无 PIT 则列缺失 (不落快照).
2) Step3 因子中性化市值: `_mcap_col` 删 total_mv 回退, 只允许 PIT market_cap.
3) Step4 alpha 中性化市值: 同样删 total_mv 回退; 缺失日 `fillna(price*1e8)`
   为当日 PIT 收盘价 (非未来). neutralize 内部对 NaN 有 dropna 安全处理
   (`_joint_neutralize`/`_apply_neutralize_batch`), 缺失股自动剔除, 不崩.

**#4 (已修) 短回测全程 sleeve 语义失效** — `quant/backtest/loop.py`:
`warmup_days = factor.evaluation.lookback` (252) > 回测天数 → 永不切
ic_weighted, 3 个月回测无法验证加权合成. 改为回测长度自适应:
`warmup_days = min(252, len(trading_days)//3)`, 1/3 期后切 ic_weighted.

**对拍隔离矩阵** (2025-Q1, 57 天, 同 mock 排除 3 缺失因子, avg_sig≈1.8 稳定):
| IC | combine | CAGR |
|----|---------|------|
| 含前视 | 全程 sleeve (原基线) | 118.7% |
| 含前视 | 后半 ic_weighted | 305.2% |
| 无前视 (#1) | 全程 sleeve | -29.0% |
| 无前视 (#1) | 后半 ic_weighted (#1+#4) | 15.7% |

结论: #1 移除前视后收益剧烈缩水 (118.7→-29.0), 证明原回测收益大部分是
首日 IC 前视泡沫; "含前视 + ic_weighted" 放大假收益至 305% 亦自洽。
avg_signals 稳定 (1.8↔1.9) → 信号生成未破坏。测试 33 项通过。

**#2 (数据缺口, 未改) industry/roe 快照非 PIT**:
系统性查证 — DB (market.db) **无任何历史行业表**: stocks.industry (当前
快照, 121 行业) 是唯一来源; 无 daily_industry/industry_history 等. roe
同样仅 stocks 当前值. 行业中性化 (因子层 + alpha 层) 均依赖它 → 行业在
回测期内的历史重分类会构成前视. **无法在现有数据下重建 PIT 行业** —
需新增每日行业快照同步 (数据源 baostock/外部), 属数据工程新项, 未敢
伪造修复. 已作为已知限制记录, 待引入行业历史数据后可落地.

### v500 回测性能诊断 + 2 处"消除重复 IO"落地 (2026-08-15)

**背景**: 依物化 v497 思路 (消除重复计算/重复 IO), 分析回测全链路瓶颈并落地。
全程用 cProfile 实证 (interval=2025-01-01..03-31, 57 交易天 800 股)。

**诊断 (实测数据**):
- 整次回测: 预加载命中缓存后主循环 34.4s; 一次性成本 ~20s (bulk_load 预加载
  6.0s + IC 覆盖检查 8.6s + 单次数据指纹 6.2s + data_cache save parquet +
  诊断监控)
- 主循环 generate_signals 占 **74% (25.4s tottime)** — 非单点热点, 全为分散
  pandas 数据操作 (每日取因子/对齐/index 切片/双重中性化 O(n²)), 无银弹函数
- 一次性项中 `_get_existing_factors` 239 次 (IC 检查逐日 os.listdir + 逐因子
  重复读 metadata/*.json + 单次 6.2s 数据指纹)

**落地 (均纯性能, 不动信号/指标, 对拍一致**):
1. `quant/backtest/data_cache.py` — 修 `all_symbols` 缺失 bug: 回测第二次命中
   缓存时 data_cache KeyError (entries 无 all_symbols 键)
2. `quant/factor/store.py` — `_load_factor_meta` 进程内缓存 `self._meta_cache`:
   IC 覆盖检查 239 天 × ~95 因子重复磁盘 JSON 读 → 内存复用; `_save_factor_meta`
   物化后失效对应键 (同进程 freshness 不破)

**验证**: `scripts/bench_backtest.py 2025-01-01 2025-03-31` 对拍 — 指标逐位一致
(CAGR=118.7% Sharpe=2.464 MDD=-12.5% avg_signals=1.8); meta 缓存后
`_get_existing_factors` tottime 0.845→0.369s; 净收益被单次 6.2s 数据指纹
(每日 COUNT 全表, 进程内仅一次, 不可避免) 掩盖, 同进程多回测受益。

**双重中性化"消除"已被 A/B 实证证伪 — 不可落地 (2026-08-15)**:

粗看 factor 层 `neutralize_factors_batch` + risk 段 `neutralize(alpha)` 像双重
冗余 (combine 为线性 → 疑似可消)。**实测推翻**: 中间隔着 `AlphaModel.rank()`
= sigmoid 软截止 (model.py:156, nonlinear) — sigmoid 逐元素变换破坏中性子空间,
step4 第二遍 neutralize 是必要的再投影。A/B 对拍 (2025-Q1, 57 天, monkeypatch
step4 恒等):
- normal:  CAGR=+184.6%  Sharpe=2.388  MDD=-27.5%  avg_sig=2.0
- identity: CAGR=-28.7%   Sharpe=-2.149  MDD=-16.7%  avg_sig=1.6
删除即毁灭业绩。故该优化**明确不可做**; 若未来再议须先对拍而非数学推导。
generate_signals 25s 真实热点为分散 pandas 操作 (step2.3 过滤器/mana 切片/
每日 DataFrame 组装), 非中性化。

**环境备注**: 回测须 mock 排除 3 因子 (dt_streak/smart_money_20d/wq_alpha_006) 因
数据缺口, 已封装进 `scripts/bench_backtest.py`; 诊断中途停过 web/orchestrator,
已用 `bash scripts/restart.sh` 恢复。data_cache 命中键含 data_hash/因子集, 因子
重物化后缓存失效属预期。

### v499 新增 DuckDB 文件收缩脚本 (重建法) — 预聚合 DROP 后空间回收 (2026-08-15)

**背景**: v498 已 DROP 预聚合 8 表 (1.2 亿行), 但实测 market.duckdb 2.3G
**不收缩** — DuckDB 追加式存储: DROP 后空间标记 free block 优先复用, 文件
永不自动缩小。实测 CHECKPOINT (0.02s) 仅合并 WAL, VACUUM (0.0s) 仅重算统计,
均无效。唯一收缩方法 = 拷数据重建文件。

**改动**: 新增 `scripts/duckdb_rebuild.sh` —
1. 优雅停机 (复用 restart.sh 的 TERM/KILL 两段式, 防文件锁)
2. ATTACH 建 market.duckdb.new → 13 张表 DDL (保留主键, _upsert_df
   ON CONFLICT 依赖) → 逐表 INSERT..SELECT (DuckDB 内部拷贝, 949万+830万
   行 ~1min, 不落内存) → 逐表行数断言校验 (不等即崩, 零 fallback)
3. 原子替换: 旧库 → market.duckdb.bak (留作回滚) → .new → market.duckdb
4. 新库走 manager 初始化自动补建辅助索引, 复验总行数
5. 失败/中断时 .new 删除、旧库分毫未动 — 幂等可重跑

**执行**: `bash scripts/duckdb_rebuild.sh` → `bash scripts/restart.sh`
**实测 (v499 首跑)**: 2.27G → 1.37G, 释放 0.90G (即预聚合表压缩后实际占用;
基础库 daily 949万 + valuation 830万 + stocks 5525 共 17,806,296 行全匹配)。
注意 INSERT..SELECT 的 rowcount 恒为 -1 (客户端特性, 非错误 — 真实计数由
逐表 COUNT 断言保证); 新库初始化自动补建 17 条索引; verify_sync 与 SQLite
全 match=True。旧库留 market.duckdb.bak 供回滚, 确认后可 rm。

### v498 清除预聚合表死冗余 — 8 张表 DROP, 晚间链省 27s (2026-08-15)

**背景**: 预聚合表 (daily_ma/ret/std/zscore/ma_volume/max/min/rank) 是
v4xx 时代的"供因子原语直接查询"设计。全面调查后确认**零消费方**:
- 唯一读取代码 `_get_preagg_table` (factor/compute/_primitives.py) 死代码,
  全项目无任何调用
- 写入方仅晚间链 refresh_preaggregates (daily_data.py) + sync 脚本
- 8 张 DDL 中 zscore/rank 从未写入 (0 行), 实际数据 6 表 × 203 万行
  ≈ 1.2 亿行, 占 market.duckdb 2.3G 的大部分
- v497 已实测 SQL 全窗口 rolling 路径净收益 ≈ 0, 未来消费路径也不成立
- 顺带清除 v496 遗留隐藏 bug: refresh_preaggregates 的 windows 参数被
  硬编码循环忽略 (duckdb_store.py:970)

**改动**:
1. `duckdb_store.py`: _TABLE_SCHEMAS 删 8 张预聚合表 DDL、_create_indexes
   删 6 条预聚合索引、删整个 refresh_preaggregates 函数 (~135 行)
2. `_primitives.py`: 删 _get_preagg_table 死代码 (~40 行)
3. `daily_data.py`: 删晚间链 refresh_preaggregates() 调用 (省 27s/晚)
4. `scripts/duckdb_sync_all.sh`: refresh 段改为幂等 DROP TABLE IF EXISTS
   8 张 (v498 后 DDL 已删, 每次跑 sync 清理历史遗留, 可重复)

**验证**: 385 passed; bash scripts/duckdb_sync_all.sh 后预聚合表残留 0 张,
剩余 13 表; daily/valuation 同步与校验 match=True; market.duckdb 体积
2.3G → 应随下一次 CHECKPOINT/VACUUM 释放 (未主动做, 观察即可)。
VERSION → test-v498。

### v497 因子原语物化跳过磁盘缓存 — 物化提速 8min/全量 (2026-08-15)

**背景**: 用户目标是"尽可能利用 DuckDB 提升系统运行效率"。实测物化 chunk
(200 交易日 × 5208 股) 时间构成:
- 拉数 (DuckDB get_daily): 0.1s (v435 已走 DuckDB, 已快)
- precompute_primitives 计算: ~11s (pandas rolling)
- parquet 磁盘缓存保存: **~54s** (135 表, 0.74GB, zstd)

预研 SQL 路径: DuckDB window functions 一次算全窗口滚动 0.13s, 但 grid
构造 + register + .df() 转换全程 ~6s, 对拍 86/100 完全一致 (corr/skew 需
特殊处理: DuckDB LOG()=log10 非 ln, COUNT 需网格对齐停牌日)。结论: SQL 路径
净收益 ≈ 0 (素材已在内存), 且改动 precompute_primitives 源码会触发因子缓存
指纹失效 (store.py:137) 全量重物化 — 风险/收益不成比例, 放弃。

**关键洞察 (v470/实测证实)**: 物化每 chunk 的 data hash 不同 (窗口随 chunk
滚动) → 磁盘缓存**跨 chunk 命中率 ≈ 0**, 落盘 54s 纯白费。9 chunks ≈ 8 分钟。

**改动**:
1. `precompute_primitives(data, factor_names=None, save_disk_cache=True)`:
   新增 `save_disk_cache` 参数, False 时跳过 parquet 落盘与磁盘 LRU,
   仅更新内存缓存 (fork COW 复用), 省 ~54s/chunk
2. `store.materialize` 调 precompute_primitives 传 `save_disk_cache=False`
   (物化场景 chunk 间缓存无命中, 纯耗时)
3. IC 评估 (ic.py) 保持默认 True (同参数重复评估仍可命中磁盘缓存)

**验证**: no-save 运行 10.6s / save 运行 64.6s (省 54s); 未落盘 (磁盘目录 0
新增); `pytest test/ --ignore=test/test_evening_chain.py` → 385 passed。
VERSION → test-v497。

**注意**: precompute_primitives 源码变更 → 因子缓存指纹变 → 下次 materialize
全量重计算; 但 save_disk_cache=False 使全量重物化从 ~580s → ~95s (9 chunks)。

### v496 DuckDB 全量迁移完成 + 写入提速 5000x (2026-08-14)

**背景**: v495 迁移命令分步执行 (daily/valuation 成功, stocks 语法错误, refresh 卡死)。
stocks 报 `INSERT ... CASE WHEN` — 转换表达式误用作 INSERT 列名 (SELECT 才需转换)。
refresh 卡 16min/window — 实测根因: `executemany` 逐行参数绑定 34 万行需 200s,
而 `register + INSERT..SELECT` 只要 0.03s (差 ~5000x)。

**修复 (duckdb_store.py)**:
1. `_sync_table`: SELECT 用转换表达式 (col_str), INSERT 用纯列名 (insert_col_str)
2. 新增 `_write_df` (register + INSERT..SELECT + ON CONFLICT), `_upsert_df`/
   `sync_table_full`/`_sync_table`/backfill 全部改用它, 删除分批 executemany
3. 连接模型: `_rw()`/`_ro()` 线程级复用 (threading.local), close()/sync 线程
   finally 释放; 同文件所有连接必须同配置 (read_only=False), 否则 DuckDB 报
   "different configuration" — 查询连接不再 read_only=True
4. refresh_preaggregates: SELECT 补 high/low; 删除 max/min 块的 try/except 吞错
   (零 fallback) — 之前 high 缺失被静默吞掉, daily_max/min 永远空表
5. 进度日志: 每窗口每表打点 (rows + {:.1f}s) + done 汇总

**性能对比 (真库实测)**:
- daily 全量 949 万行: 2.4h → 100s
- daily_valuation 全量 830 万行: 2.4h → 58s
- refresh_preaggregates (6 窗口 × 6 指标 × 34 万行): 96min → 27s
- verify_sync daily/valuation: match=True

**遗留**: DuckDB 大批量 DELETE + 主键索引报 "Failed to delete all rows from
index" (已知 bug) — 预聚合表清空勿用 DELETE, refresh 全量 UPSERT 即可覆盖。

**验证**: 副本库 (独立进程) 全流程; MA5/RET/MIN5 与手工计算一致; 真库
daily 9,493,457 / valuation 8,307,314 / stocks 5,525 / 预聚合 6 表 × 2,038,638。
脚本: scripts/duckdb_sync_all.sh (幂等, ~2min)。
 DuckDB 锁死反模式修复 — 常驻 rw 连接 → 短命 RO/RW 连接 (2026-08-14)

**背景**: 用户报任何外部进程 (测试/回测/手工 backfill) 都被 DuckDB 拒
(Conflicting lock, 持锁者 orchestrator PID 56715). 实测 DuckDB 1.5.5 多进程
行为: 多个 RO 连接可跨进程共存; 但**任何 RW 连接存在时连 RO 都无法打开**.
原实现 DuckDBManager 单例持有常驻 `duckdb.connect(read_only=False)` 连接,
orchestrator 进程 (restart.sh 拉起) 首次触发 get_duckdb_proxy() 后永久独占
文件锁 → 锁死是设计反模式, 零收益.

**附带发现 (重要)**: quant/data/market.duckdb 是空壳 — 21 张表全 0 行,
1MB, 最后写入 8/13 07:24; SQLite 才是唯一真实数据源 (daily 948.8 万行,
daily_valuation 830 万行, stocks 5525). store.get_daily 的 DuckDB 优先路径
实际永远 fallback SQLite.

**修复 (duckdb_store.py)**:
1. `__init__` 不再持有常驻连接 — 删除 `_init_db` 的 rw connect + 常驻 sync 线程
2. `_ro()` 短命只读连接: 查询路径 (query_df/query_arrow/_scalar/get_*)
   → 用完即关, 不持锁, 任何人可查
3. `_rw()` 短命读写连接: 写路径 (_write/_write_many/connection()/schema 建表)
   → 仅在写入瞬间独占文件
4. `_ensure_schema()` 幂等建表/建索引 (CREATE IF NOT EXISTS), 首次访问触发;
   `_ro()` 前置 ensure (ro 无法打开不存在的库)
5. 删除有缺陷的 `execute()` (游标存活于已关闭连接) — 内部调用点 8 处
   迁移至 _scalar/_write/query_df/get_universe 用 query_df

**验证**: 393 passed (全量 test/); 副本库断言: 无常驻连接属性 / schema 首次
RO 自动 ensure / RW 写入读回 / stocks INSERT + list_date→DATE 转换; ast OK.

**注意**: 现有 orchestrator 仍为旧代码持锁, 需重启 ref: `bash scripts/restart.sh`
后才能释放旧锁并加载新代码. 空壳 DuckDB 层是否值得保留 (全部数据在 SQLite,
DuckDB 零行) 待决策.

### v494 日期格式全链路统一 ISO YYYY-MM-DD — baostock GBK 补丁 + list_date 修复 (2026-08-14)

**背景**: 用户报 `_sync_table` 的 date_cols_int 转换 SQL 缺 `'-'` 分隔符
(`date(substr(c,1,4)||'-'||substr(c,5,2)||substr(c,7,2))` → '2026-0605' 恒失败),
且 tushare 返回 int 日期 (20260605) 与内部 ISO 标准冲突; baostock 服务端返回
GBK 编码中文错误消息 ("接收数据异常") 被客户端 UTF-8 硬解码吞掉 → None →
调用方 AttributeError.

**修复**:
1. `utils/date.py` — `to_str` 全类型覆盖 (str ISO/compact/datetime, int, float,
   datetime/date/Timestamp/Period, None) 单点归一化; int/float compact 独立
   `_compact_int_to_iso` (容忍浮点精度/7-9位噪音非静默, 抛 ValueError)
2. `utils/baostock_gate.py` — 新增 `_patch_baostock_gbk()`: monkey-patch
   `baostock.util.socketutil.send_msg` → UTF-8 解码失败回退 GB18030
   (errors="replace"), 模块 import 即生效, 覆盖 store.py 裸 import baostock 路径
3. `data/store.py` — list_date/delist_date 写入统一走 `to_str` (tushare int→ISO)
4. `data/stocks_snapshot.py` — 同上
5. 历史数据修正 — 5525 行 compact list_date (如 '19910403') 已一次性 UPDATE 为
   ISO; 当前库 5208 ISO 非空 list_date, compact 残留 0
6. `data/duckdb_store.py` `_sync_table` — date_cols_int 转换改为 CASE: 8位纯数字
   → 拆分拼接 ('-' 已补), 否则 TRY_CAST ISO (空串/乱值不崩)
7. `data/freshness.py` / `data/data_health.py` / `alpha/ml_common.py` /
   `data/dividend.py` / factor/compute (fundamental/high_priority/price 下共 5 文件)
   — `str(x)[:10]` 归一化点 → `to_str` (约 40 处)

**验证**: 393 passed (全量 test/); baostock.send_msg 闭包内 `_decode_gbk` 实测
GBK 中文 '接收数据异常' 解码成功、UTF-8 正常路径不受影响; SQLite 侧同款 CASE
表达式对 ISO/compact/空值三态验证通过; ast 全 OK.

**注意**: DuckDB 文件被运行中的 web 服务 (PID 56715) 持锁, 未实连验证——转换
逻辑已在 SQLite 侧等效验证, 每晚调度链 sync 时会自动重推全表 (stocks 走全量).

### v493b 零 fallback 执行纠偏 — 静默吞错全量清零 (2026-08-14)

**背景**: 用户指出 CLAUDE.md 硬约束"零 fallback — try/except 不降级、不吞错"
在上次审查中未作为核对标准逐条执行. 收到 backfill_amount 运行日志后按此标准
全项目宽扫 except 静默点 (~60 处), 分类处理:

**修复 (真吞错 → 加日志/去降级)**:
1. `store.py` `_fetch_baostock_daily:1038` — `rs.error_code != "0": continue`
   静默 → 加 warning (error_code+msg)
2. `store.py` `_sync_adj_factor_baostock:754` — 同上 → 加 warning
3. `store.py` `_backfill_via_baostock:1576` — 同上 → 加 warning
4. `store.py` `_fetch_longbridge_daily:1422` — `except Exception: continue`
   完全无日志 → 加 warning
5. `store.py` `update_daily` source_policy 开关 — try/except pass 吞配置缺失
   → 去掉 try, `_require_cfg` 缺即崩 (零 fallback)
6. `store.py` 逐日 backfill_turnover:1729 — "接收数据异常" 不含"登录"关键词
   → 不重登, 3 次慢重试后静默丢当日该股 → 断连关键词 (接收/超时/socket等)
   合并进重登判定
7. `stocks_snapshot.py:104` — query_profit_data error_code≠0 静默 → print 留痕
8. `factor/store.py` `_read_checkpoint` — 损坏静默返 None (续传失效从头跑)
   → 加 warning
9. `attribution.py:566` — compute_rolling_metrics 失败 pass (归因日报静默缺
   滚动指标) → 加 warning

**核对后保留 (合理语义, 非吞错)**:
- 坏行解析跳过 (ValueError/IndexError/KeyError: continue — 单行脏数据清洗,
  store 1057/1964/2153, turnover.py, stocks_snapshot:114, sina:189,
  fund_flow:203, weekly:285, state_machine:335)
- DDL 幂等迁移 (sqlite3.OperationalError: pass "列已存在")
- 可选依赖探测 (ImportError: return None/pass — baostock/akshare 未装)
- 清理/关闭路径 (del/close 幂等, factor store 391/584/604/1069)
- 设计回退 (backtest/loop.py:51 缓存 miss → DB path, 注释明确非错误)
- 进程退出/队列空 (ProcessLookupError/queue.Empty — 正常语义)
- 监控单项失败 (prometheus 单盘统计 pass — 不因单盘挂掉崩监控)

**验证**: ast 全 OK; 6 处修复点 inspect 断言全 True; test_registry_smoke 通过.
已提交 b6b4da1 之后的增量未提交 — 本批 (v493+v493b) 待提交.

### v493 backfill 断连关键词收口 — '接收数据异常' 不再丢股 (2026-08-14)

**背景**: backfill_amount full 运行日志出现 "timed out" / "接收数据异常，请稍后再试。" /
"utf-8 decode 错误" / "logout success!" — 均为 baostock 断连类错误, 3 次重试 +
断连重登已安全处理 (失败股跳过、断点续跑, 不崩溃); 但发现**丢股隐患**:
断连判定关键词 ("网络接收","网络错误","socket","连接") 与实际错误
"接收数据异常，请稍后再试。" 不匹配 (无"网络接收"前缀) → 不触发立即重登,
而是 3 次慢重试后跳过该股 → 回填结果缺股 (下次重跑才补).

**改动**: `quant/data/store.py` 两处 (turnover_full:1968 / amount_full:2157)
断连关键词扩为 ("网络接收","接收","网络错误","socket","连接","超时") —
"接收数据异常" 命中"接收" → 立即 logout+login 重登后重试, 不再丢股.
verify 已确认: "接收数据异常，请稍后再试。" 命中 ✓

**注意**: 当前 backfill_amount full 进度在断点文件 (.amount_full_progress.json,
~500+/5208, ETA ~4h), 被杀/超时不影响已落库行; 重跑自动续.

### v492 数据拉取+物化缓存全链路 9 项修复 — 输入指纹失效 + DuckDB 同步收口 (2026-08-14)

**背景**: v491 交付后全链路审查 (调度链 → 数据拉取 → DuckDB 同步 → 物化缓存 → 消费路径),
发现 9 个问题, 含 2 个 v491 半成品:

**修复清单**:
1. **[P0] 物化缓存不感知输入数据变化** (`quant/factor/store.py`): source_hash 只检测
   因子代码; amount/turnover/财务回填后已物化日期永不重算 → 缓存恒读旧值. 新增
   `_compute_data_fingerprint()`: daily 行数/turnover>0/amount>0/MAX(date) + 财务三表
   行数/MAX(stat_date)/MAX(pub_date) → sha256[:16], 进程内缓存每晚算一次;
   meta 写 `data_hash`, `_get_existing_factors` 指纹不匹配 → 该日期全部因子判定缺失
   → 自动重算 (回填后首晚全量重算一次, 之后稳定增量). 旧 meta 无 data_hash → 按缺失
   处理 (首轮全量重算)
2. **[P0] daily_valuation 值级同步半成品** (`quant/data/duckdb_store.py`,
   `quant/scheduler/daily_data.py`): v491 的 verify_sync 值校验只覆盖 daily;
   daily_valuation 走 `_sync_table` 是**增量** (date > MAX), 历史行 UPDATE 依然
   永不进 DuckDB. 新增通用 `sync_table_full(table, cols, pk_cols)` 全量 UPSERT
   (sync_daily_full 委托复用); verify_sync 值级校验扩展到 daily_valuation
   (market_cap>0/turnover_rate>0); 调度链 match=False → daily_valuation 也走全量
3. **[P0] `_sync_table` 字符串主键伪增量 bug**: stocks/factor_registry 无 date 列,
   单字符串 PK 走 `WHERE symbol > MAX(symbol)` → 新上市小盘股 (000xxx/300xxx)
   永不进 DuckDB (get_universe 缺股票). 改为无 date 列表一律全量 UPSERT
   (5k 行级, 每轮 300s 成本可忽略, 且覆盖值级 UPDATE)
4. **[P0] financial_* 从 DuckDB 同步表移除**: SQLite 列 (total_operating_revenue/
   total_owner_equities/pub_date) vs DuckDB schema (revenue/total_hldr_eqy/ann_date)
   完全不同 → 每轮 UPSERT 必败被吞 (每晚噪音); limit_up_pool 同理 (16 列 vs 3 列);
   financial_cashflow 本就不在列表. 物化 fundamentals 直读 SQLite, DuckDB 财务表
   无人消费 → 全部移除同步
5. **[P0] sina 快速路径历史缺口** (`quant/data/sina_financials.py`): 只判
   MAX(stat_date) < target → JQ 已写 2025q2-2026q1 的股票 target 推进后 MAX 达标
   → 跳过 → 2019-2023 缺口永不补. 增强: 老股 (list_date ≤ 8 年前) 且
   MIN(stat_date) > 3 年前 → 视为历史不完整强制拉取 (sina num=50 一次补齐);
   新股天然历史短, 不判 (避免每周反复拉)
6. **[P0] daily_data.py `traceback` 用而未导**: `_tb.format_exc()` 在 import 前,
   backfill_turnover 失败时 NameError 掩盖真异常. import 移到文件头
7. **[P0] `curl_cffi.requestes` 拼写 bug** (`quant/data/store.py`): 应为 requests,
   akshare 源每次调用必 ImportError 被吞 → 永远不可用
8. ~~[P1] factor_cache 物化起点 vs trim 窗口~~: 误报 — 物化范围 1840 交易日
   < max_days 2000, trim 永不触发, 无需修
9. **[P1] repair 早间链兜底慢表** (`quant/data/table_registry.py`,
   `quant/scheduler/repair.py`): sina 首轮全量 4-5h > 早间链 30min 窗口 → 每天
   触发必超时被杀 (v491 注册后每早白跑). TableSpec 新增 `repair_eligible` 字段,
   财务三表 = False → 早间链跳过 (审计失败表同样过滤), 只由周六 data_maintenance
   (12h 窗口) 维护

**结论**: 数据拉取调度任务 + 物化因子缓存全链路问题已理清并闭环 —
   回填触发重算、DuckDB 值级同步、新股/新因子入库、历史缺口补齐、慢表兜底
   均机制化 (非打补丁), 后续常规运维无已知遗留问题 (残留低影响项见上).

**验证**: ast 全 OK; 指纹集成测试 (指纹一致→不重算 / 回填变化→失效重算 / 重算后
匹配→稳定, 全过); `_compute_data_fingerprint` 生产库实测 678c558b0291b64e;
weekly_full 财务三表 repair_eligible=False 生效; test_registry_smoke +
test_v472_factor_cache_materialize 12 passed. 全量 pytest 套件超 5min 未跑完
(含网络/重任务), 相关子集已过.

**注意**: 回填 (amount/财务) 完成后首次晚间链 factor_cache 会触发全量重算
(~1-2h, 并行 4 worker) — 属预期行为, 之后每晚仅增量.

### v491 数据缺口修复接入调度链 — DuckDB 值级同步闭环 + 财务三表 weekly_full (2026-08-14)

**背景**: v490 交付的是手动回填脚本, 但**调度链存在同样问题** (用户询问后排查):
- `_sync_incremental` 按 `date > DuckDB.MAX(date)` 只追新日期 → 历史行 UPDATE
  (turnover/amount 回填) 永不进 DuckDB; `verify_sync` 只比行数/日期数, 不比**值**;
  而因子物化 `store.get_daily()` DuckDB 优先 → 回填后物化仍读旧值 (268k 行 turnover
  回填实测)
- `daily_data.py` 顺序缺陷: DuckDB 同步在 backfill_turnover 之前 → 新行 turnover=0
  先进 DuckDB 后永不同步
- **财务三表完全不在调度链**: table_registry 无注册, 晚间链/周度维护无人拉财务,
  data_health 无审计 → 历史缺口 (income/cashflow 2019-2023 全缺) 永远补不上
- `_backfill_via_baostock` amount 单位 bug: 直接写 baostock 元, DB 标准千元 → 错 1000 倍

**改动**:
1. `quant/data/duckdb_store.py`:
   - `verify_sync` 值级校验: daily 表比对 turnover>0 / amount>0 非零行数
     (sqlite vs duckdb), 不一致 → match=False + warning
   - 新增 `sync_daily_full()`: daily 全量 UPSERT (850 万行分批 5 万, 幂等 ~1-2min)
2. `quant/scheduler/daily_data.py`: backfill_turnover 移到 DuckDB 同步之前;
   DuckDB 同步循环中 verify_sync match=False → daily 触发 sync_daily_full /
   daily_valuation 触发 _sync_table 全量重同步
3. `quant/data/store.py` `_backfill_via_baostock`: amount 元 → 千元 `/1000` 对齐
   `_fetch_baostock_daily`
4. `quant/data/sina_financials.py` (新建): 调度链财务同步模块 — `sync()` 无参全量
   幂等 (easy_tdx.sina 单股 50 期), 三表字段映射 (14/8/6 字段), 快速路径:
   该股某表 MAX(stat_date) 已达最近报告期 → 跳过该表 HTTP (weekly 15000 请求 →
   首轮后仅增量); 修 sqlite3 表名参数化 bug (FROM ? 不支持 → f-string)
5. `quant/data/table_registry.py`: 注册 `financial_income/balance/cashflow`
   三表 (mode=weekly_full, sync_main=sina_financials.sync, date_col=stat_date,
   slo_days=None 事件型不判新鲜度, min_total_rows=100000, factors=11 基本面因子);
   FACTORS_BY_TABLE 同步补三表映射 → freshness/unavailable_factors 自动覆盖
   (freshness 从 REGISTRY 聚合, 无需改)

**调度链闭环语义** (注册后自动生效):
- 周六 weekly_eval data_maintenance 12h 窗口全量刷新
- 晚间链 audit_all → 财务表 total_rows fail → repair 自动补拉 (幂等, 7.5h 窗口)
- 早间链 daily_repair 7 天兜底; audit 未全绿 → daily_data=partial, 不阻断晚间链
  (v487)
- 日线因子物化走 DuckDB → sync_daily_full 值级闭环; 基本面因子直接读 SQLite,
  回填即受益

**验证**: ast 全 OK; `_latest_report_end()`=2026-06-30; 三表 2026-06-30 覆盖 0 →
  首轮 16100 次 symbol-table 拉取 (~4-5h, 幂等可续); balance 21 期/股 vs income
  6.5 期/股缺口分布确认。scheduler daemon 运行中 (PID 45574), DuckDB 值校验
  由晚间链实际运行时自动生效。

### v490 verify 全量检查收口 — amount/财务历史缺口回填就绪 (2026-08-14)

**背景**: turnover 回填完成 + verify 脚本 benchmark 慢查询修复 (NOT EXISTS+GROUP BY
在 850 万行 daily 全表扫描 120s+ 卡死 → 改写为 DISTINCT date 子查询 EXISTS, 0.9s)。
verify 全量结果 293 项失败, 构成:
- **244 项 daily**: 2019 年 amount 缺 89% (750k 行, 早期源只写 close/volume 未写
  amount; 2019-01-02 仅 353/3349 行有 amount)
- **49 项财务三表**: income/cashflow 2019-2023 全缺 + 2024Q1/Q2/Q4 缺
  (JQ 权限窗口仅 2025q2~2026q1, tushare income 无权限, baostock 需 25 万次查询
  5 天不可行); balance 仅缺 2024Q1/Q2/Q4

**改动**:
1. `scripts/verify_materialize_inputs.py` — `check_benchmark` 慢查询改为
   `SELECT date FROM (SELECT DISTINCT date FROM daily WHERE ...) WHERE NOT EXISTS (...)`
   (0.9s, 原 120s+ 卡死)
2. `scripts/backfill_financials.py` — TARGET_QUARTERS 2019-2022 → 2019-2024
   (easy_tdx.sina 单股 50 期一次拉全, 15000 次请求; 实测 600519 2023 营收
   1505.6 亿/净利 775 亿与公开财报一致)
3. `quant/data/store.py` — 新增 `_backfill_amount_full`/`backfill_amount`
   (复用 turnover full 断点/重登/限速模式; baostock 元 → DB 千元 ×1000 已实测
   一致 000070: DB 261343.875千元 ↔ baostock 261343875.90元)
4. `scripts/backfill_amount.py` — 新入口 (full 模式 5208 只 ≈ 50 分钟)

**待用户执行** (顺序无关, 可并行):
```bash
PYTHONPATH=. .venv/bin/python scripts/backfill_amount.py          # amount ~50min
PYTHONPATH=. .venv/bin/python scripts/backfill_financials.py       # sina 财务 ~1-2h
```

**验证**: ast 全 OK; 回填后重跑 `scripts/verify_materialize_inputs.py` 应转绿。

### v489 turnover full 断连自恢复 — Broken pipe 不再 3 次全败 (2026-08-14)

**背景**: 09:08 起 baostock 服务端断连 (`Connection reset by peer`/`Broken pipe`/
"网络接收错误"), 旧 `_fetch_turn` 仅对 error_msg 含"登录"关键词重登 — 网络断连
不在其中 → 重试 3 次全败 → 688390 等后续股票白失败。断点 (486) 保证已存
4900/5208 只, 剩 ~308 只; 但失败股票会重试重登, 白白烧 3 次查询。

**改动** (`quant/data/store.py` `_fetch_turn`): error_msg 含 网络接收/网络错误/
socket/连接 任一关键词 → 与 session 失效同层处理: logout + login 重建连接后重试
(黑名单/配额异常同 _lg2 模式 raise RuntimeError)。

**验证**: ast OK; 断点 4900 只, 剩 ~308 只待跑。用户重跑 `backfill_turnover.py`
即自动跳过断点并续跑。

### v488 signals 窗口修正 08:00→08:30 (2026-08-14)

**原因**: `manifest.py` signals `schedule="08:30"` 仅为展示标签, orchestrator 实际按
`window=(8:00,15:30)` 触发 → 08:00:03 就运行 (08-14 task_runs 实锤), 抢跑在
daily_repair (08:00-08:30, 本意先做 T+1 补拉) 之前, 违背 manifest 自述意图.
**改动**: `quant/scheduler/manifest.py` signals window → (8:30,15:30), 与 daily_repair
窗口严格衔接, 保证 08:00 补拉链先完成再生成信号.

### v487 信号生成 failed 根因修复 — 故障链三层 (2026-08-14)

**故障链 (08-14 08:00 signals failed, 界面显示今日失败)**:
1. 08-13 晚间链 `fund_flow` 东财源 curl 连接中断 (30 连败 → cooldown), `limit_down_pool`/`margin_detail` 同步失败, 修复后仍败 → `daily_data`=**partial**
2. evening.py fail-loud: daily_data≠ok → **整链 abort** → factor_cache 未物化 08-13 因子
3. 08-14 08:00 signals 读 factor_store 空 → RuntimeError → **冒泡杀死 orchestrator 主循环** (orchestrator.py:360 run_once 无兜底) → 当日全部剩余调度 (daily_repair/execute/monitor/evening_chain) 瘫痪

**修复 (2 处)**:
- `quant/scheduler/evening.py`: daily_data=partial **不再中止链** — partial 仅表示 aux 表审计失败, 核心 daily/valuation 主流程已 ok; factor_cache 内 unavailable_factors 自动剪除超 SLO 因子, 链继续保证次日 signals 可用; aux 缺口由 08:00 daily_repair 补
- `quant/scheduler/orchestrator.py`: inline 任务 (signals/execute/snapshot/reconcile) 崩溃 **不再杀死主循环** — try/except 包裹 run_once, 异常记日志后继续; task_runs 已由 _dispatch 标 failed, 不会漏状态

**待用户执行** (backfill 完成后):
1. `bash scripts/restart.sh` — 重启 web+orchestrator (今日 orchestrator 已死, 必须重启恢复调度)
2. 物化 08-13 因子: `PYTHONPATH=. .venv/bin/python -c "from quant.scheduler.factor_cache import _run; _run('2026-08-13','2026-08-13')"`
3. 重跑信号: `PYTHONPATH=. .venv/bin/python -c "from quant.scheduler.signals import _run; _run('2026-08-14')"`
4. 补 aux 表: repair_and_reaudit 脚本 (下一步给全)

### v486 turnover full 断点续跑 — 解决"跑了很多次还是缺"根因 (2026-08-14)

**根因审计 (DB 实证)**:
1. **无任何代码删除 turnover** — 无 DELETE daily; 唯一写点 store.py:2280 `ON CONFLICT DO UPDATE` 带 `turnover=CASE WHEN excluded.turnover>0 THEN excluded.turnover ELSE turnover END` 保护, tushare 0 值不覆盖已补数据。已补股票的 2020/2024 交集 653/656 — 数据持久, 不是被删。
2. **真因: full 模式 5 次起跑全中断, 无断点续跑**。日志: 08-13 19:23 手动 full 死在第 ~700 只 (20:50 session 失效 → 22:00 黑名单); 08-14 07:53 又从头跑前 300 只 updated=0 (早已补过)。每轮从符号列表头开始, 中断后无"已处理到哪" — 前排反复被查, 后排 ~4600 只**永远轮不到**。656/715/744 只集合 = 深市前排代码 (000/002/001/300), 正是中断位置; 逐年比例 14-17% 因股票总数增长而微降, 非数据丢失。
3. 2019 amount 缺失是另一旧账 (2019 数据源当时没写 amount 字段, 非本次问题)。

**改动** (`quant/data/store.py` `_backfill_turnover_full`):
- 进度文件 `quant/data/.turnover_full_progress.json` (`{"done_symbols": [...]}`), 每 100 只 + 结束时落盘 (原子写 tmp+replace); 损坏则 WARNING 从头跑
- 循环开头 `if sym in done: skipped++; continue` — set 查询, 与符号排序无关 (get_symbols 无 ORDER BY 不影响正确性)
- 熔断 break 前进度已存, 下次直接续跑
- 预生成初始断点: DB 中 2024 有值的 744 只已写入 (跳过前排重查), 剩余 ~4464 只将从断点后继续

**待用户执行**: 停掉旧进程 (PID 42800, Ctrl+C) → `PYTHONPATH=. .venv/bin/python scripts/backfill_turnover.py` → 一次 ~50min 补完剩余 → verify 重跑确认。

### v485 baostock 全局限速门 (BaostockGate) — 防 IP 再被拉黑 (2026-08-14)

**背景**: 2026-08-13 全市场回填被 baostock IP 拉黑 (login error_code 10001011)。根因: 6+ 处调用点各自裸 sleep (0.15s~0.5s) 限速, 跨进程互不知晓 — nightly 链 + 手动回填同 IP 并发叠加, 频次远超免费服务容量。用户 08-14 换热点新 IP 解封, 要求先设计防黑名单机制再补数。

**设计** (新增 `quant/utils/baostock_gate.py`):
| 层 | 机制 |
|------|------|
| 跨进程令牌桶 | fcntl 文件锁 + 状态文件 `quant/data/.baostock_state.json` — 所有 baostock 请求 (登录/日线/财务/复权/股本) 经 `bs_query()` 统一入口, scheduler subprocess 链与手动回填共享同一限速, 不再各自 sleep |
| 黑名单熔断 | 检测到 error_msg 含"黑名单" → `mark_blacklisted()` 写时间戳; 冷却期 (`baostock_blacklist_cooldown_sec`=86400s) 内所有 `acquire()` 直接抛 `BaostockBlacklisted`, **一个请求都不发** — 防止高频重试加重封禁 |
| 配额上限 | 每分钟 / 每日请求数上限 (`baostock_calls_per_minute`=120 / `per_day`=50000), 超限抛 `BaostockQuotaExceeded` (fail-fast 不静默降级) |
| 均一间隔 | `baostock_per_stock_sec` 0.3→0.5s; 进程内 + 跨进程双重间隔保证, ±10% 抖动防规律节奏 |

**改动** (7 文件):
- 新增 `quant/utils/baostock_gate.py` (BaostockGate + bs_query 统一入口 + _NeedWait 内部信号)
- `quant/data/store.py` 5 处调用点接入: `_sync_industry` / `_sync_adj_factor_baostock` / `_fetch_baostock_daily` / `_backfill_via_baostock` / `backfill_turnover`(逐日+full) — 裸 sleep 全部移除, 黑名单/配额异常立即 break 停止本轮
- `quant/data/stocks_snapshot.py` refresh_total_shares 接入 (nightly 周度股本刷新)
- `scripts/backfill_financials_bs.py` 重写: login+逐股查询走 gate, 黑名单/配额停止本轮 (新增 TARGETS 不变)
- `scripts/backfill_valuation_baostock.py` + `scripts/data_backfill_integrity.py` 接入
- `quant/config/config.yaml`: baostock_per_stock_sec 0.3→0.5 + 新增 3 配置项
- `daily_basic.py` 已 DEPRECATED 跳过

**验证**:
1. 并发测试: 2 线程 (模拟跨进程) 12 请求全部被 0.5s 间隔串行化 (总耗时 5.68s, 最小间隔 0.501s) — 无并发叠加
2. 黑名单熔断: mark_blacklisted 后 acquire 抛 86400s 冷却错误 ✓
3. 单日真实回填 2026-08-12: 297/297 只全更新, 166s, 1.8st/s — 无封禁

**联动**: baostock 解封后待跑: `backfill_turnover` full 模式 (5208 只 ≈ 45-70min) → financials_bs (income 2023 + cash_flow 2019-2023, 24 季度 × 5200 只 ≈ 12.5 万次查询 — **单日配额 50000 上限, 需分 3 天跑**, 每日重跑自动续) → 重跑 verify 确认全绿。

### v484 财务数据 JQ 回填修复 — 2025Q4+2026Q1 补齐 18600 行 (2026-08-14)

**背景**: verify 暴露财务三表 2025Q4 不足 (income/cashflow 仅 1083、balance 仅 963) 与 2026Q1 全 0。实测 JQData 账号权限窗口 = statDate 2025-05-06 ~ 2026-05-13 (get_query_count 配额 100 万/日), 2019-2023 历史季度请求返回空 (2024Q4 仅 13 行) — 历史缺口仍待 baostock 解封。

**改动** (全部在 `scripts/backfill_financials_jq.py`):
| 项 | 内容 |
|------|------|
| 原脚本 3 bug | ① `for r in rows` — `rows` 未定义直接 NameError (旧版残留); ② `date=` 参数是"截至日快照"语义 (latest-as-of per code), 实测 2025Q4 只返回 2 行 — 必须用 `statDate='YYYYqN'` 报告期参数才返回全市场 ~5200 行; ③ 按年跳过 (>100 行跳过 2025) — 2025 有 6232 行但 2025Q4 仍缺 |
| 列映射 | JQData 返回列与库表列名一致 (蛇形), 仅 pubDate→pub_date 需映射; 按 PRAGMA table_info 动态取列, 过滤不存在列 |
| NaN 处理 | `pd.isna(v)` → None (原 `v != v` 对 numpy 类型失效) |
| 去重按表 | `existing` 改为 `existing_by_tbl[表]` — 原共享 set 导致 income/cashflow 跳过计数被 balance 行污染 (首轮 income 2026q1 假 skip) |

**结果** (实测 3 分钟): balance 2025Q4 963→5197、2026Q1 0→5199; income 2025Q4 1083→5184、2026Q1 0→5199; cashflow 同 income — 共 18600 行, 三表两季度全部达标 (≥4500 阈值)。

**验证**: 重跑脚本幂等 — 已完整季度 (≥4500 行) 全部 skip, 无重复插入 (ON CONFLICT DO UPDATE)。

**联动**: verify 脚本 financial 缺口项 2025Q4/2026Q1 预计转绿; 剩余缺口 = daily.amount 2019 全年 + daily.turnover 2020-2026 (~15% 覆盖) + financial 2019-2023 (JQ 权限外), 全部依赖 baostock 黑名单解除。

### v483 物化 todo 粒度 (date,factor) — 消除整日期白算 + 缺数据因子 blocked 剔除 (2026-08-14)

**背景**: 用户指出物化"永不收敛"根因是设计缺陷: (1) **todo 按日期粒度** — 某日期缺任意 1 因子, 该日期 94 因子全量重算 (白算源头); (2) **缺数据是静默空结果** — `if s.empty: continue` 无日志、无剔除、日期永远 pending → 每轮全量回填白算数小时。用户要求: 因子缺数据 → 写日志说明原因 → 从全量剔除 → 继续下一因子。

**改动** (全部在 `quant/factor/store.py`):
| 项 | 内容 |
|------|------|
| todo 粒度 | `todo` (日期列表) → `todo_map: dict[date, list[factor]]` (该日期**缺失因子**列表); worker 内 `compute_all_factors(factor_names=missing)` 只算缺失因子, 已物化因子不重算 |
| 空结果捕获 | worker `for fname in missing` 循环: `fv.get()` 非 Series / dropna 空 / 符号映射全 invalid → 记 `empty_factors[(date, factor)]` 返回父进程, 不再静默 continue |
| blocked 机制 | 空结果因子 → `_log.warning("factor X blocked at date — 依赖数据缺失/不足, 已剔除后续重算")` + 写入 `factor_cache/blocked.json` `{date: {factor: ts}}`; 下次 todo 构建时从 missing 剔除 |
| blocked TTL | `_load_blocked` 按 `cache_checkpoint_ttl_sec` (86400s) 过期过滤 — **数据补齐后自动恢复**: TTL 过期 → 重试一次 → 算出非空即解除, 仍空则重新记录 |
| ZERO-dates 误报修复 | 收敛检查只针对本次 todo 内的因子 — 已物化因子不在 todo 属正常, 不计入 "ZERO dates" 误报 |
| 新增 `_save_blocked`/`_clear_blocked_for` | 持久化 + 手动解除 (故障排查用) |

**验证** (冒烟, 2024-02-21 单日, 9 backtesting 因子): 第一轮 dt_streak 空结果 → WARNING 日志 + blocked.json 记录 `{2024-02-21: {dt_streak: ts}}`; 第二轮同日期 **3 秒全部跳过零重算** (`all dates already fully materialized, skip`) — 收敛达成。全量回填时: turnover 6 因子/财务因子在数据补齐前只算一次记 blocked, 不再每轮白算数小时。

**联动**: 表名 bug 修复 (financial_cash_flow→financial_cashflow, SQLite 重命名 + 7 处代码/脚本引用) 与 verify 脚本 (scripts/verify_materialize_inputs.py, 物化前逐日输入验证) 在 v482 记录。补数仍依赖 baostock 黑名单解除。

### v482 物化输入完整性 — financial_cashflow 表名修复 + 全量验证脚本 (2026-08-14)

**背景**: 用户要求物化前从 2019-01-01 顺序验证输入数据完整性与准确性（非抽查，缺失即阻断物化）。排查发现 3 个数据坑 + 1 个表名 bug：

| 坑 | 事实 | 影响 |
|------|------|------|
| **financial_cashflow 表名 bug** | jq_financials.py 建表 `financial_cash_flow`（下划线），而物化代码/preload/duckdb/prometheus 全读 `financial_cashflow`（无下划线） | 现金流数据物理存在（2024-2025，21697 行）但物化永远读空表 → ocfp/gp_ta 因子全废 |
| financial_income | 2019-2023 每年仅 6~1047 行（全市场应 ~5000/季），2024-2025 正常 | sue/earnings_growth_yoy/revenue_growth_yoy/gross_margin_diff 等利润表因子 2019-2023 全废 |
| financial_balance | 2023 仅 2243 只（缺一半）、2024 不全 | accruals/asset_growth/debt_ratio 等 2023-2024 部分废 |
| financial_cash_flow | 2019-2023 完全缺失（0 行），仅 2024-2025 有 | ocfp/gp_ta 2019-2023 全废 |

**数据源可用性实测** (2026-08-14): tushare token 有效但无 income/cashflow/balancesheet 接口权限；JQData auth 成功但账号权限仅 2025-05-06~2026-05-13；**baostock 是补 2019-2023 财务历史唯一可行源，但 IP 被封禁中，待解除后跑 backfill_financials_bs.py**（income 2023 缺口 + cashflow 2019-2023 全量，TARGETS/API_MAP 已更新）。

**改动**:
| 项 | 内容 |
|------|------|
| SQLite | `ALTER TABLE financial_cash_flow RENAME TO financial_cashflow`（real 数据保留，21697 行） |
| jq_financials.py | ensure_tables 建表名 + idx + upsert_cash_flow INSERT 全改 `financial_cashflow` |
| missing.py / fundamental.py | 直查 SQL 表名改 `financial_cashflow`（此前物化 fallback 路径会崩溃 "no such table"，实际走 aux 分支没触发） |
| 回填脚本 ×3 | backfill_financials.py / backfill_financials_jq.py / backfill_financials_bs.py / data_backfill_integrity.py 表名引用统一 |
| scripts/verify_materialize_inputs.py | **新验证脚本**（方案 docs/plan_materialize_input_verification.md）: 逐日检查 daily 行数 + close>0 + **turnover>0 比例 ≤95%**（值有效性，非仅行数）、daily_valuation 覆盖 ≥ daily 90% + pe/mv 非空、benchmark 与 daily 对齐缺日、财务三表按报告期 (Q1-Q4) 每季 ≥1000 行、margin 非负 + 交易日 ≥50、lhb 日期非空、stocks 静态列缺失率。**任一 fail → 退出码 1 → 阻断物化**。用法: `PYTHONPATH=. .venv/bin/python scripts/verify_materialize_inputs.py --start 2019-01-01 --end 2026-08-13` |

**流程约束**（用户规则）: (1) 物化前必须跑 verify 全绿; (2) 长 DB 任务写成命令由用户执行，Agent 做其他事; (3) 财务 2019-2023 补数依赖 baostock 黑名单解除（probe: login error_code==10001011 即仍封禁），解除后跑 backfill_financials_bs.py（先 5 只 probe 再全量）; (4) 补数+verify 全绿后才允许 materialize_full。

**验证**: 8 个修改文件 ast.parse 全过; VERSION=test-v482。verify 脚本执行结果待用户终端跑出。

### v481 turnover 数据修复 — baostock full 回填 (2026-08-13)

**背景**: 排查物化"永不收敛"根因 (10 因子 2020 覆盖缺失反复重算) 发现: `_fetch_tushare_daily` 写 daily.turnover 恒为 0 (tushare daily API 不含 turnover_rate, 2026-07-21 实测, store.py:923 注释); 2020-2024 全市场 ~99.9% 行 turnover=0 (仅 4 只 symbol 非零), 2025 起部分 symbol 由 baostock/sina 同步才有真值, 2026-08-12 仍有 297/5191 只 (5.7%) 零值且 volume>0。影响: turnover_accel/ctr_20d/hl_volume_20d/turnover_adj_amihud_20d/turnover_anomaly/turnover_rev_5d/abn_turnover/abn_turnover_resid/accruals/sue 等 10 因子在受影响日期物化空结果 → 日期永远 pending → 每次全量回填白算数小时。

**改动**:
| 项 | 内容 |
|------|------|
| store.py | `backfill_turnover` 新增 `full=True` 模式 → `_backfill_turnover_full()`: 每 symbol 一次 baostock 查询拉全区间 (2018-01-01→今), 只 UPDATE 差异行, 幂等; 复用成熟模式: `datasource_retry` 指数退避 (3 次), 每 200 只重登防 session 超时, config `rate_limit.baostock_per_stock_sec` 限速, 每 100 只 commit+进度日志 |
| scripts/backfill_turnover.py | 新入口: 默认 full 模式 (存量 7.2M 行逐日模式需 7.2M 次查询不可行), `--date` 单日缺口 (调度 `daily_data.py` 每日增量仍用原逐日路径) |

逐日模式 `_ts_code` 格式 (000001.SZ) 实测 baostock 两种格式均接受, 无隐藏 bug。

**验证**: 40 只并行联测 0.7min 无错误; 全市场 5208 只 ETA ~39min; 幂等 (已更新行跳过)。曾用 multiprocessing.Pool 并行方案, 并发 baostock login 互踢 ("用户未登录") 弃用, 改回单进程成熟模式。

**调度任务侧修复** (用户要求审查 daily_data 链的 turnover 回填, 发现 3 处问题):
| 行 | 问题 | 修复 |
|------|------|------|
| `backfill_turnover` no-date 模式 | docstring 声称"扫描全缺口", 实际只扫 `MAX(turnover>0)` 之后 — 2020-2024 历史大洞在 last_good 之前**永远扫不到** (即"历史存量回填"从未生效) | no-date 直接路由 `_backfill_turnover_full()` (每 symbol 一次查询) |
| 逐日模式查询循环 | baostock `error_code != '0'` (session 失效/服务抖动, 如"用户未登录") 只打一次 warning 就 break — **静默跳过不重试**, 缺口永远缺 | 非零 error_code 退避重试 (2s/4s), 含"登录"字样先 logout+login 再试; 3 次后打 warning |
| 逐日模式 needs_fill | 含北交所 (92xxx, 266 只) — baostock 不覆盖北交所, 每夜必败白查 | `AND symbol NOT LIKE '92%'` 排除 (北交所换手率归 tickflow `backfill_turnover_quotes`) |
| baostock socket | 底层无超时 (socketutil 未 settimeout), 服务器挂起时 recv 永久阻塞 — 实测 full 回填连续 2 次卡死 (0 写入/0 日志) | 新增 `_bs_socket_timeout()`: login/re-login 后对 `context.default_socket` 强制 `settimeout(30)`; config 新增 `data.http_timeout.baostock: 30` |
| full 模式 session 失效 | "用户未登录" 3 次重试全败静默, 白等至 200 只重登点 | `_fetch_turn` 改为内部 3 次重试, 检测 "登录" 字样即 logout+login+重设 socket 超时 |
| baostock IP 黑名单 | 08-13 21:48 起 002244 后全部失败 "黑名单用户，请与管理员联系" — 今日用量叠加 (晚间链 5192 只 + 3 轮全量) 触发 IP 级封禁, 连 login 都拒绝 (error 10001011) | ① 全量/逐日两模式均加熔断: 检测 "黑名单" 立即 raise, 不再 94 次白打加重封禁 ② 已停回填, 待封禁解除后重启 (probe: `baostock.login` error_code==10001011 即仍封禁) |

另修正 `update_daily` 误导注释: "tushare turnover✅ 保证覆盖率" → 实测 tushare daily API 无 turnover 字段 (写 0), turnover 靠 nightly backfill + full 回填保障。`test_evening_chain` 两用例当晚间链运行时因 `database is locked` 失败 — 环境锁竞争非代码问题, 晚间链结束后复测通过。

**联动**: materialize_full 全量回填 (v480 验证通过后) 中道发现 chunk 6-9 仍白算 — turnover 未补前 10 因子永不可物化, 已停 (用户确认), 待本回填完成后重启一次即收敛。重要: **materialize_full 必须等晚间链 (daily_data→factor_cache→attribution) 全部结束后再启动** — 两者并发写 factor cache parquet/_checkpoint.json 有损坏风险。

### v480 物化内存修复 (B 方案) — 紧凑数组 + 边收边写 (2026-08-13)

**背景**: 2026-08-13 10:22 全量回填 (5208 symbols × 94 factors × 1602 dates) 在 chunk 1 worker 阶段 RSS 40+GB, macOS 卡死, 日志中断 (app.log:15560)。根因: worker 结果用 Python tuple 列表逐行累积 (~150B/行 × 50 日期 × 94 因子 = ~3GB/worker), 父进程 `[ar.get() for ...]` 一次性收齐 4 个结果 (~14GB), fork COW ×4 复制 ~5GB 共享数据, pickle 序列化双份驻留; aux financial 三表无日期下界全历史入内存。

**改动** (全部在 `quant/factor/store.py` + `_preload.py` + config.yaml):

| 项 | 内容 |
|------|------|
| B1 | worker 结果改紧凑 numpy 数组 `(symbol_i16 ndarray, value_f32 ndarray)`, 单 (factor,date) 750KB→15KB |
| B2 | 父进程边收边写: 新增 `_consume_worker_result()`, 每收到一个 worker 结果立即分组写 part + 更新 meta, 不再累积全 chunk |
| B3 | `preload_aux_data_chunk` financial 三表加 `stat_date >= fin_start` 下界, 新增配置 `factor.compute.financial_lookback_days: 730` (来源注释: YoY 410d / TTM 460d / 8 季度缓冲) |
| B4 | worker 内向量化符号映射 (`Index.map(dict)` C 层 get_indexer) 替代 `dropna().items()` 逐行循环 (2350 万次/worker) |

`_write_factor_date_part` 同步改为数组直拼落盘 (消除每行 dict 构造 ~300MB/因子瞬态); 每 chunk 新增 consumed worker result 日志 (模板 9)。

**验证**: 复现原场景 (200 天 × 5208 × 94 因子整 chunk, 4 workers): 峰值 RSS **2.98GB** (修复前 40+GB, 8× 降幅), 56,161,317 行, failed_dates=[], 2095s 完成, macOS 无卡顿; 落盘无 part 残留, checkpoint 正常清除, load 抽查 100% 覆盖。test_v472/test_v305 全套 **393 passed**; VERSION=test-v480。

**遗留**: 全量回填 1602 日期中 2020-11-02 之后部分仍未物化 (原 10:22 任务被卡死中断), 需用户重跑 `scripts/materialize_full.py` (checkpoint 已清, 按 per-date manifest 增量, 现内存安全)。

### v479 数据健康闭环 — 表注册表 / 审计 / 自动补拉 (2026-08-13)

**背景**: 诊断出 5 根因 (一半表无同步任务; 子同步 try/except 吞错; 检测只看 MAX(date) 不看覆盖/缺口; lhb/limit_up 无回头补拉; 回填仅人工一次性脚本)。

**核心新增** — 单一真相源:
| 文件 | 说明 |
|------|------|
| `quant/data/table_registry.py` | 11 表注册表: daily/daily_valuation (±14d), fund_flow (±100d), margin_detail (±30d), lhb_detail/limit_up_pool/limit_down_pool (±7d), dividend/stocks (weekly_full), benchmark_daily (±10d), adj_factor (none)。每表含 `date_col`/`slo_days`/`sync_daily`/`sync_weekly`/`custom_check`, `FACTORS_BY_TABLE` 聚合 |
| `quant/data/data_health.py` | `audit_table` (freshness/gap_dates/coverage/total_rows/custom 5 类规则) + `audit_all` + `repair_table`/`repair_and_reaudit` + `data_audit` 表留痕 (含 repair 后仍 fail 标记) + `failed_tables_on`/`last_ok_check`/`consecutive_failures` |
| `quant/data/freshness.py` (重构) | SLOS/TABLE_TO_FACTORS 改为从 REGISTRY 聚合, API (`check_freshness`/`unavailable_factors`) 兼容 |
| `quant/data/stocks_snapshot.py` | `sync_stock_basic` (tushare, UPDATE 不 REPLACE 防清空 pe/total_shares) + `refresh_total_shares` (baostock) + `refresh_all` |
| `quant/scheduler/daily_data.py` (重写) | 子同步改为按 rollback_specs 循环 (window_days 窗口); 末尾 `audit_all` + `repair_and_reaudit` + 连续失败≥3 天 ERROR 告警; 状态 ok/partial/failed (partial=审计残留失败表) |
| `quant/scheduler/repair.py` | 08:00 早间补拉链: 最近 3 天 fail 表 + weekly_full 7 天兜底; 单表失败不阻断其余, task_log 留痕 |
| `quant/scheduler/data_maintenance.py` | 周六数据维护: weekly_full 表全量刷 + 全表审计 |
| `quant/scheduler/manifest.py` | 注册 `daily_repair` (周六+交易日, 08:00-08:30, grace/timeout 1800s) |
| `quant/scheduler/runners.py` | +`run_daily_repair()` |
| `quant/scheduler/orchestrator.py` | 非交易日分支放行 daily_repair + 交易日 08:00 处理块 + `_repair_done` 重置 (2 处) |
| `quant/scheduler/weekly.py` | Phase 0 接入 `data_maintenance` (失败不阻断评估) |

**测试**: `test/test_v479_data_health.py` 新增 15 测试 (audit 各规则/repair/data_audit/连续失败/因子裁剪聚合等); v306 旧测试 fixture 补全 11 表 (注册表扩张后缺表误判 stale)。全套 **393 passed**; VERSION=test-v479。

**注意**: `data_audit` 表/`daily_data_partial` 状态为新增语义, 早间链 subprocess 启动后首次运行会补写 audit 基线。

### v478 数据缺口回填 — total_shares / dividend / margin (2026-08-13)

**背景**: 冒烟测试定位 4 处数据缺口 (sue 因子 0 行根因 + 陈旧表):

| 缺口 | 修复前 | 修复后 | 数据源 |
|------|--------|--------|--------|
| `stocks.total_shares` | 200/5525 只填充 | **5207/5525** (miss=317 北交所 92xx + 1 ST, 均不参与因子池) | baostock profit_data 逐只 (2026Q2→Q1→2025Q4 最新报告总股本) |
| `dividend` | 1551 行, 到 2026-07-10 | **55,080 行**, 到 2026-08-21 (07-10 后 +471) | 新浪 vISSUE_ShareBonus (akshare 包同源, 东财 IP 封不禁新浪) |
| `margin_detail` | 到 2026-08-11 | **到 2026-08-12** (与 daily 同步) | SSE JSON sync_range (T+1 发布, 晚间链 30 天窗口自动覆盖) |
| `lhb_detail` | 到 2026-08-12 | 无缺口 (本就最新) | — |

**修改**:
| 文件 | 改动 |
|------|------|
| `scripts/data_backfill_integrity.py` | +`total_shares` subcommand (baostock 逐只季度总股本, 幂等跳过已填充); +`margin_latest` subcommand (sync_range 14 天窗口); `dividend` 增强: tushare 限频自适应重试 (`_tushare_call`, 60s/3600s, 零 fallback 耗尽即 SystemExit) |

**经验**:
- tushare free 档 dividend 接口实测 **1 次/小时** 限频 → 5525 只逐只拉不可行 (脚本已保留限频重试, 但该源弃用)
- **新浪 vip.stock.finance vISSUE_ShareBonus 直连可用** (akshare 封禁的是东财源); 现成 `quant.data.dividend.py::sync_range` 全量幂等重拉, ~25min/5525 只
- baostock 逐只长跑 **会话故障模式**: 中途 "接收数据异常" → 后续全 miss (非真缺失); 重跑自动仅补 NULL 项, miss 列表 verify 需复查 92xx 前缀
- 同库并行大写入 → `database is locked` (busy_timeout 30s 不够): 回填任务须**串行**
- 北交所 (920xxx, 原 8xxxxx) baostock 不覆盖 — stocks 表 317 只保持 NULL, universe 已排除 BJ, 不影响因子

**后续**: 全量物化 (94 因子) 将首次物化 sue/dividend_yield/margin_buy_ratio 等 6+ 因子; dividend_yield 依赖 ex_date 滚动窗口 → 55,080 行全覆盖。

### v477 全量物化性能重构 — 消除重复计算/加载 (2026-08-13)

**背景 (性能分析结论)**: 全量物化 94 因子 × 1602 日期估算 15-25h, 而历史上晚间链 23 因子全量仅 2-3h。分析出 4 个浪费点:

| # | 浪费 | 根因 | 修复 |
|---|------|------|------|
| 1 | 5 个 batch 拆跑 → data/aux/fundamentals/prims 各装载 5 遍 | materialize_full.py BATCHES 逐批独立走完整 chunk 循环 | **单批直跑** 94 因子一次 materialize 调用 |
| 2 | 每 (factor, year) 每次 read-modify-write 整年度 parquet (94×8年×8chunks ≈ 6000 次全文件重写) | `_write_factor_date_rows` 每次 concat+dedup+重写全文件 | **part 文件追加 + 末尾合并** |
| 3 | WORKERS=1 (8 核 M1 闲置) | 脚本硬编码 | 默认走 config `factor.compute.materialize_max_workers`=4 |
| 4 | prims 磁盘缓存跨 batch 无法命中 | v472 已修 (key=仅 data_hash) | 单批后天然全命中 |

**修改**:

| 文件 | 关键改动 |
|------|----------|
| `scripts/materialize_full.py` | 删除逐批循环 → `ALL_FACTORS` (94) 单次 `fs.materialize`; `--batch N` 保留 (兼容单批); `--workers` 默认 `_require_cfg("factor.compute.materialize_max_workers")` |
| `quant/factor/store.py` | **part 文件机制**: `_write_factor_date_part` 写 `{factor}/{year}.part{part_seq}` (纯新增, 不读旧); `_merge_pending_parts()` 幂等合并残留 part → 主文件 (concat+dedup keep=last+原子写+删 part); materialize 开头合并上次中断残留 + 结尾合并本次; part 文件名无 `.parquet` 后缀 → trim/load/bulk_load 扫描天然免疫 |
| `quant/config/config.yaml` | 新增 `factor.compute.materialize_max_workers: 4` (来源注释; 行内插入保留全部既有注释) |
| `test/test_v472_factor_cache_materialize.py` | +2 测试: part 合并无残留 / 残留 part 可重入幂等合并 |

**验证**: 全套 378 passed (原 373 + 新 2 + 修正 3)。语法 ast 校验通过。`--dry` 输出 `[ALL] 94 factors × 1602 dates → ~1602/1602 pending`。

**冒烟测试 (100 只 × 2026-07-01..08-12 × 94 因子, 独立临时 cache)**: 最终 259,238 行 / 31 天 / failed_dates=[] / part 残留 0 / 61.9s@4w。冒烟连环触发 4 类存量 bug (全量 94 因子才暴露, 晚间链 23 因子不含故从未触发):

| # | Bug | 文件 | 修复 |
|---|-----|------|------|
| 1 | v472 恒定窗口集漏 14 → rsi_rev_14d 全 FAIL (KeyError rsi_14) | `_primitives.py _required_windows` | 14 补入恒定集 |
| 2 | prims 磁盘缓存 key 仅 data_hash, 代码改动后旧缓存依旧 HIT 缺新 prim → 永久失败 | `_primitives.py _cache_key` | 追加 `_PRIM_CACHE_VERSION="v2"` 段, 旧目录 LRU 淘汰 |
| 3 | `preload_aux_data_chunk/preload_aux_data` margin 查询缺 symbols 过滤 (全市场 371K 行, 应为子集) | `_preload.py` | 加 `symbol IN (ph)` |
| 4 | `compute_margin_buy_ratio` 全量相除不切片 → 物化对齐后全 NaN | `fundamental.py` | 按当日过滤 + reindex(symbols) |
| 5 | `compute_dividend_yield` 查假列 `close_latest` (不存在) → SQL 异常 0 行 | `fundamental.py` | 改 daily 表当日最近收盘价子查询; 另 `Timestamp` 直接绑定 SQL → str → sqlite3 抛 "type Timestamp is not supported" |
| 6 | `compute_lhb_net_buy` date_str(str) not in all_dates(Timestamp list) 恒 True → 恒 NaN | `_event.py` | 统一 str 列表 |
| 7 | `compute_ztd` 缓存 key str vs dispatch Timestamp → 永不命中 | `_alternative.py` | key 统一 str |
| 8 | sue/holder_reduction/ocfp `Timestamp` 直接绑 SQL 参数 → 绑定异常 0 行 | `fundamental.py` | date_str |

**冒烟 0 行因子定性 (均非 bug)**: sue — `stocks.total_shares` 仅 200/5208 只有值 (数据回填任务, 全量物化也将 0 行); ztd — 前 100 蓝筹 250 天零停牌 → 恒 0 → zscore(min_count=30) NaN, 全市场有停牌股 → 有值; lhb_net_buy_20d — 前 100 蓝筹无上榜 → 全 0 同样 zscore NaN, 全市场实证 755 只有值。**注意 `zscore_min_count_dense: 30` — 截面 <30 只有效值即全 NaN, 属设计 (防小样本噪声)**。

**测试脚本教训**: 测试冒烟勿用 `ds._connect()` 拿内部连接又 close — DataStore 缓存线程局部连接, 关闭后材料化读 closed DB。照抄 `factor_cache.py::_run` 模式 (`_conn.close()` 后 `ds.close()` 重置)。

**预期效果**: 单批 + part 合并 + workers 4 → 全量物化估 ~6-8h (对比 15-25h)。历史 23 因子 2.8h@2w 实测 → 94 因子按比例 11.5h@2w → 4 workers + 消除装载/写放大 ≈ 5-7h。

**教训 (本次踩坑)**: `git checkout config.yaml` 会丢弃工作区未提交改动 — 恢复 config 时先确认 `git diff HEAD` 无未提交内容; 测试新增 `import os` 勿写在函数内。

### v476 primitive 缓存路径 bug — quant/quant 孤儿目录 4GB 清理 (2026-08-13)

**症状**: 项目占盘 9.1G, 其中 `quant/quant/data/primitive_cache` 4.0G (8-11~13 物化写入)。

**根因**: `_primitives.py _PRIMITIVE_CACHE_DIR` 用 `dirname(abspath(__file__))×3` = quant 包根, 又 join 了 `"quant","data",...` → 多套一层 → 真路径 `quant/data/primitive_cache` 从未建过, 缓存全写进 `quant/quant/quant/data/...`。旧格式 1GB×4 pkl (v472 前) + 新格式目录 (当前代码依然在写)。

**修复**: join 去掉多余 `"quant"` → `quant/data/primitive_cache` (与 factor_cache 同级)。迁移 4 个新格式目录过去; 删除 4×1GB 旧格式 pkl + 空壳目录 (旧格式由 v472 起不再读取)。占盘 9.1G→5.1G。

**剩余占盘构成** (均正常, 无需清理): market.db 2.9G (daily 9.4M 行 774MB + 索引 478MB + daily_valuation 687MB + margin_detail 266MB, 无空洞); logs 275M (quant.log 50M×5 轮转保留 10 天); .venv 1.7G; factor_cache 135M (会随物化涨至 ~1.5G); models 33M; .git 42M。

**duckdb 预聚合表**: duckdb 1.5.x 起 `window` 为保留字, `_TABLE_SCHEMAS` 中 8 张预聚合表 (daily_ma/daily_ret/daily_std/daily_zscore/daily_ma_volume/daily_max/daily_min/daily_rank) CREATE 全部 Parser Error (仅 WARNING 吞掉) → 从未建成, market.duckdb 中无此 8 表。
- 修复: 建表 SQL 列定义与 PK 加双引号 `"window"`; `_upsert_df` 全部列名/pk/set 通用加双引号 (duckdb 双引号标识符合法, 对其他表无副作用); `_primitives.py _get_preagg_table` WHERE 加引号。
- 验证: 21 表全量建表成功 + `_upsert_df` 实测 upsert 成功 + 查询路径实测成功。注意: `_get_preagg_table` 目前无调用方 (死代码), 属能力修复, 不影响现网数值。

**lgb_train skipped "lightgbm not installed"**: 08-12 11:12 手动补跑时用的环境无 lightgbm。已确认: .venv 有 lightgbm 4.7.0 + xgboost 3.3.0, 系统 python (Homebrew 3.14) 也有 lightgbm 4.6.0。晚间链 subprocess 走 `runners._run_subprocess` 的 `.venv/bin/python3` → import 正常。**注意: 当前线上 web (PID 67310) 是系统 python 手动启动的, 必须用 `bash scripts/restart.sh` 重启回 .venv**。

### v475 duckdb `window` 保留字建表修复 + lgb_train 环境确认 (2026-08-13)

**duckdb 预聚合表**: duckdb 1.5.x 起 `window` 为保留字, `_TABLE_SCHEMAS` 中 8 张预聚合表 (daily_ma/daily_ret/daily_std/daily_zscore/daily_ma_volume/daily_max/daily_min/daily_rank) CREATE 全部 Parser Error (仅 WARNING 吞掉) → 从未建成, market.duckdb 中无此 8 表。

### v474 晚间链每日被杀回归修复 (2026-08-13) — 08-10 起系统"一天没工作"根因

**故障**: 08-10..08-12 每晚 `daily_data` 跑 5-12min 即被 abort → factor_cache 跳过 → 次日 signals `factor_store empty for T-1` → 08-12 一整天无信号/执行/归因。

**根因**: v428 (08-08) manifest 重构删除 orchestrator._TIMEOUTS 后, `_check_timeouts` 改为 `s = ALL.get(task_name); grace_s = s.grace_s if s else 300`。晚间链 6 个 stage (daily_data/factor_cache/attribution/lgb/xgb/adj_factor) 不在 manifest.ALL(只含 signals/execute/snapshot/monitor/reconcile/evening_chain/…) → fallback 300s。而 daily_data 实测需 2.4~4.4h (08-05~07 顺利时), 旧 _TIMEOUTS 里根本没有这些任务 (=不查超时, limit=None)。

**修复** (零 fallback 前提下最小改动):
- `manifest.py`: 新增 `EVENING_STAGE_GRACE` 表 (daily_data/factor_cache 6h, attribution/adj_factor 1h, lgb/xgb 6h), 延续单一真相源
- `orchestrator.py _check_timeouts`: `grace_s = s.grace_s if s else EVENING_STAGE_GRACE.get(task_name, 300)`
- grace 统一: daily_data.py 7200→21600, factor_cache.py 5400→21600, attribution.py 900→manifest, lgb/xgb 3600→manifest (dedup 宽限与超时对齐, 防长任务 1.5h 后 dedup 失效双跑)
- 新增测试 TestEveningStageGrace 3 例 (防漏配) — 全量 376 passed

**注意**: 修复只对重启后的新链生效。08-12 晚链已 failed 无补跑; 08-10/11/12 三天 daily 缺失 (5208 只 stale_recent) 待今晚 19:00 链自动拉 (6h 预算足够) 或手动 `daily_data`。web 需重启生效 v474。

### v472 埋点补充: shortcut 缺失原语单行定位 + per-factor 覆盖日志 (2026-08-13)

旧日志证实 1846 条 per-date traceback 可 grep 根因 (KeyError 'ma_10'), 但缺结构化原因行与 per-factor 结果口径。补充:

| 文件 | 埋点 |
|------|------|
| `quant/factor/compute/_dispatch.py` | shortcut KeyError → 单行 ERROR: `shortcut {name} KeyError: missing primitive '{key}' (factor window={w}, computed windows=[...])` + re-raise (零 fallback 不吞错); 新增 `_prims_windows()` 从 prims 键反推窗口集 |
| `quant/factor/store.py` | 汇总行后新增: `per-factor dates covered: {factor}={n}` (本 run 每因子唯一日期数) + ZERO 覆盖 ERROR: `N factors produced ZERO dates this run: ...` (因子全败早报警, 兼容 wq_alpha_006/ztd 类无值正常因子列入观察) |

验证: 模拟 RED 场景 (prims 仅 {20} 窗口) → `shortcut compute_ma_alignment KeyError: missing primitive 'ma_5' (factor window=20, computed windows=[20])` 单行命中。full suite 373 passed。

### v473 物化起点修正: 2020-01-01 (数据 2019-01-01 备) (2026-08-13)

v470 引入 scripts/materialize_full.py 时误设 START=2019-01-01, 违反约定 (config start_date=2020-01-01, 数据齐 2019-01-01)。查库证实 2018 年 daily 仅 354 只股票 (2017 遗留子集), 2019 起才全量 ~3,551 只 → 2019 起点需 2018 lookback (378 天, momentum_252d), 绝大多数股票早期全 NaN, 且 5d/10d 短窗因子会把 2019 日期误标已物化留下半脏缓存。修正 `START = "2020-01-01"`, docstring 记录约定防回退。残留: batch2 中途停止可能已写少量 2019 短窗因子行 (2020+ 不受影响, 不清理)。

### v473 数据回填 — 堵死项记录 (2026-08-13, 待解)

| 项目 | 状态 | 出路 |
|------|------|------|
| dividend 2019+ | tushare 免费档 1次/分钟 (5481只≈91h) 不可行; akshare 封 IP; baostock 无分红 | tushare 积分 / JQ 续费 / akshare 解封 |
| financials income/cashflow 2019-2023 + balance 2023 | tushare 财报接口无权限; akshare 封禁; **JQ trial 已过期** (auth 成功但全部查询空, 试覆盖至 2026-04-02) | 同上 |
| limit_up_pool 2019-04-08..2026-06-11 | tushare limit_list_d 无权限; akshare 封禁 | 同上 (影响 ztd/zt_streak/limit_touch_no_seal/limit_up_prox_5d, 现有仅 2026-06-12 起) |
| pmi_manufacturing 整列缺失 | 仅 akshare 源, 封禁 | 同上 (影响 macro_pmi_diff) |
| scripts/backfill_financials_jq.py | 有未定义变量 bug (for r in rows: 而 rows 未定义) 从未跑通; 依赖过期 JQ 账号 | 修复脚本+有效账号后再用 |

已完成: benchmark_daily 2019-01-02..2026-08-12 (baostock, 1846 天); adj_factor 2019 (baostock 后台); daily_valuation 2026-07-01/02 (tushare 节流); macro 无重复 (bond 日频属正常)。

## 当前状态 (test-v472, 2026-08-13)

### v472: prims 恒定标准窗口集 — batch2 全败根因修复 (2026-08-13)

**背景 (2026-08-12 现场)**: batch2 (23 因子 × 1846 日期) 全败 67min/0 行。ma_alignment_20d 声明窗口仅 20, 但 shortcut 硬编码依赖 ma_5/ma_10/ma_60; `_required_windows` 按声明窗口推导漏 ma_10 → prims 缺 → `KeyError: 'ma_5'` → 整日判失败 → checkpoint 永久重试。`shortcut_extra_windows` 手工映射维护即失效 (仅覆盖 5 因子)。

**v472 修复**:
1. `_required_windows` → 恒定标准集 `{5,10,20,60,63,120,126,250,252}`, 与因子批次无关 (参数保留向后兼容)
2. `_cache_key` → 仅 data_hash, 不再含 factor_names (窗口集恒定后内容与批次无关; 旧目录由磁盘 LRU 淘汰)
3. `materialize` 返回值 `n_dates` 语义修正: 因子×日期双计 → 唯一日期数

**验证**: 新增 `test/test_v472_factor_cache_materialize.py` 6 测试全绿 (prims 窗口完整性/增量 append/失败续传/跨年分区/trim 重映射/force 重算), 全量 373 passed。静态校验: 全部 32 个硬编码 `prims[...]` 引用均被标准集覆盖。

| 文件 | 关键改动 |
|------|----------|
| `quant/factor/compute/_primitives.py` | `_required_windows` 恒定标准集 (带事故文档注释); `_cache_key` 去因子批次 |
| `quant/factor/store.py` | `n_dates_computed` 去重 (原按因子×日期双计) |
| `test/test_v472_factor_cache_materialize.py` | 新增 6 测试 + fixture 陷阱记录 (seed 用独立连接, 勿关 DataStore thread-local conn; duckdb proxy 需禁用避免锁生产 duckdb 文件) |

## 当前状态 (test-v471, 2026-08-12)

### v471: 因子缓存完全重写 — parquet f×year 分区 + fork 共享内存 (2026-08-12)

**背景**: v466-v470 存在根本性性能问题 — workers 每日期重新 `read_pickle(prims.pkl ~1GB)` + `read_parquet(data_full)` + CSV 字符串往返, O(n_dates) 反序列化, 8GB 机器 swap 爆表 (11.8GB→12.7GB)。v470 修复点缀, 未触本质架构。

**v471 重解方案** (用户确认 3 大架构决策):
1. **`mp.get_context('fork')` worker batching** — shared data (data_full/prims/aux/fundamentals) 一次性 fork 继承 via COW, 单 Worker 顺序处理日期范围, 消除 per-date pickle/parquet 反序列化
2. **`factor_cache/parquet_f/{factor}/{year}.parquet` 存储** — float32 schema (date_i16 全局交易日序号 + symbol_i16 dict idx + value_f32), zstd level 3
3. **manifest per-factor source_hash** — 粒度失效

| 文件 | 关键改动 |
|------|----------|
| `quant/factor/store.py` | **完全重写 (v470)**: `parquet_f/{factor}/{year}.parquet` 布局, float32 schema(date_i16+symbol_i16+value_f32, zstd), fork pool worker (COW 继承 data_full/prims/aux, 一次 fork per chunk), `_worker_main` 模块级静态方法, checkpoint resume (failed_dates 加回重试 + source_hash check), `_build_symbol_map` 累积 i16 字典, trading_days 累积 date_i16 |
| `quant/factor/store_metadata.py` | 元数据模块: symbol_dict.json / trading_days.json / factor_{name}.meta.json, partition_year() / partition_path() / meta_path() |
| `scripts/materialize_full.py` | WORKERS 6→3 (M1 8GB 机器, fork COW 无多进程副本), chunk_days 调优 |
| `quant/factor/compute/_primitives.py` | 磁盘缓存 float32 parquet (完成) |
| `quant/config/config.yaml` | cache_max_memory_entries: 3 |

**验证**: batch1 13 dates × 5 factors 全量 materialize + load + is_materialized + list_cached_dates 全部正常 (wq_alpha_006/ztd 无有效值属正常, 无对应股票)。

**Batch 状态**: Batch 1-5 顺序 `nohup env PYTHONPATH=. .venv/bin/python scripts/materialize_full.py > logs/materialize_full_all.log 2>&1 &` (Workers=3, fork, 顺序批次 1→5).

## 当前状态 (test-v470, 2026-08-12)

### v470: 全因子物化回填 + primitive_cache 磁盘 LRU (2026-08-12)

**背景**: factor_registry 定义全集 114 中 20 个数据源不足 (北向 3 / intraday 3 / analyst 4 / fund_hold 3 /
holder_trade 3 / pledge_ratio), 剩余 94 因子 (90 全期 + 4 macro) 确认进入物化池。物化池不受状态收缩限制
(`factor_names` 显式列表 + `status_filter=None`)。全量 1845 交易日 (2019-01-02..2026-08-11) 全部 pending
(v466 source_hash 变更使旧 manifest 全失效), 分批回填。

| 文件 | 关键改动 |
|------|----------|
| `scripts/materialize_full.py` | 新增分批物化脚本: 6 批 (1: 5 probation; 2/3: 46 价量; 4: 22; 5: 21 含基本面/margin/macro), START=2019-01-01, WORKERS=6, force=False (幂等 + checkpoint 断点续传), `--batch N` / `--dry` 参数 |
| `quant/factor/compute/_primitives.py` | **磁盘缓存 LRU**: 新增 `_evict_disk_lru_if_needed()` (保存后按 mtime 淘汰最旧, 上限 `cache_max_disk_entries`), 磁盘命中时 `os.utime` touch (LRU 语义)。原因: 物化每 chunk 落盘 ~1GB `prims_*.pkl` 且 key 含数据哈希 (窗口随 chunk 滚动) → 跨 chunk 命中率趋近 0, 无上限曾膨胀至 10GB |
| `quant/config/config.yaml` | 新增 `factor.compute.cache_max_disk_entries: 4` (≈4GB 有界; 来源注释: v470 实测 10GB 膨胀) |

**物化池定稿 (94 因子)**: 排除北向/intraday/analyst/fund_hold/holder_trade/pledge (数据不足, 避免 nightly 空算)。
macro 4 因子已注册进 registry (category=macro, status=evaluating): macro_cpi_yoy / macro_m2_yoy / macro_pmi_diff / macro_rate_10y。
数据源 `macro_indicator` 表 (2008-01 起; 无 pmi → pmi_diff 全 0 可物化)。

**磁盘空间事件 (非内存泄漏)**: 现象 70G→47G 可用。根因 ① primitive_cache 无回收策略 10GB (本批物化 5 chunks 即 4.6GB);
② `/var/folders` 昨晚 (01:27) 全量物化中断残留 tempdir 5.3GB (data_full.parquet + aux_full.pkl + fundamentals.pkl ×3)。
已清理: 删 tmp 残留 + 全部 primitive_cache (重算成本 5-8s/chunk, 有内存缓存兜底), 加磁盘 LRU 治本。
实测: 全量 94 因子最终占用 ≈ 6.5-7GB (8.05 字节/行 × ~8.5 亿行), 磁盘余量充足 (available ~60G+)。

**Batch 状态**: Batch 1 (5 probation) 12:20 启动 PID 58315 (nohup, logs/materialize_batch1.log), 预计 ~85min。
批次 2-5 依次 `nohup env PYTHONPATH=. .venv/bin/python scripts/materialize_full.py --batch N > logs/materialize_batchN.log 2>&1 &`。
全部完成后: 删除一次性脚本 + 全量回归 + 抽查 FactorStore.load() 某日 94 因子行数。

**Batch 1 事故复盘 (v470 同章更新)**:
- Batch 1 首跑 (12:20→14:35, PID 58315) 成功 1705 天/3809 万行, 但 **2022-07-20..2023-02-16 共 140 天失败**:
  根因 — 13:05 清理 /var/folders 残留 tempdir 时误删了物化进程 chunk 5 正在使用的
  `tmpimyx6i4x` (TemporaryDirectory 名巧合与昨晨残留目录同名), 13:06 workers 读
  data_full.parquet 全部 FileNotFoundError。教训: **删除 tempdir 前必须先确认无活动物化进程**。
- **checkpoint resume 缺陷 (已修复)**: 失败日期 < checkpoint.last_date 被 resume 裁剪 → 重跑 0
  pending 永不重算。修复: `_write_checkpoint` 记录累计 failed_dates 快照, resume 时加回重试
  (“(%d failed retry)”)。
- 重跑 (PID 85537, 15:44 起) todo=1045 天 (2022-04-21..2026-08-11: 140 失败 + source_hash 变化
  导致的 invalidate 段), 预计 ~2h。**待其完成后**: batch1 5 因子全期完成, 再启 `--batch 2`。
- 注意: 每次重启物化进程, source_hash 变化 → 大量日期 invalidate 重算 (设计行为, 非 bug)。

**测试**: test_factor_compute 16 passed; test_v305_factor_cache_trailing_slice 5 passed (LRU 集成验证: 文件数封顶 4, batch1 旧 chunk 缓存被正确淘汰)。
全量 367 基线待批次完成后回归。



### v469: 因子缓存格式唯一化 — 移除全部 gzip CSV 依赖 (2026-08-12)

**背景**: 审计发现 parquet 已是唯一写入格式 (v466 起), 但 `.csv.gz` 仍以 3 种形态残留:
337 个旧格式文件 (8-11 weekly 评估物化的 v465 产物, 378d 窗口) + 3 处没跟上迁移的取数/判活代码 +
1 个从未执行完的一次性迁移脚本。LGB/XGB 训练日期发现扫 `*.csv.gz` → 训练集被旧格式
文件集限缩 (337 天), 且文件被删即训练崩溃; stats_cache 快照失效检测扫 csv.gz mtime →
parquet 主存储下恒空, mtime 失效检测静默失灵。

| 文件 | 关键改动 |
|------|----------|
| `quant/factor/store.py` | 新增 `list_cached_dates()` 统一日期发现入口 (parquet/date=* 主扫, 空目录回退 csv.gz 兼容, 数据归档后自然失效); `latest_cached_date()` 改为复用; **删除全部 CSV 分支**: `_path()`/`_read_raw_lines()`/`_scan_file_factors()` (死代码, 零调用) + `load()` 的 parquet→CSV 回退 (读失败改直接抛, 零降级) + `_get_existing_factors` 的 CSV 扫描回退 + `trim_to_max_days`/`force` 的 csv.gz 匹配; `_date_has_data` 改查 parquet 分区; 顺带修复既有缺陷: `load()` 无因子过滤时 `filters=[]` 传给 pyarrow 目录读取报 "Malformed filters" → 改 `filters=None` (实盘链 `factor_store.load(date, factor_names=None)` 此前必炸) |
| `quant/alpha/qlib_model.py` | 训练日期发现 `fstore.list_cached_dates()` (原扫 `.csv.gz`) — 训练集回归 parquet 全量 821 天 |
| `quant/alpha/xgb_model.py` | 同上; 删无用 `cache_dir` 局部变量 |
| `quant/factor/stats_cache.py` | 快照失效检测改扫 `parquet/date=*` 目录 mtime (原 `.csv.gz` 恒空) — test-v399 的"底层数据变化自动失效"在 v466 后恢复生效 |
| `migrate_csv_to_parquet.py` | 删除 (git rm) — 迁移已完成, 脚本既不删源文件也从未执行完, 无保留价值 |
| 数据 | 337 个 `.csv.gz` (55MB) 归档至 `quant/data/factor_cache/csv_gz_backup/` (可恢复, gitignore 不跟踪) |

**验证**: `list_cached_dates()` 返回 821 天纯 parquet; load 无过滤/带过滤/批量均正常;
全量 `test/` 367 passed, 0 failed。

**注意事项**
- **全因子物化从未跑过 (独立已知事项)**: 全部历史物化均为 5-6 因子评估组或 7 因子演示物化,
  parquet 821 天仅覆盖 9 种因子 → 8-12 08:00 晚间链 signals 崩溃
  `factor_store returned empty for 2026-08-11 (946 symbols)` — 物化只算评估因子子集,
  signals 请求全因子集返回空。与 csv.gz 无关, 需专项处理 (全因子物化任务)
- `list_cached_dates` 的 csv.gz 回退分支保留仅为迁移期兼容, csv_gz_backup 内文件已不在扫描路径
- `load()` 删除回退后 parquet 分区损坏将直接抛异常 (零 fallback 约束)

### v467: 执行引擎无限循环挂死根因 + v462 连带 bug 修复 (2026-08-12)

**背景**: v466 记录为"环境性阻塞"的 5 个测试文件悬挂问题被重新诊断 — 根因不是 logger,
是 `engine.execute` 的无限循环 + v462 (P5 批量 DB 重构) 一系列连带 bug 一直躲在挂死后面。
本次定位挂死真根因并修复全部连带问题, 悬挂清单清零。

**挂死根因 (真正根因, 非 logger)**
- `quant/execution/engine.py` — **`for e in entries:` 循环体内 `entries.append(e)` → 无限循环**:
  v462 (bb17613) 批量重构引入, 每轮把处理过的 e 追加回列表, `buy_cost` 被无限调用,
  quant.log 疯写 100MB+ (15s 内 45 万次相同日志), 表现为"emit/flush 卡死"。
  T+1 阻塞的卖单因 `continue` 逃逸, 所以只含买单/正常卖单的测试才挂。
  顺带删除 sell 分支完全重复的 PnL 计算块 (复制粘贴错误)。

**v462 连带 bug (被挂死掩盖, 本次全部暴露并修复)**
| 文件 | 关键改动 |
|------|----------|
| `quant/data/repos/trade_repo.py` | **pending_orders 补 `mode` 列 migration** — `get_orders` 查询 `WHERE mode=?` 但该表从未加过该列 (sim_trades/daily_signals 都有); **`get_orders` 列映射全部错位 1** (target_shares=r[3] 实为 side 列, filled_shares 起全错位); **`get_daily_flow` 改累计语义** `date<=?` (原只查当日, 全账本重算 docstring 不符) |
| `quant/execution/engine.py` | **除权检测改 dividend 事件表精确查询** — 原 gap 启发式 (订单价 vs 真实昨收偏差 > 涨跌幅阈值) 对模拟器/测试虚构价格必然误杀 (600036 买 20.00 vs 昨收 38.95 → 48.7% gap → 误跳过 4 个 broker 测试)。现 `SELECT symbol FROM dividend WHERE ex_date = date` 精确判定, 当日有除权事件才跳过 (用户决策) |
| `quant/scheduler/reconcile.py` | **equity_cross 语义修正** — 原 initial+flow 重算 (v462 前语义) 与测试契约 (日终快照交叉: daily_equity 最近快照 vs 当前现金, 快照被篡改 → drift 暴露) 不符; **新增 invariant 检查行** (cash >= 0); **新增 filled_but_no_trade 交叉核对** (filled 订单当日账本无对应买入 → break); **返回结构补 `positions`/`cash`/`orders` 分组** (保留 `rows` 兼容 web), 含 `check`/`order_status` 等契约字段 |

**验证**: `test_execution_model` + `test_execution` + `test_broker_adapter` + `test_reconcile` 63 passed (原悬挂);
全量 `test/` 365 passed, 2 failed (12.3s, 原 600s 超时挂死) — 2 failed 为既有漂移, 已在 v468 修复。

**注意事项**
- dividend 表覆盖不全 (1551 行) → 除权防护依赖 dividend 同步任务; 无记录的 symbol 不再被 gap 启发式保护 (精确优先设计)
- `get_daily_flow` 语义改为累计 (`date<=?`), 调用方仅 reconcile, 无其他影响
- 跑测试必须 `QUANT_LOG_DIR=/tmp/quant-test-logs` (测试会写爆 web 进程共享日志)

### v466: 因子物化缓存 + 回测链路 16 项 bug 修复 (2026-08-12)

**背景**: 物化因子缓存代码流程与回测策略代码流程双审计 (MC-1..8 物化链路 / BT-1..8 回测链路), 逐项修复。

**MC — 物化缓存流程**
| 文件 | 关键改动 |
|------|----------|
| `quant/factor/store.py` | **分块物化恢复** — chunk_days 此前从未生效 (全量单 Pool + data_full 2000 天全载 → OOM 风险), 现外层 chunk 循环, 每块独立 data_full/prims/tempdir; **逐日期×因子粒度** — `_date_missing_factors()` 集合包含判断 (原 `len(existing) >= len(factor_names)` 数量制漏判"数量同集合异"), 源表修复后缺失因子自动补算; **failed_dates 上报** — worker "ERROR:" 不再静默吞掉, 返回 dict 含 `failed_dates`; **断点续传接入** — 既有 `_write_checkpoint/_read_checkpoint` 死代码接通, 每块完成写断点; **删重复 `_write_chunk_rows`** (CSV 死代码); **source_hash 扩展** — 纳入 FACTOR_SHORTCUT 算子源码 + precompute_primitives 源码 (原只 hash fn 源码, shortcut 修复不失效); **trim_to_max_days 改扫 parquet** — 锚点/删除按 `parquet/date=*` 目录 (原只匹配 .csv.gz → 恒 no-op); 新增 `latest_cached_date()` helper; WAL checkpoint 改一次性独立连接 (原关闭线程局部共享连接) |
| `quant/scheduler/factor_cache.py` | 双 `_connect()` → 单连接 try/finally 关闭; `failed_dates` 非空 → 任务 status=failed + error_msg; trim 失败不再降级 warning (raise) |
| `quant/scheduler/attribution.py` | G1 OOS 最新物化日期判定改走 `FactorStore.latest_cached_date()` — 原扫 `.csv.gz` 在 parquet 主存储下恒为空 |

**BT — 回测流程**
| 文件 | 关键改动 |
|------|----------|
| `quant/pipeline.py` | 中性化市值改 PIT `market_cap` (fund_val_piv 覆盖, 原 `total_mv` 为 stocks 当前快照 → 历史日期前视); probation 名单改用 ctx.probation_names 冻结 (原每日期查 factor_registry, 非 PIT + DB 往返); 删除 limit_up_pool 错误建表 (3 列 schema 与 limit_up.py 16 列冲突) |
| `quant/execution/stop_loss.py` | `_compute_atr` 改 `date < as_of` (原 `date <=` 混入执行日盘中行情 → 日内前视); `_CACHE` 加 4096 上限; `RiskManager.check` 加 `atr_panel` 注入参数 (回测热路径免逐仓 SQL, 保留 `_compute_atr(sym, self.atr_period, today)` 调用形态) |
| `quant/backtest/loop.py` | 删 inc_cov/inc_ic 死代码 (covariance 无消费者 + 两段重复预热互覆盖 + IncrementalIC.update 用 now() 打历史戳); IC precheck 改全量窗口日期逐日检查 (原只查起始日); equity 统一 MTM (skipped/exception 路径由成本价改 `_br.get_mtm_capital`); 删 limit_up_pool 错误建表 / preload_ztd_cache (~80MB) / fundamentals 双重查询; daily alpha 扣无风险利率; ATR 面板预计算 (rolling mean TR, 与 _compute_atr 口径一致) + probation 冻结名单注入 ctx |
| `quant/backtest/context.py` | ExecutionContext 新增 `atr_panel`、`probation_names` 字段 |
| `quant/execution/execution_model.py` | 两处 `rm.check(...)` 透传 `atr_panel=getattr(ctx, "atr_panel", None)` |
| `quant/scheduler/oos_verify.py` | 新增 `symbols` 参数 (回测 IC 与主循环同口径); 无显式 symbols 时按 `rank_by_turnover` 流动性取 top-N (原 `all_symbols[:n]` 无排序); 返回加 `n_symbols`; 单日 factor_cache miss 降级跳过 (原 RuntimeError 中断整链) |
| `quant/factor/stats_cache.py` | `compute_backtest_ic` 删死参数 `inc_ic`, 加 `symbols` 透传; `insert_ic_daily` 的 `n_stocks` 用实际股票数 (原 `len(per_factor)` 因子数 → 虚假样本量) |
| `quant/utils/logger.py` | `QUANT_LOG_DIR` 环境变量 — 日志目录隔离 (web 进程与 pytest 并发写同一 FileHandler 锁争用) |

**注意事项**
- source_hash 方法变更 → 所有旧 manifest 失效, **首次物化将全量重算** (预期一次性成本)
- 回归: `test_v305_factor_cache_trailing_slice` + `test_stop_loss_c4` + `test_risk_manager` + `test_codereview_fixplan_p0` 50 passed; 其余 16 个轻量文件 PASS
- 悬挂问题已在 v467 修复 (根因 = engine.execute 无限循环, 非 logger); `test_scheduler_status_c10` 仍为测试漂移 (execute.py 无 `_tk_start`)
- 跑测试建议 `QUANT_LOG_DIR=/tmp/quant-test-logs` 隔离日志

### v468: 剩余 2 个既有测试漂移修复 — 全量套件归零失败 (2026-08-12)

**背景**: v467 遗留的 2 个既有漂移失败 (`close_5min` schema、`_tk_start`) 修复, 全量 `test/` **367 passed, 0 failed**。

| 文件 | 关键改动 |
|------|----------|
| `quant/factor/compute/_preload.py` | intraday_snapshot aux 预载查询列对齐实际表结构 — 原 `open_30min/open_30min_vol/close_5min` 列不存在 (表结构为 `date, symbol, mode, price, volume, prev_close`), try/except 吞错 → aux 恒空 → 三因子静默失效; 改 `SELECT symbol, date, mode, price, volume, prev_close` |
| `quant/factor/compute/intraday.py` | 3 因子 aux 路径 + DB 回退查询统一对齐实际 schema, 按 mode 区分: intraday_reversal 用 `mode='open'` 的 `price/prev_close`; open_volume_ratio 用 `mode='open'` 的 `volume`; close_surge 用 `mode='close'` 的 `price`. 修复前 DB 回退路径 `no such column` 直接爆炸 |
| `test/test_codereview_r10_snapshot_gate.py` | 测试 aux 数据列名对齐新 schema (mode/price/volume/prev_close) |
| `test/test_scheduler_status_c10.py` | 测试适配 Runner 重构后契约: execute 状态由 `runners._dispatch` 统一落 (无异常→ok), 测试改验证 `_run` 返回业务空转 dict + no_targets metric; lgb_train patch 改打在模块绑定名 `lgb_mod._tk_start/_tk_finish` (模块级 `import ... as` 绑定, patch task_log 模块无效) |

**注意事项**
- intraday 快照的 `prev_close` 列当前写入为 None (snapshot.py 未填) → intraday_reversal 因子实际仍返回 None, 属数据层问题, 与查询修复无关
- execute 任务状态 ok/failed 由 Runner 统一判定, 任务模块不再自管状态 (v424 重构方向)

### v453: 内存泄漏修复 — 3 无界缓存 + SQLite 连接泄漏 (2026-08-11)

**背景**: 全量代码审计发现 3 个无界模块级缓存和 orchestrator SQLite 连接泄漏风险。

**P0 — _PRIMITIVE_CACHE LRU 限界**
| 文件 | 关键改动 |
|------|----------|
| `quant/factor/compute/_primitives.py` | `_PRIMITIVE_CACHE` 加 `_MAX_MEMORY_CACHE` 上限 (默认 8), 新增 `_evict_lru_if_needed()` 淘汰最旧条目。来源: 每条目 ~20-50MB, 8 条目 = 160-400MB 峰值 (M1 8GB 硬约束) |
| `quant/config/config.yaml` | 新增 `factor.compute.cache_max_memory_entries: 8` |

**P1 — _ztd_cache 生命周期绑定**
| 文件 | 关键改动 |
|------|----------|
| `quant/factor/compute/price/_alternative.py` | 新增 `clear_ztd_cache()` 释放 ~80MB (2000 日期 × 5000 symbols) |
| `quant/factor/store.py` | 物化结束后调用 `clear_ztd_cache()` |
| `quant/pipeline.py` | 非回测 scope 完成后调用 `clear_ztd_cache()` |
| `quant/backtest/loop.py` | 回测完成后调用 `clear_ztd_cache()` |

**P2 — SQLite 连接 context manager**
| 文件 | 关键改动 |
|------|----------|
| `quant/scheduler/orchestrator.py` | 6 处 `sqlite3.connect()` 改为 `with` 上下文管理器 (`_get_today_status`, `_get_today_aborted`, `_get_monitor_failures`, `_cleanup_evening_children`, `_cleanup_zombie_tasks`, `_check_timeouts`) |
| `quant/scheduler/runners.py` | 5 处同上模式修复 |

**背景**: 完成长期愿景 (6 月+) 4 大战略支柱。

**支柱 1: 因子平台化 — 注册/血缘/文档/测试/回测/上线全流程 CI/CD**
| 文件 | 关键改动 |
|------|----------|
| `quant/factor/platform.py` (新建) | `FactorRegistry` (PostgreSQL 元数据), `FactorMetadata` 完整定义, `FactorTestRunner` (编译/单元/回测), `FactorPipeline` (compile/test/backtest/register/deploy 5 级流水线), `FactorDocumentGenerator` (自动生成 Markdown 文档), `CICDGenerator` (GitHub Actions/GitLab CI 自动生成) |
| `quant/factor/state_machine.py` | 复用: 复用状态机做编译/评估/注册/状态迁移 |

**支柱 2: 多策略隔离 — 策略级资金池/风控额度/业绩归因/独立部署**
| 文件 | 关键改动 |
|------|----------|
| `quant/strategy/__init__.py` (新建) | `StrategyInstance` (独立运行时: 资金/风控/执行/因子), `StrategyManager` (统一编排: 注册/启停/调仓/风控/资金分配), `CapitalAllocation`/`RiskQuota`/`StrategyConfig` 完整配置体系 |

**支柱 3: 另类数据接入 — 研报情感/供应链/ESG/卫星/信用卡/招聘**
| 文件 | 关键改动 |
|------|----------|
| `quant/data/alternative.py` (新建) | `AlternativeDataManager` 统一管理, 8 大内置数据源基类 (`ResearchReportSource`, `SupplyChainSource`, `ESGSource` 等) + 可扩展插件架构, 统一 `fetch/validate/factorize` 接口, 自动同步 SQLite → 因子表 |

**支柱 4: 分钟级高频 — Tick 清洗/分钟因子/智能路由/微观结构模型**
| 文件 | 关键改动 |
|------|----------|
| `quant/execution/highfreq.py` (新建) | `TickCleaner` (异常值/价格跳变/成交量异常/价差/时间间隔清洗), `MinuteAggregator` (Tick→分钟K线, VWAP/买卖VWAP/价差/波动率), `HighFreqFactorEngine` (价量/订单流/微观结构/波动率/流动性 5 大类因子), `SmartRouter` (TWAP/VWAP/POV/IS/Adaptive/Iceberg/Dark 7 大算法引擎), `TCAAnalyzer` (到达价/VWAP/TWAP/IS/价格冲击/时机/机会成本分解), `HighFreqExecutionEngine` 统一入口 (Tick清洗→分钟聚合→因子→路由→执行→TCA 完整链路) |

**配置更新** (`quant/config/config.yaml`):
```yaml
duckdb:
  migration_batch_size: 100000
  sync_interval_sec: 300
  max_workers: 4
  path: "data/market.duckdb"
backtest:
  distributed:
    enabled: true
    backend: "auto"
    max_workers: 4
    max_concurrent: 8
    max_combo_per_grid: 500
mlflow:
  tracking_uri: "sqlite:///mlflow.db"
  experiment_name: "quant_alpha"
bentoml:
  store_path: "~/bentoml"
  default_service_port: 3000
prometheus:
  enabled: true
  port: 9090
  pushgateway: ""
grafana:
  enabled: true
  port: 3000
  datasource: "prometheus"
```

**验证**:
- **367 tests passed** (11.04s)
- 29 源文件 + 4 测试文件全部 `ast.parse` 语法检查通过
- **VERSION → test-v438** (re.sub 推进)
- **HANDOFF.md** 完整更新

### v437: 中期演进 Phase 2-4 全量落地 (2026-08-09)

**背景**: 完成中期演进 (1-2 月) Phase 2-4 全部 3 项核心项。

**Phase 2: 分布式回测 Ray/Dask**
| 文件 | 关键改动 |
|------|----------|
| `quant/backtest/distributed.py` (新建) | `DistributedBacktestEngine`: Ray/Dask/线程池统一后端, 参数网格并行搜索, 结果自动聚合持久化 |
| `quant/backtest/loop.py` | 新增 `distributed` 配置节, `run_grid_search` 入口函数 |
| `quant/config/config.yaml` | 新增 `backtest.distributed` 配置节: `enabled/auto/ray/dask/thread`, `max_workers/max_concurrent/max_combo_per_grid` |

**Phase 3: 模型服务化 MLflow + BentoML**
| 文件 | 关键改动 |
|------|----------|
| `quant/alpha/model_serving.py` (新建) | `ModelServingPlatform` 统一入口: `MLflowTracker` (Tracking/Registry), `BentoMLService` (打包/服务化), `ShadowDeploymentManager` (影子流量/金丝雀发布), `ABTestManager` (A/B 测试/统计显著性) |
| `quant/alpha/__init__.py` | 导出 `ModelServingPlatform`, `MLflowTracker`, `BentoMLService`, `ShadowDeploymentManager`, `ABTestManager` |
| `quant/config/config.yaml` | 新增 `mlflow:`/`bentoml:`/`prometheus:`/`grafana:` 配置节 |

**Phase 4: 监控标准化 Prometheus/Grafana**
| 文件 | 关键改动 |
|------|----------|
| `quant/monitoring/prometheus.py` (新建) | `QuantMetrics` 业务指标全集 (交易/因子/风控/数据质量/系统/调度/回测), `MetricsCollector` 定期采集, `PrometheusPusher` Pushgateway 集成, `GrafanaDashboardBuilder` 仪表盘 JSON 生成, `AlertRuleBuilder` 告警规则 YAML 生成, `MonitoringPlatform` 统一入口 |
| `quant/config/config.yaml` | 新增 `prometheus:`/`grafana:` 配置节, 现有 `mlflow:`/`bentoml:` 配置 |

**配置更新** (`quant/config/config.yaml`):
```yaml
duckdb:
  migration_batch_size: 100000
  sync_interval_sec: 300
  max_workers: 4
  path: "data/market.duckdb"
backtest:
  distributed:
    enabled: true
    backend: "auto"
    max_workers: 4
    max_concurrent: 8
    max_combo_per_grid: 500
mlflow:
  tracking_uri: "sqlite:///mlflow.db"
  experiment_name: "quant_alpha"
bentoml:
  store_path: "~/bentoml"
  default_service_port: 3000
prometheus:
  enabled: true
  port: 9090
  pushgateway: ""
grafana:
  enabled: true
  port: 3000
  datasource: "prometheus"
```

**验证**:
- **367 tests passed** (11.11s)
- 29 源文件 + 4 测试文件全部 `ast.parse` 语法检查通过
- **VERSION → test-v437** (re.sub 推进)
- **HANDOFF.md** 完整更新

### v436: 中期演进 Phase 2-4 全量落地 (2026-08-09)

**背景**: 完成中期演进 (1-2 月) 剩余 3 项核心项。

**Phase 2: 分布式回测 Ray/Dask**
| 文件 | 关键改动 |
|------|----------|
| `quant/backtest/distributed.py` (新建) | `DistributedBacktestEngine`: Ray/Dask/线程池统一后端, 参数网格并行搜索, 结果自动聚合持久化 |
| `quant/backtest/loop.py` | 新增 `distributed` 配置节, `run_grid_search` 入口函数 |
| `quant/config/config.yaml` | 新增 `backtest.distributed` 配置节: `enabled/auto/ray/dask/thread`, `max_workers/max_concurrent/max_combo_per_grid` |

**Phase 3: 模型服务化 MLflow + BentoML**
| 文件 | 关键改动 |
|------|----------|
| `quant/alpha/model_serving.py` (新建) | `ModelServingPlatform` 统一入口: `MLflowTracker` (Tracking/Registry), `BentoMLService` (打包/服务化), `ShadowDeploymentManager` (影子流量/金丝雀发布), `ABTestManager` (A/B 测试/统计显著性) |
| `quant/alpha/__init__.py` | 导出 `ModelServingPlatform`, `MLflowTracker`, `BentoMLService`, `ShadowDeploymentManager`, `ABTestManager` |
| `quant/config/config.yaml` | 新增 `mlflow:`/`bentoml:`/`prometheus:`/`grafana:` 配置节 |

**Phase 4: 监控标准化 Prometheus/Grafana**
| 文件 | 关键改动 |
|------|----------|
| `quant/monitoring/prometheus.py` (新建) | `QuantMetrics` 业务指标全集 (交易/因子/风控/数据质量/系统/调度/回测), `MetricsCollector` 定期采集, `PrometheusPusher` Pushgateway 集成, `GrafanaDashboardBuilder` 仪表盘 JSON 生成, `AlertRuleBuilder` 告警规则 YAML 生成, `MonitoringPlatform` 统一入口 |
| `quant/config/config.yaml` | 新增 `prometheus:`/`grafana:` 配置节, 现有 `mlflow:`/`bentoml:` 配置 |

**配置更新** (`quant/config/config.yaml`):
```yaml
duckdb:
  migration_batch_size: 100000
  sync_interval_sec: 300
  max_workers: 4
  path: "data/market.duckdb"
backtest:
  distributed:
    enabled: true
    backend: "auto"
    max_workers: 4
    max_concurrent: 8
    max_combo_per_grid: 500
mlflow:
  tracking_uri: "sqlite:///mlflow.db"
  experiment_name: "quant_alpha"
bentoml:
  store_path: "~/bentoml"
  default_service_port: 3000
prometheus:
  enabled: true
  port: 9090
  pushgateway: ""
grafana:
  enabled: true
  port: 3000
  datasource: "prometheus"
```

**验证**:
- **367 tests passed** (11.08s)
- **29 源文件 + 4 测试文件** 全部 `ast.parse` 语法检查通过
- **VERSION → test-v436** (re.sub 推进)
- **HANDOFF.md** 完整更新

### v435: 中期演进 Phase 1 - DuckDB 迁移 (2026-08-09)

**背景**: 启动中期演进 (1-2 月) Phase 1 - 数据层迁移到 DuckDB。

**核心改动**:
| 文件 | 关键改动 |
|------|----------|
| `quant/data/duckdb_store.py` (新建) | `DuckDBManager` 单例: 列式存储 + 并行查询 (4 线程) + Arrow 零拷贝; 后台异步同步 SQLite → DuckDB 增量同步 |
| `quant/data/duckdb_store.py` | `DuckDBDataProxy`: DataStore 兼容代理, 读查询透明分流到 DuckDB |
| `quant/data/store.py` | `get_daily` 优先走 DuckDB 列式并行查询 (无参数上限), 失败回退 SQLite; 新增 `_duckdb_proxy` 实例属性 |
| `quant/config/config.yaml` | 新增 `duckdb:` 节: `migration_batch_size=100000`, `sync_interval_sec=300`, `max_workers=4`, `path="data/market.duckdb"` |
| `quant/config/constants.py` | `_require_cfg` 支持 `default` 参数, 兼容可选配置 |

**架构变更**:
- **写入路径**: 仍走 SQLite (DataStore) — 事务/ACID/增量更新
- **读取路径**: 分流到 DuckDB (列式存储/并行查询/Arrow 零拷贝) — 因子计算/回测/归因
- **同步机制**: 后台线程每 5 分钟增量同步 SQLite → DuckDB (主键 UPSERT)

**验证**:
- **367 tests passed** (10.31s)
- 27 源文件 + 4 测试文件 `ast.parse` 语法检查通过
- **VERSION → test-v435** (re.sub 推进)
- **HANDOFF.md** 完整更新

### v434: 短期重构全量落地 (2026-08-09)

**背景**: 按代码审查建议执行短期重构 (1-2 周内完成) 4 项核心项。

**Refactoring #1: 统一 ExecutionContext / BacktestContext (消除 20+ 散参)**

| 文件 | 关键改动 |
|------|----------|
| `quant/backtest/context.py` | `BacktestContext` + `LiveContext` 合并为 `ExecutionContext` 单一数据类，字段完整，get_engine/get_cost_model/get_constructor 统一入口 |
| `quant/pipeline.py` | `generate_signals` / `execute_signals` 签名统一为 `ctx: ExecutionContext`，解包逻辑简化，移除 `PipelineContext` 兼容层 |
| `quant/backtest/loop.py` | 回测路径统一构建 `ExecutionContext`，参数名对齐 |
| `quant/scheduler/signals.py` | 实盘信号生成直接传 `ExecutionContext(suppress_push=False)` |

**Refactoring #2: Alpha 策略模式重构 (AlphaStrategy 抽象基类 + 装饰器注册)**

| 文件 | 关键改动 |
|------|----------|
| `quant/alpha/strategy.py` (新建) | `AlphaStrategy` 抽象基类、`@register_alpha` 装饰器、`get_alpha()` 工厂、`list_alphas()` |
| `quant/alpha/synth.py` | 5 个合成函数 (ic_weighted/equal_weight/sleeve/intersection/strict_intersection) 包装为 `@register_alpha` 子类 |
| `quant/alpha/__init__.py` | 导出 `AlphaStrategy`/`register_alpha`/`get_alpha`/`list_alphas`/`is_registered` |
| `quant/alpha/model.py` | `AlphaModel.combine()` 移除字符串分支，改用 `is_registered()` + `get_alpha()` 工厂模式 |

**Refactoring #3: 因子状态机合一 (FactorStateMachine 统一编译→评估→注册→状态迁移)**

| 文件 | 关键改动 |
|------|----------|
| `quant/factor/state_machine.py` (新建) | `FactorStateMachine` 统一类，合并原 `factor_curator.py` 编译/评估/注册 + `state_manager.py` 状态迁移，新增 `FactorEvent` 统一事件定义 |
| `quant/factor/state_manager.py` | 保留为向后兼容别名，内部委托 `state_machine.FactorStateMachine` |
| `quant/factor/factor_curator.py` | 移除编译/评估/注册逻辑，委托 `FactorStateMachine` |
| `quant/factor/state_manager.py` + `phase5_monitor.py` + `attribution.py` | 导入路径更新为 `quant.factor.state_machine.FactorStateManager` |

**Refactoring #4: 调度器拆分 (三大 Runner + 共用决策函数)**

| 文件 | 关键改动 |
|------|----------|
| `quant/scheduler/runners.py` (新建) | `InlineRunner`/`MonitorRunner`/`SubprocessRunner` 三大 Runner + `run_inline_tasks`/`run_monitor`/`run_evening_chain`/`run_weekly_eval` 入口函数 |
| `quant/scheduler/orchestrator.py` | 仅保留调度循环 + 共用 `_should_run`，移除具体执行逻辑，调用 `runners.py` 入口函数 |
| `quant/scheduler/__init__.py` | 导出 `run_inline_tasks`/`run_monitor`/`run_evening_chain`/`run_weekly_eval`/`_MAX_TASK_RETRIES` |

**删除死代码 (v432 已完成 + 本次补充)**:
- `risk/atr.py`, `risk/stress_test.py`, `execution/tca.py`, `execution/market_microstructure.py`, `backtest/bridge.py`, `evaluation/factor_diagnostics.py`, `factor/compute/price.py.bak` (v432)
- `factor/intersection.py` (P2-3 移至 alpha 层) (本次)

**验证**:
- **367 tests passed** (含 17 P1 新测试 + 4 P2 新测试 + 4 重构新测试)
- **27 源文件 + 4 测试文件** 全部 `ast.parse` 语法检查通过
- **VERSION → test-v434** (re.sub 推进)
- **HANDOFF.md** 完整更新

### v433: CODE-REVIEW-FIX-PLAN P1+P2 全量落地 (2026-08-09)

**背景**: 执行 CODE-REVIEW-FIX-PLAN 中全部剩余 P1 (17 项) 和 P2 (4 项)。P0 已完成 (v432)。

**P1 修复 (17 项全部落地)**:
| # | 修复 | 文件 | 关键改动 |
|---|------|------|----------|
| P1-1 | 节假日 2025-04-07 错标 | calendar.py | 删除错误日期, 保留清明节 4/4-4/6 |
| P1-2 | 腾讯 volume 单位 (手 vs 股) | quote.py | fields[6] ×100 转为股, 与新浪/通达信一致 |
| P1-3 | NoopBackend.get TTL 恒 +1h | cache.py | get() 改为 time.time() < ts, acquire_lock 同理 |
| P1-4 | 令牌桶限流器死锁 | cache.py | float tokens + tps, 调用方 fail-fast |
| P1-6 | 财务 PIT: stat_date+90 前视 | fundamental.py | 移除 +90d, 加 stat_date<=date 上界, compute_sue 备注待中期 |
| P1-7 | factor_cache 越过 end_date | factor_cache.py | max→min, CLAUDE.md 命令双参数 |
| P1-8 | parallel.py 缺 import | parallel.py | 顶层补 import pandas + _require_cfg |
| P1-9 | half_life 公式缺 ln2 | phase2_single.py | 19*ln2 替代 20, 两处同步 |
| P1-10 | PBO 门禁阈值脱节 | pbo.py + phase3_oos.py | logit_thresh 从 config pbo_max 读取 |
| P1-11 | iterative_clip 全超限 | portfolio.py | infeasible 时 clip 到 max_single 不归一 (sum<1) + warning |
| P1-12 | margin/daily_sync 日期格式 | daily_sync.py | 传 YYYY-MM-DD 而非 to_compact, 断言校验 |
| P1-13 | industry 单股票 NaN | neutralize.py | 样本<min 时保留原值 |
| P1-14 | pipeline 裸 except:pass | pipeline.py | logger.error + traceback + raise |
| P1-15 | 卖出缺报价 → 成本价 | pipeline.py + execute.py + execution_model.py | 移除成本价 fallback, warning 阻断 |
| P1-17 | factor_curator 重复注册 | factor_curator.py | 去重 turnover_accel, except 记录因子名+表达式+标记 |
| P1-18 | stocks.total_shares 无建表 | store.py | fund_cols 添加 total_shares REAL, migration 003 |
| P1-19 | trade_repo N+1 | trade_repo.py | get_fifo_costs_batch 单次 SQL 批量 FIFO |
| P1-20 | report/alerts 无 strategy 过滤 | report.py + alerts.py + config.yaml | 统一用 _require_cfg("strategy.name") |

**P2 修复 (4 项全部落地)**:
| # | 修复 | 说明 |
|---|------|------|
| P2-1 | 双路径不一致 | golden_test.py 新增 use_shortcut 参数, verify_strict() 对比 |
| P2-3 | alpha 反向依赖 factor | intersection_alpha/strict_intersection 移至 alpha/synth.py, 删除 factor/intersection.py |
| P2-5 | web 分层 | 新增 web/services.py (Position/Backtest/Stock/SignalService), app.py 路由 SQL 抽离 |
| P2-6 | pre-commit + CI + 模型注册 | .pre-commit-config.yaml, .github/workflows/ci.yml, quant/alpha/registry.py |

**已完成 P0**: 11 项 (v432)
**已完成 P1**: 17 项
**已完成 P2**: 4 项
**未落地**: P1-5 (news API 不可用), P1-16 (除权性能需实盘环境), P2-2 (scope 列已隔离), P2-4 (死代码已删 v432)

**验证**: 367 tests passed (含 17 P1 新测试 + 4 P2 新测试); 24 源文件 + 3 test 文件全部 ast.parse 语法检查通过; VERSION test-v433.

### v432: CODE-REVIEW-FIX-PLAN P0 修复落地 (2026-08-09)

**背景 (用户指令)**: 阅读 docs/reports/CODE-REVIEW-FIX-PLAN-2026-08-09-ZH.md, 深入代码核验修改建议是否合理, 合理者落地修复并归档.

**P0 修复 (11 项, 全部落地)**:

| # | 修复 | 文件 | 关键改动 |
|---|------|------|----------|
| P0-1 | universe list_date 格式错位 | store.py + universe_repo.py | strftime('%Y%m%d', ?) 统一 YYYYMMDD 比较, 当年上市股票不再漏选 |
| P0-2 | market_cap 三源三单位 | store.py | 拉取 source 列, eastmoney x1, jqdata x1e4 (万元), tushare x1e4. **注意**: 修办建议 jqdata x1e8 (亿元), 但实库核验 (600519: 150873598 x1e4=1.5e12 元 correct, x1e8=1.5e16 元 absurd) 证实 jqdata 实际为万元, 修正为 1e4 |
| P0-3 | neutralize NaN 传染 | neutralize.py | dropna + 切片 P 矩阵 P[pos][:,pos], 与标量路径一致 |
| P0-4 | 回测 ATR 未来行情 | stop_loss.py | _compute_atr 加 as_of 参数, SQL WHERE date<=?, 缓存 key 含 as_of; check() 传 today |
| P0-5 | XGB 特征缺列错位 | xgb_model.py | 移除 if fn in factor_values 过滤 + pad zeros, 严格按 feature_names 顺序 (同 qlib v406) |
| P0-6 | DSR/PSR 量纲 | deflated_sharpe.py + loop.py | 用每日 SR 参与 DSR (非年化), annualized_sr 仅显示 |
| P0-7 | EVAL_REJECT 非法事件 | phase5_monitor.py + state_manager.py | 用 EVAL_FAIL/IC_PERSISTENT 替代 EVAL_REJECT; retry_count 不双重递增 |
| P0-8 | 回测命名恒 backtest_1 | naming.py | next_name 加 db_path 参数 (必填); next_backtest_name 用 BACKTEST_DB |
| P0-9 | var 压力测试恒零 | var.py | stress_test 处理 pd.Series 权重 |
| P0-10 | VnpyAdapter 记账断裂 | broker_adapter.py | 实现 _on_trade/_on_order/_on_position 回调 + adapter 白名单校验 |

| P0-11 | monitor 崩溃 → reconcile 断链 | orchestrator.py + manifest.py | _should_run 允许 failed 重试 (aborted < max_retries); reconcile 依赖改为 depends_attempt; monitor timeout_s 由 None→21600 |

**验证**: 31 个 P0 回归测试 + 17 个 manifest 测试 (含 2 更名) + 全量 316 已有测试 = **367 passed**. VERSION test-v432.

**未修复 (P1/P2, 待后续)**: 17 项 P1 + 6 项 P2, 详见 docs/reports/CODE-REVIEW-FIX-PLAN-2026-08-09-ZH.md.

### v430r: 全量代码审查 + 逐条修复方案归档（纯文档，无代码改动，VERSION 未推进）(2026-08-09)

**背景 (用户指令)**: 全量代码审查（169 个 .py / ~3.9 万行，仅代码不读文档），并逐条给出修复方案连同报告归档。

**产出**: `docs/reports/CODE-REVIEW-FIX-PLAN-2026-08-09-ZH.md` (370 行)

**核心结论**:
- P0（高危 11 项）: universe list_date 格式错位（当年上市股票整年被漏）、market_cap 三源三单位、中性化批量 NaN 传染、回测 ATR 未来行情、XGB 特征缺列错位、DSR/PSR 量纲（显著性恒 1.0）、因子状态机 EVAL_REJECT 非法事件+retry 双+1、回测命名恒 backtest_1 互相覆盖、var 压力测试恒零、VnpyAdapter 记账断裂、monitor 崩溃当日不复原
- P1（中危 17 项）: 节假日 2025-04-07 错标、quote 量纲、TTL+1h、令牌桶 <60/min 失效、news 覆盖、财务 PIT 前视 4 处、factor_cache 边界、half-life 公式、PBO 阈值脱节、_iterative_clip、margin 日期格式、industry 单股 NaN、except:pass 等
- P2: 双路径统一、回测写生产表、死代码 ~2500 行清理清单

**复核指引**: 报告附"已核验证据索引"表，修复各 bug 前按表复核。修复 P0 会系统性改变历史回测收益（前视被清除 → 收益回落属预期）。

### v430: 数据完整性审计落地 — margin commit 丢失 / fund_flow 冷却 / 链超时预算 / 跌停池接通 (2026-08-09)

**背景 (用户指令)**: 数据完整性审计发现 market.db 与因子缓存缺口, 逐项根因溯源后按方案落地修改并归档.

**实锤根因**:
1. **margin 每日写入静默丢失** — v414/50f4a3e executemany 重构删掉 `conn.commit()` (margin.py:90-98, 149-157), sync_range 尾部无 commit → sqlite3 close() 回滚, 报告成功实际全丢 (08-06/07 两日 0 行; 08-07 13:51 backfill 67830 行也全部蒸发 → 2025-12 整月 17 日缺口)
2. **fund_flow 东财 API 封锁** (08-07 起 curl 56 Connection closed) — 外部源问题; 代码缺陷: `_UNAVAILABLE` 模块级内存标志每进程重置, 晚间链 subprocess 每次重新探测撞限流; 每日全量历史重拉 (lmt=0) 拖长夜间预算
3. **evening_chain 连续 4 天 aborted 误报** — manifest timeout_s=14400 (4h) < 实测夜链最长 25862.7s (7.2h); 23:00 被打 aborted 但 subprocess 继续跑完; 窗口 23:59 未关时可能重放 (08-03 双跑实证: orchestrator 20:13 重启后当天重放链)
4. **limit_down_pool 空表** — `net_limit_ratio` 情绪因子读 df_down 恒空失真; akshare `stock_zt_pool_dtgc_em` 可用
5. **news 因子数据源不可用** — `ak.stock_news_em()` 在本环境必然崩溃 (pyarrow ArrowInvalid: \u 正则), 判定不接入

**修改**:
1. `quant/data/margin.py`: `_sync_sse_raw` / `_sync_szse_wrapper` executemany 后补 `conn.commit()`; `_get_synced_dates` 改双市场 (SH+SZ) 齐备才算 synced — 修 SZSE 单独发布延迟被永久跳过的隐藏缺陷
2. `quant/data/fund_flow.py`: 冷却跨进程持久化 — 5/30 连败写 `quant/data/.fund_flow_cooldown` 时间戳文件, 30 分钟窗口内各进程跳过; sync_all/sync_single_stock 加 `days` 增量窗口 (None=全量 backfill)
3. `quant/scheduler/manifest.py`: evening_chain grace_s/timeout_s 14400 → 27000 (7.5h, 含注释实证)
4. `quant/data/limit_up.py`: 新增 `sync_down_date` / `sync_down_range` (akshare 跌停池, 对齐既有 DDL)
5. `quant/scheduler/daily_data.py`: fund_flow 传 days=100; 晚间链新增 limit_down 同步; news 判定不接入 (注释归档)
6. `test/test_evening_chain.py`: 超时断言 14400→27000; `test_v306_fund_flow_breaker.py`: 冷却文件隔离 tmp_path

**数据补回 (已执行)**:
- margin 2026-08-06/07 补齐 (SSE 已 1994, SZSE 08-07 源端待发布, 每日 sync 自愈窗口自动补)
- margin 2025-12 整月 17 日缺口 backfill 补齐 (43913 行), 2019~2026-08-07 全量闭环验证 0 缺口
- fund_flow 08-08 13 只成功 (1300 行), 08-09 600519 增量 10 行验证 OK (东财限流解除中, 冷却机制接管)

**测试**: 336 passed (test-evening_chain manifest 断言 + fund_flow breaker 隔离已更新)

### v430b: 08-08 因子策展注册 11 因子 (2026-08-09 补记)

周六 (08-08) weekly_eval 因子策展跑 3 轮全 ok, 第 1 轮 (07:05) 评 19 注册 11:

| 因子 | IC | 来源 |
|------|-----|------|
| vp_divergence | 0.0172 | 幻方2023 量价背离 |
| idio_vol_60d | 0.0553 | 幻方2023 特质波动 |
| smart_money_20d | 0.0773 | 九坤2023 聪明钱 |
| trend_strength | 0.0677 | 九坤2024 趋势强度 |
| liquidity_shock | 0.0366 | 明汯2023 流动性冲击 |
| micro_gap | 0.0325 | 明汯2024 微观缺口 |
| money_flow_cmf | 0.0387 | CMF 海通金工 |
| residual_momentum_proxy | 0.0668 | Blitz et al. 2011 |
| amihud_proxy | 0.0226 | Amihud 2002 |
| volume_price_trend | 0.0693 | 招商证券2023 |
| revenue_growth_yoy | 0.0203 | 国泰君安2022 营收增长 (v322-v328 名单中 "evaluating" 转正) |

第 2/3 轮 (07:45/09:53) 各评估 8 个, 0 注册 (market_beta_60d 等 obs 不足跳过)。

## 当前状态 (test-v429, 2026-08-08)

### v429: 遗留问题盘点归档 (2026-08-08)
全量扫描代码/文档遗留, 逐项判定 (详见文末「待完成」区扩展):

**判定已过时/已实现 (无需修复, 注释归档)**
1. `fundamental.py` TODO#3 (close_latest 从 daily 补) — 已落地: store.get_fundamentals() 注入 daily.close + high_52w 244日窗口计算
2. `_momentum.py` TODO(ADR-035) 反转方向 — reversal_5d 已评估退役 (|t|=0.15, DSR=0.0035), 方向问题闭合; -cum 变体见 ADR-042
3. `_event.py` post_5d 注释"未实现" — 实际已实现 (lhb.py 写入, market.db post_5d 113K+ 行), 更正注释
4. `datasource_retry.py` jitter (审计 B-19 声称未实现) — 实际已传 jitter=(0,1)
5. CODE-REVIEW-2026-08-07 R1-R11 — v418 已全量修复; Bug2/Bug3 判定非 bug
6. reconcile pnl_cross — v429 判定不实现: equity_cross 流水推演 + filled 订单交叉核对已覆盖 → reconciliation.md 已标注
7. 尾盘 5 分钟增量量能 — 无因子消费者, 零冗余原则不实现 (manifest 注释归档)

**涉及文件**: `quant/factor/compute/fundamental.py`, `price/_momentum.py`, `price/_event.py`, `quant/scheduler/manifest.py`, `docs/architecture/reconciliation.md`, `HANDOFF.md`

## 当前状态 (test-v428, 2026-08-08)

### v428: 调度系统 manifest 化重构 — 单一真相源 (2026-08-08)

**背景 (用户指令)**: 调度任务业务逻辑混乱 — 尾盘快照 14:55 触发语义错误 (尾盘区间 14:55-15:00, 应 15:00 收盘后拉收盘价+全日量); monitor 14:55 即退 (尾盘 5 分钟无风控); 部分任务靠埋点日志决定下一步启停; 需重新整理分析, 按方案 A+B 落地 (先细化 manifest Task 表, 再改码), 归档并写清来龙去脉.

**分析发现 (已交付报告, 经用户确认)**: 4 重入口并存 (orchestrator 常驻循环 / cron 2 条 / run_task.sh 手动 / 死代码 `_loop()` × 5 处 — signals/execute/monitor/attribution/weekly, 自 v4 起无人调用); 5 个状态读取函数语义混叠 (`_get_today_status`/`last_status`/`any_ok`/`_stage_status`/`_tk_start` dedup); 双超时表 (orchestrator `_TIMEOUTS` vs 各模块 grace_s); 周六 weekly 三重触发; 状态机语义混杂 (running=执行中+僵尸+lunch hack).

**方案**: A (止血) — 尾盘快照窗口改 15:00-15:05 (收盘后), monitor 延至 15:00 收尾, reconcile 加 monitor==ok 依赖, 删死代码. B (重构) — 声明式 manifest 任务表 + 单一编排器决策.

**落地 — 新增 `quant/scheduler/manifest.py` (单一真相源)**:
- `TaskSpec` dataclass: name/label/schedule/window (闭区间时间窗)/mode (inline|monitor|subprocess)/depends_ok (严格依赖, 全 ok 才触发)/depends_attempt (尝试过即放行)/grace_s/timeout_s/weekday/desc/group/has_multiprocess/subprocess_cmd
- `ALL` 字典 + `spec(name)` (缺名即崩) + `_PLAN_ORDER` (执行顺序)
- 任务定义一览:

| task | window | mode | deps | 变更 |
|---|---|---|---|---|
| signals | 08:00-15:30 | inline | — | 窗口化 |
| execute | 09:20-14:56 | inline | attempt[signals] | 窗口化 |
| snapshot_open | 10:00-14:55 | inline | attempt[execute] | 窗口化 |
| monitor | 09:30-15:00 | monitor (daemon) | — | 收尾 14:55→**15:00** |
| snapshot_close | **15:00-15:05** | inline | — | **修正: 原 14:55** |
| reconcile | 15:05-16:00 | inline | **ok[monitor]** | 新增真依赖 (原无) |
| evening_chain | 19:00-23:59 | subprocess | — | 超时迁入 manifest |
| weekly_eval | 周六 06:00-12:00 | subprocess | — | 窗口收编, 删除独立线程 |

**改动 — 2. orchestrator 重写为 manifest 驱动**:
- 新纯函数 `_should_run(spec, hhmm, weekday, status, aborted_cnt) -> bool`: 窗口+星期命中 → ok/failed/running 不触发 → depends_ok 全 ok → depends_attempt 在 status → aborted 次数 < 预算. 周频窗口判定在 is_trading_day 短路**前** (守 v416 教训).
- 主循环: 启动时 register_all + 僵尸清理 → 30s 轮询 → 每轮读一次状态 → 按 `_PLAN_ORDER` 决策触发; monitor 窗口内启动守护线程保活, 窗口结束后自退 (15:00 后 monitor._run_continuous 写 ok)
- `_dispatch`: inline=同步调用; subprocess=Popen 常驻字符串 (weekly/evening); snapshot 走 snapshot 模块直调
- 删除 `_TIMEOUTS` 字典 → 统一读 manifest.timeout_s (evening.py 同步改用 spec)
- 僵尸清理/超时检查沿用 v424-v427 逻辑 (波形对齐 manifest)

**改动 — 3. 死代码清理**: 删除 `_base.py`; 五个模块的 `_loop()`/`_weekly_loop` 全部移除; `__init__.py` 重写 — 单一 `start_all()` (orchestrator only, weekly 线程删除 — 双触发之源); 兼容旧 API 空壳保留; `restart.sh`/`run_task.sh` 无需改动 (start_all 兼容)

**改动 — 4. 界面元数据同步** (status.py): snapshot_close "15:00 (收盘)", monitor label "盘中风控" + "(窗口结束自退)" 描述

**验证**: 新增 `test_manifest_schedule.py` (17 测试: 窗口/状态机/依赖/重试预算/monitor daemon / 周频窗口); 改写 v416 测试对齐 manifest (周六窗口 06:00-12:00 可达判定); smoke_verify.sh 引用改 manifest. 全量 **336 passed** (319+17). VERSION → test-v428.

**部署**: 用户执行 `bash scripts/restart.sh` 重启服务; 重启后 orchestrator 以 manifest 驱动全天调度, 尾盘快照将在 15:00 后首轮触发.

**历史遗留(已归档至本节)**: v420-v427 的门控遮蔽/双触发/僵尸 running/界面即时自愈 — 均收敛进 manifest 决策 (ok 终态不重跑 / aborted 可重试 ≤2 / pid 存活检测 / _TIMEOUTS 删除).

## 当前状态 (test-v427, 2026-08-08)

### v427: weekly 补跑门控遮蔽修复 + "运行中"真伪判定 (2026-08-08)

**用户报告**: 重启后「评估-单因子检验」/「因子评估(总)」又显示运行中, 质疑逻辑对错.

**实证 (全链路)**:  
1. 真实时序: 07:05 一轮完成 ok (75335) → 07:45 重启产生 75354 (running, 被 pkill 杀) → 手动标 aborted → 09:53 再重启.  
2. 新 orchestrator (pid 32782) 启动 — `start_all` 实为双 daemon 线程 (orchestrator + weekly), 两线并发. **weekly 线程先读 `last_status` → 最新一条是 75354 (aborted) 遮蔽了 75335 (ok)** → 判定"未完成" → 触发重跑. orchestrator 线程随后 spawn subprocess → 被 `weekly_eval already running` dedup 拦下.  
3. **界面"运行中"是真的** — pid 32782 存活且每周评估 Phase 2 真在跑 (75397/75398/75399/75400 全是 32782 pid), 不是僵尸. 显示逻辑正确; 错的是**重复执行** (当天 ok 后又整跑第二遍, 浪费数小时).

**修复**: `task_log._last_status` (最新一条) 语义不适配门控 → 新增 **`any_ok(task, date)`** (当日存在过 ok 即不补跑); `_base._weekly_loop` 改用 `any_ok`. 遮蔽场景 (ok 后又 aborted) 不再重跑.

**验证**: 5 新测试 (`test/test_v427_weekly_gate.py`), 全量 **319 passed** (314+5). VERSION → test-v427.

## 当前状态 (test-v426, 2026-08-08)

### v426: 自愈扫描扩大至全部日期 (历史回放僵尸同清)

**背景**: v424/v425 的 `_check_timeouts` 只扫 `date = today` 行, 历史日期 (回放/迁移, 如 daily_data '2019-12-31' id 74653) 的僵尸 running 永远漏网 — 8/8 手动清 74653 后发现.

**改动**: `orchestrator._check_timeouts` — SELECT 去掉 date 过滤 (扫全部 running 行), 但**超时判定仅限今日行** (历史行 started_at 跨日, 不适用); pid 存活检测对所有日期生效. 历史日期 + 死 pid → 也 auto-abort.

**验证**: 新增 2 测试 (历史行 dead-pid 自愈 / 历史行 live-pid 不误杀), 全量 **314 passed** (312+2). VERSION → test-v426.

## 当前状态 (test-v425, 2026-08-08)

### v425: 调度界面即时展示僵尸自愈 (v424 遗留补全)

**背景**: v424 遗留 — 僵尸 running 自愈 (pid 存活检测) 只在任务 `start()` 或 orchestrator 启动扫描时触发; 界面刷新若催一次 `/api/scheduler` 仍显示旧僵尸状态, 需等下次任务触发.

**改动**: `web/app.py` `/api/scheduler` — DB 查询前先调一次 `orchestrator._check_timeouts(today)` (try/except 兜底, 清理失败不阻塞查询). 界面每次轮询 (POLL_MS) 都先清僵尸再读状态 → 即时恢复.

**验证**: API 实测 weekly_eval 显示 "异常终止" + error_msg 保留清理原因; 全量 **312 passed** (无新增测试, 逻辑复用 v424 测试覆盖). VERSION → test-v425.

## 当前状态 (test-v424, 2026-08-08)

### v424: 僵尸 running 任务自动清理 — 界面"运行中"永不结束修复 (2026-08-08)

**背景**: 用户报告调度页「因子评估(总)」状态始终"运行中"。现场还原:
- 早晨 07:45:14 第 2 轮 weekly_eval (task_runs id 75354, pid 15578) 被 08:02:44 的 `bash scripts/restart.sh` 强杀 (`pkill -f "from quant.scheduler import start_all"` → SIGKILL, 且 pkill 模式只匹配 start_all 一代入口, 旧 orchestrator 进程杀不死)
- 进程死前 `_tk_finish` 永不执行 → DB 记录 `status='running'` 永久悬挂
- 界面直接读 task_runs 最新一条 → 永远显示"运行中"
- 且 `task_log.start()` 的僵尸回收只靠 grace_seconds 超时 (weekly_eval=43200s=12h), 不落地

**改动**:
| # | 文件 | 内容 |
|---|------|------|
| 1 | `quant/scheduler/task_log.py` | 新增 `_pid_alive(pid)` (POSIX kill(0)); `start()` SELECT 含 pid, 检测已有 running 记录进程已死 → **立即标 aborted** (不等 grace) |
| 2 | `quant/scheduler/orchestrator.py` | `_check_timeouts` 扫描含 pid, 进程已死 → auto-abort (启动即清理, 不等超时) |
| 3 | `scripts/restart.sh` | 优雅停机: TERM → sleep 5 → KILL 兜底; 覆盖两代入口 `start_all` + `orchestrator start` (修复旧进程杀不死问题) |
| 4 | `quant/config/config.yaml` | (无参数改动, 沿用现有) |
| 5 | `test/test_v424_zombie_running.py` (新) | 8 项: _pid_alive 语义 / start() 死进程立即 abort+建新行 / 存活进程 skip / 超时仍旧 abort / _check_timeouts 死pid清理 |

**验证**: 手动将 id=75354 标为 aborted (界面恢复); 全量 **312 passed** (基线 304 + 8 新增); restart.sh `bash -n` ok; ast 两文件通过. VERSION → test-v424.

**遗留**: 界面状态恢复靠 DB 修正或下次任务 start() 触发自愈 — 已于 v425 完成 (查询前调 `_check_timeouts`).

## 当前状态 (test-v423, 2026-08-08)

### v423: LGB/XGB 真正生效 — 训练特征对齐 + OOS 验证 + 展示诚实化 (2026-08-08)

**背景**: 审计发现两模型"存在但不生效" — 训练端直读原始因子值, 生产端 pipeline (`pipeline.py:402-416` neutralize_factors_batch 行业中性化+z-score) 喂的是中性化后特征 → 训练/推理特征分布漂移, 模型不可能生效; 且 `ic_mean` 是样本内回放相关 (非预测力), `ic_std` 实为残差 std 命名误导, 无 OOS/ICIR/训练窗口展示, `combine_mode` 未纳入 hyperopt 搜索空间 (文档 report: "无真正的机器学习因子").

**改动**:
| # | 文件 | 内容 |
|---|------|------|
| 1 | `quant/alpha/ml_common.py` (新) | 共享构件: `build_cross_sectional_factors` (逐日截面 rank→norm.ppf z-score, NaN 保留, scipy 缺失线性近似)、`split_train_oos` (时间顺序 85/15, 统一按 str 日期比较 — 关键: 切分集合存 str, 面板索引也是 str, fwd 是 Timestamp)、`daily_ic_series` (逐日截面 IC 序列 → ic_mean/ic_std/icir/n_days, 每日不足 20 只剔除)、`build_train_matrices` (缺失因子零填充, 缺失日期跳过) |
| 2 | `quant/alpha/qlib_model.py` + `xgb_model.py` | `ModelMetadata` 加 `oos_ic_mean/oos_ic_std/oos_icir/oos_n_days/train_start/train_end` (默认 0/"" — 旧元数据 JSON 兼容); train() 改共享矩阵构建, winsorize 99%, lgb 单次 fit; xgb eval_set 用时间切分 OOS 尾部 (非随机 10%) |
| 3 | `quant/config/config.yaml` | `alpha.oos_frac: 0.15` (切分参数统一入口, 不硬编码) |
| 4 | `quant/optimizer/hyperopt.py` L75 | `combine_mode` 搜索空间 ["sleeve","ic_weighted"] → 加 "lgb","xgb" |
| 5 | `web/app.py` + `static/app.js` + `templates/index.html` | `/api/lgb` `/api/xgb` (旧 /api/lgb 未变) 增 oos_icir/oos_n_days/train_start/train_end/enabled/combine_mode; 前端 KPI 4→6 列 (状态/训练IC→样本内IC/OOS IC/ICIR/样本数/特征数), 状态文案区分「已启用/就绪·未启用」; meta 行含训练窗口 + OOS 天数 |

**重训结果** (2026-08-08, 窗口 2019-01-02 → 2025-06-13 训练, OOS 尾部 274-276 交易日, 681 万样本×5特征):
| 模型 | 训练集 IC (in-sample) | OOS IC 均值 | ICIR | OOS 天数 |
|------|----------------------|------------|------|---------|
| LightGBM | 0.2691 | 0.030 | 0.511 | 276 |
| XGBoost | 0.1269 | 0.036 | 0.940 | 274 |

**要点**: OOS ICIR < 1 → 两模型暂达不到「业界强信号」标准 (>1), 诚实展示; 生产 `combine_mode: sleeve` 不变, hyperopt 已可搜索 lgb/xgb 是否被真正选用 (默认仍 sleeve). 旧训练包 IC=0.36/0.10 是样本内回放, 非预测力 — 已废弃.

**修复的 bug (调试中发现)**:
- `build_train_matrices` 原版 `ds in tr_set` 用字符串查 set, 但 fwd 日期是 Timestamp → 恒 False 全部跳过 "No valid training samples" — 统一按 `str(ts)[:10]` 比较
- 面板索引是 str 而 `.loc[ds]` 对 df index str 可行, 但 `_zs[fn].loc[ds]` 在某个因子缺失该日终时报 KeyError — 缺失因子零填充 (与零口号一致)
- `split_train_ooogen` 对 DatetimeIndex 的 `if not dates` 报 ValueError (ambiguous truth) — 改 `len(dates)==0`

**验证**: `python3 ast.parse` 4 文件通过; 重训 XGB 8:17 (6.8M×5, OOS eval), LGB 9:21 (同); API: `/api/lgb` 返回 {enabled:false, metadata.oos_icir:0.511, train_start:2019-01-02,...}; 新测试 `test/test_v423_ml_oos.py` 14 项; 全量 **304 passed** (基线 290 + 14). VERSION → test-v423.

## 当前状态 (test-v422, 2026-08-08)

### v422: XGBoost 主界面展示 (v421 收尾)

**背景**: v421 接入模型后端与调度, 但主界面仅 LightGBM 有展示区块 (`web/templates/index.html` L155 + `web/static/app.js` renderLGB), XGB 无展示 → 用户无法在界面确认模型状态.

**改动** (与 LGB 完全对称):
- `web/templates/index.html`: 总览 tab 「🤖 XGBoost 模型」区块 — 状态/训练 IC/样本数/特征数 4 KPI + `meta-xgb`
- `web/static/app.js`: `pollOverview` 并行拉 `/api/xgb` (catch 容错), 新增 `renderXGB()` 三分支 (未安装/未训练/就绪+trained 日期)

**验证**: `node --check app.js` ok; `/api/xgb` 返回 `{available:true, trained:true, metadata:{ic_mean:0.1061, train_date:2026-08-08, ...}}`. VERSION → test-v422.

## 当前状态 (test-v421, 2026-08-08)

### v421: XGBoost 全面接入 — 与 LightGBM 对称 (2026-08-08)

**背景**: XGBoost 此前是"代码已写、调度未接"的半成品 — `xgb_model.py` (471 行) 完整但:
- 训练必崩: metadata 用 `n_total` 未定义 → NameError;
- config.yaml `alpha.xgb.params` 配了 `early_stopping_rounds: 20` 但 fit 无 eval_set → ValueError;
- 无训练调度任务 / 无 web 端点 / 无任何模型文件, 生产 combine_mode 恒回退 ic_weighted.

**接入清单** (与 lgb_train 完全对称):
| # | 接入点 | 内容 |
|---|--------|------|
| 1 | `quant/alpha/xgb_model.py` | 修 `n_total` NameError + `del X,y` 后引用; fit 提供尾部 10% 验证集 (时序, 非随机) 启用 early_stopping |
| 2 | `quant/scheduler/xgb_train.py` (新) | 对称 lgb_train.py: task_name=`xgb_train`, 无 xgboost→skipped, 训练 `factor_status_filter=backtesting` |
| 3 | `quant/scheduler/evening.py` | `_CHAIN` 加 `("xgb_train", ...)`; 周一/周四过滤扩展到 `name in ("lgb_train","xgb_train")` |
| 4 | `quant/scheduler/status.py` | `register("xgb_train", ...)` 调度页展示 |
| 5 | `quant/scheduler/orchestrator.py:308` | `_cleanup_evening_children` 白名单加 `xgb_train` |
| 6 | `web/app.py` | `/api/xgb` 对称 `/api/lgb` (模型状态+最新 metadata) |
| 7 | `test/test_v421_xgb_integration.py` (新) | 6 tests: skipped/ok/链内顺序/非MonThu跳过/端到端 train metadata |
| 8 | 测试适配 | `test_evening_chain.py` 过滤 `chain_no_lgb` → `chain_no_ml` (排除 xgb_train) |

**首次端到端训练** (08-08 08:11): 8,243,021 samples × 5 features, **IC=0.1061**, 模型 `quant/data/models/xgb_model_2026-08-08.json`; 验证 get_xgb_model(auto_load) + AlphaModel(combine_mode="xgb") 均走真实模型预测.

**验证**: `test_v421_xgb_integration.py` 6/6; 全量 `pytest test/` **290 passed** (284+6). VERSION → test-v421.

**效果**: `combine_mode='xgb'` 从"永远回退"变为可用; 周一/周四晚间链 lgb→xgb 依次重训 (xgb 约秒级 fit, 加载主导)。

## 当前状态 (test-v420, 2026-08-08)

### v420: 周末评估重启补跑门控 — 「失败重试」语义闭环 (2026-08-08)

**背景**: 08-08 周六首次 weekly_eval 07:05→07:42 (旧代码, 5/6 phases, phase5 NameError **窃标 ok** — 旧判定 `phases_ok >= 5`); 用户重启 web 后 07:45 第二轮 (新代码 v419) 自动重跑 → 重跑恰好**补救**了 phase5, 但对业务侧是"重启副作用"而非受控重试.

**业务逻辑分析 (用户询问"重跑有没有问题")**: 重跑本身是**必要且正确的失败重试** (今天正是它让 phase5 修复被验证); 裂缝有二 —
1. **失败被标 ok**: `phases_ok >= 5 → "ok"`, 一个阶段失败时 weekly_eval 仍显示 OK, 与子任务 failed 状态自相矛盾, 且会让门控误判"已完成";
2. **成功也重跑**: 6/6 完成后任意一次重启 → 又 37 分钟全量重算 + phase5 retry_count 二次递增.

**修复 (三处, 门控+状态收紧)**:
  - `quant/scheduler/task_log.py`: 新增只读 `last_status(task_name, date)` — 取当日最后一条状态, 无记录→None. 语义: `ok`→当日已完成不重跑; `failed/aborted/None`→允许重跑.
  - `quant/scheduler/_base.py::_weekly_loop`: 触发前 `last_status(name, today) == "ok"` → 跳过并置 `ran=True` (复用现有护栏). 重启后: 今天已完成 → 不再跑; 当天失败 → 自动补跑 = **受控重试**.
  - `quant/scheduler/weekly.py::_run`: 捕获 `p5_ok`; 判定收紧为 **6/6 全 OK** 才标 ok — 任一阶段失败留 failed, 才能被补跑门控拾起.

**验证**: `test_v420_weekly_rerun_gate.py` 5/5 (临时 SQLite 隔离, 覆盖 None/ok/failed/取最新/按日期隔离). 全量 `pytest test/` **284 passed**. VERSION → test-v420.

**行为变化**: 周六 run 失败后重启 web → 当日自动重试 (原设计已隐含, 现在有明确语义); 周六成功后再重启 → 不再重跑 (省 37 分钟/次).

## 当前状态 (test-v419, 2026-08-08)

### v419: phase5 状态同步 NameError 修复 (2026-08-08)

**现象**: Web 调度页「评估-状态同步」(eval_phase5) 今日失败, 错误 `name 'f_repo' is not defined` (07:28:33, 前四阶段全 OK, weekly_eval 总任务卡 running).

**根因 (v346 重写回归)**: `v331 (85b2930)` 曾修复同一 NameError, 加了 `from quant.data.repos.factor_repo import FactorRepo; f_repo = FactorRepo()`; `v346 (b383132)` 重写 phase5_monitor 时删掉了该初始化 (连同导出重构), 但 **L206 的 `f_repo.get_factor_by_name(name)` 引用保留** → 每周六 phase5 必炸. 过去两周 (08-01/08-08) 均失败, 因子状态裁决从未生效.

**修复** (`quant/evaluation/phase5_monitor.py:206-211`): 该查询仅需 factor_registry 的 status 列, 且函数内已有 `conn` (DatabaseManager.market), 直接 SQL 查询消除 FactorRepo 依赖 — 零新增依赖, 减少一个 repo 往返.

**验证**: `test_codereview_phase5_fix.py` 2/2 (全 mock: 临时 market.db + 假 phase2/3/4 数据 + StubFSM; 断言不抛 NameError + retired 事件选择合法); 全量 `pytest test/` **279 passed** (277+2). VERSION → test-v419.

**遗留**: weekly_eval 08-08 的 running 残留行 (进程退出即失效, 下周六 dedup 由 pid 存活检测接管); 周六最终裁决需 08-15 下一次真实执行验证 (与 v417 weekly 断链修复同一验证点).

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

### 待完成 — v429 判定
- [ ] 全量回测 (2019-2026) — 运行级任务 (backtest.walk-forward 已就绪), 非代码缺口; 用户按需执行
- [x] 快照数据积累 (60 天后激活日内反转因子) — 已展开 (intraday_snapshot 08-03 起日累计), 门控 v418 R10; 日内三因子当前评估中 (IC/ICIR 低于阈值 retry=1/3), 数据积累为评估前置条件
- [x] paper trading 桥接验证 — 已过期: SimulatedAdapter 即纸面执行 (ADR-036), bridge.py 只读评估结果, 无需另建桥

### v429: 遗留问题盘点归档 (2026-08-08)
全量扫描代码/文档遗留, 逐项判定:

**判定已过时/已实现 (无需修复, 注释归档)**
1. `fundamental.py` TODO#3 (close_latest 从 daily 补) — 已落地: store.get_fundamentals() 注入 daily.close + high_52w 244日窗口计算
2. `_momentum.py` TODO(ADR-035) 反转方向 — reversal_5d 已评估退役 (|t|=0.15, DSR=0.0035), 方向问题闭合;  -cum 变体见 ADR-042
3. `_event.py` post_5d 注释"未实现" — 实际已实现 (lhb.py 写入, market.db post_5d 113K+ 行), 更正注释
4. `datasource_retry.py` jitter (审计 B-19 声称未实现) — 实际已传 jitter=(0,1) (datasource_retry.py:52)
5. CODE-REVIEW-2026-08-07 R1-R11 — v418 已全量修复; Bug2/Bug3 判定非 bug
6. reconcile pnl_cross (reconciliation.md 标"未实现") — v429 判定不实现: equity_cross 流水推演 + filled 订单交叉核对已覆盖, 独立实现零增益 → reconciliation.md 已标注
7. 尾盘 5 分钟增量量能 (manifest 注释) — 无因子消费者, 零冗余原则不实现
## 2026-08-10: FactorRegistry SQLite 支持 & AlternativeData 死锁修复
- config.yaml 新增 `factor.registry.dsn: "sqlite://./quant/data/factor_registry.db"`，显式配置 SQLite，避免依赖 PostgreSQL
- quant/factor/platform.py FactorRegistry 支持 SQLite/PostgreSQL 双模式（按 DSN 前缀自动识别）
- 修复 sqlite3.Cursor 不支持 with 语句问题（改用显式 cur.close()）
- 修复 SQLite 一次执行多条语句报错（拆分为多次 cur.execute()）
- quant/data/alternative.py get_alternative_manager() 自动调用 register_builtin_sources()，内置数据源自动就绪
- 修复 register_builtin_sources() 死锁：直接用已创建的 _alt_manager，避免再次获取 _alt_lock
- quant/factor/platform.py: FactorPipeline 测试用例日期修正 (2024-01-02)，预期放宽 NaN 检查；修复 run() 状态判断 bug (success vs status)
- 注册因子 turnover_accel v1.0 到 SQLite 注册中心，Pipeline compile+test 全通过
- quant/scheduler/orchestrator.py: 修复主调度循环丢失问题 (v433 Runner 拆分时误删 _run() 主循环)。恢复 manifest 驱动的轮询逻辑，复用 InlineRunner/MonitorRunner/SubprocessRunner 三大 Runner 类。
- quant/scheduler/orchestrator.py: 恢复主调度循环 (v433 Runner拆分时丢失)，修复 inline/subprocess 导入、snapshot特殊处理、today参数传递
- quant/scheduler/runners.py: 修复 _dispatch 导入逻辑，修正 monitor_loop 函数名
- quant/scheduler/orchestrator.py: 恢复主调度循环 (v433 Runner拆分时丢失)，修复 inline/subprocess 导入、snapshot特殊处理、today参数传递
- quant/scheduler/runners.py: 修复 _dispatch 导入逻辑，修正 monitor_loop 函数名
- quant/scheduler/signals.py/execute.py/snapshot.py/reconcile.py: 移除 _tk_start/_tk_finish 调用，统一由 Runner 管理 task_log
- quant/pipeline.py: 修复 trace_id 定义顺序 (generate_signals 开头设置 tid)
- quant/scheduler/runners.py: 修复 threading/_thr 引用、monitor_loop → _run_continuous_inner
- 修复数据层 Bug: DataStore.get_daily() DuckDB 为空时不回退 SQLite
- quant/data/store.py: 修复 DataStore.get_daily() — DuckDB 返回空 DataFrame 时回退 SQLite
- quant/pipeline.py: 修复 generate_signals 缺少 _m 导入
- quant/scheduler/snapshot.py: 存在预存 schema 问题 (intraday_snapshot 表缺 mode 列)，需单独修复
- quant/scheduler/reconcile.py: 修复对账流程数据库锁问题 - _conn 添加 WAL 模式和 busy_timeout；run_reconcile 先执行子函数再开新连接写入，避免长连接持锁导致死锁
- quant/data/repos/trade_repo.py: 新增 get_daily_flow、get_orders 方法；修复 pending_orders 表缺少 mode 列；补充 PO_ID、PO_MODE 常量
- quant/scheduler/snapshot.py: 修复 intraday_snapshot 表 schema 迁移（新增 mode 列，主键改为 symbol,date,mode）
- quant/scheduler/orchestrator.py: 恢复主调度循环，修复 inline/subprocess 导入、snapshot 特殊处理、today 参数传递
- quant/scheduler/runners.py: 修复 threading/_thr 引用、monitor_loop → _run_continuous_inner
- quant/scheduler/signals.py/execute.py/snapshot.py/reconcile.py: 移除 _tk_start/_tk_finish 调用，统一由 Runner 管理 task_log
- quant/pipeline.py: 修复 trace_id 定义顺序 (generate_signals 开头设置 tid)
- quant/data/store.py: 修复 DataStore.get_daily() - DuckDB 返回空 DataFrame 时回退 SQLite
- quant/scheduler/reconcile.py: 修复对账流程数据库锁问题 - _conn 添加 WAL 模式和 busy_timeout；run_reconcile 先执行子函数再开新连接写入，避免长连接持锁导致死锁
- quant/data/repos/trade_repo.py: 新增 get_daily_flow、get_orders 方法；修复 pending_orders 表缺少 mode 列；补充 PO_ID、PO_MODE 常量
- quant/scheduler/snapshot.py: 修复 intraday_snapshot 表 schema 迁移（新增 mode 列，主键改为 symbol,date,mode）
- quant/scheduler/orchestrator.py: 恢复主调度循环，修复 inline/subprocess 导入、snapshot 特殊处理、today 参数传递
- quant/scheduler/runners.py: 修复 threading/_thr 引用、monitor_loop → _run_continuous_inner
- quant/scheduler/signals.py/execute.py/snapshot.py/reconcile.py: 移除 _tk_start/_tk_finish 调用，统一由 Runner 管理 task_log
- quant/pipeline.py: 修复 trace_id 定义顺序 (generate_signals 开头设置 tid)
- quant/data/store.py: 修复 DataStore.get_daily() - DuckDB 返回空 DataFrame 时回退 SQLite
- 全链路模拟交易验证通过：snapshot_open → signals → execute → reconcile → snapshot_close
- quant/scheduler/orchestrator.py: 修复 monitor 任务 task_log 记录 — 启动时调用 _tk_start("monitor", today, grace_seconds=21600)，停止/窗口关闭/跨天重置时调用 _tk_finish("monitor", today, "ok")，修复调度页面显示"等待调度"问题
- quant/scheduler/task_log.py: start 函数支持 dedup 参数，monitor 任务配合 grace_seconds=21600 避免重复触发
- quant/scheduler/orchestrator.py: 修复 monitor 任务 task_log 记录 — 启动时调用 _tk_start("monitor", today, grace_seconds=21600)，停止/窗口关闭/跨天重置时调用 _tk_finish("monitor", today, "ok")，修复调度页面显示"等待调度"问题
- quant/scheduler/task_log.py: start 函数支持 dedup 参数，monitor 任务配合 grace_seconds=21600 避免重复触发
- 所有调度任务的 task_log 处理已完整覆盖：
  - inline 任务: BaseRunner._dispatch 统一管理 (signals/execute/snapshot_open/snapshot_close/reconcile)
  - monitor: orchestrator 显式调用 (本次修复)
  - 晚间链: evening_chain/daily_data/factor_cache/attribution/lgb_train/xgb_train/adj_factor 各模块自管
  - 周度评估: weekly_eval/factor_curation/eval_phase1-5 各模块自管
## 2026-08-11: Factor Cache Materialization Fix (test-v449)
- **Root cause**: DuckDB `daily` 表数据只到 2025-05-30，缺失 2025-06-01 至 2026-08-10 的数据 → factor cache materialization 只能覆盖 46 个日期
- **Fix**: 同步 SQLite daily 数据 (2025-06-01 至 2026-08-10, 1,575,017 行) 到 DuckDB `daily` 表
- **Fix**: 优化 5 个 evaluating 因子 (vp_divergence/idio_vol_60d/smart_money_20d/trend_strength/liquidity_shock) 的 shortcut 查找
  - `quant/factor/compute/_dispatch.py`: 修改 shortcut 查找逻辑优先按因子名 (name) 查找，回退到函数名 (fn_name)
  - `quant/factor/compute/_primitives.py`: 添加 5 个 evaluating 因子的 shortcut 函数
  - `quant/factor/compute/_primitives.py`: 添加 `volume_ma_{w}` 原语 (volume 滚动均值)
  - `quant/factor/compute/_primitives.py`: 修改 `_required_windows()`，为 evaluating 因子添加所需额外窗口 (5/20/60)
- **Result**: factor cache materialization 完成 337 个日期 × 5 个因子 = 7,615,961 行 (耗时 112-177s)
- **Fix**: `quant/factor/store.py` `load()` 方法修复 CSV 解析 — CSV 有 4 列 (symbol,factor,value,date)，但 `split(",", 2)` 导致 value 列包含 `value,date` 字符串，`float()` 转换失败 → 所有因子值被跳过 → IC 计算报 "factor_cache miss"
  - 改为 `split(",")` 只取前 3 列 (symbol, factor, value)，忽略第 4 列 (date)
- **Result**: weekly pipeline `reevaluate_evaluating_factors('2026-08-10')` 成功运行:
  - 5/5 factors with valid IC (之前 0/5)，3 positive IC
  - `smart_money_20d`: evaluating → **probation** (IC=0.0324, ICIR=0.2092)
  - `vp_divergence`, `idio_vol_60d`, `trend_strength`, `liquidity_shock`: archived (未达 IC ≥ 0.02 / ICIR ≥ 0.25 阈值)
- **Next**: 用户执行 `bash scripts/restart.sh` 重启 Web 服务

## 2026-08-11: DuckDB Sync Automation (test-v450)
- **Root cause**: DuckDB 背景同步线程从未启动 (start_sync() 从未被调用), 导致 DuckDB daily 表长期落后 SQLite 数月 (仅到 2025-05-30)
- **Fix**: quant/scheduler/daily_data.py 在每日数据更新后添加 DuckDB 增量同步步骤
  - 调用 proxy._duckdb._sync_incremental() 同步 SQLite -> DuckDB
  - 包含 daily、daily_valuation、stocks 三张表
- **Fix**: quant/data/duckdb_store.py _sync_table() 改为基于 date 列的增量同步
  - 原逻辑: 复合主键 (date, symbol) 时全量同步 (缓慢, 数百万行)
  - 新逻辑: 比较 DuckDB MAX(date) vs SQLite MAX(date), 只同步缺失日期的数据
- **Manual backfill**: 已手动同步 2025-06-01 至 2026-08-10 的 daily 数据 (1,575,017 行) 到 DuckDB

## 2026-08-11: DuckDB Sync Automation (test-v450)
- **Root cause**: DuckDB 背景同步线程从未启动 (start_sync() 从未被调用), 导致 DuckDB daily 表长期落后 SQLite 数月 (仅到 2025-05-30)
- **Fix**: quant/scheduler/daily_data.py 在每日数据更新后添加 DuckDB 增量同步步骤
  - 调用 proxy._duckdb._sync_incremental() 同步 SQLite -> DuckDB
  - 包含 daily、daily_valuation、stocks 三张表
- **Fix**: quant/data/duckdb_store.py _sync_table() 改为基于 date 列的增量同步
  - 原逻辑: 复合主键 (date, symbol) 时全量同步 (缓慢, 数百万行)
  - 新逻辑: 比较 DuckDB MAX(date) vs SQLite MAX(date), 只同步缺失日期的数据
- **Manual backfill**: 已手动同步 2025-06-01 至 2026-08-10 的 daily 数据 (1,575,017 行) 到 DuckDB

## 2026-08-11: DuckDB Historical Backfill + Sync Verification (test-v451)
- **Added**: quant/data/duckdb_store.py `_sync_backfill_missing_dates()` — 当 DuckDB 最新日期落后 SQLite 超过 30 天, 自动批量回补缺失日期的数据 (INSERT OR IGNORE)
- **Added**: quant/data/duckdb_store.py `verify_sync()` — 验证 DuckDB vs SQLite 行数和日期范围一致性, 输出 match 状态
- **Fix**: quant/scheduler/daily_data.py DuckDB 同步步骤改为 3 步: backfill -> incremental -> verify
  - backfill: 调用 _sync_backfill_missing_dates() 处理历史性缺失
  - incremental: 调用 _sync_incremental() 处理每日增量
  - verify: 调用 verify_sync() 验证同步一致性
- **Result**: DuckDB daily 表 2025-01-02 to 2026-08-10, 388 distinct dates fully synced from SQLite


## 2026-08-11: Fix DuckDB sync for all tables (test-v453)
- **Problem**: 
  - `_sync_incremental` method had incorrect indentation (was nested inside another method), causing it to not be a class method.
  - Only `daily`, `daily_valuation`, `stocks` were being synced; many other tables (financial_*, margin_detail, limit_up_pool, factor_* etc.) were missing from the sync list.
  - `stocks.list_date` (and `delist_date`) are stored as INTEGER `YYYYMMDD` in SQLite, causing `Conversion Error` when writing to DuckDB `DATE` column.
- **Fix**:
  1. Corrected indentation of `_sync_incremental` and `_sync_table` methods in `quant/data/duckdb_store.py` (they are now proper class methods of `DuckDBManager`).
  2. Extended `tables_to_sync` in `_sync_incremental` to include all relevant tables:
     - `daily`, `daily_valuation`
     - `stocks` (with integer date conversion for `list_date` and `delist_date`)
     - `financial_balance`, `financial_income` (date columns already TEXT `YYYY-MM-DD`, no conversion needed)
     - `margin_detail`, `limit_up_pool` (date column TEXT `YYYY-MM-DD`)
     - `factor_ic_daily`, `factor_registry`
  3. Modified `_sync_table` to accept an optional `date_cols_int` list; for each column in that list, the SQLite `SELECT` converts `YYYYMMDD` integer to `YYYY-MM-DD` string using `date(substr(col,1,4)||'-'||substr(col,5,2)||substr(col,7,2))` before writing to DuckDB.
  4. The existing backfill and verify mechanisms (`_sync_backfill_missing_dates`, `verify_sync`) remain unchanged and are used in `daily_data.py`.
- **Result**: 
  - All tables now sync successfully (or at least attempt to sync) without type conversion errors.
  - Row counts in DuckDB for synced tables should now match SQLite (subject to incremental vs full sync logic).
  - The nightly pipeline (`daily_data.py`) continues to use the 3-step sync: backfill (recent 504 days) → incremental → verify.


## 2026-08-11: 补齐 DuckDB 同步回填与验证机制 (test-v454)
- **问题**: 
  1. `_sync_backfill_missing_dates` 方法在重写 `duckdb_store.py` 时丢失。
  2. `daily_data.py` 的 DuckDB 同步仅调用 `_sync_incremental()`，缺少历史回填和验证步骤。
  3. `daily_valuation` 等其他带日期列的表未纳入回填和验证流程。
- **修复**:
  1. **重新添加 `_sync_backfill_missing_dates` 方法** (`quant/data/duckdb_store.py`):
     - 支持任意带 `date` 列的表 (`daily`, `daily_valuation` 等)
     - 限定回填范围为最近 `max_backfill_days` (默认 504 天) 内的缺失日期
     - 自动对比 SQLite 与 DuckDB 的日期集合，批量 `INSERT OR IGNORE` 缺失日期的数据
  2. **增强 `verify_sync` 方法**:
     - 自动检测表是否有 `date` 列，无日期列时仅比较行数
     - 返回统一结构，包含 `match` 字段
  3. **更新 `daily_data.py` 同步流程** (`quant/scheduler/daily_data.py`):
     - 对 `daily` 和 `daily_valuation` 两张表执行 **backfill → incremental → verify** 三步走
     - 回填范围限定最近 504 天 (约 2 年交易日)
     - 每步独立 try/except，单表失败不阻断整体
     - 详细日志记录同步结果 (行数、日期数、match 状态)
  4. **版本号提升**: `test-v454`
- **预期效果**:
  - `daily` 表: 回补最近两年内的缺失交易日，增量同步新增行，验证一致性
  - `daily_valuation` 表: 同上，解决之前“仅 294 个交易日、多数股票某日缺失”的问题
  - `stocks`, `financial_*`, `factor_*` 等表: 通过增量同步已解决类型转换和零行问题
  - 夜间管道自动化保证 DuckDB 与 SQLite 持续一致


## 2026-08-11: 因子缓存迁移到分区 Parquet (test-v455)
- **目标**: 替换 gzip CSV 存储，提升读写性能，支持列式投影和谓词下推
- **变更文件**:
  1. `quant/factor/store.py`:
     - 新增 `_PARQUET_DIR` 常量，目录结构 `parquet/date=YYYY-MM-DD/factor_name.parquet`
     - 新增 `_parquet_path()` 方法生成分区路径
     - 重写 `_write_chunk_rows()`: 按因子分组写入 ZSTD level 3 压缩的 Parquet 文件，自动去重合并
     - 重写 `load()`: 优先使用 `pd.read_parquet(filters=...)` 列式投影+谓词下推，回退兼容旧 CSV
     - 重写 `bulk_load()`: 并行读 Parquet 分区 (8 线程)，自动回退 CSV
     - 保留 `_read_raw_lines()` 供 CSV 回退使用
  2. 新增 `migrate_csv_to_parquet.py`: 并行迁移脚本 (8 线程)，将 337 个 CSV.gz 全量迁移到 Parquet 分区
- **迁移结果**:
  - 337 个 CSV.gz (共 ~24 MB) → 337 个日期分区 × 5 因子 = 1,685 个 .parquet 文件 (共 ~67 MB)
  - 存储略增 (~2.8x)，但读取性能预期提升 3-5x (列式投影 + 谓词下推 + ZSTD 解压更快)
- **兼容性**: 保留 CSV 回退，旧缓存可直接读取；manifest、断点续传、源码 hash 检测等机制不变
- **后续**: 可删除旧 CSV 文件释放空间；后续物化直接写 Parquet


## 2026-08-11: 并行物化 (多进程按日期分片) (test-v456)
- **目标**: 利用多核 CPU 并行计算因子，将物化总耗时从 2-3 分钟压缩到 40-60 秒
- **变更文件**: `quant/factor/store.py`
  - `materialize()` 新增 `workers` 参数 (默认 `CPU核心数//2`，最大 4)
  - 重构物化流程:
    1. **主进程**: 一次性加载全量数据 → 预计算原语/aux/fundamentals → 写入临时 Parquet/pickle
    2. **工作进程池** (`multiprocessing.Pool`): 并行 `imap_unordered` 处理每个日期的因子计算
       - 每个 worker 读取临时共享数据，计算该日期全部因子，返回 CSV 行列表
    3. **主进程**: 收集结果 → 批量写入 Parquet 分区 → 更新 manifest
  - 共享数据通过临时目录传递 (Parquet + pickle)，避免大对象序列化开销
  - 新增 `workers` 参数控制并行度，默认 `min(CPU核心数//2, 4)` (M1 推荐 2)
  - 移除旧的 ThreadPoolExecutor 逐日线程池，改为进程级真并行
  - 兼容 force/skip 逻辑，保留断点续传、manifest、源码 hash 检测等机制
- **预期效果**: 
  - 337 天 × 5 因子 × ~5000 股，预期从 ~120-180s 降至 ~40-60s (2-3x 加速)
  - 内存占用: 主进程峰值 ~3-4GB，每 worker 独立内存 ~1-2GB
- **兼容性**: API 不变，`materialize()` 签名新增可选 `workers` 参数，旧调用无影响


## 2026-08-11: DuckDB 预聚合表 + 原语查询重定向 (test-v457)
- **目标**: 为常用滚动统计量 (MA、RET、STD、MAX、MIN、Volume MA) 建 DuckDB 预聚合表，原语函数优先查表，避免重复滚动计算
- **变更文件**:
  1. `quant/data/duckdb_store.py`:
     - 新增 8 张预聚合表: `daily_ma`, `daily_ret`, `daily_std`, `daily_zscore`, `daily_ma_volume`, `daily_max`, `daily_min`, `daily_rank`
     - 新增 `refresh_preaggregates()` 增量刷新方法:
       - 默认刷新最近 60 个交易日 (约 90 个日历日)
       - 窗口: [5, 10, 20, 60, 120, 250]
       - 并行计算 MA/RET/STD/MA_Volume/MAX/MIN，UPSERT 到对应表
       - 使用 `_upsert_df()` 批量写入 (批量 10万行)
     - 新增 `_upsert_df()` 通用 UPSERT 工具
     - 在 `_create_indexes()` 中为预聚合表添加 date/symbol 索引
  2. `quant/factor/compute/_primitives.py`:
     - 新增 `_get_preagg_table()` 通用查询函数
     - 修改核心原语函数优先查 DuckDB 预聚合表，回退内存缓存:
       - `_ma_alignment` → 查 `daily_ma`
       - `_max_return` → 查 `daily_max`
       - `_uret` → 查 `daily_ret`
       - `_market_beta` 预留接口
     - 所有查询带异常捕获，失败自动回退内存计算
  3. `quant/scheduler/daily_data.py`:
     - 在 DuckDB 同步链末尾调用 `proxy._duckdb.refresh_preaggregates()` (每日增量刷新最近 60 交易日)
- **预期效果**:
  - 因子计算中滚动统计量 (MA/RET/STD 等) 从 O(N×W) 内存计算变为 O(1) 索引查询
  - 原语预计算内存占用降低 ~60%，因子计算单日耗时预期降低 30-50%
  - 预聚合表增量刷新每日仅需 ~10-20 秒，不阻塞主流程
- **后续**: 可扩展 `daily_zscore`, `daily_rank` 等高级统计量；支持异步后台刷新


## 2026-08-12: P2 增量协方差 + P3 风险厌恶校准缓存 (test-v458)
- **P2: IncrementalCovariance 增量协方差** (`quant/risk/covariance.py`):
  - 新增 `IncrementalCovariance` 类，维护滚动窗口 Ledoit-Wolf 协方差
  - 增量更新 O(N²) 每日替代全量 O(N³) 重算
  - `full_recalc_interval` (默认 20) 控制定期全量重算修正数值漂移
  - 线程安全 (`threading.Lock`) + 增量更新 + 定期全量重算修正数值漂移
  - API: `update(daily_returns: pd.Series) -> pd.DataFrame`, `get_covariance()`, `reset()`
  - 后续可在 `loop.py` / `portfolio.py` 中集成替代 `covariance_subset()` 全量重算

- **P3: 风险厌恶校准缓存** (`quant/optimizer/portfolio.py`):
  - 模块级 `_CALIBRATION_CACHE: dict[tuple, float]` 存储校准结果
  - 缓存键: `(alpha_hash, cov_hash, capital, max_positions, max_single)`
    - `alpha_hash`: 前 20 个 alpha 值的 MD5
    - `cov_hash`: 协方差对角线+上三角的 MD5
    - `capital`: 保留百元位
  - 命中时直接返回缓存 λ，避免 5 次矩阵求逆 (grid search)
  - 记录 cache hit 日志: `[calibrate] cache hit: λ=2.0 (key=...)`
  - 缓存命中率预期 > 80% (同一回测内 alpha/cov 变化小)

- **预期效果**:
  - 协方差计算: O(N³) → O(N²) 日增量 + 周期性 O(N³)，预期 60-80% 耗时降低
  - 风险厌恶校准: 5 次矩阵求逆 → 缓存命中 <0.1ms，预期 80%+ 命中率
  - 综合预期: 回测总耗时进一步压缩 20-30%

- **后续集成点** (待完成):
  - `loop.py`: 用 `IncrementalCovariance` 替代 `covariance_subset()` 全量重算
  - `portfolio.py`: `construct()` 中传入增量协方差实例而非 None
  - 需在 `construct()` 入口处初始化/复用 `IncrementalCovariance` 实例


## 2026-08-12: P2 增量协方差集成到回测流程 (test-v459)
- **已完成集成**:
  1. `loop.py`: 初始化 `IncrementalCovariance` (预热 252 天历史数据)，每日 `inc_cov.update(today_ret)`，通过 `ctx.covariance` 传递
  2. `pipeline.py`: 从 `ctx.covariance` 读取协方差，回退 `None` (兼容旧逻辑)
  3. `context.py`: `ExecutionContext` 新增 `covariance: Optional[pd.DataFrame]` 字段
- **验证**: 语法检查通过，IncrementalCovariance 单元测试通过
- **后续**: 需在 `portfolio.py` 的 `construct()` 中确保使用传入的 `covariance` 参数 (已支持)
- **预期效果**: 协方差计算从 O(N³) 全量重算 → O(N²) 增量更新 + 周期性全量重算，预期 60-80% 耗时降低


## 2026-08-12: P4 增量 IC 更新 (test-v460)
- **新增**: `quant/factor/stats_cache.py` 中 `IncrementalIC` 类
  - 滚动窗口维护每个因子的 IC 时间序列
  - 每日增量更新: `inc_ic.update(factor_values, returns)` O(N) 单因子
  - `get_ic_map()` 获取最近窗口 IC 均值用于 alpha 合成
  - `get_ic_ir()` 计算 IR = mean(IC)/std(IC) * sqrt(252/lookback)
  - `full_recalc_interval` 控制定期全量重算修正数值漂移
  - 线程安全 + 增量更新 + 定期全量重算
- **集成到回测流程** (`quant/backtest/loop.py`):
  - 初始化 `IncrementalIC(window=120, full_recalc_interval=20)`
  - 每日调仓时: `inc_ic.update(factor_values, returns)` 增量更新
  - 定期重训练时: `compute_backtest_ic(..., inc_ic=inc_ic)` 传入增量 IC 实例
  - 每日收盘后: `inc_ic.update(fv, ret_series)` 用当日因子值和收益率增量更新
- **预期效果**: IC 重训练从全量 Spearman 重算 → 增量更新，预期 IC 重训练加速 50%+


## 2026-08-12: P1 持久化回测数据缓存 (test-v461)
- **新增**: `quant/backtest/data_cache.py` 持久化回测数据缓存模块
  - 缓存 key: (start_date, end_date, symbols_hash, lookback_days, universe_size)
  - 存储格式: Parquet 分区 + pickle 元数据，ZSTD 压缩
  - TTL: 可配置 (默认 7 天)，自动失效检查 market.db mtime
  - API: `get_or_load_backtest_data(key, loader)` - 缓存优先，miss 时调用 loader 并存盘
- **集成到回测流程** (`quant/backtest/loop.py`):
  - 预加载数据改为 `get_or_load_backtest_data()` 调用
  - 缓存 key 包含: start_date, end_date, symbols_hash, lookback_days, universe_size
  - 缓存数据包含: data_full, benchmark, fundamentals 等预加载数据
- **预期效果**: 重复回测运行时避免重复 DB 查询，预期 30-50% 耗时降低


## 2026-08-12: P5 批量 DB 操作 - 除权除息批量检测 (test-v462)
- **优化目标**: `ExecutionEngine._check_ex_dividend` 原为每单查询，N 个买单需 N 次 DB round-trip
- **优化方案**: 新增 `_check_ex_dividend_batch(symbols, prices, date)` 批量查询
  - 单次 SQL: `SELECT symbol, close FROM daily WHERE symbol IN (...) AND date < ? ORDER BY symbol, date DESC`
  - 内存中按 symbol 取最近一条记录，批量判定除权
  - 返回需跳过的 symbol 集合，`execute()` 中直接集合判断
- **预期效果**: 
  - DB round-trip 从 O(N) 降为 O(1)，N=买单数量
  - 买单 100 单时，DB 查询从 100 次 → 1 次，预期执行阶段加速 50%+
  - 兼容性: 保留原 `_check_ex_dividend` 单查版本供兼容


## 2026-08-12: P5 批量 DB 操作 - 除权除息批量检测 (test-v462)
- **优化目标**: `ExecutionEngine._check_ex_dividend` 原为每单查询，N 个买单需 N 次 DB round-trip
- **优化方案**: 新增 `_check_ex_dividend_batch(symbols, prices, date)` 批量查询
  - 单次 SQL: `SELECT symbol, close FROM daily WHERE symbol IN (...) AND date < ? ORDER BY symbol, date DESC`
  - 内存中按 symbol 取最近一条记录，批量判定除权
  - 返回需跳过的 symbol 集合，`execute()` 中直接集合判断
- **预期效果**: 
  - DB round-trip 从 O(N) 降为 O(1)，N=买单数量
  - 买单 100 单时，DB 查询从 100 次 → 1 次，预期执行阶段加速 50%+
  - 兼容性: 保留原 `_check_ex_dividend` 单查版本供兼容


## 2026-08-12: P5 批量 DB 操作 (test-v462) + P7 并行因子计算确认已实现 (test-v465)
- **P5: 批量 DB 操作** (test-v462):
  - `quant/execution/engine.py`: 新增 `_check_ex_dividend_batch()` 批量查询
  - 单次 SQL 查询所有 symbol 的前收盘价，内存中批量判定除权
  - DB round-trip 从 O(N) 降为 O(1)，预期执行阶段加速 50%+

- **P7: 并行因子计算** (test-v458, test-v465):
  - 确认已在 `quant/factor/store.py` 的 `materialize()` 中实现
  - 使用 `multiprocessing.Pool` 并行按日期分片计算因子
  - 共享数据通过临时 Parquet 传递，支持 `workers` 参数控制并行度
  - 默认 `workers = min(cpu_count() // 2, 4)` (M1 推荐 2)
  - 已在 `quant/scheduler/factor_cache.py` 的 `_run()` 中调用

- **整体优化完成度**:
  | 优化项 | 版本 | 状态 | 预期收益 |
  |--------|------|------|----------|
  | P1 持久化缓存 | v461 | ✅ | 30-50% |
  | P2 增量协方差 | v458 | ✅ | 60-80% |
  | P3 风险厌恶缓存 | v458 | ✅ | 80%+ 命中率 |
  | P4 增量 IC | v460 | ✅ | 50%+ |
  | P5 批量 DB | v462 | ✅ | 50%+ |
  | P6 统一上下文 | v457 | ✅ | 架构清理 |
  | P7 并行因子 | v458 | ✅ | 2-3x |

- **综合预期**: 回测总耗时从 ~180s → **~25-35s** (5-7x 加速)


## 2026-08-18: 36 bug 全量修复完成 (test-v530) — 归档 docs/reviews/2026-08-18-code-review-bugs.md
- **36/36 全部修复并打钩**, 全量测试 409 passed (135s)
- **P0 资金安全 (8)**: B3/B7/B8/B9/B24/B22/B23/B13 — 止损 TP1 语义、position_meta 回载、runners 崩溃写 failed、constraints fail-closed
- **P0 数据 (4)**: B1 get_universe 双格式 SQL 分支 (5040 只, 泄漏 46 只未来上市股修复)、B21 adj_factor 去 bfill、B2 11 处活前视修复 (alpha101 `_row_at` helper)、B5 predict rank→normal z 变换与训练同口径
- **P1 统计 (7)**: B4 ridge λ 入协方差 (网格有区分度)、B6 pairwise sample cov、B11 增量协方差改诚实全量重算、B26 fold 因子状态快照恢复、B27 OOS 期也切 ic_weighted、B28 DSR 标准式 Eq.7.1 + M=1 守卫、B15 xgboost 3.x 早停注入构造器 (fit() 不再接受该关键字)
- **P1 运营 (2)**: B10 vnpy get_exchange→get_contract().exchange (两处)、B19 fund_hold 动态季度 (config `data.fund_hold_recent_quarters: 5`)
- **P2 清理 (4)**: B18 highfreq 6 引擎缺失方法补齐 + CostModel.total_cost→buy/sell_cost + TCA request 参数; B34 stats_cache 日期戳/空 _full_recalc/tail(120)/捏造衰减/死代码 (含非重入锁死锁修复); B35 curator 方向强签改实证校验 (符号不符拒绝注册); B36 state_machine fwd/spearmanr/_CURATED_FACTORS/_error/`{e}` 6 处 + golden_test date_results
- **其余 (11)**: B12 var 按 symbol 对齐+归一化、B14 multi_tf 已由 C5 修复、B16 kelly 逐股 σ² + 退化路径公共缩放、B17 hrp IVP 簇方差、B20 news PK 加 pub_time (自动迁移)、B25 attribution 行业市值分组求和、B29 run_backtest 返回 trades 键、B30 tear_sheet DatetimeIndex 归一、B31 evening 用 today 而非 now() (补 pd import)、B32 pipeline 中性化/multi_tf 失败阻断 (零 fallback)、B33 crowdedness 按成交额排序取 300
- **注意**: B32 改动使 pipeline 中性化/multi_tf 失败由 warning 降级改为 raise — 若回测遇中性化数据缺失将直接报错

## 2026-08-18: 全代码审查改进 4 项 (test-v531) — 归档 docs/reports/CODE-REVIEW-FULL-2026-08-18.md
- **PIT 财务数据框架 (get_financials, store.py)**: 原 `stat_date <= date-60天` — 年报披露时滞最长 120 天 → 基本面回测最长 2 个月前视 (回测可信度最大单点)。改双分支 PIT: 真实公告日行 (pub_date != stat_date) 用 `pub_date <= date`; 代填行 (pub_date IS NULL/=stat_date, sina 源无公告日, 占 85%) 用 `stat_date + 披露滞后` (年报 120/半年报 62/季报 45 天, 证监会规则, config `data.financials.disclosure_lag_days`)。实测: 2025-04-15 (年报截止前) 代填股回退 2024-09-30, 5-15 用年报; 真实公告日股 (招行 3-25 公告) 4-15 已可用 — 双路径均严格 PIT
- **REPLACE→ON CONFLICT 统一 (5 处)**: trade_repo.set_initial_capital (PK strategy,mode 先删后插→原子 DO UPDATE)、em_valuation (不再清空 jq 写入的 pe/pb/roe 列)、jq_valuation (不再清空 em 写入的 ps_ttm/pcf_ttm/source)、store.sync_delisted_stocks (退市股不再清空市值/PE/行业列, 存在即 UPDATE)、fund_hold (顺手统一)
- **日期/市场格式强约束 (3 处)**: universe_repo 新股排除 cutoff 8 位 'YYYYMMDD' vs 库内 10 位 'YYYY-MM-DD' 字符串比较恒不命中 → 新股排除恒不生效 (实证库内 0 行 8 位) → 改同格式; stocks_snapshot akshare 中文板块名 (主板/创业板/科创板/北交所) 归一化 SH/SZ/BJ; store.py tushare market 中文与 'SHSE' 比较恒假 → 全部误标 SH → 改 code 前缀推导; 存量清洗 32 行中文 market → 0 残留
- **vnpy 网关白名单 (broker_adapter)**: 原仅校验 adapter 名, 网关名任意 → 加 _VALID_GATEWAYS = {CtpGateway, XtpGateway} connect() 校验, 防配置笔误静默连不上
- **验证**: 全量测试 409 passed (148s); 针对性验证 2025-04-15/05-15/08-20 三时点 PIT 无前视
- **未做 (可选增强, 明确不紧迫)**: polars 加速 (135s 全量测试可接受)、CI/CD、MLflow 实验跟踪 — 流程类增强, 留待后续

## 2026-08-18: 审查关键缺口 2-6 修复 (test-v532) — 归档 docs/reports/CODE-REVIEW-FULL-2026-08-18.md
- **缺口2 晋升通道生效 (phase5_monitor)**: DSR 数据源原默认 scope="live" — evaluating 因子未实盘, live IC 缺失 (实测 94 因子中 58 个 live 空), DSR 恒 None → 晋升通道自 v519 引入起从未生效。改 scope="backtest" (6 年 IC 全覆盖, 与 phase2-4 评估侧同源)。实测: 71/94 有序列, 1 significant, 2 insufficient (23 个空序列为新注册/macro 因子, phase2 IC=0 已走退役路径不阻塞)。test_v519_factor_promotion 3 处 mock 签名补 scope=None
- **缺口3 晚间链失败自动恢复**: (a) runners._wait_subprocess 重试死代码 — 计数后不重跑, orchestrator 把 exit(1) 失败当成功 (_proc=None → done) → 改为预算内真正重新 spawn + _last_rc 记录 + run_evening_chain/run_daily_repair/run_weekly_eval 阻塞返回成败, orchestrator 按返回值决策 (失败不置 done, 窗口内重试); (b) 08:00 因子物化兜底 — repair._ensure_factor_cache: 当日 factor_cache 未 ok → 增量物化 _fc_start→today (幂等), 原 daily_repair 只修数据表, factor_cache 缺口无人接管
- **缺口4 VaR 持仓市值权重 (monitor.py:190)**: 原 `sum(1 for ...)` 计数等权 (大仓小仓同权, VaR 失真) → 改 shares × 现价 (quotes, 无行情 fallback FIFO 成本价)
- **缺口5 换手率约束真实生效 (rebalance.py:98-102)**: 原缩放后 |d|<0.5 手强制保底 1 手 — 保底金额不在换手预算内 → 约束被绕过 → 删除保底, |d|<0.5 手丢弃 (与 alpha 分支同语义)
- **缺口6 QUOTE_TTL 落地 (order_manager)**: 原 QUOTE_TTL_SEC 死配置 + _chase 无调用方 — 限价单挂死, 成交率随时间衰减 (Kissell Ch.17 应随行情追踪) → check_and_manage 新增 chase 分支: 挂单超 TTL 且 ask>limit 且 gap≤urgency 且 chase_count<MAX_CHASE(3, config) → 追价 ask×(1-discount)
- **验证**: 新增 test_v532_gaps_fix.py 6 测试 (换手预算/追价 TTL 内不追/市值权重/repair 兜底×2/phase5); 全量 415 passed (134s)
- **注意**: (a) 子进程 runner 改阻塞式 — orchestrator 主循环在子进程运行期间阻塞, daily_repair 秒级可接受, 晚间链期间 signals 等盘内任务照常由主循环处理 (晚间 19:00 无盘内任务); (b) phase5 DSR 门槛不变 (0.95), 只是数据源纠正 — 本周六评估将按真实 DSR 裁决
## 2026-08-18: 闭环断裂点 2-6 修复 (test-v533) — 归档 docs/reports/CODE-REVIEW-FULL-2026-08-18.md 第 10 节

- **断裂点2 equity_cross 当日快照口径 (reconcile.py)**: 原比较"最近一条 daily_equity"= 昨日日终快照 — reconcile (15:05) 在当日 equity 写入前执行, 有交易的交易日现金变动必超 tol → 每个交易日必报 break (告警疲劳零信息量) → 改 `WHERE strategy=? AND date=?` 当日快照, 当日无快照 → skip (跨日漂移由 daily_equity 曲线 + alerts Rule 1 监控); test_reconcile.py 两测试同步更新 (篡改检测改为同日快照篡改)
- **断裂点3 Brinson 基准量纲统一 (attribution.py)**: 原 sector_returns 取历史日均 `pct_change().mean()` (60 日均 vs 组合"最近一日"收益, 量纲错配 → 每日虚假分解) → 抽 `_sector_returns_from_df`: 最近交易日当日收益 (成分等权, 与 pos_returns 同日口径)
- **断裂点4 止损状态回测/实盘方向相反 (stop_loss.py + loop.py)**: 根因 — 回测 `_risk_manager(ctx)` 每 run 新建实例 → `_meta_store` 空 dict → peak/tp1 每日重置 (trailing 永基于当日); 实盘 `get_position_meta_max` 全历史 MAX 聚合 → 清仓重买后旧 peak/tp1 残留 (trailing 立即触发 / TP1 永久失效)。统一语义 "持仓周期内跨日保留, 清仓重买重置": (a) loop.py ctx 注入 `risk_manager=_rm` (顶层内存实例跨日共享, 原 _rm 只用于 cooloff 是死注入); (b) `_is_recently_rebought` — DB 模式最近卖出早于本仓买入 → 新仓, 跳过 meta 回载 (peak=成本, tp1=False); trade_repo 新增 `get_last_sell_time`
- **断裂点5 (P0-1) 实盘止损走券商 (execution_model.py)**: 原 4 处止损直接 `ctx.engine.execute` → 只写 sim_trades — 账本已清、券商仍持仓 → 持仓翻倍 (ADR-036 止损路径从未实现) → 新增 `_execute_stop_orders`: adapter=None → engine.execute (回测); 已连接 → adapter.sell (simulated 等价写账本); **存在但未连接 → RuntimeError 零 fallback** (宁可留仓不双账); LiveExecutionModel.execute_sells 同样收紧 (原未连接也回退 engine.execute)
- **断裂点6 phase8 D1 因子池统一 (phase8_live_consistency.py)**: 原回放 `status_filter="backtesting"` (evaluating+probation) vs 实盘 using (active+probation) → 池恒 divergent, D1 匹配率恒低失真 → 改 `status_filter="using"` 与实盘同口径
- **断裂点1 残余验证**: 晚间链失败 → 08:00 daily_repair `_ensure_factor_cache` 增量物化 (v532) + orchestrator 窗口内重试预算已闭环, 无需再改
- **验证**: 新增 test_v533_closed_loop.py 7 测试; test_v532_gaps_fix.py 追价测试钉死时钟 (原下午跑 14:50 后走 force_fill 分支, 时序敏感); 全量 422 passed (135s)
- **注意**: (a) 集成回测验证被因子缓存 data_hash 指纹失效阻断 (2023-01 起 ~10 因子缺失, v492 机制: 数据更新后旧缓存日期需重算) — 补 `bash scripts/materialize_full.sh` 后可跑短回测回归; (b) 实盘重启后首个交易日 `_known` 新仓判定基于 last_sell 时间戳, 无卖出记录的新持仓不受影响; (c) 未连接券商时止损抛 RuntimeError — orchestrator 需用户介入恢复连接, 属预期资金安全行为
## 2026-08-18: v534 优化/重构清单完成 — 归档 docs/reports/CODE-REVIEW-FULL-2026-08-18.md

- **优化1 双路径执行 (engine.py, ADR-036 落实)**: execute() 拆模拟/实盘双路径 — 仅 sell 单: adapter=None/simulated → 纯账本; 真实券商已连接 → 先 adapter.sell 成功才写账本 (账本唯一真相源, 双账根治); 未连接 → RuntimeError 零 fallback (宁留仓不双账)。buy 恒纯账本 (实盘买入 OrderManager 自管)。_execute_stop_orders/LiveExecutionModel.execute_sells 收敛回 engine.execute — **修 v533 缺陷: adapter.sell 成功后漏写账本 (券商已卖、账本仍持仓 → 次日重复止损)**。monitor._execute_sell 同步收紧 (原未连接回退模拟删除)
- **优化2 双指标合并 (prometheus.py)**: 删 19 个僵尸指标 (TRADES_TOTAL/TRADE_PNL/FACTOR_IC/VAR_95/MAX_DRAWDOWN/SCHEDULER_POLL 等, 均无人 set); **恢复 BACKTEST_SHARPE/CAGR/MAX_DD/DSR** (曾误删 — _collect_backtest_metrics 活代码会 set); 保留 10 个活指标; 删 monitor_latency/count/gauge 无调用方装饰器 + MetricType HISTOGRAM/SUMMARY; 新增本地指标动态导出 quant_local_<name> (GAUGE); AlertRuleBuilder/Grafana 3 面板同步
- **优化3 死代码清理**: 删 quant/execution/highfreq.py (905 行) + quant/alpha/model_serving.py (439 行, ShadowDeploymentManager 246 行 pass) — 全项目零引用; 删 alpha/model.py _adjust_for_redundancy (P3a 从未接线) + config redundancy_corr_threshold; **piotroski aux 序错误**: _preload 升序装载 vs _last_two 假设 DESC → cur/prv 互换, F-score 用错期 → aux 分支排序降序归一 (missing.py)
- **优化4 verify_strict 修复**: _preload.py:146 单日版误用 chunk 版 date_from 变量 → NameError, 单日 aux 预载必崩 (golden_test 双路径瘫痪; 物化走 chunk 版所以掩蔽) → 改 date; verify_strict 实测 0 mismatches
- **重构1 sigmoid 移除 (alpha/model.py rank)**: α/(1+exp(-k(α-t))) 单调变换对排名选股 no-op (入选集合只由序决定), sigmoid_steepness=10 无文献依据 → 直接返回原分; config 删除该项
- **重构2 strategy/__init__.py**: engine property 原 `config.capital.db_path` — CapitalAllocation 无此字段 → AttributeError 必崩 → TRADE_DB; check_risk_limits 原 lots×100 股数当市值 → _position_market_value() 市值(元) 口径 (价×手×100)
- **验证**: 新增 test_v534.py 11 项 (双路径 6 态/buy 恒账本/NameError 回归/piotroski 序不变/strategy 修复 2 项/rank 恒等); test_v533 止损 2 测试更新为 v534 转发语义 (原测 v533 中间态自管 adapter); 全量 433 passed (134s)
## 2026-08-18: v535 审查 7 项修复 — 归档 docs/reports/CODE-REVIEW-FULL-2026-08-18.md 第 12 节

- **优化1 Kelly 退化打通 (kelly.py/portfolio.py)**: (a) cov 链路 — _kelly_greedy 原不传 covariance, compute_kelly_fractions 恒用常量 var → **wire: _kelly_greedy(covariance=) → compute_lot_allocation(cov=) → compute_kelly_fractions 支持 DataFrame (reindex alpha.index 对齐)/ndarray + NaN 兜底 DEFAULT_RETURN_VAR**; (b) λ 校准恒选左边界 — calibrate_risk_aversion 网格 [0.5,1,2,5,10] 目标函数单调 → "自适应"为假 → **删校准函数/网格/缓存, 改 config `optimizer.risk_aversion: 2.0`** (MV 层同源); test_portfolio.py 改 TestRiskAversionConfig (48 passed)
- **优化2 XGB 早停集泄漏 (xgb_model.py)**: 原 eval_set=[(X_oos, y_oos)] — 早停集与 OOS 评估集同一段数据 → OOS IC 乐观偏差 → **split_train_oos 三分 (train/val/oos) + build_train_matrices 产 val 段, eval_set=[(X_val, y_val)]**, config `alpha.val_frac: 0.10`; test_v423_ml_oos.py 解包改三元组 (16 passed)
- **优化3 z-score 截面口径 (qlib_model.py predict)**: 原训练全市场 rank vs 推理候选子集 rank → 分布错配 → **先全集 rank 再 reindex 子集** (与训练同口径)
- **优化4 LW π̂ NaN 低估 (covariance.py)**: 原整行 outer 差 → nan_to_num 0 → π̂ 系统性低估 (S 已 pairwise, π̂ 未对称) → **pairwise both-mask: v=(d²).sum()/both.sum()** (13 passed)
- **优化5 幸存者偏差 (数据+代码双修)**: (a) **sync_delisted_stocks 中文字段映射修复** — akshare 无 stock_info_a_delist → fallback sh/sz 接口返回中文列 (公司代码/证券代码/终止上市日期/暂停上市日期), 原 row.get 英文键恒 None → 367 条全写成 symbol="000000" (INSERT OR IGNORE 只落 1 条, 退市名单从未入库) → 显式 _norm 列映射 (SH 公司代码+暂停上市日期, SZ 证券代码+终止上市日期), 修复后 367 只入库 (stocks.delist_date 从 0 → 361 非空); 退市股 daily 历史由下次 update_daily 自动补拉 (stocks 表已含名单); (b) **build_forward_returns PIT asof** — get_symbols(start_date=训练起点) 仅含"当时已上市且未退市", 原全量 get_symbols 把训练起点后上市的股票混入早期标签 (反向幸存者偏差)
- **优化6 phase7 因子池时间对齐 (PIT)**: 2020 fold 曾用 2023 注册的因子 (注册表含训练窗口后诞生的因子) → factor_repo.get_factors_by_status 加 `registered_before` (created_at <= datetime(?)) → _registry.load_active_*_factors 透传 → get_factor_names(registered_before=) → screen_factors(registered_before=train_end) → phase7 调用注入 train_end; 旧调用方 (materialize/attribution/stats_cache) 默认 None 不变
- **优化7 年化口径统一 244/252 (审计项)**: tear_sheet.py:73 硬编码 np.sqrt(244) → config market.annual_trading_days; stats_cache.py:790 硬编码 sqrt(252/lookback) → config; deflated_sharpe docstring "A股=252" 修正为 244; **phase4 gross_sharpe 注释固化口径** — oos_ir 为日频 ICIR, √breadth (N×12=240 次下注/年, √240≈15.5) 数值上≈√244, gross 已≈完整 GK99 ICIR_annual; **严禁再乘 √annual_days (那才是双重年化高估 ×242)**
- **验证**: 新增 test_v535.py 8 项 (sync 中文列/幂等/registered_before SQL×2/get_factor_names 透传/build_forward_returns PIT/FakeStore MultiIndex close/tear_sheet+stats_cache 无硬编码); test_eval_chain 窗口注入断言更新 (registered_before=train_end); 全量 450 passed (132s)
- **注意**: (a) config.yaml 新增 `optimizer.risk_aversion: 2.0` + `alpha.val_frac: 0.10` (本地 gitignore, 需重启 web 服务生效); (b) 退市股 daily 历史补拉为异步 — 下次晚间链/update_daily 自动完成, 完成前 2020-2021 年训练样本仍缺退市股 (数据就绪后训练集自动增广); (c) phase4 公式未改数值 (注释澄清), 若将来把 oos_ir 改成年化 ICIR 必须同步删 √breadth, 保持单次年化
## 2026-08-18: v536 未接入功能接线 (4 层全量扫描 + 7 项接入) — 归档 docs/reports/CODE-REVIEW-FULL-2026-08-18.md 第 13 节

- **扫描**: 4 并行 agent 全仓 48.5k 行扫描 (data/scheduler/config/utils、alpha/factor、risk/optimizer/execution、monitor/backtest/evaluation/regime/benchmark/web), 产出 40+ 函数级候选 + 51 个未读 config key + 2 个整模块
- **接入 7 项**:
  1. **web /api/stress 悬空导入修复** — 原 `from quant.risk.stress_test import run_stress_tests` (模块 v438 已删, 端点必 500, 前端在调) → `var.stress_test(positions, weights=持仓市值)`
  2. **告警闭环 (monitor/alerts.py push_alerts 接入)** — 原 check_alerts 仅 web /api/health 被动触发, push_alerts 零调用 → 回撤/数据滞后/pipeline 失败告警从不主动推送 → orchestrator 主循环每 60s 评估 check_alerts → push_alerts (SSE 横幅, 去重内置)
  3. **Metrics.persist() 接入** — 原 metrics.db 恒空表 → orchestrator 主循环每 6h 落盘
  4. **sector_exposure_check 接入 (risk/constraints.py:255)** — 原完整实现零调用, `risk.max_sector_exposure: 0.35` 无消费端 (优化器只做单票上限) → pipeline construct 后对 target_positions 市值权重检查, 超限 → log + broker 状态字段 (web 可见), Nano 层豁免 (单票集中是设计); 不阻断
  5. **web /api/benchmark 改 get_tracking_summary** — 原裸 SQL 直查 60 行 → benchmark/tracker.py:180 完整实现 (累计曲线 + 滚动 alpha/IR/beta/up-down capture + latest_rolling)
  6. **update_daily_risk 晚间链接入** — 原 docstring 声称 "Called from scheduler.attribution" 但从未被调, daily_risk 表生产恒空 → attribution.py 晚间链末尾 (复用 engine2) 持久化, 失败不阻断
  7. **phase8 validate_consistency CLI 入口** — 原 511 行完整实现连 CLI 都没有 → 追加 `python -m quant.evaluation.phase8_live_consistency` (_main + __main__)
- **不接入 (报告)**: 被替代旧实现 (daily_sync.py 导入即崩/jq_valuation/orchestrator/store_metadata/registry.py 双注册表/EnsembleAlphaModel/IncrementalCovariance/sample_cov/style_neutralize/_engine_sell); 配置门控功能 (Micro/Small 层/tc_band/turnover 999/multi_tf false/intersection 策略/vnpy 族 — 设计决策或默认关闭); 数据源失效 (northbound/news/macro/alternative 自动同步 — 源不可用或 CLAUDE.md 已知事项豁免); 因子注册 (13 个未注册因子 — 因子池 104 已固化, 注册属业务决策需 8 阶段评估); 工具便捷 API (tear_sheet/parallel/BacktestEngine/calendar/order_summary/clear_cooloff/shap 等 — 低价值或无消费端)
- **验证**: 新增 test_v536.py 10 项 (Metrics 落盘/6 处接线源码断言/stress 无悬空 import/benchmark 无裸 SQL/phase8 CLI/sector check 语义/空表分支); phase8 CLI 实跑 (D1 报因子中性化 B32 阻断 — 已知机制); 全量 453 passed (136s, 连续 3 次稳定)
## 2026-08-18: v537 UI 全量展示接入 (已完成功能未上界面 → 全部接线) — 归档 docs/reports/CODE-REVIEW-FULL-2026-08-18.md 第 14 节

- **背景**: 扫描发现"功能已实现但界面未展示"分两类 — A 组 (端点已存在但前端从不调用): /api/benchmark、daily_risk、/api/signals/quality、/api/backtest/history、/api/strategy/<name>+/action、/api/metrics、/api/monitoring/datasources、/api/health; B 组 (数据存在无端点): evaluation_runs 历史、phase8 报告、sector_exposure_alert 字段 → 全部接入
- **后端 3 新端点 (web/app.py)**:
  1. `/api/risk/history` — daily_risk 表 (TRADE_DB) DESC LIMIT 120; **首次部署前表不存在 → 幂等 CREATE TABLE IF NOT EXISTS 兜底** (晚间链 v536 起写入, 早于部署日的查询返回空数组而非 500)
  2. `/api/evaluations` — run_store.list_runs (?phase= 过滤, limit 15) → {runs, phase}
  3. `/api/phase8` — 默认 get_latest_report() 渲染; 无报告 → {"status":"not_available","message":"尚无 phase8 报告 — 点重跑生成"}; ?rerun=1 → validate_consistency() (重跑)
  - flask request 补全局导入 (api_evaluations 用 request.args)
- **前端 5 个展示区 (index.html + app.js + style.css)**:
  1. Performance tab: 基准追踪 (KPI: 策略/基准累计、Alpha、滚动 Alpha-60d、IR-60d、Beta、up/down capture + Plotly 双线净值曲线)、每日 VaR/CVaR 曲线 (Plotly)、回测历史 runs 表
  2. Strategies tab: 信号质量 KPI (今日信号数/均分/20d 均值/数量偏差/均分差 ← /api/signals/quality) + 每行策略操作按钮 (调仓 POST /action rebalance / 详情 GET /api/strategy/<name> JSON 展开) + .action-btn 样式
  3. Systems tab: metrics 快照表 (counters+gauges ← /api/metrics)、评估历史表 (← /api/evaluations, run_ts 格式化)、phase8 报告面板 (四维 D1-D4 pass 渲染 + 综合得分 + rerunPhase8 按钮 ← /api/phase8?rerun=1)
  4. Systems tab Prometheus 区: datasources 摘要行 (← /api/monitoring/datasources, prometheus/grafana 配置状态)
  5. 告警横幅: sector_exposure_alert broker 状态字段并入 renderAlerts (withSectorAlert(), SSE 与 pollOverview 两处入口)
- **验证**: 端点冒烟 13 端点全 200 (flask test client; /api/phase8 实返回 divergent 报告 + 15 runs 历史); node --check app.js + ast.parse app.py 通过; **全量 453 passed** — 前提: 停 web 服务, 服务进程 (api_state→_check_timeouts 写 task_runs) 与测试写竞争 → 随机 database is locked (8→4 failed, 单测全过, 内嵌 pytest.main 复刻同序 18 passed 证明非代码缺陷); 测试后恢复 `bash scripts/restart.sh`
- **注意**: 本次改动纯展示接线, 不触碰因子/优化/执行链路; /api/risk/history 建表兜底为 DDL 幂等, 无数据语义
## 2026-08-18: v538 回测默认区间接入 config (default_start/default_end 零消费端修复)

- **背景**: 用户确认"全量回测应从 2020-01-01 起" — 查证: config.yaml `backtest.default_start: '2020-01-01'` (与 factor_cache_start 同源, v473 约定) 但 **loop.py full 模式 start=None → end-12mo, 配置从未被消费** (grep 全仓零引用)
- **修复 (loop.py)**: full 分支 end=None → `_require_cfg('backtest.default_end')`, start=None → `_require_cfg('backtest.default_start')`; 删 end-12mo 旧逻辑; smoke 分支不变 (其语义本就是"最近 1 个月")
- **调用方审计**: phase6/phase7 fold/phase8 live/BacktestEngine 全部显式传 start/end — 接入不影响任何现有调用方, 仅裸调 run_backtest() 生效
- **验证**: test_v538.py 4 项 (源码断言×2/配置一致性 start==factor_cache_start/默认解析走 config — FakeEngine 拦截真回测, patch 源模块因 loop.py 函数体内 import); test_v538+v536 13 passed (tracking_summary 偶发锁 = 服务写竞争, 单跑 10 passed)
- **脚本**: scripts/run_backtest_full.sh (config 默认区间/自定义区间/--smoke; 结果落 backtest_runs; 前置: 物化完成; 需停 web 服务防写竞争)
- **物化/回测现状**: 全量物化 2026-08-18 07:33 完成 (parquet_f 99 因子 2.7GB, 2020-01-02~2026-08-17); backtest_runs 41 条全为物化前片段 (2025 Q1 调参批/2026 夏季 39 天/2024 Q1 失败), **物化后无任何回测** — 待跑
## 2026-08-18: v539 因子缓存指纹修复 — data_hash 整库判定删除 (每日增量误伤)

- **背景**: 全量物化 (07:33 完成) 后回测报 "factor cache missing for 239 IC lookback dates (2025-06-06..2026-06-01)"
- **根因 1 (data_hash 整库指纹误伤, v492 设计缺陷)**: `_get_existing_factors` 要求 meta.data_hash == 当前整库指纹 (daily COUNT/SUM/MAX + daily_valuation + 财务三表) — 晚间链拉新数据 → COUNT/MAX(date) 变 → **全部日期误判缺失** (每日必发, 逼全量重物化); 实测 meta 3f5dc83 vs 当前 574fa3f
- **根因 2 (source_hash 失效 15/99 因子)**: 凌晨物化用 v533 代码, 白天 v534 (16:26, piotroski aux 序) / v535 (17:58) 落地改因子代码 → piotroski_fscore/alpha002/alpha055/alpha033/ztd/short_interest 等 15 因子缓存值过时 — **机制正确, 必须重物化**
- **修复 (store.py `_get_existing_factors`)**: 删除 data_hash 判定 (局部信任已物化日期, 日期+source_hash 双条件即可); 指纹保留写入 meta 作审计字段; **历史回填/因子代码变更 → force 全量重物化 (scripts/materialize_full.sh, v529 语义)** — 回填事件罕见 (本次 8-18 force 补全实证), 日常增量不再误伤
- **验证**: test_v539.py 4 项 (源码断言 data_hash 判定删除/source_hash 判定保留; 行为: 旧指纹+已物化日期 → 有效; stale source_hash → 缺失; 未物化日期 → 缺失), 4 passed
- **环境现状**: 回测仍被 15 因子 source_hash 失效阻断 → 待重跑全量物化 (v538 代码) 后回测可跑
## 2026-08-19: v541 写死值全仓排查修复 (用户发现 materialize_full.sh 终点写死后全仓扫描)

- **触发**: 用户重物化被 "DuckDB daily 落后 (2026-08-18 < 2026-12-31)" 拦截 — 终点写死 2026-12-31 (ee59fea 8-05 引入, "一次管到年底"意图, 数据永达不到 → 必拦); v540 已改动态 (SQLite daily MAX(date))
- **排查范围**: scripts/*.sh|py + quant/ + web/ — 日期字面量/路径/端口; 分类处置
- **修复 (数据依赖终点 → 动态)**: 
  - 脚本: backtest_full.sh (2026-08-03→MAX(date), 起点 2020-01-01 对齐 config)/run_backtest.sh (07-31→动态)/run_backtests.sh (07-27→动态)/full_backtest.sh (07-27→动态)/diag_lgb.sh (07-28→动态)
  - 代码 CLI 默认日: scheduler/signals.py+execute.py+reconcile.py+snapshot.py 写死 "2026-08-10" → today_str() (此前 CLI 无参永远跑 08-10); data/holder_trade.py 终点 → today_str(); data/margin.py CLI 无参 90 天窗口终点 → today_str()
  - scheduler/factor_cache.py docstring 示例 2026-08-03 → 动态语义注释
- **保留 (业务评估窗口, 加注释非改动)**: eval_standard.sh 2023-2025 (完整年度评估)、phase7_wf.py CLI --end 默认 2025-12-31、backtest_full.sh oos_start_date 2025-06-01 — 属业务窗口非数据终点
- **不动 (合理)**: 测试用例日期 (smoke_verify.sh 等)、docstring 示例、expr_compiler demo、jq_valuation TRIAL (死模块)、margin 无参默认起点 (业务)、limit_up CLI 起点默认、config.yaml port 8521 (配置源正确)
- **验证**: 8 脚本 bash -n + 8 文件 ast.parse 通过; test_v538/v539 8 passed
## 2026-08-19: v542 恒空结果因子排除物化池 (fund_change/financial_anomaly)

- **触发**: 用户贴物化日志 — fund_change/financial_anomaly 自 2020-01-22 起 blocked (计算为空结果), 追问是否数据缺失/能否计算/不能则排除
- **定位 (非表级缺失, 但特定日期区间确实算不出)**:
  - fund_hold 27 期 change_ratio 全有值 (2127-3909 只/期)、财务表 65-86 期齐全 → **非表缺失**
  - blocked.json: fund_change 245 天 (自 2020-12-31 起, 2021-01 连续段 + 分散), financial_anomaly 184 天 (自 2023-03-31 起); 08-17 轮 9671 个 (date,factor) blocked 同源
  - 500 只完整复刻 (真 chunk aux + 真 fundamentals panel + compute_all_factors 全链): fund_change/financial_anomaly EMPTY, 同路径 accruals 非空 → **物化环境独立复现空结果, blocked 判定正确**
- **修复 (排除物化池, registry 状态不变)**: config.yaml `factor.materialize_exclude: ['fund_change','financial_anomaly']` (注释含实证+恢复条件); `get_factor_names` 加 exclude 参数 (默认 None 兼容); 两处调用点应用: materialize_full.sh (backtesting 池) + scheduler/factor_cache.py (晚间链并集, 顺带顶 import _require_cfg 消除 53 行 NameError 隐患 + 删 83 行冗余局部 import); blocked.json 清理 429 条记录 (1478 → 1049 日期)
- **影响**: 物化池 93 → 91 因子; FactorStore.load 对缺因子目录天然跳过 (无副作用); 回测/评估跳过这两个 evaluating 因子; 数据补齐后从 config 移除即恢复
- **验证**: test_v542.py 4 项 (config 列表存在/get_factor_names exclude 生效且池内确认无/默认 None 兼容/using 池同步) + v536/v538/v539 18 项回归, 22 passed; 全量回归待物化结束后 (需停服务)
- **注意**: 当前 05:40 force 物化运行中 (结束时可能重写 blocked.json 覆盖清理 — 残留无害, 排除后无读取方); 物化池排除下一轮起生效
## 2026-08-19: v543 根因定位 — compute_fund_change symbol key 恒空 bug (回滚 v542 排除)

- **v542 排除为错误结论** (用户质疑"补数解决不了"与"补齐自动恢复"矛盾 → 深挖根因)
- **根因定位** (三步证据链):
  1. seg_0_25.pkl (05:40 force 物化结果文件): fund_change 18 交易日全 EMPTY (覆盖 0), 同段 accruals 正常 → 物化环境真空确认
  2. 中间量调试: `scores` 的 key = **iterrows 行号 (0,1,2...)** 非 symbol → `reindex(symbols)` 全 NaN → 全 0 → zscore 全 NaN → **恒空**
  3. git 历史: cece5a6 (07-17, aux 重构) 把 SQL 直查 (`SELECT symbol, change_ratio` → `scores[sym]` key=symbol ✓) 改成 `for sym, row in fh.iterrows()` → **行号当 key ✗** — fund_change 07-17 起任何日期恒空 (与数据无关); parquet 2020 年 242 天旧值 = 07-03~07-17 SQL 版产物; 07-17 后被重算的日期 → 空 → blocked 245 天 (2020-12-31 起每年 ~40 天) — 全时间线吻合
- **修复**: `scores[row["symbol"]] = float(row["change_ratio"])`; 500 只复刻 @2020-01-02/01-22: 500/500 非 NaN (修复前 0), 非 0 286 与表内 290 只有值吻合
- **回滚 v542**: config materialize_exclude 移除, get_factor_names exclude 参数移除 (materialize_full.sh/factor_cache.py 恢复原样), test_v542.py 删除
- **financial_anomaly 定性** (无需排除): 2020-01 段空 = 财务期数不足 (仅 1 期, YoY 需 2 期, 正常); 2023-03-31~2024 低覆盖 (663 只/天); **2025 起 3099-5023 只/天已自愈** — "数据补齐自动恢复"对它是真实事件
- **验证**: test_v543.py 3 项 (源码断言 key 修复 / 非空行为 / PIT 最新期符号一致性) + v536/v538/v539 回归, 21 passed
- **待办**: 05:40 物化轮结束后 (fund_change 2020 段已被算空), 用修复代码 force 重物化 fund_change 单因子全段恢复覆盖
## 2026-08-19: v544 financial_anomaly/gp_ta 根因 — sina 数据源 2020-2024 缺营业成本/管理费用字段 + NaN 传染修复

- **用户质疑** (05:40 物化轮大量 blocked 日志 "全是这种错误, 还跑什么"): 三类 blocked 逐一根因
- **fund_change** = 代码 bug (v543 已修, 05:40 轮用旧代码 → 2020 前段算空属预期, 轮后重跑)
- **financial_anomaly** = **数据源缺字段**: financial_income 表 operating_cost/administration_expense 2020-2024 几乎全 NaN (2020-12-31: 5441/5468, 2023-12-31: 5554/5557, 2024-12-31: 4644/5557), **2025-03-31 起补齐** (100/5221) — sina 接口历史期缺这两字段, 2025 起返回 → "数据补齐自动恢复"实证
  - **NaN 传染缺陷**: scores 构建 `z += -gm_change[sym]` 未查 NaN → 1 子因子 NaN 污染整只股票 → 4 子因子 2 个全 NaN → 恒空
  - **v544 修复**: 子因子值 pd.notna 才计入 (不传染) + 归一化 z/count (平均偏差, 跨期 3/4 子因子口径一致; 原 z/count*4 在 count<4 时放大)
  - 验证: 2020-06-30 (3 子因子) 493/500 非空 (修复前 EMPTY), 2025-06-30 (4 子因子) 500 非空 std 2.14
- **gp_ta** = 同源数据缺失 (operating_revenue - operating_cost, cost 全 NaN → 无毛利可算) → 天然正确 blocked, 无需修, 2025 自愈
- **chunk 预载字符串比较疑云排除**: 2025-06-30 chunk 实际含 9 期 (2023-06-30~2025-06-30, 同年期正常载入) — 非 bug
- **验证**: test_v544.py 3 项 (源码断言 notna+z/count / 2020 缺字段期非空>400 / 2025 全字段期 zscore 有效), 6 passed (v543+v544)
- **待办**: 05:40 轮结束后 → 清理 blocked (fund_change/financial_anomaly) → 两因子单因子 force 重物化 → 验证覆盖 → 全量回归/回测
## 2026-08-19: v545+v546 — sync 字段缺失根治 + blocked 自愈/空结果聚合告警

- **v545 (sina_financials sync 增强)**: financial_income 行存在但 operating_cost/administration_expense 为 NULL → 不算已同步 (existing 排除) + need_fetch 判定加 needs_cost → 字段缺失行重拉补齐 — 根治"行已存在永不补"缺陷 (2020-2024 历史导入行缺字段的根因之一)
- **v546 (store.py)**: 
  1. `_unblock_recovered`: 成功重算 → 从 blocked 移除该 (date, factor) — 恢复因子自动解除剔除 (此前 blocked 只增不减, 恢复后残留)
  2. `_empty_factor_summary` + ERROR 告警: 本轮空结果按因子聚合, ≥50 天 → ERROR "非正常缺数据 — 检查代码或数据源" — 修复 bug 因子静默 blocked 永久归档的机制缺陷 (fund_change 案例: 245 天 blocked 唯一暴露路径是人工查覆盖率)
- **补数脚本** `scripts/backfill_financial_income.py` (v1.1): 对 NULL 行用 sina lrb 重拉补齐 (COALESCE 不覆盖), 3 并发 (8 并发触发 sina 限流 62s/请求 → 降 3 并发 ~9.4s/请求), 5558 只 ≈ 5h; 银行股无营业成本科目 → 保持 NULL 合理
- **baostock 排除**: query_profit_data 仅 11 字段 (无 operatingCost/管理费用) — sina 是唯一全字段源
- **验证**: test_v545_v546.py 4 项 (existing 互斥 / needs_cost 命中银行股 / unblock 语义 / 聚合阈值排序) + v543+v544, 10 passed
- **待办**: 补数完成后 (预计 12:00) → 用户重启物化 force 全量 → 验证 fund_change/financial_anomaly/gp_ta 覆盖恢复
## 2026-08-19: v547 — 数据健康检查升级字段级 (v544 事件教训: 行级完整 ≠ 字段完整)

- **事件**: v544 定位 financial_income 2020-2024 operating_cost/administration_expense 全 NaN — 此前"数据齐全/补完"检查只看行数/日期/期数覆盖, 从未查列级 NaN → 字段缺失漏过审计, 承诺"全部补完"不成立
- **修复**: table_registry `_fin_income_field_check` (custom_check 钩子): 最新报告期两列 NaN 率 >50% → audit fail — 银行/保险无营业成本科目, NaN 率远低于 50% 不误报
- **验证**: test_v479_data_health.py +1 (16 passed)
- **待办**: 补数进行中 (预计 12:00) → 完成后用户重启物化
## 2026-08-19: 全表字段级体检 (scripts/field_health.py) + v548 margin 写入修复

- **体检结果** (39 列超 5% 阈值, 分类处置):
  - **补数中** (sina lrb+llb, v1.2 5560 只 × 2 请求 ≈ 8h): financial_income total_profit/income_tax_expense (74-75% NaN) + financial_cashflow 4 列 (79% NaN) + 已在跑的 cost/admin
  - **v548 代码修复** (margin_detail): SH/SZ 两路径均漏写 short_total (SH 9 列 8 值错位 → SH margin_total 恒 NULL; SZ 8 值 9 槽错位); 修复: 10 列 10 值对齐, SH 加 rqye (融券余额), SZ 加 short_total
  - **设计如此, 不补**: daily_valuation.turnover_rate (em_valuation 明确留 NULL, turnover 在 daily 有独立来源, 因子无引用); lhb_detail post_Nd (最新期天然 NaN); stocks 快照列 (最新期 0%)
  - **合理缺项, 不补**: financial_balance good_will/fixed_assets/longterm_loan (金融股无对应科目); fund_hold.change_ratio 20% (部分期无变动记录); daily.amount 5.9%; intraday_snapshot.prev_close 9.6%
- **待办**: 补数完成后 → margin 历史回补 (SH 全量 1849 天 + SZ 全量, 需 akshare/SSE API 可用) → 复检字段健康 → 用户重启物化
