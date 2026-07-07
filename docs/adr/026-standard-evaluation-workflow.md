# ADR 026: 五阶段因子评估标准流程

**日期**: 2026-07-07
**状态**: 已生效
**替代**: 部分替代 ADR 007 (保留 L1 IC t-test 框架，替换 L2/L3 为 walk-forward + PBO)
**来源**: docs/research/量化因子回测策略业界标准_2026-07-07.md (10路并行搜索，学术文献+8券商+6私募)

## 背景

原有评估流程 (eval_stepwise.sh) 三个核心问题：
1. IC 计算、因子选择、回测验证三步共用同一份 120 天数据（全样本内）
2. 未做多重检验修正（31 个因子并行测试，假阳性率 ~80%）
3. 未使用 IC 半衰期过滤（A 股日频中位数仅 6.8 天）

## 决策

采用五阶段评估流程，脚本: `scripts/eval_standard.sh`

### Phase 1: 数据准备
- 股票池: 全 A，剔除 ST、上市 <60 天
- 数据周期: 日频，后复权
- 数据源: daily + limit_up_pool + daily_valuation + financial_* 表

### Phase 2: 单因子检验
- |RankIC| ≥ 0.02（券商共识底线）
- |t| ≥ 2.0（单次检验 95% 置信 + Phase 3 walk-forward OOS 双重过滤，无需 HLZ t≥3.0）
- ICIR ≥ 0.5（明汯/灵均标准）
- 半衰期 ≥ 20 天（月度调仓要求）
- 数据: n_symbols=0（全量）, lookback=120

### Phase 3: 过拟合防范 (Walk-Forward + PBO)
- 训练/测试: walk-forward rolling window，训练 78d / 测试 21d，3 个验证窗口
- embargo = 1 个交易日（T+1 制度）
- PBO < 0.3 + 夏普衰减 < 50%
- 逐个候选因子加入，OOS IR 提升则保留

### Phase 4: 交易成本扣减
- 回测已含 CostModel（佣金万三 + 最低 5 元 + 印花税千一 + 滑点）
- 扣费后夏普 > 0.3 才入库
- 涨停日买入信号不可执行

### Phase 5: 持续监控（scheduler 每日执行，不做因子增删）

## 参数来源

所有阈值存储在 `config/config.yaml` → `factor.evaluation`:
- `t_threshold: 2.0`
- `min_abs_ic: 0.02`
- `min_icir: 0.5`
- `min_half_life: 20`
- `cpcv_groups: 5`
- `cpcv_test_groups: 1`
- `embargo_days: 1`
- `pbo_max: 0.3`
- `sharpe_decay_max: 0.5`
- `net_sharpe_min: 0.3`

## 与旧流程对比

| 维度 | 旧 eval_stepwise.sh | 新 eval_standard.sh |
|------|-------------------|-------------------|
| 数据划分 | 120 天全量（训练=测试） | 训练/测试 walk-forward 分窗 |
| IC 门槛 | 无 | \|IC\|≥0.02 |
| IR 门槛 | 无 | ICIR≥0.5 |
| 半衰期 | 不算 | ≥20 天 |
| 过拟合检测 | 无 | PBO<0.3 + 夏普衰减<50% |
| 多重检验 | 无 | Phase 3 OOS 双重过滤 |
| 扣费验证 | 隐式 | 显式扣费后夏普>0.3 |
| 运行时间 | ~2h (全量) | ~3h (walk-forward 多窗口) |

## 后果

- eval_layer12.sh 保留为快速 L1 筛选（不改变 factor_registry 状态）
- eval_stepwise.sh 保留为兼容旧流程，建议用 eval_standard.sh 替代
- 所有新因子接入后必须通过 eval_standard.sh 验证才能标记 active
- 小资金（<10 万）和机构资金使用同一套标准（t=2.0 + OOS 验证，不区隔）
