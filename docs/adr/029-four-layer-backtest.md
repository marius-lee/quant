# ADR 029: 四层回测架构

**日期**: 2026-07-12
**状态**: P0 已落地 (diagnostics 模块), P1-P2 待实现
**关联**: ADR 019, ADR 028

## 背景

回测仅输出 CAGR/Sharpe，不回答"为什么亏"和"哪个因子有效"。需要业界标准的四层评估体系。

## 决策

四层递进架构：

```
因子评估(IC) → 信号合成(权重) → 组合构建(MV) → 业绩归因(PnL拆解)
```

**P0 (已落地)**:
- `backtest/diagnostics.py` — 回测前置 rolling IC + 盘中因子跟踪 + 盘后自动诊断
- `pipeline.py` — 透传 `factor_values` 和 `alpha_raw` 给回测层
- `backtest/loop.py` — 集成诊断，回测结果含 `diagnosis` 字段

**P1 (待实现)**:
- Walk-forward 训练/测试滚动窗口
- IC合成模式从 `sleeve` 切换为 `ic_weighted`

**P2 (待实现)**:
- 组合优化增强 (输出决策理由)
- 自动参数搜索 (Optuna)

## 拒绝的方案

| 方案 | 原因 |
|------|------|
| 仅增强回测报告，不改架构 | 产出给人看不是给agent看，无法自动优化 |
| 直接上Optuna | 先要有归因才能知道优化什么参数 |
| 每日计算rolling IC | 70因子×120天计算量太大，先做预回测IC |

## 验证

- 67 tests通过
- 回测输出含 `diagnosis` 字段和 `adjustments` 建议
- FactorTracker 逐日记录因子贡献
