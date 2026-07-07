# HANDOFF — quant 项目当前状态

**最后更新**: 2026-07-07 12:00 CST

## 最近提交

| 提交 | 内容 |
|------|------|
| 1437db6 | feat: P70 四新因子接入 — OIR + STR + ABN_TURN + OCFP |
| 9a55dfa | feat: factor_registry 增加 notes 字段 + 数据字典 |
| e74a00a | fix: execute_signals 执行价格 — Sina 实时开盘价替代 market.db fallback |
| 117b6d9 | fix: 除权除息检测 — ExecutionEngine._check_ex_dividend() |

## 当前状态

- **factor_registry**: 48 个因子全部 active (44 原有 + 4 新增)
- **新因子**: day_night (OIR), str (STR), abn_turnover, ocfp — 详见 docs/research/
- **执行价格链**: 已修复 (Sina 实时 open, 除权检测 10%)
- **数据字典**: docs/DATA_DICTIONARY.md

## 下一步 (必须按顺序)

1. 终端跑 `PYTHONPATH=. bash scripts/eval_layer12.sh` — 评估 48 个因子
2. 根据 L1 t-test (t≥2.0) 标记不通过因子为 deprecated, 记入 notes
3. 通过 L1 的因子跑 L3 stepwise backtest

## 关键约束

- 所有数值参数放 config/config.yaml
- 永不 fallback 执行价格
- 因子 status 变更必须记入 notes 字段 (追加式)
- 修改后文档同步更新
