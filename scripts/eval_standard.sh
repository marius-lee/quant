#!/bin/bash
# === 五阶段因子评估标准流程 ===
# 文献: De Prado (2018), Harvey/Liu/Zhu (2016), Grinold & Kahn (1999)
# 实现: evaluation/ 包 (CPCV + PBO + walk-forward)
#
# 用法:
#   PYTHONPATH=. bash scripts/eval_standard.sh
#   PYTHONPATH=. bash scripts/eval_standard.sh --phase5  # 包含 Phase 5 持续监控
#
# 设计原则:
#   不再内嵌 Python 字符串 — 所有逻辑在 evaluation/*.py 中
set -e
cd "$(dirname "$0")/.."

RUN_PHASE5=false
if [ "${1:-}" = "--phase5" ]; then
    RUN_PHASE5=true
fi

# ────────────────────────────────────────────
# Phase 1: 数据准备
# ────────────────────────────────────────────
echo "============================================"
echo "Phase 1: 数据准备"
echo "============================================"
PYTHONPATH=. .venv/bin/python3 -c "
from evaluation.phase1_data import prepare_data
prepare_data()
"

# ────────────────────────────────────────────
# Phase 2: 单因子检验
# ────────────────────────────────────────────
echo ""
echo "============================================"
echo "Phase 2: 单因子检验 (IC / |t| / ICIR / half-life)"
echo "============================================"
PYTHONPATH=. .venv/bin/python3 -c "
from evaluation.phase2_single import screen_factors
screen_factors()
"

# ────────────────────────────────────────────
# Phase 3: CPCV OOS 检验 + PBO
# ────────────────────────────────────────────
echo ""
echo "============================================"
echo "Phase 3: CPCV + PBO (Walk-Forward OOS)"
echo "============================================"
PYTHONPATH=. .venv/bin/python3 -c "
from evaluation.phase3_oos import validate_oos
validate_oos()
"

# ────────────────────────────────────────────
# Phase 4: 交易成本验证
# ────────────────────────────────────────────
echo ""
echo "============================================"
echo "Phase 4: 交易成本扣除后验证"
echo "============================================"
PYTHONPATH=. .venv/bin/python3 -c "
from evaluation.phase4_costs import verify_costs
verify_costs()
"

# ────────────────────────────────────────────
# 评估完成
# ────────────────────────────────────────────
echo ""
echo "============================================"
echo "五阶段评估完成"
echo "============================================"

if $RUN_PHASE5; then
    echo ""
    echo "============================================"
    echo "Phase 5: 持续监控报告"
    echo "============================================"
    PYTHONPATH=. .venv/bin/python3 -c "
from evaluation.phase5_monitor import run_monitor
path = run_monitor()
print(f'Report: {path}')
"
fi

echo "下一步: 检查 Phase 2-3 结果, 更新 factor_registry status 和 notes"
