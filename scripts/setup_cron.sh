#!/bin/bash
PROJ=/Users/mariusto/project/quant
crontab << 'CRONEOF'
# quant 量化实盘模拟
30 8 * * 1-5 cd $PROJ && bash scripts/run_task.sh signals >> logs/cron.log 2>&1
30 9 * * 1-5 cd $PROJ && bash scripts/run_task.sh execute >> logs/cron.log 2>&1
35 9 * * 1-5 cd $PROJ && bash scripts/run_task.sh monitor >> logs/cron.log 2>&1
0 19 * * 1-5 cd $PROJ && bash scripts/run_task.sh daily_data >> logs/cron.log 2>&1
0 20 * * 1-5 cd $PROJ && bash scripts/run_task.sh attribution >> logs/cron.log 2>&1
0 21 * * 1-5 cd $PROJ && bash scripts/run_task.sh factor_cache >> logs/cron.log 2>&1
0 6 * * 6 cd $PROJ && bash scripts/run_task.sh weekly >> logs/cron.log 2>&1
# B-08: adj_factor 接口限流 1次/小时 — 每小时灌 50 股, ~4.5 天铺满 5400 股
# 之后进入维护模式 (最旧 updated_at 优先, 除权日自动 rebase 历史)
50 * * * * cd $PROJ && bash scripts/run_task.sh adj_factor >> logs/cron.log 2>&1
CRONEOF
touch $PROJ/.cron_installed
echo "crontab 已更新，标记文件已创建"
crontab -l
