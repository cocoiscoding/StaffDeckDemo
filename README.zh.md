# StaffDeck Demo（二次开发）

> 基于 [OpenBMB/StaffDeck](https://github.com/OpenBMB/StaffDeck) 的二次开发 Demo，将持久层从 SQLite 迁移至 PostgreSQL。

**English** | [简体中文](./README.zh.md)

## 这是什么

这是 StaffDeck 的一个二次开发仓库。StaffDeck 是一套面向企业的数字员工构建与管理平台；本仓库的目的**不是**重新分发 StaffDeck，而是聚焦演示一次定向重构：**用 PostgreSQL 替换原版内嵌的 SQLite 数据库**，以适配生产级部署需求。

## 我们改了什么

相对原版，本仓库**仅改动数据库层**：

| 维度 | 原版（StaffDeck） | 本 Demo |
|------|------|------|
| 数据库 | SQLite（文件型） | PostgreSQL（Docker 部署） |
| 驱动 | 内置 | `psycopg[binary]`（psycopg3） |
| 建表 | `create_all` + 2000 行 SQLite 专属迁移 | 仅 `create_all`（后续计划接入 Alembic） |
| 连接管理 | `check_same_thread=False` | `QueuePool` + `pool_pre_ping` + `pool_recycle` |
| 锁处理 | 捕获 "database is locked" 重试 | 移除（PG 用 MVCC，无整库锁） |

业务代码（API、Agent 运行时、领域服务）**零改动** —— 重构完全收敛在数据层与配置层。

## 快速开始

### 前置条件

- Python 3.11+
- Node.js 20+
- Docker（用于启动 PostgreSQL）

### 1. 启动 PostgreSQL

```bash
docker run -d --name staffdeck-postgres \
  -e POSTGRES_USER=staffdeck \
  -e POSTGRES_PASSWORD=staffdeck123 \
  -e POSTGRES_DB=staffdeck \
  -p 5432:5432 \
  -v staffdeck-pgdata:/var/lib/postgresql/data \
  postgres:16-alpine
```

### 2. 安装后端依赖

```bash
python -m venv backend/.venv
# Windows
backend\.venv\Scripts\python -m pip install -e "backend[dev]"
# macOS/Linux
backend/.venv/bin/python -m pip install -e "backend[dev]"
```

### 3. 安装前端依赖

```bash
npm --prefix frontend-enterprise ci
```

### 4. 配置

```bash
cp backend/.env.example backend/.env
# 编辑 backend/.env，填入模型 API Key 与 APP_SECRET
```

### 5. 启动

```bash
# Windows
.\scripts\dev_up.ps1 --detach
# macOS/Linux/WSL
scripts/dev_up.sh --detach
```

打开 http://127.0.0.1:5173/workspace/gallery，默认管理员账号：`admin` / `admin`。

## 技术栈

- **前端**：React 18 + TypeScript + Vite 6 + TailwindCSS + shadcn/ui
- **后端**：FastAPI + SQLModel + OpenAI/Anthropic SDK
- **数据库**：PostgreSQL 16（本 Demo）/ SQLite（原版）

## 项目结构

```
├── backend/                  # FastAPI 后端（API、Agent 运行时、存储）
├── frontend-enterprise/      # React/TypeScript 工作台前端
├── scripts/                  # 生命周期脚本 + 数据库迁移工具
├── Dockerfile                # 多阶段构建（单端口部署）
└── k8s/                      # Kubernetes 部署模板
```

## 致谢

- 原项目：[OpenBMB/StaffDeck](https://github.com/OpenBMB/StaffDeck)（AGPL-3.0）
- 本 Demo 仅用于学习与研究用途。
