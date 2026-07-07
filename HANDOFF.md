# HANDOFF — quant 项目当前状态

**最后更新**: 2026-07-07 23:30 CST

## 最近提交

| 提交 | 内容 |
|------|------|
| b236d4a | feat: 五阶段标准回测评估完整实现 (CPCV + PBO + Phase 5) |
| c86a457 | docs: 更新 HANDOFF.md — compute.py 拆分完成 |
| 0eb2a51 | fix: 修复 compute.py 拆分的循环依赖和缺失函数 |

## 模块结构

### factor/ (Layer 2: 因子计算)
| 文件 | 职责 |
|------|------|
| `factor/compute.py` (~2550行) | 全部因子函数 + maps + compute_all_factors + load_active_* |
| `factor/registry.py` | _cs_zscore + _db_connect + _FIN_FACTORS |
| `factor/orchestrator.py` | get_factor_names (延迟导入) |
| `factor/__init__.py` | __getattr__ 惰性导入, 打破循环 |
| `config/constants.py` | 全局常量 + _require_cfg + _market_db_path |

### evaluation/ (Layer 8: 回测评估 — 新增)
| 文件 | 职责 |
|------|------|
| `evaluation/phase1_data.py` | Stage 1: 股票池 + 数据范围 |
| `evaluation/phase2_single.py` | Stage 2: IC/\|t\|/ICIR/half-life 四维筛选 |
| `evaluation/phase3_oos.py` | Stage 3: CPCV walk-forward OOS + PBO |
| `evaluation/phase4_costs.py` | Stage 4: 交易成本确认 |
| `evaluation/phase5_monitor.py` | Stage 5: 拥挤度/IC衰减/换手率/容量 |
| `evaluation/cpcv.py` | Purged Walk-Forward CV (De Prado 2018 Ch.7) |
| `evaluation/pbo.py` | PBO + DSR (De Prado 2018 Ch.8) |

## 回测评估标准 (ADR 026)

五阶段流程: 数据准备 → 单因子检验 → CPCV OOS + PBO → 交易成本 → 持续监控

- **CPCV**: N=5 groups, embargo=1d, 4 folds (De Prado 2018)
- **PBO**: logit(PBO) < -0.847 (PBO < 0.3)
- **Phase 2 阈值**: \|IC\|≥0.02, \|t\|≥2.0, ICIR≥0.5, half-life≥20d
- **执行**: `PYTHONPATH=. bash scripts/eval_standard.sh [--phase5]`

## 当前状态

- **factor_registry**: 55 因子注册, 31 active / 24 deprecated
- **执行价格**: Sina 实时 open + 除权检测 10%
- **launchd**: scheduler ✅ KeepAlive / webapp ❌
- **数据字典**: docs/DATA_DICTIONARY.md

## 下一步

1. 运行 `PYTHONPATH=. bash scripts/eval_standard.sh` 对 31 active 因子做五阶段评估
2. 根据评估结果更新 factor_registry status 和 notes
3. 可选: Registry 模式进一步拆分 compute.py 因子函数

## 关键约束

- 修改前先备份 + git 提交
- 数值参数放 config/config.yaml
- 永不 fallback 执行价格; 因子 status 变更记入 notes
- 修改后文档同步更新
