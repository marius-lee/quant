# HANDOFF — 盈迹 (quant) 项目变更日志

> **修改前**: `rg "关键词" HANDOFF.md HYPOTHESES.md docs/adr/` 三文件联动搜索，
> 避免重复踩坑、重新讨论已否决方案、遗漏已有设计。




## 2026-07-14#28: R1-R4 交易执行与归因深度改进

### 触发
报告第三节 (3.2-3.4) 的 4 项遗留缺口.

### R1: compute_trades() 组合层面约束优化

**文件**: `optimizer/rebalance.py` (~40 行新增/修改)

**变更**:
- 函数签名新增 `alpha_scores` 和 `max_trades_per_day` 参数
- 换手率超限时, 不再均匀缩放 → 改为按 alpha 得分优先级保留/丢弃:
  - 买入: 高 alpha 优先保留, 低 alpha 先丢弃
  - 卖出: 低 alpha 优先保留, 高 alpha 先丢弃
- 现金不足时, 买单按成本从低到高排序 (已有逻辑优化)
- 新增单日交易笔数限制: >max_trades_per_day → 保留交易金额最大的 N 笔

**原则**: 约束触发时, 保留最有 alpha 贡献的交易 → 组合整体质量最高

### R2: 交易频率监控

**文件**: `quant/scheduler/monitor.py` (P6-e, ~25 行新增)

**变更**: 盘中 30s 循环新增:
- 当日交易笔数 > `max_trades_per_day` → 告警
- 当日换手率 (成交额/总资产) > `max_daily_turnover_pct` → 告警

### R3: 换手率归因

**文件**: `quant/scheduler/attribution.py` (~30 行新增)

**变更**: 15:30 归因流程新增:
- 计算当日换手率 = buy/sell 总成交额 / 组合资产
- 换手效率 = PnL(bps) / 换手率(%)
- 换手率 > 50% → 高换手告警 (建议加长调仓间隔)

### R4: 信号衰减归因

**文件**: `quant/scheduler/attribution.py` (~30 行新增)

**变更**: 15:30 归因流程新增:
- 对比 `daily_signals` 中的信号价格 vs 实际执行价
- 计算平均滑点 (avg execution slip %)
- 滑点 > 1% → 告警 (执行时机或报价质量问题)

### 新增 config.yaml 键
- `monitor.max_trades_per_day: 50` (R2)
- `monitor.max_daily_turnover_pct: 0.50` (R2)

### 修改文件
- `optimizer/rebalance.py` (R1: compute_trades 签名+优先级逻辑)
- `quant/scheduler/monitor.py` (R2: P6-e 交易频率监控)
- `quant/scheduler/attribution.py` (R3+R4: 换手率归因+信号衰减归因)
- `config/config.yaml` (R2: 2 个键)

### 15:30 归因日志新增
- `R3 turnover: X% turnover, PnL=+Y (Zbps), efficiency=W bps/1% turnover`
- `R4 signal decay: avg execution slip +X% across N buys`

### 验证
- 4 个文件 Python 语法检查全部通过
- config.yaml YAML 解析 + 新键值读取通过

## 2026-07-14#27: G1-G4 实盘交易流程改进 — 业界标准全覆盖

### 触发
2026-07-14#26 的 P1-P6 落地后, 报告第二节 2.3 关键差距 G1-G4 待实现.

### G1: 在线 Walk-Forward OOS 验证

**新文件**: `quant/scheduler/oos_verify.py` (~110 行)

**逻辑**: 每日 15:30 归因流程中执行:
- 用最近 60 日 IS + 10 日 OOS 做 expanding-window IC 对比
- IS day [T-70, T-10] 计算因子 IC, OOS day [T-9, T] 验证
- 各因子 IS_IC → OOS_IC 衰减 > 50% (明汯标准) → 告警
- 不降级, 仅人工审查

**对标**: 明汯 rolling OOS IC 跟踪

### G2: 因子拥挤度检测 — 截面相关性矩阵

**修改**: `evaluation/phase5_monitor.py` — `_check_crowding()` 完全重写 (~60 行)

**变更**: 从 IC 符号一致性代理 → 真正的 pairwise Pearson 相关性矩阵:
- 加载最近交易日所有 using 因子的截面因子值 (500 只股票)
- 计算 N×N Pearson 相关性矩阵
- 标记 |r| > 0.7 的高相关对
- 统计各因子的高相关邻居数 → 拥挤风险排名 (低/中/高)

**来源**: MSCI (2018) "Crowd Control"; Lee (2025) 双曲线衰减模型

### G3: DSR / MinTRL 数学模块

**新文件**: `evaluation/deflated_sharpe.py` (~170 行)

**函数**:
- `probabilistic_sharpe_ratio()` — De Prado Eq.7.2: PSR 概率
- `expected_max_sr()` — De Prado Eq.7.1: 多重试验下的期望最大 SR
- `deflated_sharpe_ratio()` — Bailey & Lopez de Prado (2014): DSR
- `min_track_record_length()` — De Prado Eq.7.3: 最小回测长度
- `compute_dsr_for_strategy()` — 便捷函数, 从日收益序列计算 DSR+MinTRL

**参数**: A股典型 skewness=-0.5, kurtosis=8.0

### G4: 因子 PnL 归因

**新文件**: `monitor/factor_attribution.py` (~160 行)

**方法**: 因子暴露 × IC (因子收益率) = 因子边际 PnL 贡献
- `factor_pnl_attribution(positions, date)` → 持仓的因子暴露 + IC → 贡献 bps
- 方向标注: long winner / long loser / short winner / short loser / neutral
- `factor_attribution_summary()` → Markdown 格式化输出

**来源**: Grinold & Kahn (1999) Ch.7; Barra Risk Model Handbook

### 集成点

`quant/scheduler/attribution.py` 15:30 流程新增三个调用块:
- G1: `run_oos_check(today)` — OOS walk-forward 告警
- G3: `compute_dsr_for_strategy()` — DSR/MinTRL 日志
- G4: `factor_pnl_attribution(positions, today)` — 因子 PnL 日志

### 新增 config.yaml 键
- `oos_verify.train_window_days: 60` (G1)
- `oos_verify.test_window_days: 10` (G1)
- `oos_verify.decay_warn_threshold: 0.5` (G1)

### 修改文件
- `quant/scheduler/oos_verify.py` (新增, G1)
- `evaluation/deflated_sharpe.py` (新增, G3)
- `monitor/factor_attribution.py` (新增, G4)
- `evaluation/phase5_monitor.py` (G2: _check_crowding 重写)
- `quant/scheduler/attribution.py` (G1+G3+G4 集成 ~50 行)
- `config/config.yaml` (G1: oos_verify 段)

### 关键行为变化
- 15:30 归因日志出现 "G1 OOS walk-forward"、"G3 DSR"、"G4 factor PnL" 行
- 因子拥挤度报告从 IC 符号比例 → 真正的截面相关性矩阵
- 首次计算 DSR (Deflated Sharpe Ratio) 和 MinTRL

### 验证
- 7 个文件 Python 语法检查全部通过
- config.yaml YAML 解析 + 新键值读取验证通过

## 2026-07-14#26: P1-P6 实盘模拟交易流程改进 — 业界标准对齐

### 触发
docs/reports/实盘模拟交易操作流程分析_2026-07-14.md — 全量代码审计, 6 项改进建议.

### P1: monitoring 因子不参与实盘交易

**变更**: `factor/compute/_registry.py:15` — `_resolve_statuses("using")` 返回值从 `('active', 'monitoring')` 改为 `('active',)`

**原因**: monitoring 因子是已被判定为 IC 衰减的因子（质量存疑）。业界头部机构（明汯、衍复）明确区分 production（参与交易）和 observation（仅观察）。monitoring 期内应停止交易、仅做观察，确认 IC 恢复后通过 P2 升回逻辑重新启用。

**影响**: 08:30 信号生成仅加载 active 因子。monitoring 因子仅在 15:30 归因中被观察。盘前信号不再被衰减因子污染。

### P2: 因子自动升回机制 (monitoring→active)

**变更**: `quant/scheduler/attribution.py` — 新增 ~40 行升回逻辑

**逻辑**:
- 每日 15:30, 对 monitoring 因子检查: 若当日未触发 IC 衰减, 检查快照历史中连续稳定天数
- 连续 `promotion_stability_days`(5) 天 IC 稳定（与滚动均值偏差 < 30%）
- → 自动升级回 active

**原因**: 原有代码只有降级路径 (active→monitoring→retired), 无升回机制。monitoring 因子即使 IC 恢复也永远回不到 active, 与因子生命周期设计意图矛盾。

### P3: 轻量级在线 OOS IC 验证

**变更**: `quant/scheduler/attribution.py` — 新增 ~15 行 OOS 检查

**逻辑**: 对比当日 IC (OOS) vs 滚动窗口均值 (IS), 衰减率 < `oos_warn_threshold`(0.5) → 告警日志, 不自动降级

**原因**: 离线评估有完整 CPCV+PBO (Phase 3), 但实盘无在线 OOS。业界头部机构每日盘后做 forward performance tracking。此轻量级方案对标明汯做法 (rolling OOS).

### P4: IC 衰减窗口 5→20 天

**变更**: `config/config.yaml` — `attribution.ic_rolling_window: 5` → `20`

**原因**: A 股日频因子 IC 波动大 (中位数波动率 ~0.05), 5 日窗口误判率较高。业界 (明汯 20-60 日, 华泰 24 期) 建议 20 日窗口。

### P5: Brinson 归因基准从等权改为市值加权

**变更**: `quant/scheduler/attribution.py` — 基准权重从 `1/N` 改为按 `daily_valuation.market_cap` 按行业汇总的市值权重。市值数据缺失时退化为等权 (非业务逻辑 fallback, 仅数据不完整时的归因退路).

**原因**: Brinson (1985) 原始论文用市值加权基准, 业界应用 (沪深 300 行业权重) 均为市值加权。

### P6: 盘中监控补充 — 集中度 + VaR + 流动性

**变更**: `quant/scheduler/monitor.py` — 在盘中 30s 循环中新增三项检查:

- **P6-b 单票+单行业集中度**: 单票仓位 > 15% 或行业 > 40% → 告警
- **P6-c VaR 实时估算**: 用最近 60 日日线计算 parametric VaR (95% 置信), >3% → 告警
- **P6-d 流动性过滤器**: 20 日均成交额 < 3000 万 → 告警

**原因**: 原有盘中监控仅有回撤/熔断/止盈止损。业界标准 (Grinold & Kahn) 要求独立风控 daemon 覆盖集中度、VaR、流动性三项。

### 新增 config.yaml 键
- `attribution.promotion_stability_days: 5` (P2)
- `attribution.oos_warn_threshold: 0.5` (P3)
- `attribution.ic_rolling_window: 20` (P4, 从 5 修改)
- `monitor.max_single_concentration: 0.15` (P6)
- `monitor.max_sector_concentration: 0.40` (P6)
- `monitor.min_daily_turnover_amount: 30000000` (P6)
- `monitor.var_confidence: 0.95` (P6)

### 修改文件
- `factor/compute/_registry.py` (P1: 1 行变更, using→active only)
- `quant/scheduler/attribution.py` (P2+P3+P5: ~70 行新增逻辑, Brinson 市值加权重写)
- `quant/scheduler/monitor.py` (P6: ~80 行新增集中度+VaR+流动性检查)
- `config/config.yaml` (P2+P3+P4+P6: 7 个键新增/修改)

### 验证
- 4 个文件 Python 语法检查全部通过
- P1 `_resolve_statuses("using")` 返回值验证通过
- config.yaml YAML 解析 + 新键值读取验证通过

### 关联
- docs/reports/实盘模拟交易操作流程分析_2026-07-14.md (全量分析报告)
- ADR 026 (五阶段评估标准)
- HANDOFF #2026-07-12#20 (因子状态同步闭环)

## 2026-07-12#15: 评估管线加 trace_id

**变更**: 5 个 Phase 文件, 每 Phase 入口函数生成 trace_id 并注入日志。

**原因**: 正式评估管线各 Phase 是独立 Python 子进程, 日志无 trace_id 无法关联同一次评估执行。

## 2026-07-13#22: 因子窗口驱动数据加载 + compute_ztd SQL fallback 移除

### 背景
- `_thread_compute_chunk` 硬编码 `days=365` 作为数据加载窗口，隐含假设 ztd 需要 250 交易日
- `compute_ztd` 在 `_ztd_cache` 未命中时静默走 SQL 查询 — 隐性 fallback，掩盖调用方遗漏 preload_ztd_cache 的错误
- `ic.py`、`evaluation/parallel.py`、`pipeline.py` 的数据加载窗口各自独立计算，与因子实际需求无显式关联

### 变更

**1. 新建 `factor/windows.py`** (25行)
- `max_factor_calendar_days(factor_names)` — 从 `_PRICE_FN_MAP` 提取每个因子的声明 window，取最大值 × 1.5
- 默认下限 60 交易日 (90 日历日)
- `factor_names=None` → 取全部已注册因子

**2. `compute_ztd` 移除 SQL fallback** (factor/compute/price/_alternative.py)
- 删除 34 行 SQL 查询代码
- 缓存未命中 → `raise RuntimeError("ztd cache miss for {date}: preload_ztd_cache() must be called before compute_ztd")`
- 调用方忘掉预加载 → 立刻炸，fail-fast 立即可定位

**3. 四处数据加载点统一为 `max(传入天数, 因子最小窗口)`**

| 文件 | 旧值 | 新值 |
|------|------|------|
| `factor/stats_cache.py:140` | `365` 硬编码 | `max(max_factor_calendar_days(factor_names), lookback * 1.5)` |
| `factor/ic.py:92` | `lookback * 2` | `max(lookback * 2, max_factor_calendar_days(factor_names))` |
| `evaluation/parallel.py:51` | `lookback * 2` | `max(lookback * 2, max_factor_calendar_days(factor_names))` |
| `pipeline.py:102` | `_ecfg("data.lookback_days")` | `max(_ecfg("data.lookback_days"), max_factor_calendar_days(None))` |

**4. pipeline.py 实盘路径补 preload_ztd_cache** (pipeline.py:176-180)
- `generate_signals` 在 Step 3 调用 `compute_all_factors` 前填充 ztd 缓存
- 四入口全覆盖: backtest/loop.py, stats_cache.py, ic.py, pipeline.py

**5. backtest/loop.py 缩进修复** (line 195-199)
- 3 空格 → 4 空格，消除 IndentationError

### 原因
- 硬编码 365 在 ztd 退役后浪费加载，在新因子窗口更大时不够
- 隐性 SQL fallback 违反"零 fallback"原则
- 代码应显式声明约束：`max(传入天数, 因子需求)` → 读代码的人不需要跳转到其他文件理解"够不够"

### 否决
- 解析函数签名 `__defaults__` 取窗口 (脆弱：参数名不统一 `window`/`night_window`/`intraday_window`)
- 保持 SQL fallback (否认：掩盖错误，调用方永远不知道自己忘了预加载)
- 为 `_FUNDAMENTAL_FN_MAP` 补 window 字段 (否认：基本面因子不依赖日线窗口)

### 验证
- 全部 import 检查通过 (stats_cache, ic, pipeline, windows)
- `max_factor_calendar_days(None)` → 378 (momentum_252d 驱动)
- `max_factor_calendar_days(['ztd'])` → 375

### 关联
- HYPOTHESES: 因子窗口驱动数据加载设计讨论
- HANDOFF 2026-07-13 上午: ztd 预计算缓存


现每个 Phase 启动时生成 `tid = uuid.uuid4().hex[:12]`, 通过 `set_trace_id(tid)` 注入,
所有后续 `logger.info/error/warning` 自动带 `[tid]` 前缀。

**影响范围**:
- `evaluation/phase1_data.py`: +2 行 (import set_trace_id, tid 生成)
- `evaluation/phase2_single.py`: +2 行
- `evaluation/phase3_oos.py`: +2 行
- `evaluation/phase4_costs.py`: +2 行
- `evaluation/phase5_monitor.py`: +3 行 (新增 logger 导入)

**验证**: 67 tests 通过

---

## 2026-07-12#14: 股票数量统一 — factor.evaluation.n_symbols 500→800

**变更**: `config/config.yaml` `factor.evaluation.n_symbols: 500` → `800`

**原因**: 诊断快筛 (backtest) 用 800 股, 正式评估用 500 股, 同一因子 IC 统计不可对比,
两级筛选逻辑链条断裂。统一到 800 (对标中证 800, A 股量化标准基准)。

**影响**: `stats_cache.py`, `phase2_single.py` 等所有消费 `factor.evaluation.n_symbols` 的代码自动生效, 无需改动。

---

## 2026-07-12#16: 修复 compute_ic_from_values 中 DataFrame bool 判断崩溃

**Bug**: Phase 2 `compute_factor_stats` → `compute_ic_from_values` 在 `factor/ic.py:207` 
使用 `forward_5d or pd.DataFrame()` 模式。pandas 3.x 禁止 DataFrame 的隐式 `__bool__` 
转换, 直接抛 `ValueError: The truth value of a DataFrame is ambiguous`。

**根因**: `forward_5d = close.pct_change(5).shift(-5)` 永远返回 DataFrame (不会返回 None), 
`or pd.DataFrame()` 是历史遗留的 None fallback, 从未触发且语法在 pandas 3.x 下无效。

**修复**: 移除 `or pd.DataFrame()` (共 2 处: forward_5d, forward_20d)。下游已有 
`if fwd_df.empty` 检查, 无需额外 fallback。

**文件**: `factor/ic.py:207-209`

**验证**: 67 tests 通过

---

## 2026-07-12#17: 正式评估管线日志统一 — 去除 print(), 全量 logger.info

**变更**: Phase 2 和 Phase 5 中的 `print()` 调用改为 `logger.info`。

**原因**: Phase 2 用 `print("=== PASSED ===")` 输出关键结果到 stdout, 
shell 重定向到文件后若 Phase 2 中途崩溃 (如 #16 的 ValueError), 
stdout 缓冲区丢失, 无法分析失败原因。统一使用 `logger.info` 写入 app.log, 
与回测诊断模块格式一致, agent 可以 grep 一个模块名看到全貌。

**变更文件**:
- `evaluation/phase2_single.py`: `print("=== PASSED ===")` → `logger.info`
- `evaluation/phase5_monitor.py`: `print("Phase 5 report written...")` → `logger.info`
- Phase 6 的 `print(json.dumps(...))` 保留 (向用户输出回测结果, 合理用途)

**原则**: 所有 eval 管线结果输出 → `logger.info`, 不得使用 `print()`。

**验证**: 67 tests 通过

---

## 2026-07-12#18: IC 计算统一 — compute_ic() 唯一入口

**变更**: 4 个文件, IC 计算从 2 个独立函数合并为 1 个统一入口。

### 之前的问题
- `factor/ic.py` 有两个公开函数: `compute_ic` (backtest 用) 和 `compute_ic_from_values` (Phase 2 用)
- 两套独立实现各自取数据、算因子、做 Spearman 相关, 同一因子算出不同 IC 值
- 诊断阈值 0.1 (diagnostics_min_icir) vs Phase 2 阈值 0.5 (min_icir) 无法对齐

### 新架构
```
compute_ic()  — 唯一公开入口
  ├─ Mode A: factor_names + date + symbols → 取数据 → 算因子 → 统一 IC 计算
  └─ Mode B: factor_values + forward_1d → 直接用预计算值 → 统一 IC 计算
       │
       └─ _compute_ic_from_values() — 私有, 所有 Spearman 相关集中在此
            └─ _spearman_ic() — 私有, 单次横截面 Spearman 计算
```

### 变更文件
| 文件 | 变更 |
|---|---|
| `factor/ic.py` | 完全重写: `compute_ic` 统一入口 (Mode A + Mode B), `_compute_ic_from_values` 私有化, 新增 `_spearman_ic` 共享 |
| `factor/stats_cache.py` | `compute_ic_from_values(...)` → `compute_ic(factor_values=..., ...)` |
| `backtest/loop.py` | `_current_ic_map` 从 `compute_ic(...)["ic_map"]` 提取 |
| `backtest/diagnostics.py` | `result.get(name)` → `result["ic_means"].get(name)` |

### 统一返回值
```python
{
    "ic_means":   {name: float},    # 平均 IC
    "ic_irs":     {name: float},    # ICIR
    "ic_series":  {name: {date: float}},  # IC 时间序列
    "ic_decay":   {name: {"1d","5d","20d": float}},  # 多周期衰减
    "n_valid":    int,              # 有效因子数
    "n_positive": int,              # IC>0 因子数
    "ic_map":     {name: {ic_mean, ic_ir, weight, n_obs}},  # back compat
}
```

**验证**: 67 tests 通过

---

## 2026-07-12#19: ICIR 阈值对齐 A 股业界标准

**变更**: `config/config.yaml` 两个 ICIR 阈值。

### 业界标准 (附在 config 注释中)
| 来源 | ICIR 阈值 | 场景 |
|---|---|---|
| Grinold & Kahn (1999) | 0.5-1.0 | 美股多因子 |
| Qian/Hua/Sorensen (2007) | 0.25-0.5 | 单因子 |
| WorldQuant 101 | 0.3 | 全球 |
| 国内平台 (聚宽/米筐/BigQuant) | 0.2-0.3 | A股 |
| AQR (Asness et al.) | 0.3 | 全球多资产 |

### 变更
| 参数 | 旧值 | 新值 | 依据 |
|---|---|---|---|
| `min_icir` | 0.5 | **0.25** | 国内平台 0.2-0.3 中位; 0.5 对 A 股过严, fund_flow_3m (ICIR=0.28) 都能过 |
| `diagnostics_min_icir` | 0.1 | **0.15** | 统一 IC 计算后同一函数产出, 可略收紧预筛; 仍低于 Phase 2 的 0.25 |

**验证**: validate OK

---

## 2026-07-12#20: 因子状态同步闭环 — sync_factor_status() + rejected 批量重置

**变更**: 4 个文件, 补齐旧系统 eval_stepwise.sh 丢失的状态更新逻辑。

### 问题
旧 eval_stepwise.sh 评估完直接改 factor_registry.status (active/rejected)。
重构拆成 eval_standard.sh + Phase Python 模块后, 状态更新逻辑完全丢失。
38 个 rejected 是旧脚本最后一跑的僵尸状态, 之后评估管线跑再多轮也不会改任何因子状态。

### 方案
新加 sync_factor_status() 函数在 evaluation/phase5_monitor.py, 集中式状态同步:
从 evaluation_runs 读 Phase 2/3/4 结果 → 一次性更新 factor_registry.status。

状态转移:
- Phase 2 失败 → status='rejected', reason="Phase 2: IC/ICIR/t/half-life..."
- Phase 3 失败 → status='rejected', reason="Phase 3: CPCV OOS_ICIR<0..."
- Phase 4 失败 → status='rejected', reason="Phase 4: net-of-costs..."
- 全部通过 → status='active', reason="passed Phase 2+3+4"
- 不碰已有 active 因子

### 入口
eval_standard.sh 在 Phase 4 完成后自动调用 sync_factor_status() (Phase 5b)。
替代原来的手动 "下一步: 检查 evaluation_runs 结果..." 注释。

### 重置
38 个旧 rejected (migration 残留) → candidate, 下一轮正式评估重新检验。
当前分布: candidate=39, retired=22, registered=7, rejected=1, active=1

### 文件变更
| 文件 | 变更 |
|---|---|
| `evaluation/phase5_monitor.py` | +80 行: sync_factor_status() |
| `scripts/eval_standard.sh` | +12 行: Phase 5b 调用; -2 行: 删手动 TODO |
| `factor/ic.py` | (#18 残留修复) |
| `factor/stats_cache.py` | (#18 残留修复) |

**验证**: 67 tests 通过

---

## 2026-07-12#13: Phase 2 残留临时文件写删除

**Bug**: Phase 2 在 #11 修改后仍然保留了 `with open(output_json, 'w') as f: json.dump(...)` 
和 `"ic_series": stats.get("ic_series", {})` — 临时文件被删除后每次 Phase 2 运行会重建它,
且 150KB 的 ic_series 仍写入磁盘。违反"纯数据库 + 删除全部临时文件"原则。

**修复**: 
- 删除 `with open(output_json, 'w')` 行
- 从 result dict 移除 `ic_series` key (仅用于临时文件, DB 已 pop 掉)

**当前状态**: Phase 1-5 全部纯 DB 读写, 零临时文件。Phase 1/3 的函数签名仍保留旧默认值 
(`/tmp/_eval_phase*.json`) 但函数体不再使用, 属死代码, 后续可清理。

**验证**: 67 tests 通过, Phase 1 实测可运行 (db_status=ok, 5493 stocks)。

---

## 2026-07-12#12: 两步回测架构落地 — 诊断结果持久化 + 正式评估预筛

**变更**: 5 个文件改动, config.yaml 新增 3 个参数。

### 架构
```
Step 1: 诊断快筛 (每次回测自动)     Step 2: 正式认证 (手动/周频)
backtest/loop.py → diagnose()       eval_standard.sh → 5-phase pipeline
    │                                    │
    ├─→ evaluation_runs                  ├─→ load_latest("phase1") 健康检查
    │   (phase="diagnostics")            └─→ load_latest("diagnostics") 预筛
    └─→ factor_registry.status_reason       只评估 keep/boost 因子
        (backtesting 因子 only)
```

### 文件变更

**1. backtest/loop.py** (~+35 行)
- 诊断完成后调用 `save_phase("diagnostics", ...)` 写入 evaluation_runs
- 更新 factor_registry.status_reason (只改 registered/candidate/retired 因子,
  不改 active, 不改 status 字段)
- 格式: "diag:boost(ICIR=0.52,PnL=0.15,2026-07-12)"

**2. backtest/diagnostics.py** (~+5 行)
- diagnose() 中 3 个硬编码阈值改为从 config 读:
  ICIR<0.1 → cfg("factor.evaluation.diagnostics_min_icir")
  PnL>0.1  → cfg("factor.evaluation.diagnostics_pnl_threshold")
  PnL<-0.5 → cfg("factor.evaluation.diagnostics_review_threshold")

**3. evaluation/phase2_single.py** (~+35 行)
- 读取 Phase 1 产出 (DB 替代死掉的临时文件):
  load_latest("phase1") → db_status 检查, degraded 直接 abort
- 新增 prefilter_from_diagnostics 参数 (默认 True):
  True → 只评估最近一次诊断的 keep/boost 因子
  False → 全量 (--all mode)
  无诊断数据 → 退化为全量 (冷启动)

**4. scripts/eval_standard.sh** (~+8 行)
- 新增 --all flag 支持: 跳过诊断预筛, 评估全部 backtesting 因子

**5. config/config.yaml** (+3 个参数)
- factor.evaluation.diagnostics_min_icir: 0.1
- factor.evaluation.diagnostics_pnl_threshold: 0.1
- factor.evaluation.diagnostics_review_threshold: -0.5

**验证**: 67 tests 通过, 3 个 config key 验证可访问,
5 个 evaluation/*.py 全部 import OK.

**关联**: ADR 007 (因子评估标准), ADR 029 (四层回测),
HYPOTHESES 两级因子筛选架构, HANDOFF 2026-07-12#11 (评估管线去临时文件)

---

## 2026-07-12#11: 评估管线去临时文件 + 全量 fallback 修复 + evaluation_runs 精简

**变更**: 6 个文件改动, evaluation_runs 清空重建。

### 1. 全量 fallback 修复 (6 处)
- phase2_single.py: `except Exception as _e: logger.warning` -> `logger.error + traceback`
- phase3_oos.py: `except Exception: pass` -> `logger.error + traceback`
- phase3_oos.py: ic_series fallback 新增 try/except + traceback
- phase4_costs.py: `os.path.exists` 文件检查替换为 DB `load_latest`
- phase5_monitor.py: `except Exception: return` -> `logger.error + traceback`

### 2. 纯数据库数据流 (ADR 028 完整落地)
**之前**: Phase 1/2/3 写临时文件, Phase 3/4 从临时文件读, Phase 2 同时写 DB 和文件 (重复)
**之后**: 所有 Phase 读/写 `evaluation_runs` 表, 临时文件全部删除

### 3. evaluation_runs 精简
**根因**: Phase 2 将 `ic_series`(31因子*120天浮点序列) 全量存入 DB, 占 97% 体积 (150KB/行)
**修复**: `save_phase("phase2", slim)` — 写入前 `pop("ic_series")`
Phase 3 需要 IC 序列时自行通过 `compute_factor_stats()` 重算 (仅 passed 候选, 成本低)
**效果**: 每行 ~150KB -> ~3KB, 压缩比 50:1

### 4. 清理
- `/tmp/_eval_phase{1,2,3,4,6}.json` 已删除
- `evaluation_runs` 表 6 行膨胀历史数据已清除 + VACUUM

**验证**: 67 tests 通过, 5 个 evaluation/*.py 全部 import OK, evaluation_runs 表空,
所有临时文件已删除。

**关联**: ADR 026 (五阶段标准), ADR 028 (DB 持久化), HYPOTHESES 两级筛选架构

---

## 2026-07-12#10: 项目全面分析报告归档

**变更**: 新建 docs/项目全面分析报告_2026-07-12.md (305行)
**内容**: 8 个维度的全项目分析:
  1. 项目是什么 — A 股量化选股系统, Grinold & Kahn Fundamental Law
  2. 使用的技术 — Python/SQLite/Flask/scikit-learn/LightGBM/Optuna/hmmlearn
  3. 技术评估 + 应补入技术清单 + 其他可选框架 (Black-Litterman/Fama-French/Barra/Kelly/HMM/RL)
  4. 最重要的文件 (核心业务 18 个 + 基础设施 10 个)
  5. 已实现/待实现功能清单
  6. 架构评价: 整体良好, 无需大重构, 列出 4 项可选优化
  7. 已发现逻辑问题 (11 项, 全部已修复) + 代码质量总结
  8. 算法评价 + 改进建议 (因子正交化/IC衰减权重/自适应窗口/Kelly仓位)
**原因**: 用户要求全面分析项目现状并归档保存
**关联**: docs/项目全面分析报告_2026-07-12.md

## 2026-07-12#3: 全量消除硬编码 fallback

**变更**: 所有参数数值必须来自 config.yaml, 禁止代码内 fallback 默认值。

config.yaml 新增: benchmark.start_date, backtest.universe_turnover_days,
backtest.diagnosis_ic_window, backtest.progress_log_interval,
backtest.min_trading_days, risk.stop_loss_pct

pipeline.py: "2020-01-01"→cfg("data.start_date"), days=7→cfg("backtest.universe_turnover_days"),
"2025-12-01"→cfg("benchmark.start_date"), _ecfg(...,365)→_ecfg("data.lookback_days")
backtest/loop.py: <20→cfg("backtest.min_trading_days"), %60→cfg("backtest.progress_log_interval"),
lookback=120→cfg("backtest.diagnosis_ic_window")
backtest/diagnostics.py: lookback 默认值 120 移除,改为必传参数
execution/cost.py: 4处 cfg(...,fallback) 全部移除

**原因**: 系统禁止硬编码,所有参数数值必须单源(config.yaml)且有来源依据。
**否决**: cfg(key, default) 模式 (隐藏的 fallback 绕过了 fail-fast,难排查配置遗漏)。
**验证**: ast.parse 四文件通过, 67测试通过, 12个 config key 全部正确解析。
**关联**: HANDOFF 2026-07-10 stop_loss_pct 条目 (宣称补到 config 但实际未补,现已真正补上).

## 2026-07-12#2: pipeline.py — 替换 _store_owned 模式

**变更**: `generate_signals()` 和 `execute_signals()` 重构资源管理。

generate_signals 变更:
- 签名新增 `store`, `status_filter`, `suppress_push`, `universe_size`, `db_path` 参数
- `store = DataStore()` → `_store_in = store; store = store or DataStore(db_path=db_path)`
- 移除 4 处错误路径的 `store.close()`, 替换为条件守卫 `if _store_in is None: store.close()`
- `status_filter` 透传至 `compute_all_factors()` 和 `load_ic_map_from_cache()`
- 新增 Step 2.5: `universe_size` 按近 7 日平均成交额截断 universe
- 所有 `post_state()` 增加 `suppress_push` 守卫
- 新增 `results["_factor_values"]` / `results["_alpha_raw"]` 供回测诊断
- Step 2 连接查询后增加 `conn.close()`

execute_signals 变更:
- 签名新增 `prices`, `db_path`, `suppress_push`
- `prices` 提供时跳过 `fetch_quotes()` (回测直接传递历史开盘价)
- `db_path` 透传至 `ExecutionEngine`
- `stop_loss_pct` 默认值 `0.15`

**原因**: `_store_owned` flag 有 3 个风险: 异常泄漏、新增 return 点易漏 close、资源所有权不绑定对象。
**否决**: try/finally 全包裹 (需缩进 150+ 行, diff 风险和合并冲突过大)。
**验证**: ast.parse 通过, 67 测试通过.
**关联**: ADR 029, backtest/loop.py (已预先传递 kwargs).


> **使用规则**: 每次修改前，先 `rg "关键词" HANDOFF.md docs/adr/` 检查是否有过相关决策或失败尝试。
> 每个条目必须包含「否决的方案 + 原因」，防止来回试错。

## 条目格式

```
## YYYY-MM-DD: 简短标题
**变更**: 做了什么
**原因**: 为什么
**否决**: 方案A (原因) | 方案B (原因)
**验证**: 测试结果 / 命令行
**关联**: ADR NNN / 其他条目
```

---

## 2026-07-11: 大文件拆分 — factor/compute.py 二次拆分

**变更**: `factor/compute/price.py` (1908行 → `price/` 子包 3 模块)
**原因**: 模板 10 新增「Python文件 <800 行」约束
**否决**: 每个因子一个文件 (碎片化) | 按行数机械切割 (破环内聚) | 拆 fundamental.py (32 函数内聚高，无收益)
**验证**: 67 tests 通过, `from factor.compute.price import X` 兼容
**关联**: ADR 028

## 2026-07-11: 大文件拆分 — factor/compute.py 首次拆分

**变更**: `factor/compute.py` (3182行 → `factor/compute/` 包 6 模块)
**原因**: token 消耗大, 价量/基本面因子交错分布
**否决**: 单文件保持 (agent 重复读取) | 拆成 10+ 文件 (过度碎片化)
**验证**: 67 tests 通过, `from factor.compute import X` 完全向后兼容
**关联**: ADR 028

## 2026-07-11: 回测 universe 早鸟过滤 — 5176 → 800, 10× 加速

**变更**: `get_universe()` 后按成交额取前 N, N 从 config 读
**原因**: 全量 5176 股回测耗时数小时, 800 股约 5 分钟
**否决**: 实盘路径加过滤 (实盘需全量) | 硬编码 N (违反模板 10 禁止硬编码)
**验证**: 回测 run_backtest('2026-04-01', '2026-07-10', capital=5000) 跑通

## 2026-07-11: 回测数据库隔离修复 — db_path 全链路传递

**变更**: 回测使用独立 benchmark.db, 不污染 market.db
**原因**: 回测 t+1 模拟与生产数据隔离
**否决**: 回测直连 market.db (污染生产数据) | 复制整个 DB (浪费磁盘)
**验证**: 回测读写均落在 benchmark.db, market.db 未被修改

## 2026-07-11: 回测因子状态过滤修复 — backtesting 模式

**变更**: `load_active_*_factors` 支持 `status_filter='backtesting'` → 加载 registered+candidate+retired
**原因**: 回测需评估所有注册因子 (33 个), 不限于生产的 active (1 个)
**否决**: 回测用全量因子 (忽略状态字段的意义) | 新建独立加载函数 (重复逻辑)
**验证**: 回测日志显示 33 factors → 266 stocks

## 2026-07-11: 回测 open 价格查询修复 — 分批 IN 子句

**变更**: `_get_open_prices` IN 子句分批 (每批 500 symbol)
**原因**: 5176 symbol 进 SQL IN 导致部分驱动报错
**否决**: 单次查询全量 (数据库驱动限制) | 逐 symbol 查询 (5000+ 次 DB 调用)
**验证**: 回测不再报 no open prices available

## 2026-07-11: 实盘 pipeline 流程与因子状态规则落地

**变更**: 实盘: status_filter='using' → active+monitoring; 回测: status_filter='backtesting' → registered+candidate+retired
**原因**: 系统规则: using 状态因子用于生产, backtesting 状态因子用于评估
**否决**: 所有场景用同一状态过滤 (混淆实盘和评估)

## 2026-07-10: backtest 命名规则标准化

**变更**: 回测名以 `backtest` 开头+数字递增, 冒烟测试以 `smoke` 开头+数字递增
**原因**: 之前的名称 (verify3/verify4/final) 无法追溯顺序和用途
**否决**: 自由命名 (混乱不可追溯)

## 2026-07-10: stop_loss_pct 硬编码修复

**变更**: `config.yaml` 补回 `stop_loss_pct: 0.15`
**原因**: 系统禁止硬编码, 所有参数必须来自 config
**否决**: 代码内 hardcode (违反模板约束)

## 2026-07-11: zscore min_count 分层 — dense=30 / sparse=10

**变更**: `config.yaml` 中 `zscore_min_count_dense: 30`, `zscore_min_count_sparse: 10`；`_cs_zscore()` 按 `sparse` 参数自动选阈值
**原因**: 回测时基本面因子因有效值不足 min_count(原 50) 被成批丢弃; 基本面天然稀疏(财报覆盖窄), 需更低的 min_count
**否决**: 统一降低到 20 (基本面太低丢信号, 价量太高不过滤噪声) | 按因子逐个配置 (70 个因子逐个调太繁琐)
**分类规则**: 价量因子 `sparse=False`(默认) → dense=30; 基本面因子 `sparse=True` → sparse=10。分类在因子函数内部自行决定, 无中心化名单。
**验证**: 回测 33 因子全部有输出, 不再出现大批 NaN

## 2026-07-12: 四层回测架构 P0 — backtest/diagnostics.py

**变更**: 新增 `backtest/diagnostics.py` (273行), 修改 `pipeline.py` (透传因子数据), 修改 `backtest/loop.py` (集成诊断)
**原因**: 回测需要四层递进 (因子评估→信号合成→组合构建→业绩归因), P0 先落地因子评估和归因两层
**否决**: 仅增强报告不改变架构 (agent 无法自动优化) | 直接上 Optuna (需先有归因)
**验证**: 67 tests通过, 回测返回含 `diagnosis` 字段
**关联**: ADR 029, HYPOTHESES 2026-07-12

## 2026-07-12: diagnostics 三个 bug 修复

**Bug 1**: `loop.py` 中 `targets` 变量在 tracker.record_day 之前使用但之后才赋值 → NameError 被 except 吞掉
**修复**: `targets = signals.get("target_positions", [])` 移到了两个 tracker.record_day 之前

**Bug 2**: `compute_pre_backtest_ic()` 收到空 `[]` → `pre-backtest IC: 33 factors × 0 stocks` → 0 因子有效
**修复**: 从 `_last_signals["_factor_values"]` 提取实际 backtest symbols 传入

**Bug 3**: `diagnostics.py` 中 `cfg("factor.compute.zscore_min_count_sparse", 10)` — 硬编码 fallback `10`
**修复**: 移除 fallback, 该 key 在 config.yaml 已存在

## 2026-07-12: monitor failed: '"'"'capital'"'"' — push_to_web mutated input dict
**Bug**: state_broker.py InProcessBroker.update() called data.pop(_fk, None) removing '"'"'capital'"'"' and other financial keys from the caller'''"'"'s dict. Both pipeline.py and scheduler.py access report['"'"'capital'"'"'] after push_to_web(report).
**Fix**: update() now creates a filtered copy: data = {k: v for k, v in data.items() if k not in _FINANCIAL_KEYS}
**Rejected**: swapping push_to_web/cap order in callers (symptom not cause) | copy.deepcopy at call sites (waste)
**Root cause**: update() should not mutate its argument — a function receiving data for caching has no business modifying the caller'''"'"'s dictionary

## 2026-07-12: 日志终端/文件分离 + 自动错误分析工作流
**变更**: backtest/loop.py 增加 BACKTEST START/END 视觉分隔标记
**工作流**: 用户终端跑完回测后说"跑完了" → agent 自动 grep logs/quant.log 中 ERROR/WARNING → 按 trace_id 展开上下文定位问题
**日志策略**: quant.log 保持 DEBUG 全量 (JSON 结构化), agent 用 grep 过滤分析; 终端保持 INFO+ 显示进度

## 2026-07-12: _smoke.py 测试脚本 key 名修复
**Bug**: metrics 字典中 mdd_pct 不存在（正确是 max_drawdown_pct），n_errors 也在 r 顶层而非 metrics 中
**修复**: m["mdd_pct"] → m["max_drawdown_pct"]，m["n_errors"] → r["errors"]
**验证**: smoke 测试终端输出正常，不再报 KeyError


## 2026-07-12: earnings_upgrade + insider_cluster — data.index to data.columns (P0 bug fix)
**Bug**: 两个因子将 fundamentals 宽表 DataFrame 的 .index (日期) 当作股票代码列表, 导致所有 result[sym] 赋值无效, 返回全 0 序列。同时 except Exception: pass 吞掉错误, dispatch 层无感知。
**Fix**: symbols = list(data.index) → list(data.columns); except Exception: pass → logger.error(traceback)
**Root cause**: 之前 handoff 记录的 fix (data["close"].columns → data.index) 方向错误 — fundamentals 宽表 index=日期, columns=股票, 正确来源是 columns
**Verify**: 67 tests 通过; 下次 smoke 应出现 earnings_upgrade/insider_cluster 的有效值

## 2026-07-12: fundamentals DataFrame 格式确认 + 隐性 fallback 修复
**格式**: get_fundamentals() 返回 DataFrame(index=symbol, columns=[pe,pe_ttm,pb,...])。index 是股票代码，columns 是指标名。没有名为 "symbol" 的列。
**教训**: 上次 handoff 记录的 fix (data["close"].columns → data.index) 方向反了——原始代码 data["close"].columns 找不到 "close" 列导致 KeyError，改 data.index 恰好正确（因为 index 就是 symbol）。之后我误以为 index=日期 columns=symbol，改成 data.columns，导致返回指标名而非股票代码——已回退。
**pipeline.py 隐性 fallback**: 原 `if "symbol" in fundamentals.columns: fundamentals[...]` 因为 "symbol" 不是 column（是 index name），条件永远 False，回测 universe 过滤对 fundamentals 从未生效。每个交易日 fundamental 因子都多算了 5208 而非 800 只股票。
**修复**: `fundamentals["symbol"].isin(...)` → `fundamentals.index.isin(keep_syms)`（直接用 index）
**pipeline.py 隐性 fallback #2**: `except Exception: pass` 吞掉所有过滤异常 → 改为 logger.warning 记录

## 2026-07-12: 全量消除 except Exception: pass — 12 处隐性 fallback 修复

**变更**: 回测业务流程所有 `except Exception: pass/continue` 替换为 `logger.error(traceback)`

_alternative.py (8 处):
- STR 残差: pass → logger.error (回归失败不再静默)
- ABN_TURN 残差: pass → logger.error + 保留降级行为
- limit_down_pool: pass → logger.error + 保留空 DataFrame 降级
- TRCF 单symbol循环: continue → logger.error
- ideal_amplitude 单symbol循环: continue → logger.error
- northbound_streak SQL: pass → logger.error
- short_interest SQL: pass → logger.error
- fund_flow_3m SQL: pass → logger.error
- 新增 `import traceback` 到模块顶部

fundamental.py (2 处):
- OCFP TTM 查询: stderr.write → logger.error (错误进 quant.log)
- OCFP 行业中性化: pass → logger.error

pipeline.py (1 处):
- benchmark 拉取: pass → logger.error

backtest/loop.py (1 处):
- 删除死代码 `LOT_SIZE = 100` (从未使用, pipeline.py 从 config 读)

**原因**: 12 个位置存在 `except Exception: pass` 完全吞掉错误, 因子计算失败/DB查询异常/回归失败 全部静默, 从日志完全不可见。
**验证**: 67 tests 通过; grep "except Exception: pass" 结果为 0。
**关联**: HYPOTHESES 2026-07-12 隐性 fallback 全量审计

## 2026-07-12: optimizer — 恢复固定阈值建仓规则 + weighted→0 安全网

**变更**: `optimizer/portfolio.py` 建仓层级判定从动态阈值恢复为 ARCHITECTURE.md v3.0 原始设计的固定阈值。

config.yaml 新增:
- `optimizer.greedy_cap: 20000` — 低于此资金: 贪心逐手买入 (微型账户集中持仓)
- `optimizer.weighted_cap: 100000` — 低于此资金: 得分配比; 高于此: 均值-方差

optimizer/portfolio.py 变更:
- `_tier()`: 动态阈值 (avg_price × LOT_SIZE × 2) → 固定阈值 (greedy_cap / weighted_cap)
- `__init__()`: 新增 greedy_cap / weighted_cap 从 config 读取, 支持 Config Cascade
- `construct()`: weighted 产出 0 仓位时自动回退到 greedy (安全网)
- `__init__()`: config.get(key, _cfg(key)) — 测试可覆盖, 缺 key 时从全局 config 补

tests/test_portfolio.py: 4 个测试的 capital 值从旧动态阈值调整为固定阈值对齐

**原因**: 动态阈值 `lot_cost * 2` 对 ¥22 均价股票 = ¥4,482, ¥5,000 资本刚过线就进了 weighted tier。weighted tier 把 ¥5,000 除以 max_positions(20) = ¥250/只, 买不到 1 手 → 0 仓位。实际验证: 即使用 ¥20,000, weighted 也只选中了最便宜的股票(按价格而非 alpha 筛选)。固定阈值 ¥20,000 确保微型账户走 greedy(买得分最高的 1-2 只), 符合 ARCHITECTURE.md 原始设计和行业标准。

**否决**: 动态阈值 (按价格选股) | 自适应 n_stocks (仍存在价格偏向) | 纯固定阈值无安全网 (阈值配错→静默空仓)
**验证**: 67 tests 通过; ¥5,000 capital 模拟: greedy 买 2 手得分最高股票, 不再出现 0 仓位
**关联**: ARCHITECTURE.md lines 305-312 (原始设计), 2026-07-12 0 仓位分析

---
## 2026-07-12#4: stop_loss.py — 消除最后 1 处 except Exception: pass + 移除 5 处硬编码 fallback

**Bug**: `execution/stop_loss.py:148` `except Exception: pass` 完全吞掉 time_stop 的日期解析异常。
在「全量消除 except Exception: pass」(2026-07-12) 的 13 处修复中遗漏了 stop_loss.py。
**修复**: `pass` → `logger.error(traceback.format_exc())`

**硬编码 fallback**: `RiskManager.__init__` 中 5 处 `_cfg()` 调用带默认值:
- `_cfg("risk.atr_mult_take_profit_1", 2.0)` → `_cfg("risk.atr_mult_take_profit_1")`
- `_cfg("risk.atr_mult_take_profit_2", 3.0)` → `_cfg("risk.atr_mult_take_profit_2")`
- `_cfg("risk.atr_mult_trailing", 1.5)` → `_cfg("risk.atr_mult_trailing")`
- `_cfg("risk.max_hold_days", 20)` → `_cfg("risk.max_hold_days")`
- `_cfg("risk.atr_period", 20)` → `_cfg("risk.atr_period")`

**原因**: stop_loss.py 在「全量消除 hardcoded fallback」(2026-07-12#3) 的 4 文件扫描范围外, 遗漏了。
以上 5 个 key 在 config.yaml 中均存在, 移除 fallback 安全且可 fail-fast。

**验证**: 67 tests 通过; grep "except Exception:" → 仅剩 line 61 `except Exception as e:` (正确记录) 和 line 148 `except Exception:` (已修复为 logger.error)。

**范围确认**: `execution/quote.py` 和 `execution/calendar.py` 的 `except Exception` 均有 logger.warning/logger.exception 处理, 无 pass/fallback 违规。

---

## 2026-07-12: 回测测试脚本 — /tmp/_smoke.py + /tmp/_bt_full.py

**用途**:
- `/tmp/_smoke.py` — 冒烟测试 (~2min, 15 交易日)
- `/tmp/_bt_full.py` — 完整回测 (~8min, 68 交易日)

**命名规则**: 使用 `backtest/naming.py` 自动递增命名 (backtest_N / smoke_N)。

---

## 2026-07-12#5: pipeline.py — generate_signals() 缺少 exclude_symbols 参数

**Bug**: `backtest/loop.py` 传 `exclude_symbols=cooloff_syms` 给 `generate_signals()`,
但 `pipeline.py` 的 `generate_signals()` 签名不含此参数 → `TypeError: unexpected keyword argument`。
每个交易日都报错，回测全部跳过，资金纹丝不动 (CAGR=0%, elapsed=0.8s)。

**根因**: 止损冷却功能 (2026-07-12 止损改进) 在 `backtest/loop.py` 侧添加了 `exclude_symbols` 传参,
但 `pipeline.py` 侧从未接入。两处修改不同步。

**修复**:
- `pipeline.py:45`: `generate_signals()` 签名新增 `exclude_symbols: list = None`
- `pipeline.py:153-156`: Step 2.6 — 当 `exclude_symbols` 非空时，从 symbols/data/fundamentals 中过滤掉

**验证**: 67 tests 通过; smoke 测试应恢复 CACR>0 和 error=0

---

## 2026-07-12#6: 日志轮转阈值 10MB → 50MB

**变更**: `utils/logger.py` RotatingFileHandler maxBytes 从 10MB 改为 50MB
**原因**: 开发期日增长 ~8MB（2-3次回测），10MB 每 1-2 天轮转一次，上一个日志立即被冲掉。
50MB 保证开发期保留 2-3 周日志，足够回溯跨两周的 bug。
**配置**: backupCount=5 保持不变（最多保留 5×50MB=250MB）

---

## 2026-07-12#7: 统一 IC 计算模块 — factor/ic.py + walk-forward + IC 过滤

**背景**: 系统存在 3 个独立 IC 路径互不相通:
- `stats_cache.py` 从全量历史算 IC 写入 DB
- `diagnostics.py` 从回测起点前 120 天重算 IC
- `alpha/model.py` sleeve 模式完全不使用 IC 权重

**变更**: 6 个文件改动:

1. **新建 `factor/ic.py` (230行)** — 唯一 IC 计算入口
   - `compute_ic()`: 完整 pipeline, factor values → Spearman IC → 归一化权重
   - `compute_ic_from_values()`: 从预计算 factor values 算 IC (stats_cache.py 用)
   - 设计依据: Grinold & Kahn Ch6, ADR 029

2. **`backtest/diagnostics.py`** — `compute_pre_backtest_ic()` 改为委托 `factor/ic.py`
   - 删除 ~100 行重复代码
   - `FactorTracker` / `diagnose()` 保持不变

3. **`backtest/loop.py`** — walk-forward IC 重算
   - 回测启动时调 `compute_ic()` 一次
   - 每 `alpha.retrain_freq=20` 交易日 expanding-window 重算
   - ic_map 传给 `generate_signals()` → alpha model
   - diagnosis 复用同一个 ic_map (不再重复计算)

4. **`pipeline.py`** — `generate_signals()` 新增 `ic_map: dict = None` 参数
   - 传入 ic_map 优先于 DB 加载

5. **`alpha/model.py`** — sleeve 模式接入 IC 过滤
   - IC ≤ 0 的因子不参与 sleeve 选股 (保持 ADR 017 独立分仓内核)
   - `min_factors` 守卫: 过滤后因子不足时保留全部

6. **`factor/stats_cache.py`** — `_compute_ic()` 委托 `factor/ic.py`
   - ~50 行重复代码替换为 10 行委托调用

**效果**: 
- IC 从 3 个独立系统 → 1 个统一模块
- 诊断不再是"仅日志"→ 反馈到 alpha model 因子选择
- walk-forward 确保回测期内 IC 随时间更新
- 67 tests 通过

**关联**: ADR 017 (sleeve 独立分仓), ADR 029 (四层回测 P1), HYPOTHESES IC 衰减分析

---

## 2026-07-12#8: 删除北向资金因子 northbound_20d / northbound_streak

**变更**: 
- `factor/compute/price/_alternative.py`: 删除 `compute_northbound_flow()` 和 `compute_northbound_streak()`
- `factor/compute/price/__init__.py`: 移除 import 和 registry 映射
- `factor_registry`: northbound_20d / northbound_streak 标记为 `rejected`

**原因**: 证监会 2024 年中起不再发布北向资金实时数据，akshare 北向接口截止 2024-08。数据源已死。
**因子数**: 33 → 31
**验证**: 67 tests 通过

---

## 2026-07-12#9: 分层回测 — 冒烟/开发/完整三层

**变更**: `backtest/loop.py` `run_backtest()` 新增 `retrain_freq` 参数。
- `retrain_freq=None` → 从 config 读 (默认 20)
- `retrain_freq=0` → 禁用 walk-forward IC 重算，仅启动时算一次

**脚本**: 
- `/tmp/_smoke.py` — 15d, retrain_freq=0, ~2min
- `/tmp/_dev_bt.py` — 60d, retrain_freq=0, ~8min
- `/tmp/_bt_full.py` — 128d, retrain_freq=20, ~30min

**开发流程**: 每次改完 → 冒烟(2min) → 通过后跑开发回测(8min) → 功能定型后完整回测(30min)
**验证**: 67 tests 通过


---
## 2026-07-13 06:12 — 代码质量审计修复 (Code Quality Audit Fix)

### 触发
全面代码质量审计，逐项核实 9 个预列问题 + 新发现同类问题。

### 修改清单

#### #1 高优: ExecutionEngine 事务安全 (execution/engine.py)
- **问题**: execute() 在 BEGIN/COMMIT 内调用 repos.get_last_buy_price()、_check_ex_dividend() 等读操作
- **修复**: 拆分为 Phase 1 (预计算, 事务外纯读) + Phase 2 (事务内仅 record_trade)
- **影响**: 消除了事务内读已修改但未提交数据的风险

#### #2 高优: attribution.py IC 衰减 race condition (quant/scheduler/attribution.py)
- **问题**: 3 处独立 sqlite3.connect("data/market.db") 读写 factor_registry, 与周度评估可能竞争
- **修复**: 统一为单一 db_connect() (WAL + busy_timeout), 所有读写用同一个连接
- **附加**: 导入 factor.registry._db_connect 到顶部

#### #3 高优: 15 处 except Exception: pass → logging.warning
修复的文件及数量:
- web/state_broker.py: 3 处 (close price query, name lookup, position value calc)
- web/app.py: 4 处 (factor stats, close price, market valuation, latest close)
- data/trade_repo.py: 1 处 (ALTER TABLE add column)
- factor/stats_cache.py: 1 处 (load_latest failed)
- factor/ic.py: 1 处 (store.close() in merge_ic_to_registry)
- backtest.py: 1 处 (stop-loss daily price check)
- data/store.py: 5 处 (close conn, sync failures)
- pipeline.py: 已确认有日志 (无需修改)
- quant/scheduler/attribution.py: 已在 #2 中一并修复

结果: 零残留 except Exception: pass

#### #4 中优: limit_touch_no_seal 向量化 (factor/compute/price/_alternative.py)
- **问题**: 逐只 Python for 循环遍历 5200 只股票
- **修复**: 用 Pandas Series 对齐 → 向量化计算 limit_price, ret, hit 条件
- **效果**: O(n) 符号遍历 → O(1) 向量化

#### #5 中优: high52w_dist 过期数据标记 (factor/compute/fundamental.py)
- 增强注释为 TODO(#5), 注明数据过期窗口和建议修复方向

#### #6 中优: data/store.py except Exception 块已大量清理
- 5 处 pass 已修复; 其余 continue 块为逐股跳过 (合理模式, 低优先级)

#### #7 低优: _shared_limit_conn atexit 清理 (factor/registry.py)
- 新增 _close_shared() 函数, 注册 atexit

#### #8 低优: ocfp 重复代码 (factor/compute/fundamental.py)
- 删除 1018-1019 行重复的 exclude_inds + valid_syms

#### #9 低优: VERSION 语义化
- "31" → "3.1.0"

#### #10 Deferred: fundamental.py 拆分
- 1215 行 / 32 函数, 超过 800 行限制
- 建议拆为 _value.py, _quality.py, _event.py, _income.py 四个子模块
- 风险: 需要修改 factor/compute/_dispatch.py 的导入路径, 且现有状态正常
- 状态: 标记 TODO, 下次重构时处理

#### #11 低优: backtest.py 遗存文件清理
- 验证无脚本引用后删除 (功能已由 backtest/loop.py 替代)

### 参与文件
- execution/engine.py
- quant/scheduler/attribution.py
- web/state_broker.py
- web/app.py
- data/trade_repo.py
- factor/stats_cache.py
- factor/ic.py
- data/store.py
- factor/registry.py
- factor/compute/fundamental.py
- factor/compute/price/_alternative.py
- VERSION
- backtest.py (deleted)


---
## 2026-07-13 06:21 — fundamental.py 拆分方案分析 (Deferred)

### 背景
`factor/compute/fundamental.py` 当前 1215 行 / 32 函数 / 26 注册因子，超过 800 行限制。
代码质量审计将其标记为 #10 待处理。

### 分析结论
当前平均每函数 38 行, 结构已有分节注释。price 因子拆了是因为独立数据访问路径;
fundamental 因子共享同一套 infrastructure (financials 三表、fundamentals DataFrame),
拆开反而削弱内聚性。

### 推荐方案 (Deferred): Facade + Registry Pattern
```
factor/compute/fundamental/
    __init__.py          (~30 行, 聚合所有 _MAP, re-export)
    _value.py            (~350 行, 估值因子 7 个)
    _profitability.py    (~250 行, 盈利因子 6 个)
    _institution.py      (~200 行, 机构行为因子 4 个)
    _analyst.py          (~150 行, 分析师预期因子 3 个)
    _core.py             (~250 行, 质量/杠杆/风险/杂项 + 辅助函数)
```

每个子模块底部定义 `_MAP = {...}`, `__init__.py` 聚合为 `_FUNDAMENTAL_FN_MAP`。
外部接口不变 — `from factor.compute.fundamental import _FUNDAMENTAL_FN_MAP` 照常工作。
与现有 `factor/compute/price/` 拆分风格一致。

### 决策
暂不落地。等文件膨胀到 1500+ 行或新增 >10 个因子时再拆分。
当前 1215 行且结构清晰，拆分收益 < 破坏风险。


---
## 2026-07-13 06:30 — 架构清理: 双调度器 + cfg统一 + state_pusher简化

### 触发
全面架构审计发现 4 个结构问题, 其中 3 个确认存在并逐项修复。

### 修改清单

#### #1 双调度器清理
- **问题**: 旧 scheduler.py (180行, 0 Python import) 与 quant/scheduler/ (10文件, 5 daemon) 并存。
  scheduler.py 引用已不存在的 pipeline.push_to_web() 函数, 实际不可运行。
- **修复**: git rm scheduler.py; 更新 ARCHITECTURE.md, README.md, CLAUDE.md 中 6 处引用到 quant/scheduler/

#### #2 factor/compute.py 2998 行
- **核实**: 文件已不存在。已被拆分为 fundamental.py, price/_alternative.py, price/_event.py, price/_momentum.py
- **状态**: 无操作

#### #3 cfg() vs _require_cfg() 统一
- **问题**: 20 处 cfg() with hardcoded default (违反"config.yaml 唯一数据源"原则)
- **修复范围**:
  - risk/constraints.py: 6 处 cfg() with cls.attr → _require_cfg() (config已有关键值)
  - data/store.py: 10 处 cfg() with literal → _require_cfg()
  - scripts/validate.py: 3 处 cfg() with literal → _require_cfg()
  - evaluation/phase1_data.py: 2 处 → _require_cfg()
  - evaluation/phase2_single.py: 8 处 → _require_cfg()
  - evaluation/phase3_oos.py: 5 处 → _require_cfg()
  - evaluation/phase4_costs.py: 1 处 → _require_cfg()
  - evaluation/phase5_monitor.py: 2 处 → _require_cfg()
- **config.yaml 补全**: data.pytdx.connect_timeout, data.benchmark_start_date
- **结果**: 零残留 cfg(key, default) 调用 (仅函数定义保留)

#### #4 state_broker/state_pusher 竞态简化
- **问题**: pipeline.py 通过 HTTP POST 推送状态到同进程 Flask, 经 state_pusher → Flask → state_broker 三层间接
- **修复**: pipeline.py 直接 import broker from web.state_broker, 7 处 post_state() 改为 broker.update()
- **state_pusher.py**: 标记为 DEPRECATED, 保留用于回退参考

### 参与文件
- scheduler.py (deleted)
- config/config.yaml
- risk/constraints.py
- data/store.py
- scripts/validate.py
- pipeline.py
- web/state_pusher.py (deprecated notice)
- evaluation/phase1_data.py
- evaluation/phase2_single.py
- evaluation/phase3_oos.py
- evaluation/phase4_costs.py
- evaluation/phase5_monitor.py
- ARCHITECTURE.md
- README.md
- CLAUDE.md


---
## 2026-07-13 06:42 — 8 项应补入技术复查 + 未实现缺口方案

### 复查结果
8 项中 5 项已实现, 3 项未实现:

| 缺口 | 状态 | 位置 |
|------|------|------|
| Gap 1: backtrader/zipline 回测 | ❌ 未实现 | — |
| Gap 2: Optuna 超参优化 | ✅ | optimizer/hyperopt.py |
| Gap 3: HMM 市场状态 | ✅ | regime/detector.py → alpha/model.py combine_regime() |
| Gap 4: 幸存偏差修正 | ✅ | data/store.py sync_delisted_stocks() + get_universe() delist_date 过滤 |
| Gap 5: Ray/Dask 分布式 | ❌ 未实现 | evaluation/parallel.py 有 multiprocessing, 对单机已够用 |
| Gap 6: VaR/CVaR/压力测试 | ✅ | risk/var.py (6 functions, 4 stress scenarios) |
| Gap 7: 另类数据 | ❌ 未实现 | — |
| Gap 8: 基准对比 | ✅ | benchmark/tracker.py 每日 000300 alpha |

### 未实现缺口实施方案

#### Gap 7a: 新闻情绪 NLP (优先级最高)
- 数据源: akshare stock_news_em (东方财富新闻)
- NLP: SnowNLP 中文情感分析 (轻量, pip install)
- 因子: news_sentiment_1d, news_volume_5d, news_abnormal_20d
- 文件: data/news.py (~150行) + factor/compute/price/_sentiment.py (~200行)

#### Gap 7b: 宏观指标 (优先级第2)
- 数据源: akshare macro_china (CPI/PMI/M2/利率)
- 因子: macro_pmi_diff, macro_m2_yoy, macro_cpi_yoy, macro_rate_10y
- 文件: data/macro.py (~100行) + factor/compute/fundamental/_macro.py (~150行)

#### Gap 1: backtrader 事件驱动回测 (优先级第3, 改动最大)
- 浅层集成: backtrader 只做事件引擎+撮合, 信号由现有 pipeline.generate_signals() 产出
- 自动处理: 分红除权、停牌跳过、涨跌停无法成交、最小交易单位
- 删除: backtest/loop.py 中 ~200 行手工事件模拟代码

#### Gap 5: Ray/Dask (最低优先级)
- 当前 evaluation/parallel.py 的 multiprocessing.Pool 对单机已够用
- 标记为 deferred, 等实际性能瓶颈出现再引入


---
## 2026-07-13 07:00 — 应补入技术缺口: Gap 1/7a/7b 落地

### Gap 7a: 新闻情绪 NLP ✅
- **文件**: `data/news.py` (158行) — 东方财富个股新闻 + SnowNLP 情感分析
- **文件**: `factor/compute/price/_sentiment.py` (149行) — 3 个因子:
  - `news_sentiment_1d` — 当日新闻情感得分 (来源: 东方证券 2019)
  - `news_volume_5d` — 5日新闻数量异常关注度 (来源: 方正证券 2020)
  - `news_abnormal_20d` — 20日异常新闻量 (来源: 国泰君安 2021)
- **集成**: 已加入 `_PRICE_FN_MAP`, 回测可用
- **数据源**: `data/news.py` 的 `sync_news_sentiment()` (需首次运行填充数据)
- **依赖**: `pip install snownlp akshare` (SnowNLP 可选, 有 fallback 关键词分类器)

### Gap 7b: 宏观指标 ✅
- **文件**: `data/macro.py` (121行) — CPI/PMI/M2/国债收益率
- **文件**: `factor/compute/fundamental.py` (行 1206-1278) — 4 个因子:
  - `macro_pmi_diff` — PMI 偏离荣枯线 (来源: 中金公司 2019)
  - `macro_m2_yoy` — M2 同比增速 (来源: 华泰证券 2018)
  - `macro_cpi_yoy` — CPI 同比 (来源: 中信证券 2020)
  - `macro_rate_10y` — 10年国债收益率折现 (来源: DCF 模型)
- **集成**: 已加入 `_FUNDAMENTAL_FN_MAP` 的 `"macro"` 分类
- **数据源**: `data/macro.py` 的 `sync_macro_data()` (需首次运行)

### Gap 1: backtrader 事件驱动回测 ✅
- **文件**: `backtest/bt_engine.py` (318行) — 基于 backtrader.Cerebro 的浅层集成
- **设计**: backtrader 只接管事件引擎 + 撮合 + PnL 跟踪, 信号仍由 `pipeline.generate_signals()` 产出
- **自动处理**: 停牌跳过 (volume==0), 佣金 (0.025%), 最小交易单位 (100 股/手)
- **接口**: `run_backtest_bt(start, end, capital, strategy)` 与 `run_backtest()` 兼容
- **原有** `backtest/loop.py` 保持不变, 两套引擎可并行使用、对比验证

### Gap 5: Ray/Dask (deferred)
- 当前 `evaluation/parallel.py` 的 multiprocessing.Pool 对单机已够用
- 标记为 deferred, 等实际性能瓶颈出现再引入


---
## 2026-07-13 08:00 — 5 项待实现功能落地

### 修改清单

#### #11 Kelly 公式头寸管理 → optimizer/kelly.py (157行)
- **公式**: Fractional Kelly = (μ - r_f) / σ² / k (k=4, 四分之一凯利)
- **来源**: Kelly (1956) + Ralph Vince (1990) Fractional Kelly
- **集成**: 新增 `_kelly_greedy()` 方法替换贪心等权，`pipeline.py` 已传 `ic_map` 到 `PortfolioConstructor.construct()`
- **退化**: IC 全零或无 ic_map 时自动回退 alpha 比例分配
- **config**: `optimizer.kelly_fraction: 4.0`

#### #6 市场冲击模型 → execution/impact.py (170行)
- **公式**: Almgren-Chriss 线性冲击: 冲击 = σ × sqrt(Q/V)^γ × η
- **来源**: Almgren & Chriss (2001), A股 η≈0.2, γ=0.5
- **集成**: `CostModel.slippage_with_impact()` 新增方法，`buy_cost/sell_proceeds/sell_cost` 支持可选 `daily_volume`/`daily_vol` 参数
- **退化**: 无成交量数据时回退固定 0.1% 滑点
- **config**: `execution.impact_eta: 0.2`, `execution.default_daily_vol: 0.025`

#### #10 Telegram/微信告警通道 → monitor/notify.py (119行)
- **Telegram**: Bot API, 需要 `telegram_bot_token` + `telegram_chat_id`
- **微信**: 企业微信 Webhook, 需要 `wechat_webhook`
- **退化**: 通道未配置时回退本地 `logger.warning()` (不阻塞)
- **便捷函数**: `send_drawdown_alert()`, `send_error_alert()`
- **config**: `monitor.telegram_bot_token`, `monitor.telegram_chat_id`, `monitor.wechat_webhook` (均为可选, 支持 `${ENV_VAR}`)

#### #7 行业轮动策略 → alpha/rotation.py (164行)
- **框架**: 美林时钟 (Merrill Lynch 2004), A股改编
- **层 1**: PMI/CPI → 时钟象限判定 → 行业超配/低配系数
- **层 2**: 行业内因子调权 (周期=value, 成长=momentum)
- **集成点**: `alpha/model.py` 的 `combine()` 之后调用 `SectorRotator.overlay()`
- **数据**: 从 `macro_indicator` 表取 PMI/CPI, 从 `stocks` 表取行业分类
- **config**: `alpha.sector_rotation: true`

#### #8 多周期信号整合 → alpha/multi_tf.py (122行)
- **框架**: Faber (2007) 周线+日线投票制
- **规则**: 周线空头 → 日线多头 × 0 (压制); 周线中性 → 日线 × 0.5; 周线多头 → 日线空头 × 0
- **数据**: 从 `daily` 表取周线收盘价差
- **集成点**: `alpha/model.py` 的 `combine()` 之后调用 `MultiTimeframeConfirmer.confirm()`
- **config**: `alpha.weekly_weight: 0.3`

### 新增 config.yaml 键
- `optimizer.kelly_fraction: 4.0`
- `execution.impact_eta: 0.2`
- `execution.default_daily_vol: 0.025`
- `monitor.telegram_bot_token: ${TELEGRAM_BOT_TOKEN}` (可选)
- `monitor.telegram_chat_id: ${TELEGRAM_CHAT_ID}` (可选)
- `monitor.wechat_webhook: ${WECHAT_WEBHOOK}` (可选)
- `alpha.weekly_weight: 0.3`
- `alpha.sector_rotation: true`

### 新增模块 (5 个文件, 732 行)
- `optimizer/kelly.py` (157) — Kelly 头寸管理
- `execution/impact.py` (170) — 市场冲击模型
- `monitor/notify.py` (119) — 告警通道
- `alpha/rotation.py` (164) — 行业轮动
- `alpha/multi_tf.py` (122) — 多周期信号

### 修改模块
- `optimizer/portfolio.py` — 新增 `_kelly_greedy()` 方法, `construct()` 接受 `ic_map`
- `execution/cost.py` — 新增 `slippage_with_impact()`, `buy_cost/sell_proceeds/sell_cost` 支持动态冲击
- `pipeline.py` — `construct()` 调用传入 `ic_map=ic_map`
- `config/config.yaml` — 8 个新键
- `config/loader.py` — 5 条新 validation

---
## 2026-07-13 08:05 — BugFix: _dispatch.py get_logger 未绑定

### 根因
- `factor/compute/_dispatch.py` 中 `get_logger` 在函数体 `try` 块内 `from utils.logger import get_logger` (行 41)
- 对应 `except` 块行 49 直接调用 `get_logger(...)`，但 `get_logger` 被 Python 判定为局部变量
- 若 try 块在 import 之前抛异常 → `UnboundLocalError: cannot access local variable 'get_logger'`
- 触发场景: 新增的 sentiment/macro 因子在某只股票上抛异常, 而该异常恰好在第一个因子的 import 之前

### 修复
- 将 `from utils.logger import get_logger` 提升到模块顶部 (行 6)
- 删除函数体内所有冗余 inline import (行 41/50/57/58/84/85)
- 结果: `get_logger` 是模块级变量, 永不未绑定

## 2026-07-13 09:42 — 信号持久化入库 + 数据流修复 + 日志分离

### 问题根因
- signals._run 产出写入模块变量 _last_signals, execute._run 从 state_broker 读取 → 数据流中断
- state_broker 是纯内存字典, 进程重启后信号数据丢失
- web 重启后 execute 永远拿到 0 targets

### 修复内容
- data/trade_repo.py: 新增 daily_signals 表 + save_signals/get_latest_signals CRUD
- quant/scheduler/signals.py: 信号生成后写入 daily_signals 表
- quant/scheduler/execute.py: 从 daily_signals 表读取信号 (不再依赖 broker)
- scripts/run_task.py: 手动任务执行器 (signals/execute/cleanup)
- web/app.py: _capital() 硬编码→live.default_cfg; 4 处 pass→logger.warning; api_metrics 路由修复
- config/config.yaml: live.default_capital=5000; backtest.default_capital=5000; execution.impact_eta/default_daily_vol
- utils/logger.py: 日志按模块名分离 app.log vs backtest.log
- pipeline.py: 删除函数体内 get_logger 阴影导入; DataStore(db_path=None) 修复
- restart.sh: 重建 (之前被误删)

### 手动任务命令
  PYTHONPATH=. .venv/bin/python3 scripts/run_task.py signals  [YYYY-MM-DD]
  PYTHONPATH=. .venv/bin/python3 scripts/run_task.py execute  [YYYY-MM-DD]
  PYTHONPATH=. .venv/bin/python3 scripts/run_task.py cleanup  [YYYY-MM-DD]

## 标准入口点速查

每次需要跑测试时，用以下命令给用户，不要自己执行：

| 用途 | 命令 |
|------|------|
| 冒烟测试（14天） | `PYTHONPATH=. .venv/bin/python3 scripts/smoke_test.py` |
| 完整回测（自定义日期） | 写 /tmp 临时文件，参考 `scripts/smoke_test.py` 结构 |
| 五阶段正式评估 | `PYTHONPATH=. bash scripts/eval_standard.sh` |
| Web 服务 | `PYTHONPATH=. .venv/bin/python3 web/app.py` |
| 手动任务（信号/执行/监控） | `PYTHONPATH=. .venv/bin/python3 scripts/run_task.py <signals\|execute\|monitor>` |

所有入口必须在开头调用：`from utils.excepthook import setup; setup()` — 已在 scripts/smoke_test.py、eval_standard.sh、web/app.py 注入。

## 日志分布速查

| 入口 | 业务日志 | 崩溃日志 (excepthook) |
|------|----------|----------------------|
| `web/app.py` | `logs/app.log` | `logs/app.log` |
| `scripts/smoke_test.py` | `logs/backtest.log` | `logs/app.log` |
| `scripts/eval_standard.sh` | `logs/backtest.log` | `logs/app.log` |
| 临时回测脚本 `/tmp/_bt_*.py` | `logs/backtest.log` | `logs/app.log` |

分析错误时：先看 `logs/app.log`（崩溃异常），再看 `logs/backtest.log`（回测业务错误）。

---

## 2026-07-13#23: 修复 backtesting filter 错误包含 rejected

**Bug**: `_resolve_statuses("backtesting")` 返回 `('registered', 'candidate', 'retired', 'rejected')`，
多含了 `rejected`。HANDOFF#2026-07-11 和 HANDOFF#2026-07-12#14 明确规定
backtesting 过滤仅含 `registered + candidate + retired`。

**根因**: 某次修改 `_registry.py` 时误加了 `'rejected'` 进入 backtesting 元组。
rejected 因子已经过多轮评估判定无效，不应再参与回测。

**修复**: `factor/compute/_registry.py:16` — 删除 `'rejected'`。

**影响**: 冒烟测试和回测诊断不再加载 67 个 rejected 因子，仅加载 registered + candidate + retired。
当前后三者总数 = 0 + 0 + 2 = 2 个 (northbound 数据源已死)。

---

## 2026-07-13#24: 因子状态批量修正 + phase2_single.py 两 bug 修复

**背景**: 
- 67 个 rejected 因子堵塞了 backtesting 池，诊断通过的 64 个因子因状态为 rejected 无法进入 Phase 2 评估
- 2 个 northbound 因子数据源断绝（证监会 2024-08 停止发布北向资金每日明细），应标记为 rejected
- phase2_single.py 存在两个 bug: get_factors_by_status 传参类型错误 + conn.close() NameError

**因子状态变更**:
- `northbound_20d`: retired → rejected，备注「数据源断绝 — 证监会 2024-08 停止发布北向资金每日明细，此因子永久无效」
- `northbound_streak`: 已是 rejected，补全备注同上
- `sue`: retired → rejected（诊断 drop，非 northbound）
- 64 个诊断通过因子: rejected → retired，备注「回测诊断通过，待正式评估重新检验」
- **结果**: retired=64, rejected=5, active=1 — backtesting 池 = registered(0)+candidate(0)+retired(64) = 64

**Bug 修复** (evaluation/phase2_single.py):
1. `repo.get_factors_by_status("SELECT ...")` → `get_factor_names(status_filter="backtesting")`
   - 原代码传入 SQL 字符串给期望 `tuple[str,...]` 的方法，类型不匹配
   - 改用已有的 `get_factor_names()` 函数，与 `_registry.py` 的 `_resolve_statuses` 保持一致
2. 删除 `conn.close()` — `conn` 变量在函数内从未定义，会引发 NameError

**影响**: backtesting 池从 0 因子恢复为 64 个待评估因子，eval_standard.sh Phase 2 现可正常运行。

---

## 2026-07-13#25: Phase 5 探伤断点 + rejected 安全重置脚本

### A. Phase 5 全零 IC 守卫 (circuit breaker)

**位置**: `evaluation/phase5_monitor.py` → `sync_factor_status()`

**逻辑**: Phase 5 同步前检查 Phase 2 结果:
- 所有因子 IC≈0.0000
- passed 数量 = 0
- 因子数 > 4（排除真的只有少量因子且全无效的情况）

若全部命中 → CRITICAL 日志 + 拒绝同步，保留因子原状态。
防止 IC 计算因超时/数据缺失/bug 批量产全零导致假阴性误杀。

### C. rejected → retired 安全重置脚本

**脚本**: `scripts/reset_rejected.sh`

**用法**:
```bash
bash scripts/reset_rejected.sh          # 预览
bash scripts/reset_rejected.sh --apply  # 执行
```

**保护**: 只重置 `status_reason LIKE 'Phase %: %'` 的因子（Phase 2/3/4 评估失败）。
永久 rejected（northbound 数据源永死等，reason 不含 "Phase" 前缀）自动跳过。

**因子状态生命周期** (更新):
```
registered → candidate → retired → Phase 2/3/4 → active  (通过)
                                               → rejected (失败)
                                                  ↓
                                     reset_rejected.sh --apply
                                                  ↓
                                               retired (重新入池)
```

---

## 2026-07-15#32: 数据库路径统一 + 多个 NameError/JS 语法错误修复

### 触发
项目根 `data/trades.db` 和 `quant/data/trades.db` 双目录并存，路径解析三套逻辑各自为政。

### R1: 创建全局路径常量

**文件**: [`quant/config/paths.py`](/Users/mariusto/project/quant/quant/config/paths.py)

```python
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(_PROJECT_ROOT, "quant", "data")
TRADE_DB = os.path.join(DATA_DIR, "trades.db")
MARKET_DB = os.path.join(DATA_DIR, "market.db")
BACKTEST_DB = os.path.join(DATA_DIR, "backtest_trades.db")
METRICS_DB = os.path.join(DATA_DIR, "metrics.db")
```

### R2: 统一所有路径引用

**影响 14 文件**: 所有 `"data/trades.db"` / `"data/market.db"` 字符串 → `"quant/data/..."`；所有 `os.path.join(..., "data", ...)` → 导入 `quant.config.paths` 常量。

**关键修复**:
- [`quant/data/repos/_base.py`](/Users/mariusto/project/quant/quant/data/repos/_base.py): `_PROJECT_ROOT` 从 3 层 `dirname` 改为 4 层（指向项目根）；恢复误删的 `import threading`
- [`quant/data/trade_repo.py`](/Users/mariusto/project/quant/quant/data/trade_repo.py): `TRADE_DB` 从文件末尾移回类定义前（否则 `__init__` 默认参数引用时 NameError）
- [`web/state_broker.py`](/Users/mariusto/project/quant/web/state_broker.py): `_os.path.join(_root, "data", ...)` → `"quant", "data"` (三处)
- [`web/app.py`](/Users/mariusto/project/quant/web/app.py): `TRADES_DB` → `TRADE_DB` 命名统一；三处 `os.path.join` 替换为常量
- 删除 `data/` 目录

### R3: 前端竞态条件 + 语法错误

**文件**: [`web/static/app.js`](/Users/mariusto/project/quant/web/static/app.js)

1. **竞态条件**: `DOMContentLoaded` 回调中 `pollOverview()` 未 `await`，`checkPlotly` 100ms 后触发时 `window._perfData` 常为 `undefined` → 图表永远不渲染。修复: `await pollOverview()` 完成后再 `checkPlotly`。

2. **语法错误**: 第 307 行 `[1,'var(--up)')]` 多了一个 `)` → 整个 JS 解析失败。修复: 去掉多余的 `)`。

### R4: 因子页全零

**文件**: [`quant/data/repos/factor_repo.py`](/Users/mariusto/project/quant/quant/data/repos/factor_repo.py)

`count_total()` 和 `count_with_ic()` 使用 `query_scalar()`，但第 9 行 import 只有 `query_all, query_row`。补充 `query_scalar`。

### 验证

- Flask test client: 所有 5 个 API (state/performance/trades/positions/factors) 返回 200
- `factor_repo.count_total()` → 70（2 active, 64 rejected, 4 retired）
- `broker.get()` → capital=5000, total_asset=5000, PnL=0
- `node -c app.js` → syntax OK
