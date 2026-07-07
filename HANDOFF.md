# HANDOFF — quant 项目当前状态

**最后更新**: 2026-07-07 23:00 CST

## 最近提交

| 提交 | 内容 |
|------|------|
| 0eb2a51 | fix: 修复 compute.py 拆分的循环依赖和缺失函数 |
| (未提交) | fix: eval_standard.sh Phase 1 显示有效评估日期范围 |
| (未提交) | feat: 五阶段标准评估流程 — eval_standard.sh |
| (未提交) | perf: 多进程并行因子计算 + 向量化 epa/trcf/ideal_amplitude |

## 模块结构 (compute.py 拆分后)

| 文件 | 行数 | 职责 |
|------|------|------|
| `config/constants.py` | 70 | 全局常量 (从 config.yaml) + `_require_cfg` + `_market_db_path` |
| `factor/registry.py` | 45 | `_cs_zscore` + `_db_connect` + `_FIN_FACTORS` |
| `factor/orchestrator.py` | 25 | `get_factor_names` (延迟导入, 零循环依赖) |
| `factor/compute.py` | ~2550 | 全部因子函数 + maps + `compute_all_factors` + `load_active_*` |
| `factor/__init__.py` | 55 | `__getattr__` 惰性导入, 打破循环 |

### 拆分关键设计

- **__init__.py 用 __getattr__ 惰性导入**: factor.compute 的循环通过延迟属性访问解决
- **orchestrator.py 用函数内导入**: 不在顶层 import factor.compute
- **maps 留在 compute.py**: 值是函数对象引用, 移走会循环 import
- **constants 自包含**: 直接 import config.loader, 不依赖 factor 模块

## 当前状态

- **factor_registry**: 55 因子注册, 31 active / 24 deprecated
- **新因子**: OIR/STR/ABN_TURN/OCFP + seal + epa/trcf/ideal_amplitude
- **评估标准**: ADR 026 — 五阶段 (CPCV + walk-forward + PBO)
- **数据起点**: 2010-01-01
- **执行价格**: Sina 实时 open + 除权检测 10%
- **launchd**: scheduler ✅ KeepAlive / webapp ❌

## 下一步

1. 运行 `bash scripts/eval_standard.sh` 对 31 active 因子做完整五阶段评估
2. 根据评估结果更新 factor_registry status 和 notes
3. 可选: Registry 模式 (存储函数名字符串, 运行时 resolve) 进一步拆分

## 关键约束

- 修改前先备份
- 数值参数放 config/config.yaml
- 永不 fallback 执行价格
- 因子 status 变更记入 notes 字段
- 修改后文档同步 + git 提交
