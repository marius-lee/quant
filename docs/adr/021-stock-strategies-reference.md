 # ADR 021: 151 Trading Strategies — 股票策略完整参考手册

 **日期**: 2026-07-05
 **来源**: Kakushadze, Z. & Serur, J.A., "151 Trading Strategies" (2018, Springer)
 **章节**: Ch.3 Stocks (21 策略, Eqs 1-95)
 **关联**: ADR 020 (核心提取与分析), ADR 007 (因子评估), config.yaml factor.windows

 ---

 ## 策略总览

 | # | 策略 | 英文名 | 类别 | 公式范围 | 模式 | 项目关联 |
 |---|------|------|------|--------|------|---------|
 | 3.1 | 价格动量 | Price-Momentum | 动量 | Eqs 1-8 | 截面 | momentum_10d (窗口错误) |
 | 3.2 | 盈利动量 | Earnings-Momentum | 基本面动量 | Eq 9 | 截面 | **缺失** (SUE) |
 | 3.3 | 价值 | Value (B/P) | 价值 | Eq 10 | 截面 | bp_ratio (t=1.4) |
 | 3.4 | 低波动异常 | Low-Volatility Anomaly | 低波动 | Eqs 3-5 | 截面 | volatility_20d (t=0.5) |
 | 3.5 | 隐含波动率 | Implied Volatility | 波动率 | — | 截面 | 不适用 (需期权数据) |
 | 3.6 | 多因子组合 | Multifactor Portfolio | 组合 | Eqs 10-12 | 截面 | ic_weighted / equal_weight ✓ |
 | 3.7 | 残差动量 | Residual Momentum | 动量 | Eqs 13-17 | 截面 | **缺失** (高优先) |
 | 3.8 | 配对交易 | Pairs Trading | 统计套利 | Eqs 18-26 | 时序 | 缺失 |
 | 3.9 | 均值回复(单簇) | Mean-Reversion (Single) | 均值回复 | Eqs 27-47 | 截面 | reversal_5d (方法弱) |
 | 3.10 | 均值回复(加权回归) | Mean-Reversion (Weighted) | 均值回复 | Eqs 48-53 | 截面 | 缺失 |
 | 3.11 | 单均线 | Single MA | 技术 | Eqs 54-56 | 时序 | 不推荐 |
 | 3.12 | 双均线 | Two MAs | 技术 | Eqs 57-58 | 时序 | 不推荐 |
 | 3.13 | 三均线 | Three MAs | 技术 | Eq 59 | 时序 | 不推荐 |
 | 3.14 | 支撑与阻力 | Support & Resistance | 技术 | Eqs 60-63 | 时序 | 不推荐 |
 | 3.15 | 通道交易 | Channel (Donchian) | 技术 | Eqs 64-66 | 时序 | 成交量确认可参考 |
 | 3.16 | 并购套利 | Event-Driven M&A | 事件驱动 | — | — | 不适用 (需并购数据) |
 | 3.17 | KNN机器学习 | ML Single-Stock KNN | ML | Eqs 67-76 | 时序 | 计算量过大 |
 | 3.18 | 风险模型 | Risk Model | 风控 | Eqs 77-93 | 截面 | cov/constraints 部分实现 |
 | 3.19 | 做市 | Market-Making | HFT | Eq 94 | 时序 | 不适用 (HFT) |
 | 3.20 | Alpha组合 | Alpha Combos | 组合 | Eq 95 | 截面 | sleeve 相关 |
 | 3.21 | 方法论综述 | Comments | 综述 | — | — | 截面 vs 时序对比 |

 ---

 ## 3.1 Price-Momentum (价格动量) — Eqs 1-8

 ### 核心逻辑
 未来收益与过去收益正相关 (惯性效应)。形成期 T 通常为 12 个月，跳过最近 S=1 个月 (因短期反转/流动性效应)。

 ### 关键公式
 ```
 R_i(t, T) = [P_i(t) - P_i(t-T)] / P_i(t-T)                        (3.1)
 Rbar_i = (1/T) * Σ R_i(t, τ)                                        (3.2)
 S_i = (Rbar_i - mean) / std                                        (3.3-4)
 σ_i = std(R_i) over formation period                                 (3.5)
 ```

 ### 交易规则
 - 按选择标准 (Rbar_i, S_i 或 σ_adjusted) 降序排列
 - 买入 top decile (赢家), 卖出/做空 bottom decile (输家)
 - 可构建 dollar-neutral (零成本) 或 long-only 组合
 - 持有期: 1 个月或更长 (更长持有期收益递减)
 - 权重: 等权、1/σ²、或 |S| 加权

 ### 标准参数
 | 参数 | 推荐值 | 依据 |
 |------|-------|------|
 | 形成期 T | 3/6/9/12 个月 = {63, 126, 189, 252} 天 | Jegadeesh & Titman (1993) |
 | 跳过期 S | 1 个月 ≈ 21 天 | 短期反转效应 |
 | 持有期 | 1 个月 | 同上 |

 ### 关键文献
 - Jegadeesh & Titman (1993): 开创性论文
 - Asness (1994), Asness et al. (2013, 2014)
 - Grinblatt & Moskowitz (2004)

 ### 与项目对照
 - `momentum_10d`: 形成期仅 10 天, 低于标准下限 21 天。IC 不显著可理解。
 - 建议注册 `momentum_63d`, `momentum_126d`, `momentum_252d` 三变体。

 ---

 ## 3.2 Earnings-Momentum (盈利动量) — Eq 9

 ### 核心逻辑
 类似价格动量，但排序依据是盈利超预期程度 (SUE)。

 ### 关键公式
 ```
 SUE_i = [EPS_i(t) - EPS_i(t-4Q)] / σ[EPS_unexpected]_8Q          (3.9)
 ```
 - EPS_i(t): 最近公布的季度 EPS
 - EPS_i(t-4Q): 4 季度前 EPS
 - σ_8Q: 过去 8 季度未预期盈利的标准差

 ### 交易规则
 - 按 SUE 排序, 买入 top decile, 卖出 bottom decile
 - 持有期: 6 个月

 ### 关键文献
 - Chan et al. (1996)
 - Bernard & Thomas (1989, 1990)

 ### 与项目对照
 **完全缺失。** 需要: 季度 EPS 数据 + 至少 8 个季度历史。`data/store.py` 的 `get_financials()` 可能已有数据基础。

 ---

 ## 3.3 Value (价值) — Eq 10

 ### 核心逻辑
 B/P = 每股账面价值 / 价格。高 B/P (价值股) 未来收益高于低 B/P (成长股)。

 ### 公式
 ```
 B/P = Book Value Per Share / Price                                  (3.10)
 ```
 注: B/P 与 Book-to-Market 等价 (总量 vs 每股)。

 ### 交易规则
 - 按 B/P 排序, 买入高 B/P (价值股), 卖出低 B/P (成长股)
 - 价格: 可用最新价格 (Asness 2013) 或与账面价值同期价格 (Fama-French 1992)
 - 持有期: 1-6 个月

 ### 关键文献
 - Rosenberg et al. (1985)
 - Fama & French (1992, 1993)

 ### 与项目对照
 - `bp_ratio`: 直接对应, IC=+0.0281, t=1.4 (边缘显著)。扩展 lookback 至 500 天预计可通过 t=2.0。

 ---

 ## 3.4 Low-Volatility Anomaly (低波动异常) — Eqs 3-5

 ### 核心逻辑
 实证发现: 历史低波动组合未来收益 **高于** 历史高波动组合 (与 CAPM 预测相反)。

 ### 公式
 ```
 σ_i = std(R_i)  over lookback period                               (3.4-5)
 ```

 ### 交易规则
 - 按 σ 排序, 买入 bottom decile (低波动), 卖出 top decile (高波动)
 - 计算窗口: 6 个月 (126 天) 至 1 年 (252 天)
 - 持有期: 相似长度, 无需 skip period

 ### 关键文献
 - Ang et al. (2006): 特质波动率异常
 - Haugen (1995), Black (1972)

 ### 与项目对照
 - `volatility_20d`: 窗口 20d 远低于标准 126d。t=0.5 不显著可理解。建议注册 `volatility_126d`。
 - `idio_vol_20d`: 同理应扩展。

 ---

 ## 3.5 Implied Volatility (隐含波动率)

 ### 核心逻辑
 基于期权市场信息: 看涨期权 IV 上升 → 预期正收益, 看跌期权 IV 上升 → 预期负收益。

 ### 交易规则
 - 买入 ΔCall_IV top decile, 卖出 ΔPut_IV top decile
 - 或使用 ΔCall_IV - ΔPut_IV 差值

 ### 关键文献
 - An et al. (2014), Chen et al. (2016)

 ### 与项目对照
 **不适用。** 需要个股期权数据, A 股期权市场仅覆盖 50ETF/300ETF 等少数标的。

 ---

 ## 3.6 Multifactor Portfolio (多因子组合) — Eqs 10-12

 ### 方法 1: 组合权重法 (Eq 3.10)
 ```
 I_A = w_A * I,  Σ w_A = 1                                           (3.10)
 ```
 为每个因子独立构建子组合, 按权重 w_A 分配资金。权重可选: 等权、1/σ、相关性调整。

 ### 方法 2: 排名合成法 (Eq 3.12)
 ```
 s_i = (1/K) * Σ_A rank_A(i)                                        (3.12)
 ```
 各因子的 demeaned rank 取平均得综合得分。

 ### 重要观察
 > "value and momentum are negatively correlated and combining them can add value" — Asness et al. (2013)

 ### 与项目对照
 - `alpha.method: ic_weighted` — 方法 1 的 IC 加权变体，文献验证通过 ✓
 - `alpha.sleeve` — 独立子组合架构，与方法 1 逻辑一致
 - 排名合成法 (Eq 3.12) 未实现 — 备选组合方案

 ---

 ## 3.7 Residual Momentum (残差动量) — Eqs 13-17 ⭐ 高优先级

 ### 核心逻辑
 与普通动量不同: 先回归掉 Fama-French 三因子, 取残差动量。已剥离市场/规模/价值共同因子, 信号更纯。

 ### 三步法

 **Step 1: 36 个月回归 (估计 β)**
 ```
 R_i(t) = α_i + β1*MKT(t) + β2*SMB(t) + β3*HML(t) + ε_i(t)       (3.13)
 ```

 **Step 2: 12 个月形成期残差**
 ```
 ε_i(t) = R_i(t) - [β1*MKT(t) + β2*SMB(t) + β3*HML(t)]            (3.14)
 ```
 注: 不含 α_i, 用 36 个月的 β 估计值。

 **Step 3: 残差标准化**
 ```
 Rbar_i^res = (1/T) * Σ ε_i(t)                                      (3.15)
 S_i^res = Rbar_i^res / std(ε_i)                                    (3.16-17)
 ```

 ### 交易规则
 - 按 S_i^res 排序, 买入 top decile, 卖出 bottom decile
 - 形成期: 12 个月 (skip 1 个月), 回归窗口: 36 个月

 ### 关键文献
 - Blitz et al. (2011)
 - Fama & French (1993) 三因子模型

 ### 与项目对照
 **完全缺失 — 最高优先级新增。** 与 zt_streak/dt_streak (A 股特有) 理论互补。
 A 股适配: 用 沪深300 替代 MKT, 自由流通市值替代 SMB, B/P 替代 HML。

 ---

 ## 3.8 Pairs Trading (配对交易) — Eqs 18-26

 ### 核心逻辑
 找历史高度相关的两只股票, 当价差偏离时: 做空 "rich" (正去均值收益) + 做多 "cheap" (负去均值收益)。

 ### 关键公式
 ```
 r̃_A = r_A - r̄,  r̃_B = r_B - r̄                                      (3.22-24)
 Q_A*P_A(t₀) + Q_B*P_B(t₀) = I  (总投资)                           (3.25)
 Q_A*P_A(t₀) + Q_B*P_B(t₀) = 0  (dollar-neutral)                   (3.26)
 ```

 ### 关键文献
 - Gatev et al. (2006), Vidyamurthy (2004)

 ### 与项目对照
 **缺失。** 需要协整检验 + 配对筛选管道。非当前优先级。

 ---

 ## 3.9 Mean-Reversion — Single Cluster (均值回复-单簇) — Eqs 27-47

 ### 核心逻辑
 配对交易的泛化: N 只高相关股票 (如同一行业) 同时做均值回复。

 ### 关键公式
 ```
 r̃_i = r_i - r̄                                                        (3.27-29)
 D_i = -γ * r̃_i                                                       (3.32)
 γ = I / Σ|r̃_i| * P_i(t₀)                                            (3.33)
 ```
 约束: Σ D_i = I (总投资), Σ D_i = 0 (dollar-neutral)。

 ### 多簇泛化 (Eqs 34-47)
 用线性回归统一处理多行业: Ω 为 N×K 二进行业矩阵, 回归残差自动满足簇内中性。

 ### 与项目对照
 - `reversal_5d`: 单变量反转, t=0.2 不显著。书中 3.9 方法是多股票簇内同时反转, 更稳健。
 - 3.9 方法利用截面相关性过滤噪声, 比单变量 `reversal_5d` 更适合 A 股。

 ---

 ## 3.10 Mean-Reversion — Weighted Regression (均值回复-加权回归) — Eqs 48-53

 ### 核心逻辑
 均值回复的权重回归版本, 用波动率倒数加权回归 + 通用载荷矩阵 Ω, 样本外更稳定。

 ### 关键公式
 ```
 R = Ω * f + ε                                                       (3.38-40)
 w_i = 1/σ_i^2  (波动率倒数加权)                                     (3.49-50)
 R̃ = (I - Ω(Ω^T W Ω)^{-1}Ω^T W) * R                               (3.51-52)
 ```

 ### 关键性质
 - 簇中性: Σ_{i in A} r̃_i = 0 (每个行业/簇内均值为零)
 - 自动 dollar-neutral: Σ r̃_i = 0 (若截距在 Ω 中) (Eq 3.53)

 ### 与项目对照
 - `risk/neutralize.py` 已有行业中性化, 但未用于生成交易信号
 - 3.10 方法可替代 `reversal_5d`: 先行业中性化再算反转, 去除行业联动噪声

 ---

 ## 3.11-3.15 技术分析策略 — Eqs 54-66

 ### 书中定性
 书中明确指出: 单股票技术分析策略 (MA/Support/Resistance/Channel) 被许多专业人士视为 "不科学" (unscientific)。它们在截面统计套利框架下才获得统计意义。

 | 策略 | 公式 | 信号 | 评估 |
 |------|------|------|------|
 | 3.11 Single MA | SMA/EMA 交叉 (3.54-56) | P > MA → 买入 | 不推荐作为因子 |
 | 3.12 Two MAs | 快/慢 MA 交叉 (3.57-58) | MA_short > MA_long → 买入 | 同上 |
 | 3.13 Three MAs | 三 MA 过滤 (3.59) | 三 MA 同向确认 | 同上 |
 | 3.14 Support/Resistance | Pivot Point (3.60-63) | 突破 R → 买入; 跌破 S → 卖出 | 同上 |
 | 3.15 Channel | Donchian Channel (3.64-66) | 触及 floor → 买入; 触及 ceiling → 卖出 | 成交量确认可增强 |

 ### SMA/EMA 公式
 ```
 SMA(t, T) = (1/T) * Σ P(t-i)                                       (3.54)
 EMA(t, T) = λ*P(t) + (1-λ)*EMA(t-1), λ = 2/(T+1)                  (3.55)
 ```

 ### Donchian Channel 公式
 ```
 Ceiling = max_{t-T+1 ≤ τ ≤ t} P(τ)                                 (3.64)
 Floor   = min_{t-T+1 ≤ τ ≤ t} P(τ)                                 (3.65)
 ```

 ### 与项目对照
 - 这些不适合放入因子库 (书中自评 "unscientific")
 - Donchian Channel 的成交量确认思路与 `vol_price_corr_10d` (t=3.4) 有间接关联

 ---

 ## 3.16 Event-Driven M&A (并购套利)

 ### 交易规则
 - **现金并购**: 做多目标公司股票, 赚取并购价与市场价的差额
 - **换股并购**: 做多目标公司 + 做空收购方 (按换股比例)
 - 风险: 交易失败 (并购破裂)

 ### 与项目对照
 **不适用。** 需要实时 M&A 事件数据, 属于事件驱动策略而非截面因子。

 ---

 ## 3.17 ML Single-Stock KNN (K近邻机器学习) — Eqs 67-76

 ### 核心逻辑
 用 KNN 算法预测单股票未来收益 (时序), 无截面交互。

 ### 关键公式
 ```
 Y(t) = 未来 T 个交易日累计收益                                      (3.67)
 X(t) = (MA_价格, MA_成交量, ...)                                    (3.68-71)
 X̃_a = [X_a - min] / [max - min]  (归一化)                          (3.72)
 Y_pred = (1/k) * Σ Y(t_j)  for k nearest neighbors                  (3.74)
 ```
 信号: If Y_pred > θ_long → long; if < θ_short → short。

 ### 关键参数
 - 训练集 60%, 交叉验证 40%
 - k ≈ √N_sample
 - 特征: 日线价格 + 成交量 MA

 ### 与项目对照
 **缺失。** 书中定位为单股票时序策略 (无截面交互)。交叉验证和 KNN 计算量大, 不适合作为日频截面因子。

 ---

 ## 3.18 Risk Model / Dollar-Neutrality (风险模型) — Eqs 77-93

 ### 完整推导
 ```
 P = Σ E_i * H_i          组合预期盈亏                              (3.77)
 V = √(H^T C H)           组合波动率                                (3.78)
 S = P / V                Sharpe Ratio                              (3.79)
 w_i = H_i / I            持仓权重                                  (3.80)
 Σ w_i = 1                满仓约束                                  (3.81)
 ```

 **无约束最优解** (最大化 Sharpe):
 ```
 w = γ * C^{-1} * E                                                  (3.85)
 ```

 **Dollar-Neutral 约束解** (Σ w_i = 0):
 ```
 w = γ * [C^{-1}E - (1^T C^{-1}E / 1^T C^{-1}1) * C^{-1}1]        (3.93)
 ```

 ### 关键文献
 - Markowitz (1952): 均值-方差优化
 - Grinold & Kahn (2000): 多因子风险模型

 ### 与项目对照
 - `risk/covariance.py`: ledoit_wolf_cov ✓
 - `risk/constraints.py`: max_single_position ✓
 - **缺失**: Eq 3.93 的 dollar-neutral 显式解未实现 (当前用 equal_weight bypass)

 ---

 ## 3.19 Market-Making (做市) — Eq 94

 ### 核心逻辑
 赚取 bid-ask spread, 需区分 dumb flow (无信息散户) vs smart flow (有毒知情交易)。

 ### 与项目对照
 **不适用。** 需要 Level 2 报价 + HFT 基础设施, 与日频截面因子完全不同的时间尺度。

 ---

 ## 3.20 Alpha Combos (Alpha 组合) — Eq 95 ⭐ 与 P43 sleeve 直接对应

 ### 11 步组合流程 (Kakushadze & Yu 2017b)
 1. 计算各 alpha 时间序列收益 R_{A,i}(t_s)
 2. 时序去均值: R̃ = R - mean(R)
 3. 归一化方差: R̂ = R̃ / σ
 4. PCA 截断: 保留前 M 个主成分
 5. 截面去均值: 逐个截面减去均值
 6. 二次 PCA 截断: 保留前 M* 个主成分
 7. 计算期望收益 E
 8. d-day MA: **E_A(t_s) = (1/d) Σ R_A(t_{s-a})** (Eq 3.95)
 9. 回归: E_A ~ PCA 矩阵 (无截距, 单位权重)
 10. 权重 = 回归残差: w_A = ε_A (去除共同风险)
 11. 归一化: Σ w_A = 1

 ### 核心思想
 > "ubiquitous alphas are faint, ephemeral and cannot be traded on their own... one combines a large number of such alphas and trades the combined 'mega-alpha'"

 ### 与项目对照
 - P43 sleeve: 每个因子独立分仓, 保留独立信号
 - 3.20 方法: PCA + 回归去重, 压缩为单一 mega-alpha
 - 两方法互补: sleeve 适合少数强因子 (当前), 3.20 适合大量弱因子 (>10)

 ---

 ## 3.21 方法论总结

 书中最后明确区分两类策略:

 | 类型 | 代表策略 | 方法论 | 有效性 |
 |------|---------|------|--------|
 | **Technical Analysis** | MA, S/R, Channel, KNN | 单股票时序 | 被广泛质疑 |
 | **Statistical Arbitrage** | Momentum, Value, Mean-Reversion | 截面统计 | 有学术支撑 |

 关键论点: 截面方法通过大样本统计获得稳健性。这与我们的截面因子评估框架完全一致。

 ---

 ## 因子库对照矩阵

 ### 已有因子 (有文献背书)
 | 我们的因子 | 书中策略 | 标准窗口 | t-stat | 状态 |
 |-----------|---------|---------|--------|------|
 | bp_ratio | 3.3 Value | 无明确窗口 | 1.4 | 近边缘, lookback扩展可过 |
 | volatility_20d | 3.4 Low-Vol | 126-252d | 0.5 | **窗口过短** (20 vs 126d) |
 | roa | — | — | 2.6 | 通过, 书中无直接对应 |
 | roe_reported | — | — | 2.4 | 通过 |
 | zt_streak | — | — | 7.1 | A 股特有, 通过 |
 | dt_streak | — | — | 7.1 | A 股特有, 通过 |
 | vol_price_corr_10d | — | — | 3.4 | 通过 |
 | reversal_5d | 3.9/3.10 | — | 0.2 | **方法弱** (单变量 vs 加权回归) |
 | momentum_10d | 3.1 Price-Mom | 63-252d | 0.1 | **窗口过短** (10 vs 63d) |

 ### 缺失因子 (优先级排序)
 | 优先级 | 因子 | 书中策略 | 实现难度 | 预期效果 |
 |--------|------|---------|---------|---------|
 | P0 | Residual Momentum | 3.7 | 中 (需 Fama-French) | 与 zt_streak 互补, 信号更纯 |
 | P1 | Momentum 63d/126d/252d | 3.1 | 低 (仅改参数) | 替代 10d, 应符合 t≥2.0 |
 | P1 | Earnings Momentum (SUE) | 3.2 | 中 (需季度 EPS) | 基本面动量, 与价格动量互补 |
 | P2 | Mean-Reversion Weighted | 3.10 | 中 | 替代 reversal_5d, 行业中性化后反转 |
 | P2 | Dollar-Neutral Optimizer | 3.18 | 低 | Eq 3.93 替代 equal_weight |
 | P3 | Low-Vol 126d/252d | 3.4 | 低 | 替代 20d, 符合标准窗口 |

 ### 已完成/有基础
 | 项目 | 书中策略 | 状态 |
 |------|---------|------|
 | ic_weighted 多因子合成 | 3.6 方法 1 | ✓ 已实现且符合文献 |
 | sleeve 分仓架构 | 3.6 / 3.20 | ✓ P43 已完成 |
 | ledoit_wolf_cov | 3.18 风险模型 | ✓ 已实现 |
 | 行业中性化 | 3.10 基础设施 | ✓ neutralize.py 已完成 |

 ---

 ## 附录 A: Ch.4 ETFs 策略 (间接关联)

 Ch.4 的 ETF 策略本身不适用于 A 股个股因子, 但以下概念有参考价值:

 | 策略 | 核心概念 | 可迁移部分 |
 |------|---------|----------|
 | 4.1 Sector Momentum Rotation | 行业轮动动量 | 可与 3.6 多因子组合结合 |
 | 4.2 Alpha Rotation | Alpha 替代原始收益排序 | 与 ic_weighted 逻辑一致 |
 | 4.3 R-squared Strategy | 回归 R² + Alpha 双重排序 | 可用于因子评估 (评估 IC 稳定性) |
 | 4.4 Mean-Reversion (IBS) | Internal Bar Strength | 可用于替代 reversal_5d |

 ---

 ## 附录 B: 公式索引

 | Eq编号 | 含义 | 所在策略 |
 |--------|------|---------|
 | 3.1 | 累计收益 R_i(t,T) | 3.1 Price-Momentum |
 | 3.2 | 月均收益 Rbar_i | 3.1 |
 | 3.3-4 | 风险调整收益 S_i | 3.1 |
 | 3.5 | 形成期波动率 σ_i | 3.1 / 3.4 |
 | 3.9 | SUE (标准化未预期盈利) | 3.2 Earnings-Momentum |
 | 3.10 | B/P 比率 | 3.3 Value |
 | 3.10-12 | 多因子组合权重/排名 | 3.6 Multifactor |
 | 3.13 | Fama-French 三因子回归 | 3.7 Residual Momentum |
 | 3.14 | 残差计算 (形成期) | 3.7 |
 | 3.15-17 | 残差标准化 | 3.7 |
 | 3.18-26 | 配对交易收益/仓位 | 3.8 Pairs Trading |
 | 3.27-44 | 均值回复去均值/多簇 | 3.9 Mean-Reversion |
 | 3.48-53 | 加权回归残差 | 3.10 Weighted Regression |
 | 3.54-55 | SMA/EMA 定义 | 3.11 Single MA |
 | 3.56-59 | MA 交叉信号 | 3.11-3.13 |
 | 3.60-63 | Pivot Point / 支撑阻力 | 3.14 Support & Resistance |
 | 3.64-66 | Donchian Channel | 3.15 Channel |
 | 3.67-76 | KNN 特征/预测/信号 | 3.17 ML KNN |
 | 3.77-84 | 组合 P&L/波动率/Sharpe | 3.18 Risk Model |
 | 3.85 | 无约束最优 w | 3.18 |
 | 3.89-93 | Dollar-Neutral 约束解 | 3.18 |
 | 3.94 | 做市 P&L | 3.19 Market-Making |
 | 3.95 | Alpha 期望收益 (d-day MA) | 3.20 Alpha Combos |

 ---

 ## 参考版本
 - EPUB 文本提取 (Ch.3 完整提取, Ch.4 前段)
 - 公式编号引用书中原始编号 (3.x)
 - 与 ADR 020 (核心提取) 互补: ADR 020 聚焦分析与行动建议, ADR 021 聚焦完整参考
 - 与 ADR 007 (因子评估框架) 的评估标准对齐
 - 窗口参数推荐已在 config.yaml `factor.windows` 中落地
