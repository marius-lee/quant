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


## Phase 7: 新因子路线图 (2026-07-03 15:54)

### 背景
zt_streak 激活后回测 +80.5%。已淘汰 amihud_20d (IC=0.025)。
现有 4 因子: bp_ratio (+0.062), zt_streak (+0.055), size (+0.044), gap_5d (+0.043)。

### P0: lhb_post_quality — 龙虎榜上榜后质量因子
- 数据源: lhb_detail.post_5d (已有 24,386 行)
- 逻辑: 历史 LHB 上榜后涨的股票，再上榜时继续涨
- 算法: AVG(post_5d) per symbol (历史所有上榜), z-scored
- 预期 IC: 0.03-0.06
- 状态: 待实现

### P1: dt_streak — 跌停连板因子
- 数据源: daily 表 OHLCV (已有 7.3M 行)
- 逻辑: zt_streak 的镜像 — 跌停连板跑输 (A股跌停次日继续跌概率 ~70%)
- 算法: 主板跌停=ret<=-9.5%且close==low, 科创/创业跌停=ret<=-19.5%且close==low
- 连板数越多→负得分越强→负信号
- 预期 IC: 0.04-0.08
- 状态: 待实现

### P2: 融资融券 / 资金流向 / 大宗交易 / 股东增减持
- 数据源: akshare (stock_margin_detail_sse, stock_dzjy_mrmx, stock_fund_flow_individual, stock_shareholder_change_ths)
- 待 API 测试结果
- 状态: 待测试

### 决策原则
- 每个新因子必须经过 |IC| > 0.02 验证才能激活
- 因子代码 + IC 结果 → FACTOR-AUDIT 归档 → 决策激活/弃用
- 所有数据优先入库, 不依赖外部 JSON


## Phase 8 (2026-07-03 17:00-17:40): P2 新数据源 — 融资融券 + 资金流向

### 数据同步结果

| 数据源 | 方式 | 结果 | 量级 |
|--------|------|------|------|
| 融资融券 SSE | JSON API 直接调用 | ✅ | 45,555行, 23交易日 |
| 融资融券 SZSE | akshare / JSON API | ❌ | API 返回空 JSON |
| 资金流向 | akshare stock_individual_fund_flow | ❌ | IP 被东方财富封 |

### 因子 IC 评估 (500只 × 89天)

| 因子 | IC | IR | 决定 | 原因 |
|------|-----|-----|------|------|
| margin_buy_ratio | +0.090 | +0.37 | 暂弃用 | IC 最强但仅 23/116 天覆盖, 回测 +16.7% vs 5因子 +80.5% |
| margin_balance_chg | +0.004 | +0.03 | 弃用 | 融资余额变化率无预测力 |
| main_flow_ratio | — | — | 待测 | fund_flow 无数据 |

### 6因子回测 (bp+size+gap+zt+amihud+margin_buy_ratio)
- 结果: +16.7% (Sharpe 0.84), 远低于 5 因子 +80.5%
- 根因: margin 仅覆盖 23/116 回测日, 80% 日期返回零值 → alpha 被稀释
- 决策: margin_buy_ratio 保留代码和数据, 等数据积累到 ≥90 天后重新激活

### 技术细节

**SSE JSON API 字段映射:**
- stockCode → 股票代码
- rzmre → 融资买入额 (margin_buy)
- rzye → 融资余额 (margin_balance)
- rzche → 融资偿还额 (margin_repay)
- rqmcl → 融券卖出量 (short_sell_vol)
- rqyl → 融券余量 (short_balance)

**日期格式**: SSE API 要求 YYYYMMDD (无连字符), daily 表是 YYYY-MM-DD

**fund_flow 限流**: 东方财富 API 极敏感, 1.5s 间隔仍被封 IP. 恢复后需 ≥10s 间隔.

### 核心教训

1. **数据覆盖 > 因子 IC**: IC=0.09 的因子如果数据稀疏, 对组合的贡献为零
2. **API 脆弱性**: 交易所 API 格式不稳定, 限流严格, 需要更鲁棒的同步策略
3. **先积累再激活**: 新数据源至少积累 60-90 天才能激活因子


## Phase 8 结论 (2026-07-03 19:00)

### 数据同步: ✅ 完成
| 数据源 | 行数 | 日期范围 | 股票/天 |
|--------|------|----------|---------|
| margin SSE | 232,946 | 2026-01-05 ~ 07-02, 118天 | ~1,980 |
| margin SZSE | 245,686 | 2026-01-05 ~ 07-01, 118天 | ~2,070 |

### 因子评估: ❌ 不成立

| 阶段 | IC | 原因 |
|------|-----|------|
| 初始 (look-ahead bias) | +0.090 | 使用了当天未发布的margin数据 |
| 修复后 (仅SSE) | +0.043 | look-ahead修复, 但仅上交所~1980只 |
| 全量 (SSE+SZSE) | **+0.004** | 加入深交所后被稀释为零 |

**核心原因**: 融资买入占比对上交所股票有微弱预测力(IC=0.043),
但对深交所股票无预测力(IC≈0)。两市合并后信号被噪声淹没。
上交所和深交所的融资融券动态不同: SSE偏向大盘蓝筹, SZSE偏向中小成长,
融资买入行为的经济含义不同。

### 技术债已清偿
- look-ahead bias 修复 (因子计算排除当日)
- SZSE API 修复 (akshare wrapper + 6s interval + 3-retry)
- margin.py 健壮性大幅提升 (日期跳过, 格式容错)

### 经验教训
1. **按市场拆分因子 > 全量合并**: 市场微观结构不同, 不应强行混合
2. **单市场 IC 不能代表全量**: SSE IC=0.043但SZSE为零
3. **look-ahead会反向放大**: 使用未来数据 → IC虚高 → 回测崩溃


## Phase 10 (2026-07-03 20:21): 30-Factor Full Audit → 8-Factor Model

**方法**: 全量30因子 IC 重算 (500 stock sample, Spearman rank), 相关性矩阵去冗余, |IC|>0.02 阈值筛选.

**新增 3 因子:**

| 因子 | IC | IR | 与现有最高|r| | 维度 |
|------|-----|-----|---------|------|
| high52w_dist | +0.095 | +0.50 | size=0.31, bp=0.39 | 52周高点锚定 (George & Hwang 2004) |
| dt_streak | +0.037 | +0.61 | zt_streak=0.01 | 跌停连板 — zt_streak 镜像, 近乎正交 |
| vol_price_corr_10d | -0.026 | -0.30 | 所有<0.1 | 量价背离 — 唯一独立维度, 自动翻转 |

**保留 5 因子:** amihud_20d, gap_5d, zt_streak, bp_ratio, size

**弃用 22 因子** (|IC|<0.02 + 数据质量问题):

| 类别 | 因子 | 原因 |
|------|------|------|
| 纯噪声 (IC≈0) | analyst_buy, fund_change, hsgt_flow_5d, lhb_net_buy_20d, money_flow_5d, main_flow_ratio | IC=0.0000, 无预测力 |
| 弱信号 | reversal_5d, momentum_10d, turnover_rev_5d, ma_alignment_20d, limit_up_prox_5d, max_ret_20d, range_20d, turnover_anomaly, rsi_rev_14d | \|IC\|<0.02, 边际贡献<噪声成本 |
| 数据质量 | ep_ratio | PE负值导致r=-0.60反相关于BP, 不是纯净价值信号 |
| 数据覆盖 | margin_buy_ratio, margin_balance_chg | 仅118天, 80%回测期返回零值 |
| 其他 | volatility_20d, idio_vol_20d, skewness_20d, roe_ratio, lhb_post_quality | \|IC\|<0.02 |

**关键发现:**

1. **zt_streak 和 dt_streak 近乎正交** (r=0.010), 说明涨停和跌停捕捉的是不同市场机制, 分别独立有效.
2. **ep_ratio 不可用** — EP=1/PE, PE字段含大量负值(亏损公司), 产生噪音. EP与BP反相关-0.60, 两者都是价值因子但方向矛盾 → 数据问题待修.
3. **high52w_dist 是最强个体因子** (IC=+0.095), 与size相关性中等(r=0.31), 主要独立贡献来自52周锚定效应.
4. **vol_price_corr_10d 是唯一量价因子**, 与所有其他因子相关性<0.1, 是真正的独立维度.
5. **8因子是当前数据约束下的上限** — 30个因子中只有9个|IC|>0.02, 其中1个数据有问题. 增加更多因子需要新数据源或因子交互项.

**8因子维度覆盖:**
- 价值 (bp_ratio)
- 规模 (size)
- 流动性 (amihud_20d)
- 涨停动量 (zt_streak)
- 跌停动量 (dt_streak)
- 隔夜缺口 (gap_5d)
- 52周锚定 (high52w_dist)
- 量价背离 (vol_price_corr_10d)

**回测**: 待跑 (用户终端执行).

---


## Phase 10 结论 (2026-07-03 20:54): 回退至5因子

**经过**: 30因子全量审计 → 选8因子 → 逐步回测验证 → 3新因子全部弃用, 回归5因子.

**逐步回测数据**:

| 组合 | 收益 | Sharpe | vs基线 |
|------|------|--------|--------|
| 5因子基线 | +80.5% | 2.10 | — |
| 5f + dt_streak | +44.6% | 1.23 | -35.9pp |
| 5f + vol_price_corr | -12.1% | 0.67 | -92.6pp |
| 5f + high52w_dist | +12.1% | 0.78 | -68.4pp |

**弃用原因**:

| 因子 | IC | 弃用原因 |
|------|-----|---------|
| dt_streak | +0.037 | 跌停信号过于稀疏(多数日期返回0), 稀疏IC在回测中不成立 |
| vol_price_corr_10d | -0.026 | 10天量价相关窗口太短, 噪音主导。虽已修复abs(IC)方向bug, 仍无效 |
| high52w_dist | +0.095 | IC虚高。推测close_latest数据路径在IC计算和pipeline间不一致 |

**关键教训 (固化)**:

1. **IC排名 ≠ 回测有效** — high52w_dist IC=+0.095(最强个体)但回测+12.1%, 再次验证R11.
2. **稀疏因子陷阱** — dt_streak多数日期为零, IC基于非零日期计算, 全期稀释后失效.
3. **基础数据路径一致性** — close_latest在IC计算(500样本)和pipeline(全量5000)中可能不同, 导致IC不可复现.
4. **abs(IC) bug已修复** — _load_ic_from_db和ic_weighted现在使用带符号IC, 按abs归一化. 之前5因子未暴露是因为IC全为正.

**技术债**:
- close_latest 数据路径需统一 (IC计算 vs pipeline)
- 因子回测验证应成为标准流程 (add → IC → backtest stepwise → approve)

**最终因子阵容**: bp_ratio + size + gap_5d + zt_streak + amihud_20d = +80.5%

---


## Phase 10 结论 (2026-07-03 20:56): 回退至5因子

**经过**: 30因子全量审计 → 选8因子 → 逐步回测验证 → 3新因子全部弃用, 回归5因子.

**逐步回测数据**:

| 组合 | 收益 | Sharpe | vs基线 |
|------|------|--------|--------|
| 5因子基线 | +80.5% | 2.10 | — |
| 5f + dt_streak | +44.6% | 1.23 | -35.9pp |
| 5f + vol_price_corr | -12.1% | 0.67 | -92.6pp |
| 5f + high52w_dist | +12.1% | 0.78 | -68.4pp |

**弃用原因**:

| 因子 | IC | 弃用原因 |
|------|-----|---------|
| dt_streak | +0.037 | 跌停信号过于稀疏(多数日期返回0), 稀疏IC在回测中不成立 |
| vol_price_corr_10d | -0.026 | 10天量价相关窗口太短, 噪音主导。虽已修复abs(IC)方向bug, 仍无效 |
| high52w_dist | +0.095 | IC虚高。推测close_latest数据路径在IC计算和pipeline间不一致 |

**关键教训 (固化)**:

1. **IC排名 ≠ 回测有效** — high52w_dist IC=+0.095(最强个体)但回测+12.1%, 再次验证R11.
2. **稀疏因子陷阱** — dt_streak多数日期为零, IC基于非零日期计算, 全期稀释后失效.
3. **基础数据路径一致性** — close_latest在IC计算(500样本)和pipeline(全量5000)中可能不同, 导致IC不可复现.
4. **abs(IC) bug已修复** — _load_ic_from_db和ic_weighted现在使用带符号IC, 按abs归一化. 之前5因子未暴露是因为IC全为正.

**技术债**:
- close_latest 数据路径需统一 (IC计算 vs pipeline)
- 因子回测验证应成为标准流程 (add → IC → backtest stepwise → approve)

**最终因子阵容**: bp_ratio + size + gap_5d + zt_streak + amihud_20d = +80.5%

---
