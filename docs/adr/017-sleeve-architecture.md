# ADR 017 — 多因子分仓架构

日期: 2026-07-05 | 状态: accepted
关联: P43, ADR 007 (因子评估), ADR 015 (生产配置)

## 背景

项目此前使用 composite 模式做多因子合成：IC 加权或等权将多个因子压缩为单一得分截面，取 top 30% 买入。此方法的缺陷是维度压缩 — reversal（超跌买入）、volatility（低波偏好）、momentum（趋势延续）三种逻辑被加权求和后互相对冲抵消。

## 决策

新增 `alpha.combine_mode` 配置项，支持两种正交的合成模式：

- composite — 加权压缩为单一得分（保留原 logic）
- sleeve — 每个因子独立选 top N 只股票取并集（保留因子独立信号），设为默认

## 实现

config.yaml:
```
alpha:
  combine_mode: sleeve
  sleeve:
    positions_per_factor: 8
    min_factors: 1
```

factor/synth.py 新增 sleeve_compose():
- 每个因子独立按 z-score 降序取 top N
- 所有入选股票取并集
- 返回 Series(index=symbol), 值=1.0

pipeline.py 按 combine_mode 分支:
- sleeve: 调用 sleeve_compose(), 跳过 soft cutoff
- composite: 保留 ic_weighted/equal_weight/intersection 逻辑

## 理论依据

- Grinold & Kahn (2000) Ch8: 因子逻辑独立时，分别选股优于线性压缩
- 实践: Fama-French 三因子模型中子组合独立构建

## 测试

26/26 (20 原有 + 6 sleeve_compose 新增)


---

> **注意**: 本文档中 `positions_per_factor: 8` 为设计时示例值，当前 `config.yaml` 实际值为 20。`min_factors: 1` 无变化。
