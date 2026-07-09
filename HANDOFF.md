# HANDOFF — quant 项目当前状态

**最后更新**: 2026-07-09 23:30 CST

> 旧版归档: docs/HANDOFF-2026-07-02.md / docs/HANDOFF-2026-07-03.md (已 superseded)
> 项目根只有一个 HANDOFF.md 作为单一真相源

---

## 最近提交 (2026-07-09)

| 提交 | 内容 |
|------|------|
| `8a91932` | fix: compute_str/compute_abn_turnover 加回最小样本门槛 (P73) |
| `54eae57` | perf: compute_str + compute_abn_turnover 逐循环→groupby 4021s→88.6s (P73) |
| `1096651` | refactor: 调度器拆分为三个独立模块 (signals/execute/attribution) |
| `a0fca84` | feat: 前端调度状态tab页 + status.py状态注册表 |
| — | fix: data/store.py + factor/compute.py + optimizer/portfolio.py + UI「盈迹」(P72) |
| — | refactor: 因子注册表集中化 + 消除重复定义 + 连接层统一 + pipeline 抽取 (P69) |
| — | fix: execute()事务回滚 + compute_str() SQL注入 + DataStore线程安全 + config包init (P71) |

### P72: Pipeline 信号生成修复 + UI 重新设计

**#1 data/store.py _cfg→cfg**: 模块导入 `cfg` 但 5 处使用 `_cfg`, Step 1 每次报 NameError。统一为 `cfg`。

**#2 factor/compute.py market_conn 模块级导入**: P69 重构时替换 `sqlite3.connect()` 为 `market_conn()`
但删除了模块级 import (仅 2 个函数有局部导入)。导致 4 个因子函数 NameError
(compute_str / compute_asset_growth / compute_holder_reduction / compute_pledge_ratio)。
修复: 模块级 `from data.store import market_conn as _market_conn`, 因子有效数 29→37。

**#3 optimizer/portfolio.py prices.iloc 对齐错误**: `p = prices.loc[common]` 按 common index
(字母序) 排序, 但 `p.iloc[:n_top]` 和 `prices.iloc[:n_stocks]` 取的是字母序前 N 只
而非 Alpha 前 N 只的均价。修复: 3 处 `.iloc[:n]` → `.loc[alpha.index[:n]]`。

**#4 greedy 0 手快速失败**: 不再静默返回空持仓, 改为 `raise ValueError(...)`。

**#5 UI 重新设计**: 双主题「交易室」/「研报页」, 品牌「盈迹」, 纯白文字。

**验证**: 500 symbols × 37 valid factors → 1 position (000615 @ ¥3.37)。

---

### P73: Pipeline 因子计算性能优化 — 4021s → 88.6s

**问题**: 第一次完整 5208 股 pipeline 运行耗时 4021.7s (67 分钟)，瓶颈在因子计算阶段
price factors 每 5 个耗时 ~30 分钟。

**根因**: `compute_str()` 和 `compute_abn_turnover()` 使用 pandas iterrows 逐行循环
计算滚动标准差/均值，O(n²) 复杂度。5208 股 × 243 天 = 126 万行，每次循环 ~1ms。

**修复**:
- `compute_str()`: iterrows 循环 → `groupby('symbol')['turnover'].std()` 单次向量化
- `compute_abn_turnover()`: iterrows 循环 → `groupby('symbol')['turnover'].mean()` 单次向量化
- 加回 `max(window//2, 10)` 最小样本门槛，确保 groupby 版与旧循环版数学等价

**验证**: 两次 pipeline 运行输出完全一致:
  - 4 个相同仓位: 002598 / 002759 / 002727 / 002132
  - 价格+股数完全相同: ¥6.82/100, ¥23.43/100, ¥10.45/100, ¥5.63/100
  - 有效股数: 560
  - 67/67 测试全绿

**各阶段耗时(88.6s)**:
| 阶段 | 耗时 | 说明 |
|------|------|------|
| data | ~19s | 21 只 stale 日线补拉 |
| load | ~6s | 5208 symbols, 243 天 |
| factor | ~66s | 41 因子: price 18(53s) + fundamental 24(12s) |
| risk | ~0.1s | 协方差矩阵 + 中性化 + 过滤 |
| optimizer | <0.1s | equal_weight 4 pos |

### P69: 架构清理 — 因子注册表集中化 + 消除重复定义 + 连接层统一 + pipeline 抽取

**#1 因子注册表集中化**: 33 个分散的动态注册块迁移到静态 _PRICE_FN_MAP (27→38)
+ _FUNDAMENTAL_FN_MAP (5→27)。两 map 移至文件末尾，解决 Python 前向引用问题。

**#2 消除重复定义**: compute_margin_buy_ratio / compute_gross_margin_diff /
compute_financial_anomaly / compute_roe_trimmed 各定义两次，保留完整版
删除简化版 (~200 行死代码)。margin_buy_ratio 价格版重命名为
compute_margin_buy_ratio_price 避免与基本面版冲突。

**#3 连接层统一**: 15 处 `sqlite3.connect(db)` → `market_conn('ro')` /
`market_conn('rw')` (factor/compute.py 12 + web/app.py 3 + execution/engine.py 1)。
TRADE_DB 连接保留原样。

**#4 pipeline.py 抽取**: HTTP 推送函数 (_post_state 等 ~60 行) 提取到
web/state_pusher.py，pipeline.py Step 3 Alpha 缩减为 5 行 AlphaModel 调用。

**#5 文件结构**: alpha/ 包从空壳变为实际模块 (model.py + synth.py + __init__.py)。


| — | fix: execute()事务回滚 + compute_str() SQL注入 + DataStore线程安全 + config包init (P71) |

### P71: 代码安全与稳定性修复

**execution/engine.py — execute() 事务回滚 + IndentationError 修复**: 
execute() 的 for 循环体缺少缩进 (SyntaxError)，且批量订单 BEGIN 后从不 COMMIT/ROLLBACK/CLOSE。
修复：try/except/finally 模式，异常时 rollback，finally 中 close。

**factor/compute.py — compute_str() SQL 注入修复**: 
第 2430 行 symbol 直接拼入 SQL 字符串。替换为参数化查询 + bind params。

**factor/compute.py — compute_ocfp() 连接层绕过修复**: 
第 2575 行 raw sqlite3.connect 绕过 market_conn()，缺少 WAL 模式。
替换为 `market_conn("ro")`。

**data/store.py — DataStore._connect() 线程安全**: 
shared _conn 无锁保护，多线程并发可能创建多个连接并泄漏。
新增 `threading.Lock` + `_make_conn()` 工厂方法。

**config/__init__.py**: 添加包级 docstring (之前为空文件)。

**全量 pyc 清理**: 删除所有 stale .pyc，解决 stats_cache 旧版本 devnull 残留。

**已验证**: 6 文件 131+ 70- 行改动，24/30 测试通过 (6 个 synth 失败为预现有 API 签名不匹配)。

---


| 提交 | 内容 |
|------|------|
| 736decd | perf: 主进程不加载全量 OHLCV — worker 返回 close, 内存 ~5GB→~200MB, 消除 swap |
| 0b62c3a | fix: 移除 ProcessPoolExecutor 600s 超时 — 500股×120天被误杀, 恢复五阶段全流程 |
| 5ec691d | fix: stats_cache 模块级 sys.path 守卫 — spawn worker 确保能找到项目模块 |
| 8e64647 | fix: vol_price_corr 除零保护 — std()>0 检查 |
| 498c88b | fix: epa 因子注册 — _FUNDAMENTAL_FN_MAP→_PRICE_FN_MAP |
| a6f366a | test: 500 stocks × 120 days — 中等规模验证 |
| bb3dfce | fix: pair_corrs 闭包变量泄漏 + 重复 store.close() |
| ed203e1 | fix: ic_series + corr_counts 未初始化 NameError |
| 1a20eee | fix: ProcessPoolExecutor 正确实现 — worker 自加载 (ADR 027) |
| c20bfee | debug: 移除 worker logger — 隔离 logging 锁嫌疑 |
| bd78c68 .. 72bc69e | ThreadPoolExecutor 死锁调试 (7 次提交, 已废弃) |
| b236d4a | feat: 五阶段标准回测评估 (CPCV + PBO + Phase 5) |

---

## 核心架构

```
layer 0: data/       — akshare 数据拉取 + store
layer 1: factor/     — 因子计算 + 合成 + 评估缓存
layer 2: risk/       — 行业/市值中性化
layer 3: alpha/      — pipeline 调度 (当前隐含在 pipeline.py 中)
layer 4: execution/  — 下单引擎 + 除权检测
layer 5: web/        — Flask 前端
layer 8: evaluation/ — 五阶段回测评估 (新增)
```

### factor/ 模块 (compute.py 拆分后)

| 文件 | 行数 | 职责 |
|------|------|------|
| `factor/compute.py` | ~2980 | 全部因子函数 + maps(P69集中化:38+27) + compute_all_factors |
| `factor/registry.py` | 45 | _cs_zscore, _db_connect, _FIN_FACTORS |
| `factor/orchestrator.py` | 25 | get_factor_names (延迟导入) |
| `factor/synth.py` | — | equal_weight, ic_weighted, sleeve_compose |
| `factor/stats_cache.py` | ~530 | IC/IR/decay/corr: 因子计算 ProcessPoolExecutor(6进程自加载DB), IC+相关性 ThreadPoolExecutor |
| `factor/__init__.py` | 55 | __getattr__ 惰性导入, 打破循环 |
| `config/constants.py` | 80 | 全局常量 + _require_cfg + _market_db_path |

### evaluation/ 包 (五阶段回测)

| 文件 | 职责 |
|------|------|
| `evaluation/phase1_data.py` | 股票池验证 + 数据范围 |
| `evaluation/phase2_single.py` | IC/\|t\|/ICIR/half-life 四维筛选 |
| `evaluation/phase3_oos.py` | CPCV walk-forward + PBO 检验 |
| `evaluation/phase4_costs.py` | 交易成本后验证 |
| `evaluation/phase5_monitor.py` | 持续监控 (拥挤度/衰减/换手率/容量) |
| `evaluation/cpcv.py` | Purged WF-CV (De Prado 2018 Ch.7) |
| `evaluation/pbo.py` | PBO + DSR (De Prado 2018 Ch.8) |

---

## 性能改进

| 模块 | 改动 | 效果 |
|------|------|------|
| `stats_cache.py` 因子计算 | ThreadPoolExecutor → ProcessPoolExecutor (worker 自加载, ADR 027) | ~140s → ~16s (9×加速) |
| `factor/compute.py` compute_abn_turnover | iterrows 循环 → groupby.mean() 向量化 | ~15min → ~2s |
| `factor/compute.py` compute_str | iterrows 循环 → groupby.std() 向量化 | ~30min → ~3s |
| `stats_cache.py` IC 评估 | for name in factors → ThreadPoolExecutor(6) | ~30s → ~6s |
| `stats_cache.py` 相关性矩阵 | for i,j in pairs → ThreadPoolExecutor(6) | ~10s → ~2s |
| `config.yaml` | `factor.evaluation.max_workers: 6` | 可调整并行度 |

### 并发架构 (最终方案 ADR 027, 2026-07-08)

**ProcessPoolExecutor worker 自加载** — ADR 027:

- 主线程只传元数据 (symbols + date_strs + factor_names, <10KB) → ZERO DataFrame pickling
- 每个进程打开独立 DataStore (WAL 并发读), 加载 daily + fundamentals + financials
- 6 进程 × 独立 GIL → 真正 OS 级并行 (~9× 加速 vs ThreadPoolExecutor)
- 主进程不再加载 OHLCV: eval_dates 从 SQL DISTINCT date 获取, close 由 worker 返回后拼接
  (全量 5493 股时主进程内存从 ~5GB 降至 ~200MB, swap 消除)
- 主线程 `store.close()` 在 spawn 前调用, 无 WAL 锁继承
- **as_completed 不加 timeout**: ProcessPoolExecutor 段去掉了 600s 超时 (af2d24e 引入)。
  500 股×120 天单 chunk 需 ~700s, 600s 超时误杀正常计算。worker 内层 try/except 已兜底,
  系统级崩溃极罕见且超时无法恢复。IC/相关性 ThreadPoolExecutor 段保留 timeout。

**已废弃**:
- ThreadPoolExecutor stateless worker → GIL 串行化, 6 线程 = 1 线程性能
- ProcessPoolExecutor initargs 传 DataFrame (187MB/35MB) → pickle 启动延迟 1-2min
- ThreadPoolExecutor 共享 DataStore._conn → 死锁

**保留 ThreadPoolExecutor 用于**: IC 计算和相关性矩阵 (轻量级, ≤6s 完成)

---

## 回测标准 (ADR 026)

**流程**: `PYTHONPATH=. bash scripts/eval_standard.sh [--phase5]`

| 阶段 | 方法 | 阈值 |
|------|------|------|
| Phase 1 | 全A 5493只, ST由pipeline过滤, 含退市股 | backtest_start_date=2010-01-01 |
| Phase 2 | 单因子 IC/\|t\|/ICIR/half-life | \|IC\|≥0.02, \|t\|≥2.0, ICIR≥0.5, HL≥20d |
| Phase 3 | CPCV N=5, embargo=1d + PBO | logit(PBO) < -0.847, Sharpe decay <50% |
| Phase 4 | 扣费后 Sharpe 确认 | Net Sharpe > 0.3 |
| Phase 5 | 监控报告 (可选 --phase5) | 拥挤度/IC衰减/换手率/容量 |

---

## 当前状态

- **config.yaml**: n_symbols=0 (全量 A 股回测, ADR 026 标准配置)
- **并发**: ProcessPoolExecutor worker 自加载 (ADR 027), 6 进程, 无 as_completed timeout (500×120 单 chunk ~700s, 误杀风险 > 兜底价值)
- **factor_registry**: 64 因子注册, 35 active / 23 deprecated
- **因子覆盖**: OIR/STR/ABN_TURN/OCFP + 涨跌停六因子 + EPA/TRCF/ideal_amplitude + margin_buy_ratio (融资买入占余额比, margin_detail 表, 广发2024) + analyst_consensus (分析师共识度, analyst_forecast 表, 中信建投2022) + EPD/EPDS (估值偏离) + 毛利率TTM差分/财务异常复合/单季度ROE(掐头) (Phase 3 财务三因子)
- **已修复 4 bug**: epa 注册错误 / ocfp 签名不匹配 / vol_price_corr 除零 / seal_time 格式越界
- **执行价格**: Sina 实时 open + 除权检测 10% (ADR 017)
- **launchd**: scheduler ✅ (KeepAlive) / webapp ❌ (须走 restart.sh, ADR 025)
- **SIGTERM 安全**: _clean_exit() 先清 ProcessPoolExecutor 子进程再退出, .compute_pids 追踪 worker PID (P68)
- **数据字典**: [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md)
- **ADR 档案**: [docs/adr/](docs/adr/) (027 条)
- **备份**: factor/stats_cache.py.bak (ThreadPoolExecutor 版本, 已废弃)

---

## 研究资料 (Claude Code 搜索)

| 文件 | 内容 |
|------|------|
| [docs/research/A股量化因子全量研究报告_完整版_2026-07-07.md](docs/research/A股量化因子全量研究报告_完整版_2026-07-07.md) | 154KB, 6合1: 普查+涨跌停+数据源+回测标准+未覆盖因子+微型异象 |
| [docs/research/因子正交化最佳实践_2026-07-08.md](docs/research/因子正交化最佳实践_2026-07-08.md) | 5种方法对比, 推荐对称正交(Löwdin), 天风实证 IR 1.71→2.58 |
| [docs/research/微型异象因子_2026-07-07.md](docs/research/微型异象因子_2026-07-07.md) | 8个方向, 3可落地 (质押delta/可转债隐波/问询函), 5不可 |
| [docs/research/量化因子回测策略业界标准_2026-07-07.md](docs/research/量化因子回测策略业界标准_2026-07-07.md) | CPCV+PBO+walk-forward 标准流程, 已落地为 evaluation/ 包 |

---

## 最近评估结果 (2026-07-08)

- **Phase 2**: 31 因子全量评估, zt_streak 唯一通过 (IC=+0.0556, t=8.2, IR=+0.75, HL≈26d), 耗时 2485s
- **Phase 3**: CPCV N=5, PBO=0.000, OOS_ICIR=+0.808, 通过
- **Phase 4**: 扣费后验证通过
- **Phase 5**: 监控报告已生成 docs/reports/monitor_2026-07-08.md

## 最近修复 (2026-07-09)

**P73 因子计算性能优化** (2026-07-09):
- compute_str + compute_abn_turnover 逐循环向量化, pipeline 4021s → 88.6s (45×加速)

**P69 架构清理** (2026-07-09):
- 因子注册表全部集中到静态 maps

### P68: ProcessPoolExecutor 孤儿进程内存泄漏 — 根因修复

详情见 CHANGELOG.md §3.6.0.

**3 层防护**: (1) executor.shutdown(wait=True); (2) PID 文件 .compute_pids 追踪, 模块加载/崩溃时自动清理; (3) web/app.py SIGTERM handler → _clean_exit() 读文件杀所有 worker PID.

**清理**: 删除 scheduler.py (根目录 standalone) / restart.sh / 三个 launchd plist, 双调度器问题已消除.

## 下一步计划

1. **立即可做**: 运行 `eval_standard.sh` 拿 Phase 2+3 结果, 看几个因子通过
2. **取决于1**: 如果通过的因子少 → 做对称正交化(Löwdin), 捞被埋没的因子
3. **如果1+2 后效果仍不够** → 接入质押比例变化(delta), 可转债隐波差
4. **调度**: Phase 5 monitor 配入 scheduler 每日盘后运行

## 关键约束

- 修改前先备份 + 提交 git
- 数值参数放 config/config.yaml, 永不硬编码
- 永不 fallback 执行价格
- 因子 status 变更记入 notes 字段 (追加式)
- 修改后文档同步更新, 根 HANDOFF.md 是唯一真相源

### P75: 架构对标业界标准 — 四段式调度 + 盘中风控 + IC 衰减监控 (`f236fd8`)

**改动方案（4 项）**:
1. execute.py 不再重算 generate_signals — 改为读 Redis 中 08:30 产出
2. 新增 monitor.py 盘中实时风控 daemon (09:35-14:55)
3. attribution 加 IC 衰减快照 — 每日对比 factor_registry 权重变化
4. UI 调度页按角色分组展示 (盘前 / 盘中 / 盘后)

**对标 Grinold & Kahn 标准流程**:
- 盘前 batch: signals (08:30) — 因子计算 + alpha 合成 + 组合优化 → 写入 Redis
- 盘中执行: execute (09:30) — 读 Redis targets → 下单执行
- 盘中风控: monitor (09:35-14:55) — 回撤/单股/熔断检查
- 盘后归因: attribution (15:30) — PnL 归因 + IC 衰减检测

**数据库新增**: None（IC 快照写入 Redis JSON，不新增 DB 表）
**测试**: 67 passed
### P74: 调度器拆分 + 前端调度Tab页 (`1096651` / `a0fca84`)

**调度器拆分** (`quant/scheduler/`):
- `signals.py` — 盘前信号 (08:30, has_multiprocess=True)
- `execute.py` — 开盘执行 (09:30, has_multiprocess=True)
- `attribution.py` — 盘后归因 (15:30)
- `_base.py` — 通用 `_timed_loop()` + 状态上报
- `status.py` — 线程安全 register/update/all_tasks
- `__init__.py` — `start_all()` 三独立 daemon 线程

**前端调度Tab**:
- `/api/scheduler` → `{"data": {"tasks": [...]}}`
- 7列表格: 任务/时间/状态/多进程/上次执行/耗时/错误
- 5色状态: running(橙)/error(红)/waiting(灰)/sleep(深灰)/idle(绿)
- 5s 轮询, tab切换启停

**关键决策**: 任务拆分后如果某任务的多线程导致内存泄漏, 可单独停掉该任务, 其他任务不受影响。
