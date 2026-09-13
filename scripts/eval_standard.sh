#!/bin/bash
# === 五阶段因子评估标准流程 ===
# 文献: De Prado (2018), Harvey/Liu/Zhu (2016), Grinold & Kahn (1999)
# 实现: evaluation/ 包 (CPCV + PBO + walk-forward)
#
# 用法:
#   PYTHONPATH=. bash scripts/eval_standard.sh
#   (--phase5 监控报告已移除: run_monitor 与 scheduler/attribution 重复, 2026-07-26 合并)
#
# 设计原则:
#   不再内嵌 Python 字符串 — 所有逻辑在 evaluation/*.py 中
set -e
# ── VERSION: git commit hash + timestamp ──
GIT_HASH=$(git log -1 --format='%h %ci' 2>/dev/null || echo 'unknown')
VER_NUM=$(cat VERSION 2>/dev/null || echo '?')
DIRTY=$(git status --porcelain -- factor/ evaluation/ config/ 2>/dev/null | head -20 | tr '\n' ' ')
if [ -n "$DIRTY" ]; then
    echo "=== VERSION: #$VER_NUM ($GIT_HASH) [DIRTY: $DIRTY] ==="
else
    echo "=== VERSION: #$VER_NUM ($GIT_HASH) [clean] ==="
fi

cd "$(dirname "$0")/.."

# ────────────────────────────────────────────
# Phase 1: 数据准备
# ────────────────────────────────────────────
echo "============================================"
echo "Phase 1: 数据准备"
echo "============================================"
PYTHONPATH=. .venv/bin/python3 -c "
from quant.utils.logger import offline_mode
from quant.utils.excepthook import setup; setup()
with offline_mode():
    from quant.evaluation.phase1_data import prepare_data
    prepare_data()
"

# ────────────────────────────────────────────
# Phase 2: 单因子检验
# ────────────────────────────────────────────
echo ""
echo "============================================"
echo "Phase 2: 单因子检验 (IC / |t| / ICIR / half-life)"
echo "============================================"
# Ensure no stale DB locks from previous phases
python3 -c "import sqlite3; c=sqlite3.connect('quant/data/market.db'); c.execute('PRAGMA wal_checkpoint'); c.close()" 2>/dev/null || true

# v629: screen_factors 已重构为 evaluate-all (无 diagnostics 预筛参数)
# --all 标记保持兼容, 但现在所有模式均评估全部 backtesting 因子
PYTHONPATH=. .venv/bin/python3 -c "
from quant.utils.logger import offline_mode
from quant.utils.excepthook import setup; setup()
with offline_mode():
    from quant.evaluation.phase2_single import screen_factors
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
from quant.utils.logger import offline_mode
from quant.utils.excepthook import setup; setup()
with offline_mode():
    from quant.evaluation.phase3_oos import validate_oos
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
from quant.utils.logger import offline_mode
from quant.utils.excepthook import setup; setup()
with offline_mode():
    from quant.evaluation.phase4_costs import verify_costs
    verify_costs()
"

# ────────────────────────────────────────────
# 评估完成
# ────────────────────────────────────────────
echo ""
echo "============================================"
echo "五阶段评估完成"
echo "============================================"

# -- Phase 5b: 状态同步 (评估结果 -> factor_registry) --
echo ""
echo "============================================"
echo "Phase 5b: 因子状态同步"
echo "============================================"
PYTHONPATH=. .venv/bin/python3 -c "
from quant.utils.logger import offline_mode
from quant.utils.excepthook import setup; setup()
with offline_mode():
    from quant.evaluation.phase5_monitor import sync_factor_status
    r = sync_factor_status()
    print(f'  rejected={len(r[\"rejected\"])} active={len(r[\"active\"])} unchanged={r[\"unchanged\"]}')
    for n in r['rejected']: print(f'    X {n}')
    for n in r['active']:   print(f'    V {n}')
"

# Phase 6: 策略级全链路回测 (Gap 1 — 事件驱动 walk-forward)
# 输入: Phase 3 通过的因子 (candidate + active)
# 输出: equity curve, Sharpe, MDD, CAGR, benchmark delta
RUN_PHASE6=false
for arg in "$@"; do
    case $arg in
        --phase6) RUN_PHASE6=true ;;
    esac
done

if $RUN_PHASE6; then
    echo ""
    echo "============================================"
    echo "Phase 6: 策略级全链路回测 (North Star v644)"
    echo "============================================"
    # v644: Nano tier, 3 active factors, equal_weight post-warmup, 20.9x
    PYTHONPATH=. .venv/bin/python3 -c "
from quant.utils.logger import offline_mode
from quant.utils.excepthook import setup; setup()
with offline_mode():
    from quant.evaluation.phase6_backtest import run_strategy_backtest
    import json
    result = run_strategy_backtest(
        start_date='2023-01-01',
        end_date='2025-12-31',
        capital=5000,
        output_json='/tmp/_eval_phase6.json',
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
"
    echo ""
    echo "--- North Star Backtest (2025-08-29 → 2026-09-08) ---"
    PYTHONPATH=. .venv/bin/python3 scripts/run_backtest.py
fi
