# ============================================================
# StaffDeck Dockerfile（多阶段构建）
#
# 架构：前端构建（Node） → 运行时（Python，单端口模式）
# 单端口模式：前端 dist + 后端 API 由同一个 FastAPI 进程提供服务
# ============================================================


# ---------- Stage 1: 前端构建 ----------
FROM node:20-alpine AS frontend-builder

WORKDIR /build

# 先复制依赖清单，利用 Docker 层缓存
COPY frontend-enterprise/package.json frontend-enterprise/package-lock.json ./frontend-enterprise/

# 安装依赖（在 frontend-enterprise 子目录执行）
RUN cd frontend-enterprise && npm ci

# 复制前端源码并构建
COPY frontend-enterprise/ ./frontend-enterprise/

RUN cd frontend-enterprise && npm run build
# 产物在 frontend-enterprise/dist/


# ---------- Stage 2: Python 运行时 ----------
FROM python:3.11-slim AS runtime

# 安装系统级依赖
#   - tini:   PID 1 init，正确转发信号（uvicorn 优雅关闭需要）
#   - curl:   健康检查
#   - libffi: cryptography 库的 FFI 依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
        tini \
        curl \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制依赖清单并安装，利用 Docker 层缓存
COPY backend/pyproject.toml backend/single_port_app.py backend/desktop_launcher.py ./backend/
COPY backend/app/__init__.py ./backend/app/__init__.py

RUN pip install --no-cache-dir "./backend"

# 复制后端源码
COPY backend/ ./backend/

# 从 Stage 1 复制前端构建产物
# 目录结构：/app/backend/ + /app/frontend-enterprise/dist/
# single_port_app.py 通过 backend/../frontend-enterprise/dist 定位前端
COPY --from=frontend-builder /build/frontend-enterprise/dist ./frontend-enterprise/dist

# 创建数据目录（通用技能运行时临时文件；PostgreSQL 数据由外部数据库服务管理）
RUN mkdir -p /data

# 默认环境变量（可通过 docker run / K8s 覆盖）。
# 数据库默认连接通过环境变量注入的 PostgreSQL 实例；生产部署应在 docker run / K8s
# 中显式覆盖 DATABASE_URL、APP_SECRET 等敏感配置。
ENV APP_HOST=0.0.0.0 \
    APP_PORT=5173 \
    DATABASE_URL="postgresql+psycopg://staffdeck:staffdeck123@localhost:5432/staffdeck" \
    CORS_ORIGINS="*" \
    TOOL_BASE_URL="http://localhost:5173" \
    GENERAL_SKILL_RUNTIME_AUTO_INSTALL="false" \
    AUTO_INIT_DB="true" \
    PYTHONUNBUFFERED=1

WORKDIR /app/backend

EXPOSE 5173

# 健康检查（/api/health 返回 {"status":"ok"}）
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://127.0.0.1:${APP_PORT}/api/health || exit 1

# tini 作为 PID 1，确保信号正确转发
ENTRYPOINT ["tini", "--"]

# 启动单端口服务（SPA + API 同端口）
CMD ["sh", "-c", "uvicorn single_port_app:app --host ${APP_HOST} --port ${APP_PORT}"]
