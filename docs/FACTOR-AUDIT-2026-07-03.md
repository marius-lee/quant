# 因子决策审计轨迹 (2026-07-03)

**原则**: 每次因子增删必须有 IC 数据支撑、书面原因、可回溯。

---

## 初始状态 (会话开始)

16 个因子, 全在 `FACTOR_REGISTRY` + `FUNDAMENTAL_FACTOR_REGISTRY` 硬编码 dict 中。
IC 数据来自旧 `factor_cache.json` (500 只采样, 日期不详)。

## Phase 1 (dc90a31): factor_registry 表创建

16 因子全量入库, IC 和 status_reason 写入数据库。基于旧 IC 数据:

| 因子 | IC | 判定 | 原因 |
|------|-----|:--:|------|
| bp_ratio | +0.059 | active | 最强个体因子 |
| reversal_5d | +0.036 | active | A股反转效应 |
| amihud_20d | +0.032 | active | 非流动性溢价 |
| momentum_10d | +0.024 | active | 中期动量 |
| gap_5d | +0.024 | active | 隔夜缺口 |
| turnover_rev_5d | +0.022 | active | 换手率反转 |
| skewness_20d | +0.016 | deprecated | IC<0.02 |
| range_20d | +0.009 | deprecated | 纯噪声 |
| volatility_20d | +0.006 | deprecated | 纯噪声 |
| idio_vol_20d | +0.006 | deprecated | 纯噪声 |
| max_ret_20d | +0.005 | deprecated | 纯噪声 |
| hsgt_flow_5d | 0.000 | experimental | 无数据 |
| high52w_dist | 0.000 | experimental | 无数据 |
| ep_ratio | -0.020 | deprecated | PE失真 |
| roe_ratio | -0.012 | deprecated | 推导精度差 |
| size | -0.101 | deprecated | 方向理解错(实为大盘溢价) |

## Phase 2 (888bf75): 方向修正 + 相关性矩阵

- momentum_10d: -cum → cum → 回退到 -cum (89af8d9)
  - 原因: IC=+0.024 是在 -cum 方向下测的, 翻转向量后 IC 失效
- bp_ratio: -bp → bp → 回退到 -bp
  - 原因: 同上, IC=+0.059 依赖 -bp 方向
- 教训: 因子代码方向与 IC 值耦合, 改代码=IC 失效, 必须重新测

## Step 3 (f049178): size 激活 + RSI 新增

- **size**: deprecated → active
  - 原因: IC=|0.101| 是全部 16 因子中最强
  - 方向修正: -log(total_mv) → +log(total_mv), A股大盘溢价
  - IC 修正: -0.101 → +0.101
- **rsi_rev_14d**: 新增 → active
  - 预期 IC: 0.03-0.04 (A股 RSI 均值回复实证)
  - 实际 IC: 待测
- 因子数: 6 → 8

## IC 全量重算 (2026-07-03 10:12)

**方法**: 1000 只股票 × 120 交易日, Spearman rank IC
**脚本**: `/tmp/r.py` — `compute_factor_stats(n_symbols=1000, lookback=120)`

| 因子 | IC | IC_IR | 与旧值差异 |
|------|-----|-------|-----------|
| size | +0.080 | +0.33 | -0.021 (大盘溢价略降但仍最强) |
| bp_ratio | +0.054 | +0.25 | -0.005 (稳定) |
| amihud_20d | +0.044 | +0.23 | +0.012 (更好了) |
| turnover_rev_5d | +0.025 | +0.18 | +0.003 (稳定) |
| reversal_5d | +0.015 | +0.09 | **-0.021 → 跌破 0.02 阈值** |
| gap_5d | +0.011 | +0.09 | **-0.013 → 跌破 0.02 阈值** |
| momentum_10d | +0.008 | +0.05 | **-0.016 → 跌破 0.02 阈值** |
| rsi_rev_14d | -0.004 | -0.02 | **零信号, RSI均值回复不成立** |

## 精炼至 4 因子 (2026-07-03 10:14)

按 |IC|>0.02 标准淘汰 4 个弱因子:

| 淘汰因子 | IC | 原因 |
|---------|-----|------|
| reversal_5d | 0.015 | 全量重算跌破阈值 |
| gap_5d | 0.011 | 全量重算跌破阈值 |
| momentum_10d | 0.008 | 全量重算跌破阈值 |
| rsi_rev_14d | -0.004 | 零信号 |

**结果**: 回测 +1.0% → -24.8% ❌

## 恢复至 7 因子 (2026-07-03 10:17)

**教训**: 纯 |IC|>0.02 阈值筛选忽略了信号多样性价值。IC=0.01 的因子单看是噪声, 组合中提供平滑, 防止 alpha 被单一因子(size)完全主导。

恢复: reversal_5d(0.015), gap_5d(0.011), momentum_10d(0.008)
保留淘汰: rsi_rev_14d(-0.004, 负IC不能要)

## 最终 7 因子 (当前)

| # | 因子 | IC | IC_IR | 入选原因 |
|---|------|-----|-------|---------|
| 1 | size | +0.080 | +0.33 | 最强个体因子, A股大盘溢价 |
| 2 | bp_ratio | +0.054 | +0.25 | 价值/成长, 稳定 |
| 3 | amihud_20d | +0.044 | +0.23 | 非流动性溢价 |
| 4 | turnover_rev_5d | +0.025 | +0.18 | 换手率反转 |
| 5 | reversal_5d | +0.015 | +0.09 | 弱信号但提供多样性 |
| 6 | gap_5d | +0.011 | +0.09 | 弱信号但提供多样性 |
| 7 | momentum_10d | +0.008 | +0.05 | 最弱但防止size过度集中 |

**筛选标准** (写入 factor_registry 表):
- |IC| > 0 (必须有正向预测力, 排除 rsi_rev_14d)
- 弱因子(IC<0.02)保留但 IC 加权自动降权
- 所有决策记录在 factor_registry.status + status_reason

## 操作规范

1. **加因子**: 先在 1000 只 × 120 天上测 IC → IC>0 才入库 → status='active', 写 status_reason
2. **删因子**: UPDATE status='deprecated', 写 status_reason → 不改代码
3. **改方向**: 禁止直接改代码 → 先重测 IC → 如果方向错, 改代码后必须重测 IC 更新 factor_registry
4. **IC 重算**: 每月或每次因子变更后运行 `compute_factor_stats(n_symbols=1000, lookback=120)`

---

## Phase 4 (2026-07-03 12:35): 数据修复后全量重评

**根因发现**: Phase 1-3 的所有 IC 数据基于 Sina 未复权日线 (除权除息日单日跳 -35%)。
数据修复后 (删除 Sina 源, 全部改用 tencent/akshare qfq 前复权) 重测 IC:

| 因子 | 修复前 IC | 修复后 IC | 变化 |
|------|----------|----------|------|
| bp_ratio | +0.054 | **+0.062** | ↑ 最强 |
| size | +0.080 | +0.044 | ↓ 但仍强 |
| gap_5d | +0.011 | **+0.043** | ↑↑ 从噪声变强因子 |
| amihud_20d | +0.044 | +0.025 | ↓ |
| turnover_rev_5d | +0.025 | +0.012 | ↓ 跌破阈值 |
| reversal_5d | +0.015 | **-0.018** | ↻ 方向反转! |
| momentum_10d | +0.008 | **-0.014** | ↻ 方向反转! |

### 修复动作

1. **方向修正** (eb402d3): momentum_10d 和 reversal_5d 从 `-cum` 改为 `+cum`
   - 原因: 旧 IC 基于脏数据, 错误认为 A 股存在短期反转效应
   - 干净数据证实: A 股短期是动量效应, 不是反转
   
2. **退役 3 弱因子** (eb402d3): momentum_10d, reversal_5d, turnover_rev_5d
   - 原因: |IC| < 0.02, 纯噪声。Phase 3 的"多样性"论证基于脏数据, 不成立

### 最终 4 因子 (当前)

| # | 因子 | IC | 类别 | 来源 |
|---|------|-----|------|------|
| 1 | bp_ratio | +0.062 | 价值 | Fama & French (1992) |
| 2 | size | +0.044 | 规模 | Fama & French (1993) |
| 3 | gap_5d | +0.043 | 隔夜 | A 股 T+1 独有异象 |
| 4 | amihud_20d | +0.025 | 流动性 | Amihud (2002) |

覆盖 Fama-French 五因子中的 2 个维度 (价值 HML + 规模 SMB), 
加上 A 股特有的隔夜缺口和流动性溢价。4 因子之间相关性低 (avg pairwise ρ < 0.3)。

### 退役因子 (禁止恢复)

以下因子 IC 低于 0.02 阈值, 已在 factor_registry 标记为 deprecated。
**任何情况下不得重新激活**, 除非用 ≥1000 只股票 × ≥120 天重新验证且 |IC| > 0.02:

| 因子 | 退役时 IC | 退役原因 |
|------|----------|---------|
| momentum_10d | -0.014 | 方向错误 + IC 不足 |
| reversal_5d | -0.018 | 方向错误 + IC 不足 |
| turnover_rev_5d | +0.012 | IC 不足 |
| ep_ratio | -0.020 | PE 失真 (A 股 PE 受非经常损益污染) |
| roe_ratio | -0.012 | EPS/BVPS 推导精度差 |
| rsi_rev_14d | -0.004 | 零信号 |
| volatility_20d | +0.006 | 纯噪声 |
| skewness_20d | +0.016 | IC 不足 |
| max_ret_20d | +0.005 | 纯噪声 |
| range_20d | +0.009 | 纯噪声 |
| idio_vol_20d | +0.006 | 纯噪声 |
| hsgt_flow_5d | 0.000 | 无数据 |
| high52w_dist | 0.000 | 无数据 |

### 核心教训

**"多样性"不能成为保留噪声因子的借口。** Phase 3 恢复 7 因子的"多样性"论证是错误的 —
那 3 个弱因子 (IC=0.008-0.015) 在 IC 加权合成中总权重 < 15%, 
对 alpha 的贡献是纯随机扰动。减到 4 因子后信号更纯净。


## Phase 5: lhb_net_buy_20d 新增 (2026-07-03 14:48)

### 添加原因
- lhb_detail 表补齐后拥有 25,490 行龙虎榜数据 (4,292 只股票, 2025-01~2026-07)
- 龙虎榜净买入是 A 股最直接的机构/游资资金流信号
- A 股实证: 龙虎榜净买入与次日收益正相关 (IC≈0.04-0.08)

### 因子设计
- 窗口: 20 个交易日
- 公式: SUM(net_buy) / AVG(circ_mv) — 净买入占流通市值比
- 未上榜股票赋 0 (中性)
- 截面 z-score 标准化

### 状态
- 注册为 experimental, 待 IC 评估
- 代码位置: factor/compute.py compute_lhb_net_buy()
- 如果 |IC| > 0.02 且方向正确, 激活替换最弱因子 amihud_20d (IC=0.025)


## Phase 6: zt_streak 激活 (2026-07-03 15:31)

### 设计
- 数据源: daily 表 OHLCV (不依赖 limit_up_pool API)
- 算法: 主板(60/00)涨停=ret>=9.5%且close==high, 科创/创业(68/30)涨停=ret>=19.5%且close==high
- 连板计数: 从当日往前连续涨停天数
- 评分: 倒U型 (1连板→1, 2→3, 3→6, 4→10, 5→8, 6+→递减)
- 覆盖: 6年历史数据 (2020-2026 daily表 7.3M行)

### IC 评估
- IC=+0.0553 IR=+0.75 — 5因子中IR最高(第二名的2倍)
- 与 bp_ratio 相关性最低, 提供独特信号

### 回测验证
- 从 -11.0% → +80.5% (Sharpe -0.36 → +2.10)
- 超额收益 α: +75.0%
- 信息比率: 2.01
- 周期: 2026-01-01 → 2026-06-30 (6个月, 24个调仓日)
- 对比: 5因子(含zt_streak) vs 4因子(不含zt_streak) → zt_streak贡献了91.5%的收益增量
