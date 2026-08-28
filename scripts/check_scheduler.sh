#!/bin/bash
# scripts/check_scheduler.sh - 检查调度页面任务状态
# 用途: 验证 v562 修复是否生效, 检查晚间链是否有"等待调度"误判
# 用法: bash scripts/check_scheduler.sh

cd "$(dirname "$0")/.."

echo "=== 检查调度页面任务状态 ==="
echo ""

# 检查 Web 服务是否运行
if ! curl -s -f http://localhost:8521/api/scheduler > /dev/null 2>&1; then
    echo "❌ Web 服务未运行在端口 8521"
    exit 1
fi

echo "✅ Web 服务正常运行"

# 获取并显示编排器模式
echo ""
echo "编排器模式:"
curl -s http://localhost:8521/api/scheduler | .venv/bin/python -c "
import sys, json
data = json.load(sys.stdin)
mode = data['data'].get('orchestrator_mode', 'unknown')
dagster = data['data'].get('dagster_enabled', False)
print(f'  模式: {mode}, Dagster 启用: {dagster}')
"

# 检查关键任务状态
echo ""
echo "关键任务状态:"
curl -s http://localhost:8521/api/scheduler | .venv/bin/python -c "
import sys, json
data = json.load(sys.stdin)
key_tasks = ['daily_repair', 'signals', 'execute', 'monitor', 'evening_chain']
for t in data['data']['tasks']:
    if t['task'] in key_tasks:
        status = t['status']
        last_run = t['last_run']
        error = t.get('error_msg', '')
        # 显示状态
        if status == 'success':
            print(f'  ✅ {t[\"task\"]}: 今日已执行 ({last_run})')
        elif status == 'running':
            print(f'  🔄 {t[\"task\"]}: 运行中 ({last_run})')
        elif status == 'waiting':
            print(f'  ⏳ {t[\"task\"]}: 等待调度')
        elif status in ['error', 'timeout']:
            err_short = error[:50] if error else '未知错误'
            print(f'  ❌ {t[\"task\"]}: {status} ({err_short})')
        else:
            print(f'  {t[\"task\"]}: {status} ({last_run})')
        if error:
            print(f'      错误: {error[:80]}')
"

# 额外检查: 查询数据库中的晚间链状态
echo ""
echo "数据库记录 (最近 3 天晚间链记录):"
PYTHONPATH=. .venv/bin/python -c "
from quant.scheduler.task_log import query_date
from datetime import date, timedelta
import json

# 查询最近 3 天的记录
for days_ago in range(3):
    d = (date.today() - timedelta(days=days_ago)).strftime('%Y-%m-%d')
    rows = query_date(d)
    evening_rows = [r for r in rows if r['task_name'] == 'evening_chain']
    if evening_rows:
        latest = evening_rows[0]  # 按 id DESC 排序
        print(f'  {d}: 状态={latest[\"status\"]}, 开始={latest[\"started_at\"]}, 结束={latest[\"finished_at\"]}')
        if latest['error']:
            print(f'      错误: {latest[\"error\"][:60]}')
"
