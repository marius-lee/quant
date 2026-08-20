#!/bin/bash
# Quant Dagster 启动脚本
# 用法: ./scripts/start_dagster.sh [dev|prod]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

MODE="${1:-dev}"
ENV_FILE="${PROJECT_ROOT}/.env.dagster"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# 检查 Docker
check_docker() {
    if ! command -v docker &> /dev/null; then
        log_error "Docker 未安装"
        exit 1
    fi
    if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
        log_error "Docker Compose 未安装"
        exit 1
    fi
    log_info "Docker 环境检查通过"
}

# 检查环境变量文件
check_env_file() {
    if [[ ! -f "$ENV_FILE" ]]; then
        log_warn "未找到 $ENV_FILE，从模板创建..."
        cp "${PROJECT_ROOT}/.env.dagster.example" "$ENV_FILE"
        log_warn "请编辑 $ENV_FILE 填入真实配置 (TUSHARE_TOKEN 等)"
        return 1
    fi
    log_info "环境变量文件检查通过"
    return 0
}

# 构建镜像
build_image() {
    log_info "构建 Dagster 镜像..."
    docker build -f "${PROJECT_ROOT}/Dockerfile.dagster" -t quant-dagster:latest "$PROJECT_ROOT"
}

# 启动服务
start_services() {
    log_info "启动 Dagster 服务 (${MODE} 模式)..."
    
    cd "$PROJECT_ROOT"
    
    if [[ "$MODE" == "prod" ]]; then
        docker-compose -f docker-compose.dagster.yml --env-file "$ENV_FILE" up -d --build
    else
        docker-compose -f docker-compose.dagster.yml --env-file "$ENV_FILE" up -d
    fi
    
    log_info "服务启动完成"
    log_info "Web UI: http://localhost:3000"
    log_info "PostgreSQL: localhost:5432"
}

# 等待服务就绪
wait_for_services() {
    log_info "等待服务就绪..."
    
    local max_attempts=30
    local attempt=1
    
    while [[ $attempt -le $max_attempts ]]; do
        if curl -sf http://localhost:3000/healthcheck &>/dev/null; then
            log_info "Dagster Webserver 就绪"
            return 0
        fi
        
        if [[ $attempt -eq $max_attempts ]]; then
            log_error "服务启动超时"
            docker-compose -f "${PROJECT_ROOT}/docker-compose.dagster.yml" logs --tail=50
            return 1
        fi
        
        log_info "等待中... ($attempt/$max_attempts)"
        sleep 5
        ((attempt++))
    done
    
    return 1
}

# 查看日志
logs() {
    cd "$PROJECT_ROOT"
    docker-compose -f docker-compose.dagster.yml --env-file "$ENV_FILE" logs -f "$@"
}

# 停止服务
stop() {
    log_info "停止 Dagster 服务..."
    cd "$PROJECT_ROOT"
    docker-compose -f docker-compose.dagster.yml --env-file "$ENV_FILE" down
}

# 清理
clean() {
    log_warn "清理所有数据 (不可恢复)..."
    read -p "确认清理所有数据? (y/N): " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        cd "$PROJECT_ROOT"
        docker-compose -f docker-compose.dagster.yml --env-file "$ENV_FILE" down -v
        docker image rm quant-dagster:latest 2>/dev/null || true
        log_info "清理完成"
    fi
}

# 主函数
main() {
    case "${1:-start}" in
        start)
            check_docker
            if check_env_file; then
                build_image
                start_services
                wait_for_services
            fi
            ;;
        stop)
            stop
            ;;
        restart)
            stop
            sleep 2
            main start
            ;;
        logs)
            logs "${@:2}"
            ;;
        clean)
            clean
            ;;
        status)
            docker-compose -f "${PROJECT_ROOT}/docker-compose.dagster.yml" --env-file "$ENV_FILE" ps
            ;;
        *)
            echo "用法: $0 {start|stop|restart|logs|clean|status} [dev|prod]"
            exit 1
            ;;
    esac
}

main "$@"