#!/usr/bin/env bash
# 行业 PIT 同步守护续跑 (v508) — baostock 网络中断自动重试, 断点续跑不丢进度.
#
# 背景: 2026-08-16 同步在 3509/5557 时遇 baostock 网络接收错误 (10002007) fail-fast 退出.
#       baostock 免费 session 网络抖动属暂时性故障, 重跑即续 (industry_history 表内符号自动跳过).
#
# 用法:
#   bash scripts/resume_industry_sync.sh            # 前台循环 (每轮同步完自动重跑, Ctrl+C 退出)
#   nohup bash scripts/resume_industry_sync.sh > /tmp/industry_sync4.log 2>&1 &   # 后台守护 (推荐)
#
# 幂等: 内层 sync_industry_history.sh 幂等断点续跑; 循环仅负责重启它.
set -euo pipefail
cd "$(dirname "$0")/.."

MAX_RETRIES=${1:-20}   # 最多自动重试轮数 (每轮同步到崩溃或完成)
ROUND=0

while [ $ROUND -lt "$MAX_RETRIES" ]; do
  ROUND=$((ROUND + 1))
  # v513: 日请求软上限检测 — 达上限说明当日配额已尽 (baostock 服务端实证 ~5.3万/日
  # 即拉黑), 继续重试只会空转; 提示换热点后停止, 等待用户换网络续跑.
  LIMIT_STATE=$(PYTHONPATH=. .venv/bin/python3 -c "
from quant.utils.baostock_gate import gate
import json
r, c, limit = gate.day_limit_reached()
print('reached' if r else 'ok', c, limit)" 2>/dev/null || echo "ok 0 0")
  set -- $LIMIT_STATE
  if [ "$1" = "reached" ]; then
    echo "[$(date '+%H:%M:%S')] ✗ 今日 baostock 请求已达上限 ($2/$3) — 通知已发送 (macOS/Server酱/Web横幅)."
    # v518: 等待式换热点检测 — 每 30s 探测公网 IP, 变化自动清零续跑 (无需手动重启);
    # 跨天 (新一天配额恢复) 亦自动续跑. 日志去重: 仅进入等待/检测到变化各写一条.
    echo "[$(date '+%H:%M:%S')] ⏳ 进入等待模式: 每 30s 探测公网 IP, 检测到换热点自动清零续跑. (Ctrl+C 退出)"
    WAIT_LOGGED=0
    while true; do
      WATCH=$(PYTHONPATH=. .venv/bin/python3 -c "
from quant.utils.baostock_gate import gate
r, _ = gate.probe_and_reset_if_rotated()
d, _, _ = gate.day_limit_reached()
print('rotated' if r else ('reset' if not d else 'wait'))" 2>/dev/null || echo "wait")
      if [ "$WATCH" = "rotated" ]; then
        echo "[$(date '+%H:%M:%S')] ✦ 检测到公网 IP 已变化 — 日计数已清零, 黑名单已解除, 自动续跑!"
        break
      fi
      if [ "$WATCH" = "reset" ]; then
        echo "[$(date '+%H:%M:%S')] ✦ 新的一天/配额已恢复 — 日计数自动重置, 续跑!"
        break
      fi
      if [ "$WAIT_LOGGED" = "0" ]; then
        echo "[$(date '+%H:%M:%S')]   等待换热点中... (每 30s 探测, 无变化期间不再刷日志)"
        WAIT_LOGGED=1
      fi
      sleep 30
    done
  fi
  # 表内已同步符号 (断点判断)
  SYNCED=$(PYTHONPATH=. .venv/bin/python3 -c "
import sqlite3
from quant.config.paths import MARKET_DB
c = sqlite3.connect(MARKET_DB, timeout=10)
print(c.execute('SELECT COUNT(DISTINCT symbol) FROM industry_history').fetchone()[0])
c.close()" 2>/dev/null || echo "?")
  echo "[$(date '+%H:%M:%S')] ── ROUND $ROUND — 已同步 $SYNCED/5557, 启动同步..."
  if ! bash scripts/sync_industry_history.sh; then
    echo "[$(date '+%H:%M:%S')] ✗ 本轮同步失败, 10s 后自动续跑"
    sleep 10
    continue
  fi
  # 正常退出: 检查是否已全量完成
  FINAL=$(PYTHONPATH=. .venv/bin/python3 -c "
import sqlite3
from quant.config.paths import MARKET_DB
c = sqlite3.connect(MARKET_DB, timeout=10)
n = c.execute('SELECT COUNT(DISTINCT symbol) FROM industry_history').fetchone()[0]
c.close()
print(n)" 2>/dev/null || echo "?")
  if [ "$FINAL" != "?" ] && [ "$FINAL" -eq 5557 ] 2>/dev/null; then
    echo "[$(date '+%H:%M:%S')] ✔ 全量同步完成: $FINAL/5557"
    exit 0
  fi
  echo "[$(date '+%H:%M:%S')] 同步退出但未全量 ($FINAL/5557), 5s 后续跑"
  sleep 5
done
echo "[$(date '+%H:%M:%S')] 达到最大重试轮数 $MAX_RETRIES — 需人工介入 (可调参重跑)"
exit 1