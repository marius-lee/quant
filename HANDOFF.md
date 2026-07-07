# HANDOFF — quant 项目当前状态

**最后更新**: 2026-07-07 22:30 CST

## 最近提交

| 提交 | 内容 |
|------|------|
| (未提交) | fix: eval_standard.sh Phase 1 显示有效评估日期范围 (backtest_start_date: 2010-01-01) |
| (未提交) | feat: 五阶段标准评估流程 — eval_standard.sh (CPCV+walk-forward+PBO) |
| (未提交) | perf: 多进程并行因子计算 + 向量化 epa/trcf/ideal_amplitude |
| (未提交) | feat: P72 数据源适配三因子 — EPA + TRCF + 理想振幅 |
| (未提交) | feat: P71 涨跌停四因子 — seal_turnover_ratio + seal_time + limit_touch_no_seal + net_limit_ratio |
| (未提交) | refactor: 批量标记 24 因子为 deprecated, 记入 notes |
| 1437db6 | feat: P70 四新因子接入 — OIR + STR + ABN_TURN + OCFP |
| 9a55dfa | feat: factor_registry 增加 notes 字段 + 数据字典 |
| e74a00a | fix: execute_signals 执行价格 — Sina 实时开盘价替代 market.db fallback |
| 117b6d9 | fix: 除权除息检测 — ExecutionEngine._check_ex_dividend() |

## 当前状态

- **factor_registry**: 55 因子注册, 31 active / 24 deprecated (详见 docs/research/因子失效记录_2026-07-07.md)
- **新因子 (11 个)**: P70 (OIR/STR/ABN_TURN/OCFP) + P71 (seal_turnover_ratio/seal_time/limit_touch_no_seal/net_limit_ratio) + P72 (epa/trcf/ideal_amplitude)
- **评估标准**: ADR 026 — 五阶段标准流程 (CPCV + walk-forward + PBO), 脚本 `scripts/eval_standard.sh`
- **评估数据起点**: `config.yaml factor.evaluation.backtest_start_date = 2010-01-01` (排除股权分置改革前不可比数据)
- **执行价格链**: 已修复 (Sina 实时 open, 除权检测 10%) — ADR 017
- **计算性能**: 向量化 epa/trcf/ideal_amplitude + ThreadPoolExecutor 并行因子计算
- **launchd KeepAlive**: scheduler ✅ / webapp ❌ (须走 restart.sh) — ADR 025
- **数据字典**: docs/DATA_DICTIONARY.md

## 下一步

1. ✅ 已完成 — 因子失效记录 (24 deprecated)
2. ✅ 已完成 — 多进程并行因子计算 (ThreadPoolExecutor)
3. ✅ 已完成 — 五阶段标准评估流程 (CPCV + walk-forward + PBO)
4. 下一步: 运行 `PYTHONPATH=. bash scripts/eval_standard.sh` 对 31 active 因子做完整五阶段评估
5. 根据 Phase 2-3 结果更新 factor_registry status 和 notes

## 关键约束

- 所有数值参数放 config/config.yaml
- 永不 fallback 执行价格
- 因子 status 变更必须记入 notes 字段 (追加式)
- 修改后文档同步更新
