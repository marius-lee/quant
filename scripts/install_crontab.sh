#!/bin/bash
# 安装/更新 crontab — 先删除旧条目再写入
PROJ=/Users/mariusto/project/quant

crontab -r 2>/dev/null

cat > /tmp/quant_crontab << 'CRONEOF'
# quant 量化实盘模拟
30 8 * * 1-5 cd /Users/mariusto/project/quant && bash scripts/run_task.sh signals >> logs/cron.log 2>&1
30 9 * * 1-5 cd /Users/mariusto/project/quant && bash scripts/run_task.sh execute >> logs/cron.log 2>&1
35 9 * * 1-5 cd /Users/mariusto/project/quant && bash scripts/run_task.sh monitor >> logs/cron.log 2>&1
0 19 * * 1-5 cd /Users/mariusto/project/quant && bash scripts/run_task.sh daily_data >> logs/cron.log 2>&1
0 20 * * 1-5 cd /Users/mariusto/project/quant && bash scripts/run_task.sh attribution >> logs/cron.log 2>&1
0 6 * * 6 cd /Users/mariusto/project/quant && bash scripts/run_task.sh factor_cache >> logs/cron.log 2>&1 && bash scripts/run_task.sh weekly >> logs/cron.log 2>&1
CRONEOF

crontab /tmp/quant_crontab
echo "--- 当前 crontab ---"
crontab -l
rm -f /tmp/quant_crontab
