#!/bin/bash
# 后台进度监控 — 每 10 分钟轮询 ocfp 增量物化 + adj_factor 补数 (v528).
# 用法: nohup bash scripts/monitor_progress.sh > logs/monitor_progress.log 2>&1 &
# 幂等: 只读日志, 不触碰任务; 两个任务都完成或进程消失即退出, 完成后 macOS 通知.
LOG=logs/monitor_progress.log
MAT=logs/materialize_ocfp.log
ADJ=logs/sync_adj_factor_manual.log
echo "[$(date '+%H:%M:%S')] 监控启动: 每 600s 轮询一次" > "$LOG"
loop=0
while true; do
  sleep 600
  loop=$((loop + 1))
  mat_consumed=$(grep -c "consumed segment" "$MAT" 2>/dev/null || echo 0)
  mat_alive=$(pgrep -f "materialize_segment" | wc -l | tr -d ' ')
  adj_line=$(tail -1 "$ADJ" 2>/dev/null | grep -oE "[0-9]+/5557 \([0-9]+%\)" | tail -1)
  adj_done=$(grep -c "RESULT:" "$ADJ" 2>/dev/null || echo 0)
  echo "[$(date '+%H:%M:%S')] loop=$loop ocfp: consumed=$mat_consumed seg_alive=$mat_alive | adj: $adj_line done=$adj_done" >> "$LOG"
  if [ "$adj_done" -ge 1 ] && [ "$mat_alive" -eq 0 ]; then
    osascript -e 'display notification "ocfp 增量物化 + adj_factor 补数均已结束" with title "后台任务完成" sound name "Glass"' 2>/dev/null
    echo "[$(date '+%H:%M:%S')] 全部完成, 退出" >> "$LOG"
    exit 0
  fi
done