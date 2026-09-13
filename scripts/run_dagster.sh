#!/usr/bin/env bash
# 用途: 以 Dagster 模式启动因子物化/调度编排 (替代 Legacy 30s 轮询编排)
# 版本: v1.0.0
# 用法:
#   bash scripts/run_dagster.sh            # 启动 dagit Web UI (默认端口 3000)
#   bash scripts/run_dagster.sh daemon     # 后台 daemon 模式 (生产)
#   bash scripts/run_dagster.sh job daily_pipeline 2024-01-01 2025-12-31  # 单跑 job
# 幂等性: 仅启动服务/提交任务, 物化本身幂等 (见 FactorStore.materialize)
#
# 前置: factor.distributed.enabled (config.yaml) 控制是否启用 Ray 跨分区并行.
#   false -> factor_cache 资产回退到与 Legacy 相同的 subprocess 物化 (已正确);
#   true  -> 启用 Ray 分布式引擎, 且引擎经 in_process 复用主进程直算,
#            避免 Ray 分区 × 内部 subprocess 嵌套过订 (v569 修复).
#
# 注意: 本脚本只负责编排启动; 重启 Web 服务仍是用户执行 `bash scripts/restart.sh`.
set -euo pipefail
cd "$(dirname "$0")/.."

export QUANT_ORCHESTRATOR=dagster
export PYTHONPATH="$(pwd)"

MODE="${1:-ui}"
case "$MODE" in
  ui)
    .venv/bin/python -m dagit -f quant/orchestrator/dagster_assets.py -a definitions -p 3000
    ;;
  daemon)
    .venv/bin/python -m dagster daemon -f quant/orchestrator/dagster_assets.py -a definitions
    ;;
  job)
    JOB="${2:?usage: run_dagster.sh job <job_name> [start] [end]}"
    START="${3:-$(date +%Y-%m-%d)}"
    END="${4:-$(date +%Y-%m-%d)}"
    .venv/bin/python -m dagster job execute \
      -f quant/orchestrator/dagster_assets.py -a definitions \
      -j "$JOB" -c "{\"start_date\": \"$START\", \"end_date\": \"$END\"}"
    ;;
  *)
    echo "unknown mode: $MODE (ui|daemon|job)" >&2; exit 2
    ;;
esac
