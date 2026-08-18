#!/usr/bin/env bash
# =============================================================================
# run_backtest_full.sh — 全量回测 (v538)
# 用途: 跑全市场全区间回测 (默认区间 = config backtest.default_start/end,
#       即 2020-01-01 → 评估截止日; 与因子物化起点同源 v473 约定)
# 用法:
#   bash scripts/run_backtest_full.sh                 # config 默认区间
#   bash scripts/run_backtest_full.sh 2024-01-01 2025-12-31   # 自定义区间
#   bash scripts/run_backtest_full.sh --smoke          # 冒烟 (10 只×22 天)
# 幂等性: 每次生成新 backtest_runs 记录 (naming.next_backtest_name),
#         不覆盖既有记录; 可重复执行
# 前置: 因子缓存已全量物化 (bash scripts/materialize_full.sh, 2026-08-18 完成)
# 输出: 结果落库 backtest_trades.db#backtest_runs + 终端打印 metrics
# 环境: 需停 web 服务 (其写 task_runs 与回测写库竞争) — 测试/回测完再 restart
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."

START="${1:-}"
END="${2:-}"
MODE="full"
if [ "${1:-}" = "--smoke" ]; then
  MODE="smoke"; START=""; END=""
fi

if [ -n "$START" ] && [ -n "$END" ]; then
  echo "[run_backtest_full] 自定义区间: $START → $END (mode=$MODE)"
elif [ "$MODE" = "full" ]; then
  echo "[run_backtest_full] config 默认区间 (backtest.default_start/end)"
fi

PYTHONPATH=. .venv/bin/python - <<PYEOF
from quant.backtest.loop import run_backtest

r = run_backtest(
    start_date="$START" or None,
    end_date="$END" or None,
    capital=5000,
    mode="$MODE",
)
m = r.get("metrics", {})
print("=" * 70)
print("BACKTEST DONE:", r.get("strategy"), "|", r.get("start_date"), "→", r.get("end_date"))
for k in ("sharpe", "cagr_pct", "max_dd_pct", "total_return_pct", "final_equity",
          "sortino", "calmar", "win_rate", "n_days", "errors", "elapsed_sec"):
    if k in m:
        print(f"  {k}: {m[k]}")
print("=" * 70)
PYEOF