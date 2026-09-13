#!/usr/bin/env bash
# 因子全量物化进度 + 异常监控 (v573 期)
# 用途: 物化后台运行期间, 每 600s 记录一次进度(物化分区文件数 / 进程存活)
#       并扫描真实错误关键字(Traceback/OOM-kill/Killed/segfault/watchdog 等), 异常或完成后停.
# 版本: v2 (2026-08-30) — 修正: 分区文件无 .parquet 后缀, 计数改 -type f;
#       错误模式去掉裸 "oom"(避免 headroom 误中), 改用 out of memory / oom-kill.
# 用法: nohup bash scripts/monitor_materialize.sh >/dev/null 2>&1 &
# 幂等: 仅读取 + 追加日志, 不修改任何物化数据
# 输出: logs/monitor_materialize.log
set -u
LOG="logs/materialize_from_scratch.log"
MON="logs/monitor_materialize.log"
INTERVAL=600
CACHE_DIR="quant/data/factor_cache/parquet_f"
echo "[monitor $(date '+%Y-%m-%d %H:%M:%S')] started interval=${INTERVAL}s target=$LOG" >> "$MON"
while true; do
  TS=$(date '+%Y-%m-%d %H:%M:%S')
  if pgrep -f "from quant.factor.store import FactorStore" >/dev/null 2>&1; then ALIVE=Y; else ALIVE=N; fi
  if [ -d "$CACHE_DIR" ]; then
    PQN=$(find "$CACHE_DIR" -type f 2>/dev/null | wc -l | tr -d ' ')
  else
    PQN=0
  fi
  ERR=$(tail -60 "$LOG" 2>/dev/null | grep -ciE "traceback|out of memory|oom-kill|memoryerror|killed|segfault|watchdog|too many open files|abort\(|zsh:.*terminat" || true)
  LAST=$(tail -1 "$LOG" 2>/dev/null | cut -c1-130)
  echo "[monitor $TS] alive=$ALIVE parts=$PQN err_scan=$ERR | $LAST" >> "$MON"
  if grep -q "DONE" "$LOG" 2>/dev/null; then
    echo "[monitor $TS] DETECTED 'DONE' — materialization finished." >> "$MON"
    break
  fi
  if [ "$ALIVE" = "N" ]; then
    echo "[monitor $TS] PROCESS GONE WITHOUT DONE — possible crash, inspect $LOG" >> "$MON"
    break
  fi
  sleep $INTERVAL
done
