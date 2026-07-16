#!/bin/bash
PROJ=/Users/mariusto/project/quant
crontab << 'CRONEOF'
# quant 量化实盘模拟
30 8 * * 1-5 cd $PROJ && bash scripts/run_task.sh signals >> logs/cron.log 2>&1
30 9 * * 1-5 cd $PROJ && bash scripts/run_task.sh execute >> logs/cron.log 2>&1
35 9 * * 1-5 cd $PROJ && bash scripts/run_task.sh monitor >> logs/cron.log 2>&1
0 19 * * 1-5 cd $PROJ && bash scripts/run_task.sh daily_data >> logs/cron.log 2>&1
0 20 * * 1-5 cd $PROJ && bash scripts/run_task.sh attribution >> logs/cron.log 2>&1
0 6 * * 6 cd $PROJ && bash scripts/run_task.sh weekly >> logs/cron.log 2>&1
CRONEOF
touch $PROJ/.cron_installed
echo "crontab 已更新，标记文件已创建"
crontab -l
