# ADR 022: 151 Trading Strategies — 可落地四档方案

**日期**: 2026-07-05
**来源**: ADR 020 (核心提取) + ADR 021 (完整参考) 对照项目现状
**关联**: HANDOFF.md, config.yaml

---

## 背景: 波动率拖累诊断

2026-07-05 eval_stepwise.sh 输出含一个引人注意的内部矛盾:

```
Final wealth: ¥17,984.73   (初始 ¥100,000)
Cumulative return: -82.0%
Sharpe (est): 0.843
Benchmark (000300): +28.1%
Excess return (α): -109.1%
Tracking error (ann): 231.6%
```

Sharpe 0.843 (正) 与 -82% 收益如何共存? **波动率拖累 (volatility drag)**:

| 指标 | 值 | 来源 |
|------|---|------|
| 算术日均收益 μ | +0.72% | 驱动 Sharpe=0.843 |
| 日波动率 σ | 13.5% | σ²/2 = 0.91% 方差减损 |
| 几何日均收益 | -0.19% | μ - σ²/2 |
| 850天累积 | (0.9981)^850 ≈ 18% | 即 ¥17,985 |

**诊断: 信号有效但仓位失控。** σ=13.5% 日波动是沪深300 (σ≈1.2%) 的 11 倍。
sleeve 模式 2 因子 × 15 只股票 = z-score 排序可能高度集中到同一批标的。

这是**好消息**: α 存在 (Sharpe 0.843), 只需修正仓位管理即可。

---

## 四档可落地内容

### 第一档: 改参数即生效 (低风险, 即时)

| # | 改动 | 现状 | 书中依据 | 影响 |
|---|------|------|---------|------|
| 1 | `factor.windows.volatility` | 20d | Ch.3.4: 6-12月 = 126-252d | t=0.5→预计显著 |
| 2 | `factor.windows.idiosyncratic_vol` | 20d | 同上 | t=0.5→预计显著 |
| 3 | `alpha.sleeve.positions_per_factor` | 15 | Grinold & Kahn 最低20只 | 降集中度, 减小波动率拖累 |
| 4 | `risk.max_single_position` | 0.10 | 当前σ=13.5%下10%=灾难 | 0.05 更安全 |

### 第二档: 注册新因子变体 (1-2天)

**2.1 动量窗口标准化**
- `compute_momentum()` 已参数化 window, 只需在 `factor_registry` 新增三行:
  - `momentum_63d` (T=3月, 标准下限)
  - `momentum_126d` (T=6月, 最常见)
  - `momentum_252d` (T=12月, Jegadeesh & Titman 1993 基准)
- 当前 `momentum_10d` (T=10天, 偏离标准 21d 下限) 保持 deprecated 供对照

**2.2 ✅ Residual Momentum (Ch.3.7) — P58 已实现 (2026-07-06)**
- 三步法 (详见 ADR 020 §4):
  1. 36月 Fama-French 回归估计 β
  2. 12月形成期残差计算 (skip 1月)
  3. 残差标准化 → 截面排序
- A 股适配: 沪深300=MKT, 自由流通市值=SMB, B/P=HML
- `risk/neutralize.py` 已有回归基础可复用
- 与 zt_streak/dt_streak 互补: 残差动量剥离共同因子, 信号更纯

### 第三档: 算法升级 (3-5天)

**3.1 Mean-Reversion Weighted Regression (Ch.3.10)**
- 替代当前 `reversal_5d` (单变量, t=0.2)
- 利用 `risk/neutralize.py` 行业矩阵 Ω + 波动率倒数加权
- 行业中性化后残差作为反转信号 → 去除行业联动噪声

**3.2 Dollar-Neutral 优化器 (Eq 3.93)**
- `optimizer/portfolio.py` 当前用 `equal_weight` bypass 优化
- Eq 3.93 提供显式解: `w = γ * [C^{-1}E - (1^T C^{-1}E / 1^T C^{-1}1) * C^{-1}1]`
- `ledoit_wolf_cov` 已有, 工作量为改写 optimizer

### 第四档: 需要新数据 (暂缓)

| 序号 | 因子 | 阻断条件 |
|------|------|---------|
| 1 | SUE / Earnings Momentum (Ch.3.2) | 需 8 季度 EPS 数据, 确认 `data/market.db` `financials` 表 |
| 2 | 隐含波动率 (Ch.3.5) | A 股个股期权不存在 |
| 3 | 并购套利 (Ch.3.16) | 需实时 M&A 事件数据 |

---

## 窗口参数校对表

已有因子与书中标准窗口对照 (来源: ADR 021 §因子库对照矩阵):

| 因子 | 当前窗口 | 标准窗口 | 状态 |
|------|---------|---------|------|
| volatility_20d | 20d | **126d** | 需修改 |
| idio_vol_20d | 20d | **126d** | 需修改 |
| amihud_20d | 250d | 250d | ✓ |
| skewness_20d | 60d | 60d | ✓ |
| momentum_10d | 10d | 63-252d | 注册新变体 |
| reversal_5d | 5d | 无标准窗口 | 方法升级 (Ch.3.10) |

---

## 因子遗漏清单 (Ch.3 全部 21 策略)

| 书中策略 | 项目对应 | 状态 |
|---------|---------|------|
| 3.1 Price-Momentum | momentum_10d | 窗口错, 需注册 63/126/252d |
| 3.2 Earnings-Momentum | — | 缺失, 第四档 |
| 3.3 Value (B/P) | bp_ratio | t=1.4 边缘, lookback 扩展可过 |
| 3.4 Low-Vol | volatility_20d | 窗口错, 第一档修复 |
| 3.5 Implied Vol | — | 不适用 |
| 3.6 Multifactor | ic_weighted + sleeve | ✓ 符合文献 |
| 3.7 Residual Momentum | — | **缺失**, 第二档.2 |
| 3.8 Pairs Trading | — | 缺失, 暂不优先 |
| 3.9 Mean-Reversion | reversal_5d | 方法弱, 第三档.1 |
| 3.10 Weighted Reg | — | 缺失, 第三档.1 |
| 3.11-3.15 Technical | — | 不推荐 (书中自评 "unscientific") |
| 3.16 M&A | — | 不适用 |
| 3.17 ML KNN | — | 计算量过大, 暂不适用 |
| 3.18 Risk Model | cov/constraints | 部分实现, 第三档.2 |
| 3.19 Market-Making | — | 不适用 (HFT) |
| 3.20 Alpha Combos | sleeve | ✓ 已实现 |
| 3.21 Comments | — | 方法论对齐 ✓ |

---

## 推进顺序与预计收益

```
第一档 (改参数, 30min)  → 预期: volatility/idio_vol 通过 t=2.0, 波动率拖累减轻
  ↓
第二档.1 (动量变体, 1h) → 预期: 3-4 个 active 因子池
  ↓
第二档.2 (残差动量, 1d) → 预期: 新因子类型, 与现有 zt 系列低相关
  ↓
第三档 (算法升级, 3d)  → 预期: reversal 升级 + optimizer 升级
```
