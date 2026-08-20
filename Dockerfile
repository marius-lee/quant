# quant 量化交易系统 - 生产级多阶段构建
# 构建: docker build -t quant:latest -f Dockerfile .
# 运行: docker run -d --name quant quant:latest

# ═══════════════════════════════════════════════════════════════
# Stage 1: 基础构建环境
# ═══════════════════════════════════════════════════════════════
FROM python:3.12-slim AS builder

# 安装系统依赖 (构建时需要)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    gcc \
    g++ \
    libpq-dev \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /build

# 复制依赖文件 (利用层缓存)
COPY pyproject.toml ./
COPY quant/ ./quant/

# 安装 Python 依赖到虚拟环境
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir --upgrade pip setuptools wheel && \
    pip install --no-cache-dir -e . && \
    pip install --no-cache-dir piptools && \
    pip-compile pyproject.toml -o requirements.txt && \
    pip install --no-cache-dir -r requirements.txt

# ═══════════════════════════════════════════════════════════════
# Stage 2: 运行时基础镜像 (最小化)
# ═══════════════════════════════════════════════════════════════
FROM python:3.12-slim AS runtime

# 安装运行时系统依赖 (仅运行时需要)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    ca-certificates \
    tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# 设置时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 创建非 root 用户
RUN groupadd -r -g 1000 quant && \
    useradd -r -u 1000 -g quant -d /home/quant -s /sbin/nologin -c "Quant User" quant && \
    mkdir -p /home/quant /app /data /logs /config && \
    chown -R quant:quant /home/quant /app /data /logs /config

# 从构建阶段复制虚拟环境
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 设置工作目录
WORKDIR /app

# 复制应用代码
COPY --chown=quant:quant quant/ ./quant/
COPY --chown=quant:quant scripts/ ./scripts/
COPY --chown=quant:quant config/ ./config/

# 创建必要目录
RUN mkdir -p /app/quant/data /app/logs /app/tmp && \
    chown -R quant:quant /app

# 切换到非 root 用户
USER quant

# 环境变量
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    QUANT_CONFIG_PATH=/config/config.yaml \
    QUANT_DATA_DIR=/data \
    QUANT_LOG_DIR=/logs

# 暴露端口
EXPOSE 8521 9090 9091

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8521/health || exit 1

# 入口点
ENTRYPOINT ["python", "-m", "quant.web.app"]

# 标签 (OCI Image Spec)
LABEL org.opencontainers.image.title="Quant Trading System" \
      org.opencontainers.image.description="A-share quantitative trading system with distributed factor engine, real-time risk control, and multi-tenant isolation" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.authors="Quant Team" \
      org.opencontainers.image.source="https://github.com/marius-lee/quant" \
      org.opencontainers.image.licenses="MIT"
