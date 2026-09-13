#!/usr/bin/env bash
# 用途: 轮询全量物化进程, 退出后自动运行 scripts/verify_factor_cache.py 校验缓存.
# 版本: v1.0.0
# 用法: nohup bash scripts/watch_factor_cache_done.sh >/dev/null 2>&1 &
# 幂等性: 纯监视+触发校验, 可重复启动(会再等一轮, 但进程已退出则立即跑校验).
set -u
PROC_MATCH="quant.scheduler.factor_cache"   # 物化 python 进程 argv 真实特征(非日志重定向名)
LOG="logs/factor_cache_verify.log"

echo "[$(date)] watcher started; waiting for '$PROC_MATCH' to finish ..." >> "$LOG"
while pgrep -f "$PROC_MATCH" >/dev/null; do
  sleep 60
done
echo "[$(date)] materialization process gone; running factor cache verification ..." >> "$LOG"
PYTHONPATH=. .venv/bin/python scripts/verify_factor_cache.py >> "$LOG" 2>&1
echo "[$(date)] verify exit=$? (report: logs/factor_cache_verify.json)" >> "$LOG"
