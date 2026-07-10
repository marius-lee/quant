# HANDOFF — 盈迹 (quant) 项目当前状态

**最后更新**: 2026-07-10 08:30 CST

> 旧版归档: docs/HANDOFF-2026-07-02.md / docs/HANDOFF-2026-07-03.md (已 superseded)
> 项目根只有一个 HANDOFF.md 作为单一真相源

---

## 最近提交 (2026-07-09 ~ 2026-07-10)

| 提交 | 内容 |
|------|------|
| `acab523` | fix: 全局硬编码清零 — 21处API延迟/SQLite超时/Redis/SSE全部迁入config.yaml |
| `9e093d2` | fix: execution/quote.py 缩进 + ProcessPoolExecutor→ThreadPoolExecutor 归档 |
| `ca9f9a5` | refactor: P78 ProcessPoolExecutor→ThreadPoolExecutor — 根除多进程内存泄漏 (stats_cache.py -239行) |
| `878fba1` | fix: P77#10 根除多进程内存泄漏 — 显式terminate/kill替代pgrep (stats_cache.py + web/app.py) |
| `5c38dc3` | docs: HANDOFF 更新止盈止损 |
| `eb7bee5` | feat: 止盈止损统一管理 — 移至 monitor.py 盘中风控 (P75#4) |
| `b219a39` | docs: HANDOFF P75 标记完成 |
| `f236fd8` | refactor: 架构对标业界标准 — 四段式调度 + 盘中风控 + IC衰减 (P75) |
| `d49de9b` | docs: HANDOFF 更新 P74 |
| `a0fca84` | feat: 前端调度状态tab页 — 展示调度任务运行状态 (P74) |
| `1096651` | refactor: 调度器拆分为独立模块 signals/execute/attribution (+monitor) (P74) |
| `67b3f67` | fix: state_broker 双重修复 |
| `749dcc0` | fix: post_state positions→n_positions |
| `8fdcaa4` | feat: Alpha 候选池 6 列 |
| `6728aff` | feat: target_positions 增加 score + reason 字段 |
| `8a91932` | fix: compute_str/compute_abn_turnover 加回最小样本门槛 (P73) |
| `54eae57` | perf: compute_str + compute_abn_turnover 向量化 4021s→88.6s (P73) |
| `5bf9db7` | docs: HANDOFF 更新 P73 性能优化 |

---

## 核心架构 (对标 Grinold & Kahn)

```
┌──────────────────────────────────────────────────────────┐
│  研究阶段 (离线, 按需)                                      │
│  eval_stepwise.sh → compute_factor_stats(800股,120天)      │
│  → IC 权重写入 factor_registry                             │
├──────────────────────────────────────────────────────────┤
│  实盘阶段 (每日调度, 4 daemon 线程)                           │
│                                                          │
│  盘前 │ signals      08:30  因子+alpha+优化 → Redis targets │
│  盘中 │ execute      09:30  读Redis→下单 (检查熔断flag)     │
│       │ monitor    09:35-14:55 回撤/熔断/止盈止损/行情拉取  │
│  盘后 │ attribution 15:30  PnL归因 + IC衰减检测             │
└──────────────────────────────────────────────────────────┘

layer 0: data/       — 数据拉取 (tencent/tushare) + DataStore
layer 1: factor/     — 因子计算 + 合成 + IC评估缓存
layer 2: risk/       — 行业/市值中性化 + 协方差 + 过滤
layer 3: alpha/      — AlphaModel (等权/IC加权合成)
layer 4: execution/  — 下单引擎 + 行情 + 交易日历
layer 5: web/        — Flask 仪表盘 + Redis state_broker
layer 6: monitor/    — 盘中风控 (回撤/熔断/止盈止损)
layer 8: evaluation/ — 五阶段回测评估 (CPCV+PBO)
```

### factor/ 模块

| 文件 | 职责 |
|------|------|
| `factor/compute.py` | 41 因子函数 + 静态注册 maps (PRICE:18, FUNDAMENTAL:23) + compute_all_factors |
| `factor/stats_cache.py` | compute_factor_stats: ProcessPoolExecutor × 4 并行因子值 + ThreadPoolExecutor IC/相关性 |
| `factor/synth.py` | equal_weight, ic_weighted, sleeve_compose |
| `factor/registry.py` | _cs_zscore, factor_registry DB 读写 |
| `alpha/model.py` | AlphaModel.combine() + rank() |

### quant/scheduler/ (P74)

| 文件 | 职责 |
|------|------|
| `_base.py` | _timed_loop() 通用定时循环 + 状态上报 |
| `signals.py` | 08:30 generate_signals → Redis (has_multiprocess) |
| `execute.py` | 09:30 读Redis targets → execute_signals (has_multiprocess) |
| `monitor.py` | 09:35-14:55 风控: 回撤/熔断/止盈/止损 (P75) |
| `attribution.py` | 15:30 Brinson 归因 + IC 衰减快照 |
| `status.py` | 线程安全 register/update/all_tasks (带 group 字段) |
| `__init__.py` | start_all() 四 daemon 线程 |

### evaluation/ 包 (五阶段回测)

| 阶段 | 方法 | 阈值 |
|------|------|------|
| Phase 1 | 全A股票池验证 | backtest_start=2010-01-01 |
| Phase 2 | IC / |t| / ICIR / half-life | |IC|≥0.02, |t|≥2.0, ICIR≥0.5 |
| Phase 3 | CPCV N=5 + PBO | logit(PBO) < -0.847 |
| Phase 4 | 扣费后 Sharpe | Net Sharpe > 0.3 |
| Phase 5 | 监控报告 | 拥挤度/衰减/换手率/容量 |

---

## P75: 架构对标业界标准 + 止盈止损 (`f236fd8` / `eb7bee5`)

**#1 execute.py 不重算**: 删掉 `generate_signals()` 重算, 改为 `broker.get().get("signals")` 读 Redis。无信号快速失败不 fallback。09:30 执行从 ~90s 降到 ~5s。

**#2 monitor.py 盘中风控**: 09:35-14:55 每 30s:
- 回撤 > 5% → 告警
- 总资产 < 95%初始 → Redis circuit_breaker → execute 拒绝执行
- 行情 API 5s 限频

**#3 attribution IC 衰减**: Brinson 归因后对比 factor_registry 权重 vs 昨日 Redis 快照, 跌幅 >30% 告警。不新增 DB 表。

**#4 止盈止损 (eb7bee5)**: monitor.py统一管理:
- 止盈: 浮盈 ≥ 20% → 卖出 50% 锁利, 同日不重复
- 止损: 浮亏 ≤ -15% → 全部卖出
- 旧止损从 `/api/quotes` 移除

**#5 UI 调度页分组**: 盘前/盘中/盘后三组, 状态栏 ⚠ 告警计数, 5 种状态色。

**配置新增**: `stop_profit_pct: 0.20`

---

## P74: 调度器拆分 + 前端调度Tab (`1096651` / `a0fca84`)

**调度器拆分**: signals (08:30) / execute (09:30) / monitor (09:35) / attribution (15:30), 各自 daemon 线程互不依赖。has_multiprocess 标记用于 UI 告警。

**前端调度Tab**: `/api/scheduler` endpoint, 5s 轮询, 7 列表格 (任务/时间/状态/多进程/上次执行/耗时/错误)。

---

## P73: 因子计算性能优化 — 4021s → 88.6s (45×)

compute_str 和 compute_abn_turnover 从 iterrows 逐循环改为 groupby 向量化, O(n²) → O(n log n)。

三次独立 pipeline 运行验证: 4 个相同仓位 (002598/002759/002727/002132), 价格股数完全一致。

---

## P72: Pipeline 信号生成修复 + UI 重新设计

- data/store.py: `_cfg`→`cfg` 修复 NameError
- factor/compute.py: market_conn 模块级导入, 因子有效数 29→37
- optimizer/portfolio.py: `.iloc[:n]`→`.loc[alpha.index[:n]]` 对齐
- greedy 0 手: 不再静默返回, 改为 raise ValueError
- UI: 双主题「交易室」/「研报页」, 品牌「盈迹」

---

## P69+P71: 架构清理 + 安全修复

- 因子注册表集中化: 33 个分散注册 → 静态 _PRICE_FN_MAP + _FUNDAMENTAL_FN_MAP
- 消除重复定义: 5 个函数各定义 2 次 → 保留完整版 (~200 行死代码删除)
- 连接层统一: 15 处 raw sqlite3.connect → market_conn()
- SQL 注入修复 (compute_str 等)
- DataStore._connect() 线程安全锁
- execute() 事务回滚 (try/except/finally)

---

## P68: ProcessPoolExecutor 孤儿进程内存泄漏 — 根因修复

3 层防护:
1. executor.shutdown(wait=True)
2. PID 文件 .compute_pids 追踪, 崩溃时自动清理
3. web/app.py SIGTERM handler → _clean_exit()

清理: 删除 scheduler.py / restart.sh / crontab / launchd plist, 消除进程重启泄漏。

---

## 并发架构 (ADR 027)

ProcessPoolExecutor worker 自加载:
- 主进程只传元数据 (symbols + dates + factor_names, <10KB)
- 每个 worker 打开独立 DataStore (WAL 并发读)
- 4 worker × 独立 GIL → OS 级并行
- 主进程内存 ~5GB → ~200MB (swap 消除)
- as_completed 不加 timeout (500×120 单 chunk ~700s, timeout 会误杀)

保留 ThreadPoolExecutor: IC 计算 + 相关性矩阵 (轻量, ≤10s)

---

## 当前状态

- **config.yaml**: n_symbols=800 (中证800), lookback=120, max_workers=4 (M1 8GB cap)
- **调度器**: 4 daemon (signals/execute/monitor/attribution), launchd 管理
- **止盈止损**: monitor.py 统一管理, stop_profit_pct=0.20 / stop_loss_pct=0.15
- **factor_registry**: 64 因子注册, 41 active, ic_weight 列存储 IC 权重
- **State Broker**: Redis 跨进程, broker.get() 合并方向 cached→state, 清除旧缓存污染
- **测试**: 67 passed (24/30 原始, 后修复为 67/67)
- **HANDOFF**: 项目根唯一真相源

---

## 关键约束

- 所有数值参数仅存 config/config.yaml, 代码中用 _require_cfg() 快速失败 (零 fallback)
- 修改前先 git commit 归档
- **永不 fallback** — 静默降级 = 隐藏 bug, 改 raise
- **>5 秒必埋点** — 模板 9 (coding-standards SKILL.md)
- 因子 status 变更记入 notes 字段 (追加式)
- 修改后文档同步更新, 根 HANDOFF.md 是唯一真相源
- 沙箱受限的命令发给用户在终端执行
- 不删历史 DB 数据 (日线从 2020 至今)


### P77#10: 多进程内存泄漏根除 (`878fba1`)

**根因**: ProcessPoolExecutor.shutdown(wait=False, cancel_futures=True) 对卡在阻塞 I/O
(sqlite3 查询 5000+ 股) 中的 worker 进程完全无力 — 它只发信号, 不杀进程。
pgrep 兜底不可靠, 导致 worker 指数级累积 → OOM。

**修复 (3 层防护)**:
1. **compute_factor_stats finally 块重写**: shutdown 前显式 proc.terminate() → sleep(2) 
   → proc.kill() 每个 worker (行业标准做法)
2. **_MAX_WORKERS=1**: M1 8GB 单 worker 杜绝并发泄漏, 内存 ~100MB fixed
3. **_cleanup_process_pool 增强**: SIGTERM→SIGKILL + pgrep 兜底阶段2 (漏网者)

**其他**:
- signal SIGTERM/SIGKILL 模块级引入, 消除局部 import
- web/app.py _clean_exit 删除 16 行重复 PID 清理, 导入 _cleanup_process_pool
- _worker_timeout: 120→180s, 适配全量 5208 股因子计算

### P76: 因子5状态生命周期 — 对标 WorldQuant/AQR (`d813c44`)

**背景**: factor_registry 原只有 2 状态 (active/deprecated), 回测结果与 status 不同步。
64 注册因子中 41 标 active 但仅 zt_streak 通过完整 Phase 2+3+4。

**新 5 状态模型** (业界标准):

| status | 含义 | 实盘 | 触发 |
|--------|------|:--:|------|
| registered | 已注册未评估 | - | 新因子入库 |
| candidate | 通过初筛 | - | Phase 2: |t|≥2.0 |
| active | 实盘生产 | ✓ | Phase 3+4 全通过 |
| monitoring | 生产中告警 | ✓ | IC 衰减>30% |
| retired | 已退役 | - | 持续衰减 |
| rejected | 回测淘汰 | - | Phase 2 未通过 |

**迁移**: 旧 active 按 |IC|≥0.02 AND |IC_IR|≥0.5 映射:
64 → active(1:zt_streak) / candidate(2) / rejected(36) / retired(23)

**自动流转**: eval_stepwise.sh + attribution.py IC 衰减→active→monitoring

**UI**: 因子页 KPI 栏 6 列 (总注册/active/candidate/rejected/retired/有效计算)
---

### P77: 因子页面数字消失 — 2 个根因修复 (`66960cb`)


### P79: 全局硬编码清零 (`acab523`)

**config.yaml 修复**: 合并重复 `data:` key (第144行覆盖第30行的bug), 新增 13 个配置键:

| 键路径 | 默认值 | 原硬编码位置 |
|--------|--------|-------------|
| `data.api_delay.limit_up` | 0.3 | data/limit_up.py:143 |
| `data.api_delay.holder_trade` | 0.3 | data/holder_trade.py:142 |
| `data.api_delay.lhb` | 0.5 | data/lhb.py:163 |
| `data.api_delay.jq_valuation` | 0.15 | data/jq_valuation.py:221 |
| `data.api_delay.fund_flow` | 5.0 | data/fund_flow.py:142 |
| `data.api_delay.dividend` | 0.2 | data/dividend.py:126 |
| `data.api_delay.daily_basic` | 0.1 | data/daily_basic.py:60 |
| `data.api_delay.northbound` | 0.3 | data/northbound.py:125 |
| `data.sqlite.timeout` | 30 | daily_sync.py, evaluation/run_store.py, holder_trade.py, dividend.py (共8处) |
| `data.sqlite.busy_timeout` | 30000 | data/store.py, factor/registry.py (共3处) |
| `cache.redis.socket_connect_timeout` | 2 | data/cache.py:113 |
| `cache.retry_delay` | 0.5 | data/cache.py:342 |
| `web.sse.queue_timeout` | 30 | web/app.py:509 |
| `execution.quote.max_batch_workers` | 4 | execution/quote.py:100 |

**所有文件用 `_require_cfg()` (快速失败), 禁止 fallback。**

**最终扫描**: `time.sleep(数字)` 清零 / `timeout=数字` 清零 / `max_workers=数字` 清零 / `_cfg(x, fallback)` 清零 (16 文件全部 py_compile 通过)

**#1 c.close() bug**: `web/app.py api_factors()` 中 `c.close()` 在后续 `c.execute()` 前调用 → `ProgrammingError` → 异常处理器只设 `n_registered`/`n_active`，其他字段(总计/候选/淘汰/退役/已评估)丢失 → JS 显示为横线。

**修复**: `c.close()` 移到所有查询后; 异常处理器改为设 0 而非污染数据。

**#2 margin_buy_ratio 重复**: `_PRICE_FN_MAP` 和 `_FUNDAMENTAL_FN_MAP` 同时注册 `margin_buy_ratio` → get_factor_names 只返回 1 个 → 静态注册 65 但 DB 只有 64。

**修复**: 价格版重命名为 `margin_buy_ratio_5d` (5日均值); factor_registry 64→65 行; 因子数自洽 (38价格+27基本面=65)。
