# Scout Agent Docker Image
# 多阶段构建，减小最终镜像大小

# Stage 1: 构建依赖
FROM python:3.11-slim as builder

WORKDIR /build

# 安装构建依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖到虚拟环境
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir -r requirements.txt

# Stage 2: 运行环境
FROM python:3.11-slim

LABEL maintainer="Scout Agent <scout@example.com>"
LABEL description="Scout Agent - 智能个人助手"
LABEL version="1.0.0.0"

# 设置环境变量
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SCOUT_DATA_DIR=/app/data \
    SCOUT_WEB_HOST=0.0.0.0 \
    SCOUT_WEB_PORT=8848

# 安装运行时依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# 创建非 root 用户
RUN groupadd -r scout && useradd -r -g scout -m scout

# 从 builder 阶段复制虚拟环境
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# 设置工作目录
WORKDIR /app

# 复制应用代码
COPY --chown=scout:scout . .

# 创建数据目录
RUN mkdir -p /app/data && chown -R scout:scout /app/data

# 切换到非 root 用户
USER scout

# 健康检查
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8848/health || exit 1

# 暴露端口
EXPOSE 8848

# 启动命令
CMD ["python", "-m", "scout.cli", "--web", "--host", "0.0.0.0", "--port", "8848"]
