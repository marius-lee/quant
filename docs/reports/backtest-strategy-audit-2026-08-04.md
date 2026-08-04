# 回测策略流程完整分析报告

日期: 2026-08-04
分析范围: 全链路 — backtest/loop.py → pipeline.py → alpha/ → risk/ → optimizer/ → execution/

## 一、整体流程概览（代码实际路径）

```
回测入口 backtest/loop.py::run_backtest()
  ├─ 初始化: ExecutionEngine, FactorStore, DataStore, CostModel, RiskManager
  ├─ 预加载: 全量行情数据, 预计算共享原语(primitives), 预加载涨停数据
  ├─ Walk-forward IC 初始化: compute_backtest_ic(120天)
  ├─ Regime detector: HMM 用起始日前数据训练（PIT正确）
  │
  └─ 主循环: for each trading day T:
     │
     ├─ [调仓日判定] is_rebalance_day(T+1)  → weekly / daily
     │  ├─ 非调仓日: 跳过信号生成, 只跑硬止损
     │  └─ 调仓日:
     │    │
     │    ├─ [Step 1] 数据更新: skip_pull=True (回测)
     │    ├─ [Step 2] 加载行情 + 基本面 × UniverseRepo
     │    ├─ [Step 2.3] 风险预过滤: 流动性/价格/ST/涨停封死
     │    ├─ [Step 2.5] 股票池缩减: rank_by_turnover → top N
     │    ├─ [Step 3] 因子 + Alpha:
     │    │    ├─ FactorStore.load() 读缓存因子值
     │    │    ├─ ic_map = compute_backtest_ic(120d window, retrain every 60d)
     │    │    ├─ probation 因子 IC 减半
     │    │    ├─ Bayesian shrinkage (仅live)
     │    │    ├─ AlphaModel.combine() / combine_regime()
     │    │    └─ AlphaModel.rank() → sigmoid 软截断
     │    ├─ [Step 4] 风险:
     │    │    ├─ neutralize(alpha, industries, market_caps) → 联合回归
     │    │    └─ covariance_matrix(log_ret, ledoit_wolf) ← 懒计算
     │    └─ [Step 5] 优化:
     │         ├─ PortfolioConstructor.construct() → Nano/Micro/Small 三层
     │         ├─ _apply_tc_band() → 成本带过滤
     │         └─ _apply_min_weight() → 最低仓位过滤
     │
     ├─ [Execution] SimulatedBroker.execute() at T+1 open prices:
     │    ├─ ExecutionModel.run(): 冷却 → 止损 → delta → validate → 分单
     │    ├─ BacktestExecutionModel: 买卖均市价立即成交
     │    └─ Almgren-Chriss 冲击成本应用于成交价
     │
     ├─ [风控] 止损标的 → 冷却登记 (N天禁止买回)
     └─ [记录] MTM收盘价权益 → equity_curve
```

---

## 二、严重问题（按严重程度排序）

### 问题 1: 协方差矩阵 N >> T，Ledoit-Wolf 无法挽救

**位置**: `risk/covariance.py:128-162`, `pipeline.py:336`, `optimizer/portfolio.py:286-288`

**问题**:
- 股票池 800 只（`universe_size: 800`），协方差窗口 252 天（`risk.covariance.window: 252`）
- 日收益面板中，800 只股票 × 252 天 → 样本数 T=252 < 股票数 N=800
- 样本协方差矩阵 rank ≤ 252，但需要 800×800，矩阵奇异不可逆
- Ledoit-Wolf 收缩将矩阵推向"常数相关"目标，本质上假设所有股票间相关系数相同 — 这个假设在 A 股板块轮动剧烈的市场中非常不现实

**业界标准**:
- Barra USE4 使用结构化多因子协方差模型：Σ = X·F·X' + Δ，其中 X 是因子暴露矩阵，F 是因子协方差矩阵，Δ 是对角特质风险
- 这种模型只需估计少量因子间的协方差（通常 30-80 个因子），而不是股票间的全协方差矩阵
- 世界量化（WorldQuant）和九坤都使用因子模型协方差，而不是直接股票样本协方差

**修改方案**:
不再对股票收益直接算协方差，改为：
1. 从因子值构造每只股票的因子暴露向量（截面 z-score），维度 M ≈ 因子数（~20-40）
2. 估计因子协方差矩阵 F_{M×M}（M << N，M << T 自然成立）
3. Σ_stock = X·F·X' + diag(特质风险)
4. 这种方法在 800 只股票 × 252 天时仍然可靠

---

### 问题 2: 交易成本是后置过滤器而非优化目标

**位置**: `optimizer/portfolio.py:313-316`, `optimizer/portfolio.py:323-419`

**问题**:
- 优化器先产生"理想组合"（不考虑换仓成本），然后用 `_apply_tc_band` 事后过滤
- 过滤逻辑：对每对"买入 B 换卖出 A"的配对，检查 benefit < λ × cost
- 这是一个贪心逐对判定，而不是全局最优
- 更关键的是：过滤后可能导致组合不再是最优的 — 当 A 被恢复、B 被削减后，剩余各股的权重分布偏离了初始优化解

**业界标准**:
- Gârleanu & Pedersen (2013) "Dynamic Trading with Predictable Returns and Transaction Costs": 将交易成本直接纳入 Bellman 方程，求得解析解
- Almgren & Chriss (2005): 将冲击成本融入优化目标函数
- Kolm & Ritter (2019): 在均值-方差目标中加 λ·TC 惩罚项

**修改方案**:
在均值-方差优化中直接惩罚换仓：
```
目标: max α'w - (λ/2)·w'Σw - κ·‖w - w_prev‖₁
```
其中 ‖w - w_prev‖₁ 的每一项乘以该股票的换仓成本率。这可以直接写入优化器求解（quadratic + L1 penalty，可转化为 QP 或用 proximal gradient）。

---

### 问题 3: 中性化顺序错误 — 在因子合成后而非前

**位置**: `pipeline.py:333-334`

**问题**:
- `neutralize(alpha, industries, market_caps)` 是在 AlphaModel.combine() 将多因子合成为一个 alpha score 之后执行
- 此时不同因子的行业/市值偏差已经相互混合，中性化无法区分"动量因子选了银行"还是"价值因子选了银行"
- 结果：小市值溢价（A 股最显著的风格效应）可能在合成前被某些因子携带进入 alpha，然后被联合回归部分消除，但消除不彻底

**业界标准**:
- Barra USE4: 每个原始因子先做行业+市值中性化，再合成
- WorldQuant: 每个 alpha 独立中性化
- Grinold & Kahn Ch.4: "neutralize raw signals, not composite scores"

**修改方案**:
1. 因子值从 FactorStore 取出后，对每个因子的截面 z-score 独立做行业+市值中性化
2. 中性化后才送入 AlphaModel.combine()
3. pipeline.py Step 4 的全局 neutralize 可以保留作为二次保险，但主力中性化应在因子层面完成

---

### 问题 4: Sleeve 模式丢失多因子确认信息

**位置**: `alpha/synth.py:87-134`

**问题**:
- `sleeve_compose()` 对每个因子独立取 top N，取并集，重叠股票取 max(z-score)
- 一支被 5 个因子同时选为 top 的股票，和一支只被 1 个因子勉强选入的股票，在最终 pool 里没有差异化
- max(z-score) 操作后，sleeve 模式退化为"按最高单因子得分排"，丢失了多因子共振信息

**业界标准**:
- Grinold & Kahn: 因子合成可以用等权、IC加权、或动态 IC 加权
- Qlib: 使用加权 rank 分位数
- 华泰金工: sleeve 模式用 rank 合成而非 raw z-score max

**修改方案**:
Sleeve 模式下不是取 max，而是:
1. 每个因子将原始 z-score 转为截面 rank 分位数 (0~1)
2. 对出现在多个因子 top-N 中的股票，取其平均 rank 分位（而非 max）
3. 被多因子同时选中的股票自动获得更高平均排位

---

### 问题 5: IC 估计窗口与回测期混叠风险

**位置**: `backtest/loop.py:430-437`

**问题**:
- 今天是 2024-06-15，IC 用 2024-02-15 ~ 2024-06-14 的 120 天数据估计
- 需要确认 `compute_backtest_ic` 内部使用了严格 PIT 截断

**修改方案**:
在 `compute_backtest_ic` 中增加显式的日期截断断言，确保只用 `≤ start_date` 的交易数据。

---

### 问题 6: 止损执行时机 — 已验证正确

**位置**: `backtest/loop.py:494-498`, `backtest/broker.py:46-67`

**分析结论**: 止损在 delta 计算前执行（ExecutionModel.run() line 139-150），资金计算路径正确。无问题，标记为已验证。

---

### 问题 7: Kelly 在 Small 层中使用但参数未动态调整

**位置**: `optimizer/portfolio.py:421-447`

**问题**:
- `_kelly_greedy` 使用 `kelly_fraction: 0.5`（半 Kelly）
- 但 Kelly 公式需要 win_rate 和 win/loss ratio，这些都是静态的
- 市场状态（牛市/熊市/震荡）会显著改变胜率和盈亏比

**修改方案**:
当有 regime_label 时，动态调整 kelly_fraction（牛市 0.6-0.8，震荡 0.3-0.5，熊市 0.1-0.2）。

---

### 问题 8: 缺乏显式的 OOS 验证

**问题**:
- Walk-forward 回测本身是 OOS，但所有参数都是固定的
- 没有一个 holdout period 用于独立验证策略表现

**修改方案**:
1. 在 `run_backtest` 中增加 `oos_start_date` 参数
2. oos 期冻结所有参数
3. 分别报告 IS 和 OOS 指标

---

### 问题 9: 换手率未在优化器层面约束

**位置**: 缺失

**问题**:
- 唯一限制换手的是成本带逐笔判定
- 没有 max_turnover 参数限制单日调仓占总资产的比例
- `compute_trades()` 有 `max_turnover_ratio` 参数但未传入

**修改方案**:
在 construct() 中增加全局换手率检查。

---

### 问题 10: 成本带 σ_daily 硬编码

**位置**: `optimizer/portfolio.py:49-52`

**问题**:
- `σ_daily: 0.02` 设为固定值，但 A 股不同板块的日波动率差异很大

**修改方案**: σ 应使用截面各股的实际波动率或板块波动率。

---

### 问题 11: VaR 检查因 cov is None 而永不执行

**位置**: `pipeline.py:355-368`

**问题**: v393 将协方差改为懒计算，`cov = None`。但 Step 4 的 VaR 检查条件 `cov is not None` 永远为 False。

**修改方案**: VaR 检查移到 optimizer construct() 内部，在协方差计算后执行。

---

### 问题 12: 参数对齐

| 参数 | 当前值 | 问题 | 建议 |
|------|--------|------|------|
| optimzer.tc_horizon_days | 1 | predict()使用5日forward | 改为 5 |
| alpha.top_fraction | 0.3 | 240只→30只 gap 过大 | 阈值可选 |
| backtest.diagnosis_ic_window | 120 | IC采样误差大 | 分析报告记录 |
| optimzer.nano_cap | 30000 | ¥5,000起步差异大 | 降至 10000 |
| optimzer.micro_cap | 100000 | 原值保留 | 不变 |
