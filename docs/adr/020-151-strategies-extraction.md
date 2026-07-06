# ADR 020: 151 Trading Strategies — 核心理论与公式提取

**日期**: 2026-07-05
**来源**: Kakushadze & Serur, "151 Trading Strategies" (2018, Springer)
**章节**: Ch.3 Stocks (21 策略, 93+ 公式)
**关联**: ADR 007 (因子评估), P43 (sleeve), P45 (死锁修复), config.yaml factor.windows

---

## 1. 价格动量 (3.1, Eqs 1-9)

### 公式
```
R_i(t, T) = [P_i(t) - P_i(t-T)] / P_i(t-T)                          (3.1)
```
P_i(t) = 股票 i 在时刻 t 的收盘价，T = lookback。

截面标准化后得信号:
```
S_i(t) = [R_i(t,T) - mean(R)] / std(R)                               (3.4)
```

阈值进出场:
```
If S_i > θ_long  → Buy
If S_i < θ_short → Sell/Short                                         (3.5-6)
```

### 标准参数
- T ∈ {1, 3, 6, 12} 个月 ≈ {21, 63, 126, 252} 个交易日
- 再平衡频率: 日频 (θ 可基于 S 的分布)

### 与项目对照
- `momentum_10d` (T=10): 极短期动量，偏离标准范围下限 (21d)。t=0.1 不显著可理解。
- `zt_streak` (涨停板动量): A 股特有，本书无对应。资金溢出效应替代了纯价格动量。
- 建议: 注册 T=21/63/126 三个动量变体，取代当前 10d 版本。

---

## 2. 价值因子 (3.3, Eqs 10-14)

### 公式
```
B/P = Book Value Per Share / Price                                   (3.10)
```
排序后分组，做多高 B/P (价值股)，做空低 B/P (成长股)。

### 标准参数
- 财报数据季度更新 (与 A 股季报周期对齐)
- 再平衡频率: 月频或季频
- 来源: Rosenberg et al. (1985), Fama-French (1992)

### 与项目对照
- `bp_ratio`: 直接对应，IC=+0.0281, t=1.4。接近 2.0 门槛。
- lookback=120 时 t=1.4，如扩到 500 预计 t≈2.9。
- `ep_ratio` (E/P): 书中也提到但 IC=-0.0026，A 股盈利数据质量可能影响。

---

## 3. 低波动异常 (3.4, Eqs 14-20)

### 公式
```
σ_i(t) = std(R_i(t-d, t)) for d ∈ {20, 60, 120}                    (3.14)
```
做多低波动股票，做空高波动股票。截面 z-score 后排序。

### 标准参数
- 波动率窗口: 20/60/120 天
- 引用: Ang et al. (2006), Haugen (1995)

### 与项目对照
- `volatility_20d`: 窗口正确，t=0.5 不显著 — 可能是 A 股低波动效应本身弱
- `idio_vol_20d`: t=0.5 也不显著
- 可考虑 60d 窗口变体（与 config.yaml `factor.windows.volatility: 20` 一致，书中有推荐）

---

## 4. 残差动量 (3.7, Eqs 15-20) ⭐ 缺失因子

### 公式
区别于普通动量：先回归掉共同因子，取残差作为动量信号。
```
R_i^residual = R_i - β_i * F                                          (3.15-17)
```
其中 F 为市场指数或行业因子收益。

### 与项目对照
- **当前缺失此因子类型**。我们的 `momentum_10d` 是原始动量。
- 残差动量在学术界表现优于原始动量 (Blitz et al. 2011)
- 实现: `compute_residual_momentum(data, date, window=126, benchmark_ret)`
  → 在 `benchmark_ret` 上回归，取残差 → 截面 z-score

---

## 5. 均值回复 — 加权回归 (3.10, Eqs 48-53)

### 公式
```
R_i,expected = Σ_j Ω_ij * R_j(-T)                                     (3.48)
```
其中 Ω_ij 基于行业分类矩阵或 PCA 载荷。本质是用相关股票的近期收益预测自身收益。

### 与项目对照
- `reversal_5d` (Lehmann 1990): t=0.2 不显著。书中指出纯单变量均值回复在成熟市场已衰减。
- 加权回归版本 (3.10) 比单变量版本 (3.9) 更稳健 — 利用截面相关性过滤噪声。
- 实现方向: 用 `fundamentals["industry"]` 构建 Ω 矩阵 (已完成行业中性化基础设施)。

---

## 6. 多因子组合 (3.6, Eqs 20-26)

### 公式
```
S_composite = Σ_k w_k * S_k                                            (3.20)
```
权重 w_k 可选:
1. 等权: w_k = 1/K
2. IC 加权: w_k ∝ |IC_k| (与我们的 ic_weighted 完全一致)
3. 风险平价: w_k ∝ 1/σ_k

### 与项目对照
- `alpha.method: ic_weighted` — **文献验证通过**
- `alpha.combine_mode: sleeve` — 书中无直接对应，但 3.20 Alpha Combos 讨论了类似的分仓思路
- 当前 P43 sleeve 架构合理，缺乏的是 ≥3 个候选因子

---

## 7. Alpha 组合 (3.20, Eq 95) ⭐ 直接关联 P43

### 公式与流程
```
E_A(t_s) = (1/d) * Σ_{a=1}^d R_A(t_{s-a})                             (3.95)
```
d-day moving average of alpha returns.

组合流程 (Kakushadze & Yu 2017b):
1. 计算各 alpha 的时间序列收益 R_A(t_s)
2. 序列 demean (去均值)
3. 归一化方差 (各 alpha 同等贡献)
4. PCA 降维 (保留前 M 个主成分)
5. 截面 demean (去共同因子)
6. 计算期望收益 E_A
7. 回归 E_A ~ 主成分矩阵
8. 取残差作为 alpha 权重

### 与项目对照
- P43 sleeve 是其中一种组合方式(保持独立信号)
- 书中方法更激进: PCA + 回归 → 去重 → 压缩为单一 mega-alpha
- **如果 sleeve 效果不佳，这是备选方案**

---

## 8. 风险模型与美元中性化 (3.18, Eqs 77-93)

### 公式
```
Portfolio PnL:     P = Σ E_i * H_i                                    (3.77)
Portfolio Variance: V = sqrt(H^T C H)                                 (3.78)
Sharpe Ratio:       S = P / V                                         (3.79)
Weights:            w_i = H_i / I                                     (3.80)
Sum Constraint:     Σ w_i = 1                                         (3.81)
```

**无约束最优解** (Eq 3.85):
```
w = γ * C^{-1} * E
```

**美元中性约束** (Eqs 3.91-93):
```
C * w = λ * E - μ * 1
Σ w_i = 0  (dollar-neutral)
w = γ * (C^{-1} E - (1^T C^{-1} E / 1^T C^{-1} 1) * C^{-1} 1)
```

### 与项目对照
- `risk/covariance.py`: 已实现 `ledoit_wolf_cov`
- `risk/constraints.py`: `max_single_position: 0.10` 符合业界
- **缺失**: 美元中性化约束未在优化器中实现 (目前用 equal_weight)
- **缺失**: Eq 3.93 的显式解可用于替代当前的均值-方差数值求解

---

## 9. 窗口参数验证

| 参数 | config.yaml 当前值 | 书中推荐 | 一致性 |
|------|-------------------|---------|--------|
| volatility window | 20d | 20-60d (Andersen 2001) | ✓ |
| amihud window | 250d | 12mo / ~250d (Amihud 2002) | ✓ |
| skewness window | 60d | ≥60d (Barberis & Huang 2008) | ✓ |
| momentum lookback | 10d (momentum_10d) | 21-252d (Ch.3.1) | **✗ 太短** |
| IC lookback | 120d | 无直接推荐, 60mo(月频)≈250d(日频) | **可讨论** |
| train_window | 500d | 对应 G&K 60 月 ≈ 250d (日频不足 60 月) | 可讨论 |

---

## 10. 行动建议

### 立即可行
1. ✅ 注册 `residual_momentum_126d` (P58, 2026-07-06) — factor/compute.py + factor_registry, 待 eval
2. ✅ 注册 `momentum_63d` / `momentum_126d` / `momentum_252d` (P33) — **A股实证: IC≈0 (0.005/0.001/0.001), 经典价格动量在A股不成立,** 全部 deprecated
3. ❌ `optimizer/portfolio.py`: 增加 dollar-neutral 约束选项 (Eq 3.93) — 未实现

### 需要讨论
4. ❌ `factor.evaluation.lookback: 120 → 250/500` — 未决策 (P45运行后仍剩1 active因子, 扩窗口待议)
5. ❌ 3.20 Alpha Combos: PCA+回归 组合方案 — 未讨论

### 文档
6. ❌ 每个 `compute_*` 函数增加 "Literature" 注释行 — 未执行 (仅 compute_momentum + compute_residual_momentum 有)
