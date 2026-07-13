# HYPOTHESES — 设计讨论与方案记录

## 2026-07-12: 两级因子筛选架构 — 诊断快筛 → 正式评估的闭环


## 2026-07-13: 因子窗口驱动数据加载 — max(传入天数, 因子需求) 模式

**背景**: 系统有四处数据加载点各自决定加载天数，与因子实际窗口需求无显式关联。`_thread_compute_chunk` 硬编码 365，`ic.py` 用 `lookback*2`，`pipeline.py` 用 `data.lookback_days`。

**核心原则** (用户提出): 如果传入的参数天数 < 因子自身的逻辑要求（如 ztd=250 交易日），取因子要求；如果传入天数 ≥ 因子要求，取传入天数。代码里显式写 `max(传入天数, 因子最小需求)`，好读易懂。

**方案**: 
1. `factor/windows.py`: `max_factor_calendar_days(factor_names)` 从 `_PRICE_FN_MAP` 提取各因子 window，取最大值 × 1.5
2. 四处调用点统一为 `max(传入天数, max_factor_calendar_days(factor_names))`
3. `compute_ztd` 删除 SQL 回退，缓存未命中 → RuntimeError（fail-fast）

**设计取舍**:
- 选择从 `_PRICE_FN_MAP` 取 window 而非解析函数签名：window 在注册表已有声明，是单一真相源
- 不处理 `_FUNDAMENTAL_FN_MAP`：基本面因子不依赖日线窗口
- `compute_ztd` 不再有数据库连接能力：职责从"想办法搞到数据"收窄为"从缓存取值并 z-score"
- `pipeline.py` 使用 `factor_names=None`（全量已注册因子）：实盘路径一次数据加载，不增加 DB 查询

**状态**: 已落地 (2026-07-13)

**关联**: HANDOFF 2026-07-13#22

---

## 2026-07-13: ztd 预计算缓存四入口全覆盖

**背景**: `backtest/loop.py` 调用 `preload_ztd_cache`，但 `stats_cache.py`、`ic.py`、`pipeline.py` 没有。`compute_ztd` 的 SQL 回退路径掩盖了这个遗漏。

**方案**: 四个 `compute_ztd` 调用入口全部在调用前显式 `preload_ztd_cache`：
- `backtest/loop.py:202` — 回测主循环前
- `factor/stats_cache.py:181` — ThreadPoolExecutor 前
- `factor/ic.py:110` — per-day 循环前
- `pipeline.py:176-180` — 实盘信号生成 Step 3 前

**验证方法**: 若任一入口遗漏 → `compute_ztd` raise RuntimeError → fail-fast 立即可定位。

**状态**: 已落地 (2026-07-13)

**关联**: HANDOFF 2026-07-13#22


**背景**: 当前系统存在两套独立的因子 IC 评估路径, 互不相连:
1. backtest/diagnostics.py — 回测前 120 天 IC 快照, 秒级, 每次回测自动跑
2. evaluation/ 五阶段正式评估 — 全量历史 CPCV+PBO, 数小时, 手动触发

诊断模块的结果只打日志, 不写 factor_registry, 不喂给正式评估。
导致诊断说 7 个有效但实盘只有 1 个 active 的脱节。

**核心逻辑** (用户提出):
> 如果诊断快筛 reject 一个因子, 正式评估也必定 reject。
> 因此诊断应作为正式评估的前置过滤器。

**逻辑验证**:
- 诊断阈值: ICIR < 0.1 -> drop
- 正式 Phase 2 阈值: ICIR < 0.5 -> fail
- 诊断用 120 天窗口, 正式用全量历史。120 天是全量子集。
- 子集 ICIR < 0.1, 全量绝不可能跳到 > 0.5 -> 诊断 reject => 正式必定 reject (成立)
- 诊断 pass 不保证正式 pass (因为正式还有 CPCV/PBO/成本回测三道关)

**边界情况**: 因子在最近 120 天 ICIR < 0.1, 但在 5 年历史上 ICIR > 0.5 -> 诊断误杀。
但这种情况说明因子已经停滞失效至少半年, 不应进实盘 -> 误杀是正确行为。

**架构设计**:

全部 backtesting 因子 (31 个)
    |
    v
[一级: 诊断快筛]  <- 120天 IC, 秒级, 每次回测自动跑
    |            <- 阈值: ICIR >= 0.1 -> keep/boost
    |            <- ICIR < 0.1 -> drop
    |
    +- drop (25 个) -> 状态保持 backtesting, status_reason 标记 failed_diagnostics
    |                 不浪费算力跑 CPCV
    |
    +- keep/boost (6 个) -> 晋级
           |
           v
[二级: 正式评估]  <- 5 阶段, 数小时, 手动/周频触发
    |            <- Phase 2: |IC|>=0.02, |t|>=2.0, ICIR>=0.5, half-life>=20d
    |            <- Phase 3: CPCV + PBO
    |            <- Phase 4: 成本感知回测
    |            <- Phase 5: 实盘监控
    |
    +- 全部过关 -> status=active
    +- 任一失败 -> status=rejected

**数据流**:
1. backtest/loop.py run_backtest() 完成后, diagnosis 结果通过 evaluation/run_store.py
   保存到 evaluation_runs 表 (phase=diagnostics)
2. phase2_single.py 的 screen_factors() 新增可选参数 prefilter_from_diagnostics=True,
   读取 evaluation_runs 中最新 phase=diagnostics 记录, 只处理 keep/boost 因子
3. eval_standard.sh 新增 --all flag 绕过预筛 (用于全量审计)
4. factor_registry.status_reason 更新 failed_diagnostics_YYYY-MM-DD (不改变 status,
   保留 backtesting 身份, 允许下次恢复)

**配置**: config.yaml 新增 factor.evaluation.diagnostics_min_icir: 0.1 (诊断通过阈值)

**状态**: 待讨论落地

**替代方案**:
- 诊断直接改 status 为 rejected (否决: 120天窗口太短, 因子可能恢复, 不应永久拒绝)
- 不连诊断和正式评估, 保持现状 (否决: 诊断结果只用不存, 浪费信息)
- 诊断完全替代正式评估 (否决: 无 CPCV/PBO 保护, 过拟合风险)

**关联**: ADR 007 (因子评估标准), ADR 029 (四层回测), HANDOFF 2026-07-12#7 (IC 统一)

---

> **强制规则**: 凡讨论涉及以下任一内容，必须当场写入本文件，不等用户提醒：
>   1. 新方案 / 设计思路
>   2. 分类 / 命名 / 约定
>   3. 方案之间的取舍（含被否决的）
>   4. 对现有架构的质疑或改进方向
>
> **写入时机**: 讨论当场。若一个讨论回合结束后 HYPOTHESES.md 无新增条目，
>   视为本轮遗漏，下一轮补上。
>
> **搜索**: `rg "关键词" HYPOTHESES.md HANDOFF.md docs/adr/` 三文件联动。


## 2026-07-11: 因子三分法 — dense / sparse / event

**背景**: `_cs_zscore` 的 `min_count` 需要区分因子类型，不同因子截面有效股票数差异巨大。
**提案**: 三类因子，各自独立 min_count 阈值

| 类型 | 覆盖度 | min_count | 因子举例 | 当前 |
|------|--------|-----------|----------|------|
| dense | ~5000 股 | 30 | 动量/反转/波动率/成交量 | ✅ `sparse=False` |
| sparse | ~3000 股 | 10 | EP/ROE/应计/资产增长 | ✅ `sparse=True` |
| event | <500 股 | 3~5 | 涨跌停/龙虎榜/北向/内幕/分析师 | ❌ 未实现，用 dense 凑合 |

**状态**: 待实现 — 代码只落地了两类。第三类 event 需要调整 `_cs_zscore` 签名或新增 `event_threshold` 参数。
**替代方案**: 不分类，统一 min_count=20 (否决: dense 过不过滤噪声, sparse 太严格丢信号)
**关联**: HANDOFF 2026-07-11 zscore min_count

## 2026-07-11: 大文件拆分 — 从单一文件到嵌套包

**背景**: `factor/compute.py` 3182 行，agent 每次读取消耗大量 token。
**提案**: 
1. 首次拆: `compute.py` → `compute/` 包 (price.py + fundamental.py + _registry.py + _dispatch.py)
2. 二次拆: `price.py` (1908行) → `price/` 包 (_momentum.py + _event.py + _alternative.py)
3. `fundamental.py` (1213行) 暂不拆 — 32 函数内聚性高，拆分无收益
**状态**: 已采纳 (2026-07-11 落地)
**替代方案**: 
- 每个因子一个文件 (否决: 70+ 文件碎片化)
- 按行数机械切割 (否决: 破环函数边界)
**关联**: ADR 028, HANDOFF 2026-07-11 大文件拆分

## 2026-07-11: 回测 universe 过滤策略

**背景**: 全量 5176 股回测耗时数小时，需缩减 universe。
**提案**: `get_universe()` 后按成交额排序取前 N, N 从 config 读 (`backtest.n_symbols` 或 `factor.evaluation.n_symbols`)。实盘路径保持全量。
**状态**: 已采纳 (2026-07-11 落地, N=800)
**替代方案**: 
- 按市值取前 N (否决: 小盘股成交额不足可能无价)
- 实盘也缩量 (否决: 实盘需全量覆盖)
- 硬编码 N (否决: 违反模板 10)
**关联**: HANDOFF 2026-07-11 回测 universe 早鸟过滤

## 2026-07-11: 回测数据库隔离

**背景**: 回测 t+1 模拟需要独立数据，不能污染生产 market.db。
**提案**: 回测使用独立 `benchmark.db`, pipeline 中 db_path 全链路参数传递。
**状态**: 已采纳 (2026-07-11 落地)
**替代方案**:
- 回测直连 market.db (否决: 污染生产)
- 复制整个 DB (否决: 浪费磁盘)
**关联**: HANDOFF 2026-07-11 db_path 修复

## 2026-07-11: factor_registry status 状态机

**背景**: 实盘和回测需要使用不同状态的因子。
**提案**:

| 场景 | status_filter | 实际匹配 |
|------|---------------|----------|
| 实盘 | `using` | active + monitoring |
| 回测 | `backtesting` | registered + candidate + retired |
| 全量评估 | `None` | 全部 |

**状态**: 已采纳 (2026-07-11 落地)
**替代方案**:
- 统一用 active (否决: 回测无法评估候选因子)
- 回测用全量 (否决: 忽略状态字段意义)
**关联**: HANDOFF 2026-07-11 因子状态过滤修复

## 2026-07-11: 回测命名规则

**背景**: 回测名混乱 (verify3/verify4/final/smoke6/smoke7), 无法追溯顺序。
**提案**: backtest1/backtest2/... 递增, smoke1/smoke2/... 递增。
**状态**: 已采纳 (2026-07-10 落地)
**替代方案**: 自由命名 (否决: 混乱不可追溯)
**关联**: HANDOFF 2026-07-10 命名规则标准化

## 2026-07-11: config 驱动参数 vs 硬编码

**背景**: 历史代码中有硬编码参数 (stop_loss_pct 等), 违反编码规范。
**提案**: 所有参数必须从 `config.yaml` 读取, 代码内不得有 magic number。通过 `_require_cfg()` 统一校验。
**状态**: 已采纳 (持续执行)
**替代方案**: 允许合理的默认值 hardcode (否决: 模板 10 硬约束)
**关联**: HANDOFF 2026-07-10 stop_loss_pct, ADR 015 production-config-standards

## 2026-07-11: HANDOFF / ADR / HYPOTHESES 三层归档

**背景**: 之前讨论过的方案和决策经常丢失，导致重复讨论或方案 A→B→A 循环。
**提案**:

| 文件 | 内容 | 写入时机 |
|------|------|----------|
| HYPOTHESES.md | 设计讨论/方案/分类 | 讨论当场 |
| HANDOFF.md | 已落地的代码变更 | 改动完成后 |
| docs/adr/NNN-*.md | 架构级决策 | 决策定型时 |

**状态**: 已采纳 (2026-07-11 创建)
**替代方案**: 全部放 HANDOFF (否决: 太长难搜索, 没有区分讨论和决策)
**关联**: ADR 001 (ADR 机制本身)

---

## 2026-07-11: 回测亏损根因分析 — 组合层 vs 因子层

**背景**: 回测 CAGR=-30.5%, Sharpe=-1.356, 仅 2 只持仓。需判断是因子无效还是组合层问题。
**提案**: 根因在组合层, 并非因子无效:

1. **资本不足** (根因): 5000 元买 A 股, 最低 100 股/手, 均价 ¥60 时连 1 只都买不起。optimizer 被迫挑最便宜的, 选股空间被价格扭曲。
2. **sleeve 稀释**: 33 因子等权合成, 动量与反转天然反向, 合成分数被稀释接近随机。
3. **风险层→optimizer 过度压缩**: 266 候选 → 2 持仓, 信号被过度过滤。
4. **交易成本**: 67 天来回交易 1-2 只, 摩擦成本吞掉微薄 alpha。
5. **市场状态**: 2026-04~07 震荡市, 动量因子自然表现差。

**诊断结论**: 先看因子 IC/ICIR, 不看回测 Sharpe。IC>0.02 的因子就是有效的, 亏钱是组合层问题。

**状态**: 已讨论, 组合层优化待实现
**关联**: HANDOFF zscore min_count

## 2026-07-11: 小资金品种选择 — 三条可行路径

**背景**: 资本 5000 元, A 股现货最低交易单位 100 股 (~1500/手), 无法分散化。梁文锋先例是期现套利, 非单边选股。
**提案**: 三条路径, 不需要 50 万:

| 路径 | 品种 | 最低资金 | 杠杆 | 因子复用度 |
|------|------|---------|------|-----------|
| ETF 轮动 | 行业/宽基 ETF | ¥500-2000/手 | 无 | 需 ETF 层面因子 |
| 可转债 | CB 双低/多因子 | ¥1000-5000/手 | 无 | 可复用部分价量 |
| A 股低价过滤 | A 股 <¥10 | ¥5000 持 3-5 只 | 无 | 全量复用 + 价格过滤 |

**推荐**: ETF 轮动 — 交易单位小、流动性好、行业 ETF 可映射现有因子。

**状态**: 待讨论决定方向
**替代方案**: 
- 等待积累资本 (否决: 无法接受, 启动资金就是 5000)
- 做期货 (否决: 风险超过当前阶段)

---

## 2026-07-11: 因子有效性危机 — 70 因子仅 1 个 IC 过关

**背景**: 70 个因子来自 (a) 书籍分析 (b) 训练数据推荐 (c) Claude Code 网搜"当前A股有效因子"，但仅 1 个 IC 达标。
**问题**: 是因子本身无效，还是实现/数据有 bug？

**待排查方向**:
1. 前视偏差 (look-ahead bias): 回测中是否用了未来数据？
2. 幸存偏差: 因子评估是否包含已退市股票？
3. IC 评估窗口: 是否恰逢市场风格切换期？
4. 实现偏差: 因子公式是否与原始论文一致？
5. 数据质量: 复权、停牌、ST 股处理是否正确？

**状态**: 待调查
**替代方案**: 放弃现有因子库重新搜集 (否决: 先排查再决定)

---

## 2026-07-12: 四层回测架构 — P0 落地

**背景**: 回测仅输出 CAGR/Sharpe，无法解释盈亏原因，无法自动优化。
**提案**: 因子评估(IC)→信号合成(权重)→组合构建(MV)→业绩归因(PnL拆解)。P0 优先：rolling IC 前置 + 因子归因 + 自动诊断。
**状态**: P0 已落地 (`backtest/diagnostics.py` 273行)
**后续**: P1 walk-forward + IC weighted synthesis (待实现)
**关联**: ADR 029, HANDOFF 2026-07-12

---

## 2026-07-12: 隐性 fallback 全量审计

**审计范围**: 回测业务流程全部 Python 文件 (`backtest/`, `pipeline.py`, `factor/compute/`, `execution/`)

### 发现: 13 个位置存在隐性 fallback

**P0 — 4 处 `except Exception: pass` 完全吞掉错误 (无任何日志)**

| # | 文件:行 | 函数 | 影响 |
|---|---------|------|------|
| 1 | `factor/compute/price/_alternative.py:585` | `compute_northbound_streak` | SQL 查询失败 → 全 0 序列, 因子静默失效 |
| 2 | `factor/compute/price/_alternative.py:611` | `compute_short_interest` | SQL 查询失败 → 全 NaN, 因子静默失效 |
| 3 | `factor/compute/price/_alternative.py:642` | `compute_fund_flow_3m` | SQL 查询失败 → 全 0 序列, 因子静默失效 |
| 4 | `pipeline.py:164` | `generate_signals` Step 3 | benchmark 拉取失败 → benchmark_ret=None, 依赖基准的因子(如 STR)静默退化 |

**P1 — 5 处 `except Exception: pass/continue` 带降级行为 (日志缺失)**

| # | 文件:行 | 函数 | 影响 |
|---|---------|------|------|
| 5 | `_alternative.py:232` | STR 残差计算 | sklearn 回归失败→用未中性化的 raw 值, 无日志 |
| 6 | `_alternative.py:318` | ABN_TURN 残差 | 回归失败→回退 `turn_series.loc[common]`, 无日志 |
| 7 | `_alternative.py:503` | TRCF 单symbol循环 | 单股异常→静默跳过, 无日志 |
| 8 | `_alternative.py:535` | ideal_amplitude 单symbol循环 | 单股异常→静默跳过, 无日志 |
| 9 | `fundamental.py:1065` | OCFP 行业中性化 | 中性化失败→保持原始值, 无日志 |

**P2 — 2 处使用 stderr 而非 logger (日志不进 quant.log)**

| # | 文件:行 | 函数 | 影响 |
|---|---------|------|------|
| 10 | `fundamental.py:1040` | OCFP TTM 查询 | 异常写入 stderr, `grep quant.log` 看不到 |
| 11 | `_alternative.py:344` | `_get_limit_pool` | limit_down_pool 表不存在→空 DataFrame, 无日志 |

**P3 — 死代码 / 无害但冗余**

| # | 文件:行 | 问题 |
|---|---------|------|
| 12 | `backtest/loop.py:28` | `LOT_SIZE = 100` 硬编码, 但从未使用 (pipeline.py:38 从 config 读) |

### 修复策略

- P0/P1: 所有 `except Exception: pass/continue` → 替换为 `logger.error(f"X failed: {traceback.format_exc()}")`
- P2: `stderr.write` → `logger.error`
- P3: 删除 `backtest/loop.py:28` 的 `LOT_SIZE = 100`

### 已有日志的正确案例 (供参考)

- `factor/compute/_dispatch.py:48-50`: `logger.error(f"price factor {name} failed: {traceback.format_exc()}")`
- `factor/compute/_dispatch.py:83-84`: `logger.error(f"fundamental factor {name} failed: {traceback.format_exc()}")`
- `fundamental.py:1138-1140`: `logger.error(f"insider_cluster failed: {traceback.format_exc()}")`
- `fundamental.py:1178-1180`: `logger.error(f"earnings_upgrade failed: {traceback.format_exc()}")`

## 2026-07-12: 正式评估管线 trace_id 设计

**背景**: 评估管线 5 个 Phase 是独立 `python3 -c "..."` 子进程, 之前的日志只有模块名
(evaluation.phase1/2/3/4/5), 无 trace_id。同一轮评估执行中 Phase 1-4 的日志无法关联。

**方案**: 各 Phase 入口函数生成 `tid = uuid.uuid4().hex[:12]`, 通过 `set_trace_id(tid)` 注入 contextvars,
后续所有 logger.info 自动带 `[tid]` 前缀。每个 Phase 独立 trace_id (不同进程无法共享)。

**设计决策**: 没用同一个 trace_id 贯穿全部 Phase, 因为 shell 脚本无法在子进程间传递 Python 对象。
trace_id 可后期通过 Phase 间的 `evaluation_runs` DB 记录关联 (各 Phase 保存时写入 tid)。

---


## 2026-07-12: IC 计算统一 — 从两套函数到一个入口

**问题回顾**: HANDOFF #12 设计了"两步架构"但把 IC 统一标记为"暂不统一"。实践中暴露:
- `compute_ic` (backtest) 和 `compute_ic_from_values` (Phase 2) 各有独立的 Spearman 计算循环
- 同一因子在诊断里 ICIR>0.1, Phase 2 里 ICIR<0.5, 阈值无法对齐
- 根本原因不是阈值不同, 是两套 IC 计算本身就不一致

**方案**: `compute_ic` 成为唯一公开函数, 支持取数据模式 (backtest) 和预计算模式 (Phase 2),
底层 Spearman 相关集中在 `_spearman_ic` 私有函数, 确保同一份数学实现。

**后续**: 阈值仍不同 (0.1 vs 0.5), 但现在是同一个函数算出的 IC, 可以对齐讨论。

## 2026-07-13: 因子状态同步闭环 — eval_standard.sh → factor_registry

**背景**: 旧 eval_stepwise.sh 的单体架构包含了状态更新逻辑 (评估完直接改 factor_registry.status)。
重构为 eval_standard.sh + Phase Python 模块后, 状态更新逻辑丢失, 38 个 rejected 成为僵尸状态。

**决策**: 新加 sync_factor_status() 集中式状态同步, 放在 eval_standard.sh Phase 4 之后。
不放在各 Phase 模块内部——Phase 模块只负责评估写 evaluation_runs, 状态管理是编排层职责。

**影响**: 每次跑 eval_standard.sh 后, factor_registry.status 自动更新。跑之前 rejected→candidate 的
因子会被重新评估。评估管线形成闭环。


## 2026-07-13 06:21 — fundamental.py 拆分决策

**假设**: 1215 行 fundamental.py 不需要立即拆分，共享 infrastructure 的内聚性 > 文件行数限制。
**状态**: Deferred。等膨胀到 1500+ 行或新增 >10 因子时采用 Facade + Registry 方案重新评估。

---

## 2026-07-13: 冒烟测试分层设计 — A/B/C 三档

**背景**: 冒烟测试最初 14 天 × 800 股 × 120 天 IC 窗口 × 67 因子，单次耗时 ~80 分钟，失去"快速验证"意义。因子来源 `status_filter="backtesting"` 展开为 `('registered','candidate','retired')` — rejected 不参与回测，但当前 0 个 backtesting 状态因子，实际使用 67 个 rejected + 2 个 retired 因子。

**三档分层**:

| 档位 | 交易日 | 股票 | IC窗口 | 因子 | 耗时 | 用途 |
|------|--------|------|--------|------|------|------|
| A (冒烟) | ≥10 | 200-300 | 60天 | 首次全量/日常backtesting池 | 2-5分钟 | 管线不崩 |
| B (快筛) | 60-120 | 500-1000 | 120天 | backtesting池 | 10-30分钟 | 因子方向性检查 |
| C (正式) | 252+ | 全市场 | 全历史 | backtesting池 | 数小时 | CPCV+PBO认证 |

**方案**:
1. `backtest/loop.py` `run_backtest()` 新增 `universe_size`, `ic_lookback`, `factor_status_filter` 可选参数
2. `scripts/smoke_test.py` 硬编码 A 档参数: 300股/60天IC/10交易日
3. 因子池切换: 首次 `factor_status_filter=None`(全量) → 稳定后 `factor_status_filter="backtesting"`
4. `config/config.yaml` 添加冒烟覆盖注释

**状态**: 已落地 (2026-07-13)

**关联**: `scripts/smoke_test.py`, `backtest/loop.py:run_backtest()`, `config/config.yaml:backtest.*`
