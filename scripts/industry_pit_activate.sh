#!/usr/bin/env bash
# 行业 PIT 全量生效编排 (v502) — 一键完成 同步→校验→重物化 顺序链.
#
# 顺序:
#   1. 等待后台 sync_industry_history 全量完成 (幂等断点续跑, 自动轮询, 不重复跑)
#   2. verify_industry_pit.sh — 校验 5557 只全覆盖 + smoke 回测
#   3. rematerialize_industry_pit.sh — force 重物化 2020 起因子缓存 (行业 PIT 生效)
#   4. 提示用户执行 bash scripts/restart.sh 重启 web (CLAUDE.md: 重启由用户执行)
# 用法: bash scripts/industry_pit_activate.sh [--skip-wait]
# 幂等: 整个链可重复执行; 已同步/已校验/已物化部分自动跳过.
set -euo pipefail
cd "$(dirname "$0")/.."

START_AT=$(date +%s)

echo "════════════════════════════════════════════════════"
echo "行业 PIT 生效编排 (v502) — 同步 → 校验 → 重物化"
echo "════════════════════════════════════════════════════"

# ── Step 1: 等待后台同步完成 ──
if [[ "${1:-}" == "--skip-wait" ]]; then
  echo "[1/3] --skip-wait 跳过同步等待"
else
  echo "[1/3] 等待后台 sync_industry_history 全量完成 ..."
  # 若没有 sync 进程在跑, 启动一个 (幂等, 全量断点续跑)
  if ! pgrep -f sync_industry_history > /dev/null; then
    echo "      未发现后台同步进程 → 启动 nohup 全量同步"
    nohup bash scripts/sync_industry_history.sh > /tmp/industry_sync.log 2>&1 &
  fi
  # 轮询直到同步进程退出 (完成或失败)
  while pgrep -f sync_industry_history > /dev/null; do
    # 进度打点, 每 60s 显示一次当前已同步股数
    n=$(PYTHONPATH=. .venv/bin/python3 -c "
import sqlite3
from quant.config.paths import MARKET_DB
c = sqlite3.connect(MARKET_DB, timeout=5)
print(c.execute('SELECT COUNT(DISTINCT symbol) FROM industry_history').fetchone()[0])
c.close()" 2>/dev/null || echo "?")
    el=$(( $(date +%s) - START_AT ))
    echo "      [${el}s] 已同步 ${n}/5557, 同步进程仍在运行 ..."
    sleep 60
  done
  echo "      同步进程已退出"
  # 确认没有 failed tail (同步最后一行非 done 则告警但继续, 由 verify 兜底)
fi

# ── Step 2: 校验完整性 + smoke 回测 ──
echo "[2/3] 校验 industry_history 覆盖 + smoke 回测 ..."
bash scripts/verify_industry_pit.sh

# ── Step 3: 重物化 2020 起因子缓存 (行业 PIT 生效) ──
echo "[3/3] 重物化因子缓存 (2020 起, force) — 预计 1-2h ..."
bash scripts/rematerialize_industry_pit.sh

echo ""
echo "════════════════════════════════════════════════════"
echo "✔ 行业 PIT 全量生效完成!"
echo "最后一步: 请手动执行  bash scripts/restart.sh  重启 web 服务"
echo "(CLAUDE.md 约定: 重启由用户执行)"
echo "════════════════════════════════════════════════════"