# HANDOFF — quant 项目当前状态

**最后更新**: 2026-07-07 11:05 CST

## 最近提交

| 提交 | 内容 |
|------|------|
| 9a55dfa | feat: factor_registry 增加 notes 字段 + 数据字典 |
| e74a00a | fix: execute_signals 执行价格 — Sina 实时开盘价替代 market.db fallback |
| 117b6d9 | fix: 除权除息检测 — ExecutionEngine._check_ex_dividend() |

## 当前状态

- **factor_registry**: 44 个因子全部 active，notes 字段已加，等待跑 eval_layer12.sh 后按 t-test 标记
- **执行价格链**: 已修复，Sina 实时 open 替代 fallback，除权检测 10% 阈值
- **数据字典**: docs/DATA_DICTIONARY.md 已建，22 张表

## 下一步

1. 终端跑 `eval_layer12.sh` → 产出 L1/L2 结果
2. 根据 t-test 标记不通过因子为 deprecated（记入 notes）
3. 通过 L1 的因子跑 L3 stepwise backtest

## 关键约束

- 所有数值参数放 config/config.yaml
- 永不 fallback 执行价格
- 修改后文档同步更新
- factor 状态变更必须记入 notes 字段
