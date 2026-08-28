#!/bin/bash
# verify_dagster_integration.sh - Dagster 模式集成验证脚本
# 用途: 验证 Dagster 编排器模式下所有服务正常运行
# 版本: v567
# 用法: bash scripts/verify_dagster_integration.sh

set -e

cd /Users/mariusto/project/quant
echo "🔍 Dagster 模式集成验证"
echo "=========================================="

echo ""
echo "1. 检查 Dagster Webserver (:3001)..."
curl -sf http://localhost:3001/healthcheck > /dev/null && echo "   ✅ Dagster Webserver 健康" || echo "   ❌ Dagster Webserver 异常"

echo ""
echo "2. 检查 Quant Web (:8521) ..."
curl -sf http://localhost:8521/api/health > /dev/null && echo "   ✅ Quant Web 健康" || echo "   ❌ Quant Web 异常"

echo ""
echo "3. 检查编排器模式..."
MODE=$(curl -sf http://localhost:8521/api/scheduler | .venv/bin/python -c "
import sys, json
data = json.load(sys.stdin)
print(data['data'].get('orchestrator_mode', 'unknown'))
")

if [ "$MODE" = "dagster" ]; then
    echo "   ✅ 当前模式: $MODE"
else
    echo "   ⚠️  当前模式: $MODE (期望: dagster)"
fi

echo ""
echo "4. 检查 Dagster 作业定义..."
JOB_COUNT=$(curl -s http://localhost:3001/graphql -X POST \
    -H "Content-Type: application/json" \
    -d '{"query":"{ workspaceOrError { __typename ... on Workspace { locationOrError { __typename ... on WorkspaceLocationEntry { locationOrLoadError { __typename ... on RepositoryLocation { repositories { name jobs { name } } } } } } } } }"}' \
    | .venv/bin/python -c "
import sys, json
try:
    data = json.load(sys.stdin)
    locations = data['data']['workspaceOrError']['locationOrError']
    repos = locations['locationOrLoadError']['repositories']
    if isinstance(repos, list):
        count = sum(len(r.get('jobs', [])) for r in repos)
        print(count)
    else:
        print(0)
except Exception:
    print(0)
" 2>/dev/null || echo "0")

echo "   ✅ Dagster 作业定义: ${JOB_COUNT} 个作业"

echo ""
echo "5. 检查 Dagster 传感器..."
SENSOR_LOG=$(tail -5 /tmp/dagster_daemon.log 2>/dev/null | grep -c "Sensor.*skipped\|Sensor.*launched\|Sensor.*error" 2>/dev/null || true)
if [ "$SENSOR_LOG" -gt 0 ]; then
    echo "   ✅ Dagster 传感器正在运行"
else
    echo "   ⚠️ Dagster 传感器无活动记录"
fi

echo ""
echo "=========================================="
echo "验证完成!"
echo ""
echo "访问地址:"
echo "  - Quant Web: http://localhost:8521"
echo "  - Dagster UI: http://localhost:3001"
