# 因子状态池与物化范围 (权威参考)

> 目的: 固化"因子状态池 (using/backtesting) 语义"与"空/旧表对物化无影响"两个
> 已核实结论, 防止后续会话重复分析。最后核实: 2026-08-16。
> 本文件是**唯一权威** — 若与 DATA_DICTIONARY.md 或 ADR-041 冲突, 以本文件为准。

## 1. 状态枚举 (实现为准, 非文档)

`quant/factor/state_machine.py:71-76` 定义**四态**:

| 状态 | 含义 | 当前实例数 (2026-08-16) |
|------|------|------------------------|
| `evaluating` | 待评估 (原 candidate), 新因子入口 | 4 (macro_cpi_yoy / macro_m2_yoy / macro_pmi_diff / macro_rate_10y) |
| `active` | 通过完整评估+IC 未衰减 → 实盘全权重 | **0** — 当前库中无实例, 但状态机路径存活 (见下) |
| `probation` | IC 衰减观察期 (原 monitoring), 衰减权重 | 5 (dt_streak / wq_alpha_006 / alpha002_vol_div / alpha055_pos_vol / smart_money_20d) |
| `archived` | 归档 (原 retired+rejected 合并), status_reason 区分原因 | 83 (已物化目录中) — 全库 107 |

**为什么 active 当前为 0**: 状态机转换路径全部存活 —
`evaluating --EVAL_OK--> active` (weekly.py:131), `probation --IC_RECOVERED--> active` (attribution.py:369)。
只是近 4 个 reeval 轮次中无因子达标 (5 个 probation 均为 Phase 2 marginal, 4 个 macro 仍 evaluating)。
**不是状态失效, 是当前无合格者**。active 出现是正常事件, 不要当异常。

## 2. 状态池过滤器 (using / backtesting) — 仍在用, 非弃用

`quant/factor/compute/_registry.py` `_resolve_statuses()`:

| 过滤器 | 解析为 | 语义 | 当前有效因子 |
|--------|--------|------|--------------|
| `using` | `('active', 'probation')` | 实盘信号池 — 归因/实盘 OOS | 5 (probation) |
| `backtesting` | `('evaluating', 'probation')` | 回测/评估池 — 训练、物化、回测 OOS | 9 (4 evaluating + 5 probation) |
| 物化池 | `backtesting ∪ using` (factor_cache.py:50-51) | 因子缓存的物化范围 | 9 |

### 全部使用点 (scheduler/manifest 链)

| 调用者 | 过滤器 | 用途 |
|--------|--------|------|
| `scheduler/factor_cache.py:50-51` | backtesting ∪ using | 物化池 — 唯一数据来源 |
| `scripts/rematerialize_industry_pit.sh:25` | backtesting | 行业 PIT 重物化 (9 因子) |
| `monitor/factor_attribution.py:48,121` | using | 实盘归因 |
| `scheduler/attribution.py:176` | using | 归因 (D1-D3) |
| `scheduler/crowdedness.py:52` | using | 拥挤度监测 |
| `scheduler/lgb_train.py:40` / `xgb_train.py:40` | backtesting | LightGBM/XGBoost 训练 |
| `scheduler/oos_verify.py:207,221,238` | 参数化 | using=实盘 / backtesting=回测 |

### ADR-041 与实现的**已知分歧** (勿再当 bug)

ADR-041 文档写 `backtesting = evaluating + probation + archived`,
但 `_registry.py:52-53` **实现不含 archived** (archived 不参与任何池)。
实现正确 — ADR-041 是过时描述, 以本文件为准。

## 3. 数据完整性核查结论 (已核实, 勿重复排查)

### 3.1 check_freshness 全绿 (2026-08-16, 14 张 SLO 表)

daily / daily_valuation / adj_factor / margin / lhb / fund_flow / limit_up /
down_pool / benchmark_daily / divided / stocks 均 2026-08-14 (周六正常 lag2);
财务三表 2026-06-30 (半年报正常); macro_indicator 2026-06 (月度正常)。
**无数据缺失, 每日拉取增量闭环正常** (store.py:2392 精准缺口分析)。

### 3.2 空/旧表不影响因子物化 — 无须补数, 勿报"重大发现"

| 表 | 状态 | 为什么不影响物化 |
|----|------|------------------|
| `daily_basic` | 空 (0 行) | 因子层读 `daily_valuation` (综合估值/行情面) 与 `adj_factor`; daily_basic 无因子依赖 |
| `derived_daily` | 空 (0 行) | 因子计算不走它, 数据源是 daily_valuation |
| `analyst_forecast` | 旧 (7 月最后窗口) | 仅 archived 因子 (如 short_interest 类) 用 — archived 不在物化池 9 因子中 |
| `pledge_stat` | 旧 | v376 已移除其因子依赖, 当前物化池无引用 |

**核实方法** (重复验证用): 物化池 9 因子依赖 — dt_streak→limit_down_pool (已绿);
4 个 macro_* → macro_indicator (2026-06 月度正常); smart_money_20d 等纯价量 →
daily/benchmark_daily (已绿)。**任何依赖源均在 SLO 内**。

### 3.3 "92 因子"构成 (勿误读为缺失)

factor_cache/parquet_f 目录 92 个因子 = **83 archived (历史物化残留, 不再更新) +
9 当前池因子**。2025 仅 11 个文件正是池内 9 + 2 个刚归档截至该年的残留;
2026 仅 5 个 = 5 个 probation (macro 因子日频无 2026 数据, 物化窗口即止)。
**这不是缺失** — archived 因子按设计不参与物化。

## 4. 当前物化池 9 因子清单 (2026-08-16)

```
dt_streak, wq_alpha_006, alpha002_vol_div, alpha055_pos_vol, smart_money_20d   (probation)
macro_cpi_yoy, macro_m2_yoy, macro_pmi_diff, macro_rate_10y                    (evaluating)
```

物化范围: 2020-01-01 起 (行业 PIT 为重物化的数据范围), 依赖源全部在 SLO 内。