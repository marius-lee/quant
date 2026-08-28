## monitor_dagster_build.sh - 实时监控 Dagster 服务构建/启动状态

cd /Users/mariusto/project/quant

echo "🔍 监控 Dagster 服务构建进度..."
echo "=========================================="

# 定义颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

check_progress() {
  echo ""
  echo "$(date '+%H:%M:%S') 检查点:"
  echo "  容器状态:"
  docker-compose -f docker-compose.dagster.yml --env-file .env.dagster ps 2>/dev/null | grep -E "(postgres|daemon|webserver)" || echo "  ⏳ 容器未启动"
  
  echo "  端口监听:"
  lsof -i :3000 -i :5432 2>/dev/null | grep LISTEN || echo "  ⏳ 端口未监听"
  
  echo "  健康检查:"
  if curl -sf http://localhost:3000/healthcheck >/dev/null 2>&1; then
    echo -e "  ${GREEN}✅ Dagster Webserver 健康检查通过${NC}"
  else
    echo -e "  ${YELLOW}⏳ Dagster Webserver 未就绪${NC}"
  fi
  
  echo "  Web 服务:"
  curl -sf http://localhost:8521/api/health >/dev/null 2>&1 && echo "  ✅ Quant Web (:8521) 运行正常" || echo -e "  ${RED}❌ Quant Web (:8521) 异常${NC}"
}

# 每 10 秒检查一次, 共 6 次 (1 分钟)
for i in $(seq 1 6); do
  check_progress
  echo "------------------------------------------"
  if [ $i -lt 6 ]; then
    sleep 10
  fi
done

# 显示实时日志 (最后 20 行)
echo ""
echo "📄 最近的 Dagster 日志:"
docker-compose -f docker-compose.dagster.yml logs --tail=20 2>/dev/null | tail -20
