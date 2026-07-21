---
# HANDOFF — 2026-07-21 (test-v174, baostock turnover 回填)

## 当前运行状态
- **turnover 回填**: baostock 重写完成, 待手动跑
- **exclude_zero_turnover_days**: 0 (临时关闭, 待回填完成后恢复5)
- **daily 写入模式**: INSERT ... ON CONFLICT DO UPDATE (turnover 受 CASE WHEN 保护)
- **7 源回退链**: 全部可用
- **因子库**: 上次物化 48 因子入库
- **baostock**: 新增依赖, 免费无需注册, turn值与tushare一致

---
## test-v175 — 回填进度日志改进 + CLAUDE.md 编辑规则强化 (2026-07-21)

### 背景
- config.yaml 多次因 apply_patch 产生缩进错误 (4空格→3空格混排)
- backfill_turnover 首批 50 只处理完才出现第一条进度, 用户以为卡住

### 变更
1. **store.py:852** — 新增即时起始日志: "starting, first progress at 50 stocks (~Xs)"
2. **CLAUDE.md** — 编辑工具新增硬约束:
   - YAML 文件禁止 apply_patch, 必须用 yaml.safe_load/dump
   - VERSION 行禁止 apply_patch, 必须用 re.sub
   - 速查表新增 "重启" 规则: Agent 只给命令文本

### 涉及文件
- quant/data/store.py
- web/app.py (VERSION → test-v175)
- CLAUDE.md

---

## test-v174 — baostock turnover 回填重写 (2026-07-21)

### 背景

test-v173 用 sina 做 backfill_turnover, 但 sina K线接口不含 turnover 字段 (实测确认)。
诊断所有可用数据源后, baostock 是唯一满足条件的:

| 源 | turnover字段 | 限速 | 批量 |
|----|-------------|------|------|
| sina K线 | ❌ 无 | 逐只 0.05s | N/A |
| tickflow 行情(免费) | ❌ 无 | 5只/批 10次/分钟 | 5只 |
| tushare daily_basic | ✅ turnover_rate | 1次/分钟 | 支持但限速无用 |
| **baostock** | ✅ **turn** | **0.3s/只 无硬限** | 逐只 |

baostock turn 值与 tushare daily_basic turnover_rate 完全一致 (600519: 0.8492%)。
来源: scripts/check_turnover_sources.py 实测。

### backfill_turnover 重写

**文件**: quant/data/store.py:764-842

**旧策略**: sina HTTP K线, 逐只 urlopen, 读  字段 → 字段不存在 → 永远0 updated
**新策略**: baostock login → 逐只 query_history_k_data_plus(date,turn) → UPDATE daily SET turnover

设计要点:
- 一次 login() 全量循环 logout(), 避免反复登录开销
- 每只 0.3s 间隔 (config: rate_limit.baostock_per_stock_sec)
- 3 次重试 + 指数退避 (2s/4s/6s)
- 每 100 只 commit, 每 500 只打进度日志
- 只写 turnover, 不碰 OHLCV (UPDATE daily SET turnover=? WHERE symbol=? AND date=?)
- 覆盖 last_good 当天也包括 (07-10只有6只BJ有turnover, 其余5421只需补)

时间估算: 5400只 × 0.15s查询 + 0.3s间隔 ≈ 30分钟

### .gitignore 修复

**文件**: .gitignore
 →  +  + 
之前  匹配所有名为 data 的目录, 导致  源码也被 gitignore 屏蔽。

### 诊断脚本

新增 3 个 turnover 数据源诊断脚本:
-  — sina K线返回字段
-  — tickflow 行情 turnover 字段
-  — tushare daily_basic 批量支持
-  — 全数据源对比

---

## 下一步

1. 跑 baostock backfill_turnover:
   [07-21 10:43:06] CRITICAL quant | 未捕获异常: ParserError: while parsing a block mapping
  in "/Users/mariusto/project/quant/quant/config/config.yaml", line 53, column 3
expected <block end>, but found '<block mapping start>'
  in "/Users/mariusto/project/quant/quant/config/config.yaml", line 74, column 4
  File "<string>", line 2, in <module>
    from quant.data.store import DataStore
  File "/Users/mariusto/project/quant/quant/data/store.py", line 18, in <module>
    from quant.config.loader import load as _load_config
  File "/Users/mariusto/project/quant/quant/config/loader.py", line 199, in <module>
    validate()
    ~~~~~~~~^^
  File "/Users/mariusto/project/quant/quant/config/loader.py", line 65, in validate
    cfg = load()
  File "/Users/mariusto/project/quant/quant/config/loader.py", line 58, in load
    _config = yaml.safe_load(f)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/__init__.py", line 125, in safe_load
    return load(stream, SafeLoader)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/__init__.py", line 81, in load
    return loader.get_single_data()
           ~~~~~~~~~~~~~~~~~~~~~~^^
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/constructor.py", line 49, in get_single_data
    node = self.get_single_node()
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 36, in get_single_node
    document = self.compose_document()
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 55, in compose_document
    node = self.compose_node(None, None)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 84, in compose_node
    node = self.compose_mapping_node(anchor)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 133, in compose_mapping_node
    item_value = self.compose_node(node, item_key)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 84, in compose_node
    node = self.compose_mapping_node(anchor)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 127, in compose_mapping_node
    while not self.check_event(MappingEndEvent):
              ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/parser.py", line 98, in check_event
    self.current_event = self.state()
                         ~~~~~~~~~~^^
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/parser.py", line 438, in parse_block_mapping_key
    raise ParserError("while parsing a block mapping", self.marks[-1],
            "expected <block end>, but found %r" % token.id, token.start_mark)

2. 验证 turnover:
   [07-21 10:43:06] CRITICAL quant | 未捕获异常: ParserError: while parsing a block mapping
  in "/Users/mariusto/project/quant/quant/config/config.yaml", line 53, column 3
expected <block end>, but found '<block mapping start>'
  in "/Users/mariusto/project/quant/quant/config/config.yaml", line 74, column 4
  File "/Users/mariusto/project/quant/scripts/check_turnover_progress.py", line 2, in <module>
    from quant.data.store import DataStore
  File "/Users/mariusto/project/quant/quant/data/store.py", line 18, in <module>
    from quant.config.loader import load as _load_config
  File "/Users/mariusto/project/quant/quant/config/loader.py", line 199, in <module>
    validate()
    ~~~~~~~~^^
  File "/Users/mariusto/project/quant/quant/config/loader.py", line 65, in validate
    cfg = load()
  File "/Users/mariusto/project/quant/quant/config/loader.py", line 58, in load
    _config = yaml.safe_load(f)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/__init__.py", line 125, in safe_load
    return load(stream, SafeLoader)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/__init__.py", line 81, in load
    return loader.get_single_data()
           ~~~~~~~~~~~~~~~~~~~~~~^^
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/constructor.py", line 49, in get_single_data
    node = self.get_single_node()
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 36, in get_single_node
    document = self.compose_document()
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 55, in compose_document
    node = self.compose_node(None, None)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 84, in compose_node
    node = self.compose_mapping_node(anchor)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 133, in compose_mapping_node
    item_value = self.compose_node(node, item_key)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 84, in compose_node
    node = self.compose_mapping_node(anchor)
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/composer.py", line 127, in compose_mapping_node
    while not self.check_event(MappingEndEvent):
              ~~~~~~~~~~~~~~~~^^^^^^^^^^^^^^^^^
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/parser.py", line 98, in check_event
    self.current_event = self.state()
                         ~~~~~~~~~~^^
  File "/Users/mariusto/project/quant/.venv/lib/python3.14/site-packages/yaml/parser.py", line 438, in parse_block_mapping_key
    raise ParserError("while parsing a block mapping", self.marks[-1],
            "expected <block end>, but found %r" % token.id, token.start_mark)

3. 回填完成后恢复 config: exclude_zero_turnover_days: 0 → 5
---
## test-v181 — Nano 层排名集中策略 + docs 统一弃用标记 (2026-07-21)

### 背景
资本 ¥5,000 下每只股票只买 100 股（3 只 × 1 手），佣金 ¥15 占 0.33% 本金。
分析报告 C3 明确写"集中持仓减少交易笔数是唯一解"，但代码的 `_equal_weight_greedy`
用轮转均分实现，与报告结论矛盾。

用户提议: alpha 排名第一全仓 → 剩余买第二 → 直到没钱。这与 Grinold & Kahn 框架、
Kirby & Ostdiek (2012)、分析报告 C1/C3 结论一致。

### 变更

**1. portfolio.py — 新增 _rank_concentrated() + Nano 路由**
- 新方法 `_rank_concentrated()`: 按 alpha 降序逐只满仓买入，剩余资金不买碎股
  设计依据: Grinold & Kahn (2000) N=1-2 时最大化 IC; Kirby & Ostdiek (2012) 换手成本>分散化; C3 单笔<¥10K 集中是唯一解
- `construct()` Nano 分支: `_equal_weight_greedy` → `_rank_concentrated(a, p, capital)`
- `_equal_weight_greedy` 保留不动 — 仍是 Micro 层 fallback

**2. monitor.py — Nano 层豁免单票集中度告警**
- total < nano_cap → single_conc_limit = 1.0 (实质关闭)
  Nano 层 90%+ 单票集中度是预期行为，非风险事件

**3. config.yaml 注释对齐**
- nano_cap 注释: 补充豁免说明 + 文献来源
- max_single_concentration 注释: 注明 Nano 层自动豁免

**4. 文档全量对齐 — 旧参数名/旧值弃用标记**
- ARCHITECTURE.md: 旧代码示例 `equal_weight_cap: 20000` → 当前 3 层架构
- configuration.md: `equal_weight_cap, weighted_cap` → `nano_cap, micro_cap`
- capital-segmentation-analysis: greedy_cap/weighted_cap 加 ⛔ 已弃用标记
- CHANGELOG.md: P63 equal_weight_cap 加弃用注释

**5. test_portfolio.py — 4 新测试 + 兼容更新**
- 新增 test_rank_concentrated_buys_max_of_top_stock / _multi_stock / _alpha_ordering / _micro_fallback
- 旧 Nano 测试 method 断言更新为 "rank_concentrated"

### 涉及文件
- quant/optimizer/portfolio.py (新方法 _rank_concentrated, Nano 路由改)
- quant/scheduler/monitor.py (Nano 集中度豁免)
- quant/config/config.yaml (注释对齐)
- web/app.py (VERSION → test-v181)
- test/test_portfolio.py (4 新测试 + 兼容)
- docs/architecture/ARCHITECTURE.md (旧代码示例→当前)
- docs/getting-started/configuration.md (参数名对齐)
- docs/reports/capital-segmentation-analysis-2026-07-15.md (弃用标记)
- CHANGELOG.md (弃用标记)
- docs/handoffs/HANDOFF.md (本记录)
---
## test-v182 — tickflow API key 实时行情接入 + 当天日线数据路由 (2026-07-21)

### 背景
test-v181 修复了 Nano 层策略问题, 但 daily_data 任务对 07-21 拉取到 0 行。
诊断发现: 全部 7 个数据源都返回 0 行 — tickflow 免费版 "日K为历史数据, 盘中不会实时更新",
tushare 同理, pytdx/zzshare 也未入库当天数据, 腾讯/akshare TLS 指纹被封。

用户在 tickflow.org 已注册并获取 API key, API key 支持 `tf.quotes.get()` 实时行情
(含 OHLCV + turnover_rate), 但 _fetch_tickflow_daily 一直硬编码使用 TickFlow.free()。

### 变更

**1. store.py — 新增 _fetch_tickflow_quotes() 方法 (L670)**
- 使用 `TickFlow(api_key=_require_cfg("data.tickflow_api_key"))` 拉实时行情
- 映射 tickflow 行情字段 → 日线行格式: open/high/low/last_price/volume/amount/ext.turnover_rate
- amount 元→千元 (与免费版 klines 对齐)
- ext 字段兼容 Series/dict 两种返回格式 (tickflow SDK version variance)

**2. store.py — _fetch_tickflow_daily() 当天路由 (L634-637)**
- `datetime.today().strftime("%Y-%m-%d")` 判断当天日期
- start_date >= 当天时 → 路由到 `_fetch_tickflow_quotes()` (API key 实时行情)
- 历史日期 → 维持原逻辑 `TickFlow.free().klines.batch()` (历史K线)

**3. 注释对齐**
- _fetch_tickflow_daily docstring 补充路由说明 + 来源
- _fetch_tickflow_quotes docstring 标注 "tickflow 免费版日K仅历史数据" + 来源
- 回退链注释 (L1170-1176) 已正确标注 tickflow 为第一优先源

### 影响
- 当天日线数据现在可通过 tickflow API key 实时行情获取 (含 turnover_rate)
- 历史数据拉取不受影响 (继续用免费版 klines.batch)
- 回退链: tickflow(当天用API key) → zzshare → pytdx → sina → tencent → akshare → tushare(如有token)
- 腾讯/akshare 仍在 TLS 指纹封禁中, 不影响 (tickflow 在链首即命中)

### 涉及文件
- quant/data/store.py (_fetch_tickflow_daily 路由 + _fetch_tickflow_quotes 新方法)
- web/app.py (VERSION → test-v182)
- docs/handoffs/HANDOFF.md (本记录)

---
## test-v185 — tickflow 当天快速路由修复 (2026-07-21)

### 背景
`_fetch_tickflow_daily` 有快速路由: `start_date >= _tdy` 时跳过免费版直走 API key quotes。
但 `update_daily` 的 `batch_start` 取 `min(MAX(date))` 永远是历史日期,
导致快速路由死代码 — 当天数据实际走「免费版拉全量历史 + API key 补充」的低效路径。

### 变更

**1. store.py L1204-1209 — update_daily 当天快速路由覆写**
- 新增判断: gap 分析结果只有 stale_recent(无 missing/stale) 时,
  覆写 `start` 为 `datetime.today()`
- 效果: `batch_start` → 今天 → `to_compact(batch_start) >= to_compact(_tdy)` → 触发快速路由

**2. store.py L638 — _fetch_tickflow_daily 日期比较归一化**
- `start_date >= _tdy` → `to_compact(start_date) >= to_compact(_tdy)`
- 防止 YYYYMMDD vs YYYY-MM-DD 格式不匹配导致误判
- 来源: `to_compact` 已通过 import 可用 (L10)

### 影响
- 调度器 19:00 `update_daily()` (无参) → gap 分析 → 全部 stale_recent → `start=today`
  → tickflow 直接走 API key quotes, 跳过免费版冗余拉取
- 历史回填(含 missing/stale): 不受影响, `start` 不覆写
- 手动 `update_daily(start='2026-07-21')`: 已通过 L1229 逻辑生效, 不受影响

### 涉及文件
- quant/data/store.py (2处修改)
- web/app.py (VERSION → test-v185)
- docs/handoffs/HANDOFF.md (本记录)

---
## test-v186 — 审计报告 6 Bug 修复 (2026-07-21)

### 审查来源
`docs/reports/comprehensive-audit-2026-07-21.md` — 26 Bug 全量审查,
经逐条验证确认 C2/C3/H1/H2/H5/H8/C6 为真实 Bug (C1/C5 为误报)。

### 修复清单

| 编号 | 文件 | 问题 | 修改 |
|------|------|------|------|
| C2 | quant/risk/covariance.py:98 | LW收缩公式 pi_mat *= T/(T-1)³ 低估~57倍 | → pi_mat /= T (LW 2004 eq.17) |
| C3 | quant/benchmark/tracker.py:172-173 | 累积收益用 strat_cum(从未更新) 始终0% | → 改用 s_eq/b_eq |
| H1 | quant/data/store.py:1466 | trade_date = trade_date 无操作 | → 注释移除 |
| H2 | scripts/run_task.sh:81 | while true: (Python语法错误) | → while True: |
| H5 | quant/optimizer/kelly.py:73 | alpha.var()(截面方差~1.0)替代收益率方差(~0.0004) | → DEFAULT_RETURN_VAR=0.0004 |
| C6 | quant/execution/stop_loss.py:109 | tp1_hit未写回position dict | → p["_tp1_hit"]=True |
| H8 | quant/execution/stop_loss.py:101 | peak未写回position dict | → p["_peak"]=peak |

### 涉及文件
- quant/risk/covariance.py (1行)
- quant/benchmark/tracker.py (2行)
- quant/data/store.py (1行注释)
- scripts/run_task.sh (1行)
- quant/optimizer/kelly.py (3行)
- quant/execution/stop_loss.py (2行新增)
- web/app.py (VERSION → test-v186)
- docs/handoffs/HANDOFF.md (本记录)

---
## test-v187 — 审计报告 HIGH 级 4 Bug 修复 (2026-07-21)

| 编号 | 文件 | 问题 | 修改 |
|------|------|------|------|
| H10 | quant/risk/var.py:239 | start="2026-01-01" 硬编码, 2027年起崩溃 | → 动态计算 `today - timedelta(days=365)` |
| H9 | quant/optimizer/rebalance.py:146 | 现金分配未按alpha排序, 低alpha先占资金 | → `buy_orders.sort(alpha)` 降序 |
| H4 | quant/monitor/notify.py:20-21 | _telegram_token() 硬编码返回 "" | → `_require_cfg("monitor.telegram_bot_token")` |
| H7 | quant/execution/engine.py:142 | get_last_buy_price(LIFO) → PnL不准确 | → 新增 `get_average_cost`(FIFO), engine改用之 |

### 涉及文件
- quant/risk/var.py (3行)
- quant/optimizer/rebalance.py (3行)
- quant/monitor/notify.py (3行)
- quant/data/trade_repo.py (+15行 get_average_cost)
- quant/execution/engine.py (3行)
- web/app.py (VERSION → test-v187)
- docs/handoffs/HANDOFF.md (本记录)

---
## test-v188 — 审计报告 MEDIUM 级 3 Bug 修复 (2026-07-21)

| 编号 | 文件 | 问题 | 修改 |
|------|------|------|------|
| M8 | quant/regime/detector.py:21 | HMM标签 1:"bear" 应为 "sideways" | → {0:"bull", 1:"sideways", 2:"bear"} |
| M1 | quant/factor/compute/_primitives.py:386 | turnover_change 映射到错误的 _turnover_reversal | → 移除映射,标注TBD |
| M2 | quant/factor/compute/_primitives.py:400 | abn_turnover shortcut与 _alternative.py OLS版冲突 | → 移除shortcut,回退到完整OLS |

### 审计报告误报确认

| 编号 | 判定 | 原因 |
|------|------|------|
| C1 | 不属实 | config.yaml 可正常 parse, 当时已修复 |
| C4 | 不属实 | to_compact 已在 benchmark.py:13 import |
| C5 | 不属实 | portfolio_value 是函数参数, 正确定义 |
| M9 | 不属实 | startswith(("4","8","92")) 用 "92" 非 "9", 900xxx 不匹配 |

### 涉及文件
- quant/regime/detector.py (1行)
- quant/factor/compute/_primitives.py (2行注释替换)
- web/app.py (VERSION → test-v188)
- docs/handoffs/HANDOFF.md (本记录)

---
## test-v189 — 审计报告 M3/M4/M5/M6/M7/M10 修复 (2026-07-21)

| 编号 | 文件 | 问题 | 修改 |
|------|------|------|------|
| M3 | web/app.py + trade_repo.py | index() raw sqlite3.connect 绕过 TradeRepo | → +get_open_position_cost(), index() 调用之 |
| M4 | web/static/app.js:405-412 | renderRiskExposure 引用不存在的 rd.var/rd.cvar | → 使用 rd.summary.var_95_pct 等真实字段 |
| M5 | web/static/app.js:304,348 | Plotly colorscale 用 CSS 变量字符串('var(--up)')无法解析 | → getComputedStyle() 解析为实际色值 |
| M6 | quant/monitor/report.py:59 | unrealized始终为0,从未计算持仓盈亏 | → 从 daily 表取最新收盘价估算 |
| M7 | quant/monitor/alerts.py:41-51 | 检查从未写入的 last_daily_sync 字段 | → 直接查 daily 表 MAX(date) |
| M10 | quant/risk/constraints.py:108 | docstring声称过滤"行业暴露上限"但从未检查 | → docstring 修正,注明调用方单独检查 |

### 涉及文件
- quant/data/trade_repo.py (+17行 get_open_position_cost)
- web/app.py (index() 用 TradeRepo, VERSION→test-v189)
- web/static/app.js (M4+M5 前端修复)
- quant/monitor/report.py (+15行 unrealized PnL)
- quant/monitor/alerts.py (M7 重写为daily表直接查询)
- quant/risk/constraints.py (M10 docstring)
- docs/handoffs/HANDOFF.md (本记录)

---
## test-v190 — 审计报告 H3/H6 修复: 回撤告警 + 权重裁剪收敛 (2026-07-21)

### H6 — 权重裁剪不收敛

| 位置 | 旧代码 | 新代码 |
|------|--------|--------|
| calibrate_risk_aversion L95 | `np.minimum(w, max_single) + /sum()` | `_iterative_clip(w, max_single)` |
| _score_weighted_rounding L338 | 同上 | 同上 |
| _mean_variance_lot L379 | 同上 | 同上 |
| _risk_parity L418 | `w.clip + /sum()` | `_iterative_clip(w, max_single)` |

新增 `_iterative_clip()` 函数: 迭代裁剪超限权重→重归一化, 20 步收敛, De Prado (2019) Ch.3。

### H3 — 回撤告警用累计收益替代 peak-to-trough (方案 B)

**trade_repo.py**:
- 新增 `daily_equity` 表: date, cash, position_value, total_equity, drawdown_pct
- 新增 `record_daily_equity()`: 计算 peak-to-trough 回撤并写入
- 新增 `get_max_drawdown(lookback_days=60)`: 查询 60 日窗口最大回撤

**alerts.py**: Rule 1 从 `total_pnl/capital`(累计收益) 改为 `TradeRepo().get_max_drawdown()`

**state_broker.py**: 每次 `_update` 后调用 `record_daily_equity()` 写入快照

### 涉及文件
- quant/optimizer/portfolio.py (+17行 _iterative_clip + 4处替换)
- quant/data/trade_repo.py (+47行 table + 2 methods)
- quant/monitor/alerts.py (Rule 1 重写)
- web/state_broker.py (+4行 快照写入)
- web/app.py (VERSION → test-v190)
- docs/handoffs/HANDOFF.md (本记录)

---
## test-v192 — pinv 伪逆回退 + VaR 权重确认 (2026-07-22)

### 背景
审计报告 (comprehensive-audit-2026-07-21.md) 列出两项待修改:
1. VaR计算: 仅用等权假设做快速估算 → 应使用优化器输出权重
2. 均值-方差优化: np.linalg.inv 对奇异矩阵脆弱 → 应增加 pinv 伪逆回退

### 变更

**1. VaR 权重** — 审计确认无需修改
- `var.py:update_daily_risk()` (L237-241): weights 来自实际持仓市值 (price × shares / total)
- `monitor.py` (L150-157): weights 来自实际持仓数量
- 两处均已使用 real portfolio weights, 非等权假设 → 审计误报, 无需改动

**2. pinv 伪逆回退** — portfolio.py 2处修复
- `calibrate_risk_aversion()` L81-84: LinAlgError → 改用 `np.linalg.pinv(Sigma)` 而非返回保守默认值 2.0
- `_mean_variance_lot()` L394-405: 同样 LinAlgError → pinv 回退, 避免降级为等权
- 来源: Ledoit & Wolf (2004) 对病态协方差的稳健处理建议

### 涉及文件
- quant/optimizer/portfolio.py (pinv fallback ×2)
- web/app.py (VERSION → test-v192)
- docs/handoffs/HANDOFF.md (本记录)

---
## test-v193 — task_runs 去重: monitor 防膨胀 (2026-07-22)

### 背景
task_runs 表 74,382 行中 monitor 占 74,184 行 (99.7%)。根因: monitor 进程
频繁重启 (orchestrator 每周期检查), 每次 `start()` 无条件 INSERT 新行。
6 天产生 7.4 万条重复 OK 记录。

### 变更
1. **task_log.py — start() 新增 `dedup` 参数**
   - `dedup=True` 时先 DELETE 同 task_name+date 旧行, 再 INSERT
   - 默认 `False` 保持向后兼容
   - 设计: 每天每任务最多一行, finish() 复用同一行

2. **monitor.py — 调用处传入 `dedup=True`**
   - `_tk_start("monitor", today, dedup=True)`
   - 效果: monitor 每天最多 1 条 task_runs 记录

### 涉及文件
- quant/scheduler/task_log.py (start() dedup param)
- quant/scheduler/monitor.py (1行 dedup=True)
- web/app.py (VERSION → test-v193)
- docs/handoffs/HANDOFF.md (本记录)
