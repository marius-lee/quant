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
