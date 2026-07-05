# ADR 018: 因子评估死锁 — 入口过滤导致 deprecated 因子永不可恢复

**日期**: 2026-07-05  
**状态**: 已识别，待修复  
**关联**: ADR 007 (因子评估标准), P43 (sleeve 架构)

## 问题描述

`eval_stepwise.sh` 的三层评估管道存在逻辑死锁：

1. **Layer 1+2** 调用 `compute_factor_stats()` → `compute_all_factors()` → `load_active_price_factors()`
2. `load_active_price_factors()` 执行 SQL: `WHERE status='active'`
3. 因此 **只有 status='active' 的因子参与 IC 估计**
4. **Layer 3** 第一步将所有因子置为 deprecated，然后只重新激活 Layer 1+2 的候选因子

### 死锁链路



## 影响范围

- **35 个注册因子中 34 个被永久排除**，即使它们的 IC 统计显著
- dt_streak (IC=+0.0498, 高于 zt_streak 的 +0.0424) 也被排除
- 修改 lookback/n_symbols 等评估参数**不会改变结果** — 因为被排除的因子根本不被计算
- 手工 `activate_candidates.py` 激活的因子，下次 eval 又会被 deprecate

## 因子库全景 (35 个, |IC| > 0.02 的 10 个)

| 因子 | IC | IR | 状态 | 类别 |
|------|-----|-----|------|------|
| dt_streak | +0.0498 | +0.70 | deprecated | limit_down |
| zt_streak | +0.0424 | +0.65 | **active** | limit_up |
| high52w_dist | +0.0400 | +0.20 | deprecated | technical |
| vol_price_corr_10d | -0.0306 | -0.36 | deprecated | volume_price |
| roa | +0.0284 | +0.28 | deprecated | profitability |
| bp_ratio | +0.0281 | +0.13 | deprecated | value |
| size | +0.0247 | +0.17 | deprecated | size |
| roe_reported | +0.0239 | +0.24 | deprecated | profitability |
| debt_ratio | -0.0236 | -0.21 | deprecated | leverage |
| gap_5d | +0.0212 | +0.18 | deprecated | technical |

其中 9 个 |IC|>0.02 的因子被排除在评估之外。

## 修复方案

### 方案 A: eval_stepwise.sh 全量激活 (推荐)

在 Layer 1+2 之前，临时激活所有因子：

```python
# 保存当前 active 集合
active_before = [r[0] for r in conn.execute(
    "SELECT name FROM factor_registry WHERE status='active'"
).fetchall()]

# 全量激活 (IC=0.0 的因子自然会被 t-test 淘汰)
conn.execute("UPDATE factor_registry SET status='active', status_reason='batch re-evaluation'")
conn.commit()
```

Layer 1+2 运行后，Layer 3 照常 deprecate all + stepwise 筛选。

**优点**: 最小改动，不侵入 compute 模块  
**缺点**: 如果脚本中途崩溃，所有因子会保持 active（可通过保存/恢复 active_before 解决）

### 方案 B: compute_all_factors 增加 factor_names 参数

修改 `compute_all_factors()` 和 `load_active_price_factors()` 支持可选的因子名列表覆盖：

```python
def compute_all_factors(data, date, fundamentals=None, benchmark_ret=None, factor_names=None):
    if factor_names is not None:
        # 使用指定列表，忽略 status 过滤
        ...
    else:
        # 默认行为：只算 active
        ...
```

**优点**: 不影响因子状态，语义更清晰  
**缺点**: 改动范围更大（涉及 compute.py、store.py 查询）

## 与 lookback 参数的关系

这两个问题是**正交的**：

- **死锁修复**: 让 35 个因子都能进入评估管道
- **lookback 扩展**: 让进入管道的因子获得更稳定的 IC 估计（更高 t-stat）

两者都需要修复。如果只修死锁不改 lookback，dt_streak 等可能仍需在 n=120 下通过 t-test 才能成为候选；如果只改 lookback 不修死锁，仍然只有 zt_streak 被评估。

**建议顺序**: 先修死锁 → 全量评估看结果 → 根据结果决定是否需要扩 lookback。

## 关联 PR

- P43: 多因子分仓架构 (sleeve) — 需要 ≥3 个因子才有意义
- P44: 回测窗口标准化 — 让最终回测 Sharpe 可靠
- 待创建 P45: 因子评估死锁修复
