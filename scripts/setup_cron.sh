#!/bin/bash
# 安装 crontab — 仅涵盖 orchestrator 之外的兜底任务 (test-v416)
# 设计依据:
#   - 日频任务 (signals/execute/monitor/daily_data/attribution/factor_cache)
#     由 scripts/restart.sh 常驻 orchestrator 负责, 不进 cron (v301 cron清理决策)。
#   - weekly (周六 06:00): 第三重冗余 — orchestrator 周六分支 + _weekly_loop
#     独立线程之外的最后一层保障 (cron 独立于 python 进程, 进程崩溃仍可触发;
#     _tk_start dedup 保证三路并发不重复执行)。
#   - adj_factor: B-08 接口限流 1次/小时, 必须排定 (否则 5400 股铺不满)。
PROJ=/Users/mariusto/project/quant
# 注意: macOS bash 3.2 双引号 heredoc 不展开变量, 必须用非引号 heredoc,
# 且 heredoc 内不含 \$ 转义需求 (已实测验证 v416)
(crontab << CRONEOF
# quant 量化实盘模拟 (test-v416: 仅 adj_factor + weekly 兜底, 日频任务归 orchestrator)
0 6 * * 6 cd $PROJ && bash scripts/run_task.sh weekly >> logs/cron.log 2>&1
# B-08: adj_factor 接口限流 1次/小时 — 每小时灌 50 股, ~4.5 天铺满 5400 股
# 之后进入维护模式 (最旧 updated_at 优先, 除权日自动 rebase 历史)
50 * * * * cd $PROJ && bash scripts/run_task.sh adj_factor >> logs/cron.log 2>&1
CRONEOF
)
touch $PROJ/.cron_installed
echo "crontab 已更新(路径已展开为 $PROJ), 标记文件已创建"
crontab -l