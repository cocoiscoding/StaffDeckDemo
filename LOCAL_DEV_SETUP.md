# 本地开发环境搭建指南

本文档介绍如何在本地快速启动 StaffDeck 开发环境。

> **适用场景**：团队成员拿到代码后，在本地搭建开发环境（PostgreSQL + 后端 + 前端）。

---

## 环境要求

| 软件 | 版本要求 | 说明 |
|------|---------|------|
| **Docker Desktop** | 最新版 | 用于运行 PostgreSQL 数据库 |
| **Python** | ≥ 3.11 | 运行后端 FastAPI 服务 |
| **Node.js** | ≥ 18（推荐 20） | 构建前端 Vue 应用 |
| **pnpm 或 npm** | pnpm ≥ 8 或 npm ≥ 10 | 前端包管理 |
| **Git** | 最新版 | 克隆代码仓库 |

---

## 第一步：克隆代码仓库

```bash
git clone <仓库地址> StaffDeck
cd StaffDeck
git checkout feature-1.0.1   # 切换到功能分支
```

---

## 第二步：启动 PostgreSQL 数据库（Docker）

### 2.1 启动数据库

项目根目录已提供 `docker-compose.yml`，一条命令即可启动 PostgreSQL：

```bash
docker compose up -d
```

输出类似：
```
[+] Running 2/2
 ✔ Network staffdeck-main_default    Created
 ✔ Container staffdeck-postgres      Started
```

### 2.2 验证数据库就绪

```bash
docker compose ps
```

确认 `staffdeck-postgres` 状态为 `Up (healthy)`。

也可以直接连进去测试：
```bash
docker exec -it staffdeck-postgres psql -U staffdeck -d staffdeck -c "\dt"
```

### 2.3 常用数据库操作

| 操作 | 命令 |
|------|------|
| 启动数据库 | `docker compose up -d` |
| 停止数据库（保留数据） | `docker compose down` |
| 重置数据库（删除所有数据） | `docker compose down -v` |
| 查看数据库日志 | `docker compose logs -f postgres` |
| 连接 PG 命令行 | `docker exec -it staffdeck-postgres psql -U staffdeck -d staffdeck` |

> **端口冲突？** 如果本地 5432 已被占用，修改 `docker-compose.yml` 中的端口映射为 `"5433:5432"`，并同步修改 `.env` 中的 `DATABASE_URL`。

---

## 第三步：配置后端环境变量

### 3.1 复制配置模板

```bash
cd backend
cp .env.example .env
```

### 3.2 关键配置项（默认值通常无需修改）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DATABASE_URL` | `postgresql+psycopg://staffdeck:staffdeck123@localhost:5432/staffdeck` | PG 连接串（与 docker-compose 保持一致） |
| `AUTO_INIT_DB` | `true` | 首次启动自动建表 + 写入演示数据 |
| `APP_SECRET` | `change-me-in-development` | Session 密钥（开发环境可用默认值） |

> 如果你修改了 docker-compose 中的用户名/密码/端口，请同步修改 `.env`。

---

## 第四步：启动后端服务

```bash
cd backend

# 安装依赖（推荐使用虚拟环境）
pip install -e .

# 启动后端（开发模式，自动重载）
python -m uvicorn app.main:app --host 0.0.0.0 --port 5173 --reload
```

首次启动时会看到建表日志：
```
INFO:     Creating all tables...
INFO:     Application startup complete.
```

验证后端是否启动成功：
```bash
curl http://localhost:5173/api/health
# 返回 {"status":"ok","app":"Skill Agent Loop Service"}
```

---

## 第五步：启动前端（可选）

```bash
cd frontend-enterprise

# 安装依赖
npm install   # 或 pnpm install

# 启动前端开发服务器（默认 5174 端口）
npm run dev
```

浏览器访问 `http://localhost:5174` 即可看到 StaffDeck 界面。

---

## 完整启动流程速查

```bash
# 1. 启动 PG 数据库
docker compose up -d

# 2. 配置后端环境变量（首次）
cd backend && cp .env.example .env && cd ..

# 3. 启动后端
cd backend && pip install -e . && python -m uvicorn app.main:app --host 0.0.0.0 --port 5173 --reload

# 4. 启动前端（另开终端）
cd frontend-enterprise && npm install && npm run dev
```

---

## 常见问题

### Q1：后端启动报 "connection refused" 或数据库连接失败？

**原因**：PG 容器还没就绪，或 `DATABASE_URL` 配置错误。

**解决**：
```bash
# 1. 确认容器在运行且健康
docker compose ps

# 2. 确认端口未被占用
docker compose logs postgres

# 3. 核对 .env 中的 DATABASE_URL 与 docker-compose.yml 一致
```

### Q2：端口 5432 被占用？

修改 `docker-compose.yml`：
```yaml
ports:
  - "5433:5432"   # 改成 5433
```

同步修改 `backend/.env`：
```
DATABASE_URL="postgresql+psycopg://staffdeck:staffdeck123@localhost:5433/staffdeck"
```

### Q3：如何完全重置数据库（删掉所有数据重新开始）？

```bash
docker compose down -v     # -v 表示删除数据卷
docker compose up -d       # 重新启动（PG 会重新初始化）
```

后端启动时会因 `AUTO_INIT_DB=true` 自动重建表结构。

### Q4：如何查看/编辑数据库中的数据？

使用数据库 GUI 工具连接：
- **主机**：`localhost`
- **端口**：`5432`
- **用户名**：`staffdeck`
- **密码**：`staffdeck123`
- **数据库名**：`staffdeck`

推荐工具：DBeaver / pgAdmin / Navicat / DataGrip。

---

## 数据库架构说明

StaffDeck 使用 **PostgreSQL 16** 作为生产数据库，通过 **SQLModel ORM**（SQLAlchemy + Pydantic）操作数据。

- **表结构定义**：`backend/app/db/models.py`（35 张表）
- **建表逻辑**：`backend/app/db/database.py` 的 `init_db()`（幂等，首次启动自动执行）
- **ORM 查询**：分布在 `backend/app/api/*.py` 各业务模块（不手写 SQL）
- **连接池**：`pool_size=10, max_overflow=20, pool_pre_ping=True`
