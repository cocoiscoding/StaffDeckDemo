---
name: "project-bootstrap-guide"
description: "Systematically analyze an unfamiliar full-stack project repository, identify its tech stack, architecture and dependencies, and derive a complete, verified set of startup steps. Invoke when the user obtains an existing project and needs to understand its architecture or figure out how to run it locally."
---

# Project Bootstrap Guide / 项目启动指南

> 本 Skill 用于指导大模型在面对一个**陌生的、完整的前后端项目代码库**（可能含数据库、中间件）时，以系统架构师视角，**有序、严谨、可验证**地推导出"如何在本地把项目跑起来"的完整方案。

---

## 一、技能定位与适用场景

### 1.1 适用场景
- 实习生 / 新成员接手一个既有项目，需要快速搞清楚"这东西怎么跑起来"。
- 拿到一份完整代码压缩包或 Git 仓库，技术栈未知或部分未知。
- 项目存在前端 + 后端（+ 数据库 / 缓存 / 消息队列等中间件），需要协同启动。
- 大模型需要先**理解项目架构**，再给出**可执行的启动步骤**。

### 1.2 目标产物
经过本 Skill 的分析后，应输出一份结构化报告，至少包含：
1. 项目架构总览（技术栈、模块划分、模块间关系）。
2. 运行环境要求（语言版本、依赖工具、外部服务）。
3. **可直接复制执行的启动命令清单**（按正确顺序排列）。
4. 启动后的验证方法与访问地址。
5. 常见报错预案与排障指引。

### 1.3 核心原则
- **先侦察后行动**：在给出任何命令前，必须先充分阅读项目中的"指纹文件"（配置文件、清单文件、启动脚本）。
- **顺序敏感**：依赖安装 → 配置 → 数据库 → 后端 → 前端，顺序错误必然失败。
- **可验证**：每一步都要说明"如何判断这一步成功了"。
- **最小化猜测**：基于文件证据做推断，而不是凭空假设；遇到不确定处，显式标注假设并请用户确认。

---

## 二、核心方法论：六阶段分析框架

面对任何陌生项目，严格按以下六个阶段推进。每个阶段都给出了**要找什么文件、看到什么信息、得到什么结论**。

### 阶段一：项目全貌识别（鸟瞰）
**目标**：判断项目是单体还是多模块、是否 monorepo、前端后端在哪里。

**侦察动作**：
1. 列出根目录文件结构（`LS` 根目录）。
2. 重点识别以下"结构指纹"：

| 文件 / 目录 | 含义 |
|-------------|------|
| `package.json` + `package-lock.json` | Node.js / 前端项目 |
| `lerna.json` / `nx.json` / `turbo.json` / `pnpm-workspace.yaml` | monorepo（多包仓库） |
| `package.json` 中含 `"workspaces"` 字段 | npm/yarn workspace monorepo |
| `apps/` `packages/` `services/` 目录 | 常见 monorepo 分层结构 |
| `pom.xml` / `build.gradle` / `build.gradle.kts` | Java / Kotlin 项目（Maven / Gradle） |
| `requirements.txt` / `Pipfile` / `pyproject.toml` / `poetry.lock` | Python 项目 |
| `go.mod` | Go 项目 |
| `composer.json` | PHP 项目 |
| `Gemfile` | Ruby 项目 |
| `Cargo.toml` | Rust 项目 |
| `docker-compose.yml` / `docker-compose.yaml` | 容器化编排（**重要：可能一键启动全套**） |
| `Dockerfile` | 单服务容器构建 |
| `Makefile` | 常聚合了 install/start/test 等快捷命令 |

**结论产出**：
- 项目拓扑：`单体` / `前后端分离` / `monorepo`。
- 各模块位置：哪个目录是前端、哪个是后端、哪个是数据库脚本。
- 是否有容器化方案（若有 `docker-compose`，优先考虑作为启动主路径）。

---

### 阶段二：技术栈指纹识别
**目标**：精确定位每一层使用的技术栈，作为后续命令选择的依据。

**2.1 前端指纹**（在 `package.json` 的 `dependencies` / `devDependencies` 中匹配）：

| 关键依赖 | 框架 / 工具 |
|----------|------------|
| `react`, `react-dom` | React |
| `vue` | Vue |
| `@angular/core` | Angular |
| `svelte` | Svelte |
| `next` | Next.js（React 全栈框架） |
| `nuxt` | Nuxt.js（Vue 全栈框架） |
| `vite` | Vite 构建工具（常配 `vite.config.*`） |
| `webpack` | Webpack（常配 `webpack.config.*`） |
| `@vue/cli-service` | Vue CLI |
| `react-scripts` | Create React App |

> **关键区分**：Next.js / Nuxt.js 这类**全栈框架**，前后端可能在一个项目里，不能简单套用"前后端分离"的启动模式。

**2.2 后端指纹**：

| 语言 / 生态 | 关键文件 / 依赖 | 典型框架 |
|------------|----------------|----------|
| Node.js | `express` / `koa` / `@nestjs/core` / `fastify` | Express / Koa / NestJS / Fastify |
| Java | `pom.xml` 含 `spring-boot-starter`；`application.yml` / `application.properties` | Spring Boot（最常见） |
| Python | `django` / `flask` / `fastapi` / `tornado` | Django / Flask / FastAPI |
| Go | `go.mod`；含 `gin` / `echo` / `fiber` | Gin / Echo / Fiber |
| PHP | `composer.json` 含 `laravel/framework` / `symfony` | Laravel / Symfony |
| Ruby | `Gemfile` 含 `rails` / `sinatra` | Rails / Sinatra |
| .NET | `*.csproj` / `*.sln` | ASP.NET Core |

**2.3 数据库 / 中间件指纹**（看依赖 + 配置）：

| 指纹 | 服务 |
|------|------|
| `mysql2` / `pymysql` / `mysql-connector` / `spring-boot-starter-data-jpa` + `mysql` 驱动 | MySQL |
| `pg` / `psycopg2` / `postgresql` 驱动 | PostgreSQL |
| `mongoose` / `MongoClient` / `motor` | MongoDB |
| `ioredis` / `redis` / `jedis` / `go-redis` | Redis |
| `amqplib` / `pika` / `kafka` 客户端 | RabbitMQ / Kafka |
| `sequelize` / `typeorm` / `prisma` / `gorm` / `mybatis` / `hibernate` | ORM（影响迁移与建表方式） |

**结论产出**：一张"技术栈清单表"，列明前端 / 后端 / 数据库 / 缓存 / ORM 各用了什么。

---

### 阶段三：运行环境与依赖审计
**目标**：明确启动前需要的运行时版本与全局工具，避免"装不上、跑不起来"。

**侦察动作**：
1. 查找版本约束文件：

| 文件 | 约束内容 |
|------|---------|
| `.nvmrc` / `.node-version` | Node.js 版本 |
| `.java-version` / `pom.xml` 的 `<java.version>` | JDK 版本 |
| `.python-version` / `runtime.txt` / `pyproject.toml` 的 `requires-python` | Python 版本 |
| `.tool-versions` (asdf) | 多语言版本（asdf 工具） |
| `engines` 字段（package.json） | Node / npm 版本约束 |
| `go.mod` 的 `go` 指令 | Go 版本 |

2. 识别依赖安装命令（按包管理器）：

| 生态 | 包管理器识别 | 安装命令 |
|------|------------|---------|
| Node.js | 有 `pnpm-lock.yaml` → pnpm；`yarn.lock` → yarn；`package-lock.json` → npm | `pnpm install` / `yarn` / `npm install` |
| Java | `pom.xml` → Maven；`build.gradle` → Gradle | `mvn install` / `gradle build`（通常可自动下载依赖） |
| Python | `poetry.lock` → poetry；`Pipfile` → pipenv；`requirements.txt` → pip | `poetry install` / `pipenv install` / `pip install -r requirements.txt` |
| Go | `go.mod` | `go mod download`（通常 build 时自动） |
| PHP | `composer.lock` | `composer install` |

3. **monorepo 特殊处理**：
   - pnpm workspace：在**根目录**执行 `pnpm install`（会安装所有子包依赖）。
   - 启动某个子包：通常在根目录用 `pnpm --filter <pkg> dev` 或 `turbo run dev`，或在子包目录单独执行。

**结论产出**：
- 必装运行时清单（含版本）。
- 每个模块的依赖安装命令（注意根目录 vs 子目录的执行位置）。

---

### 阶段四：数据持久化层处理
**目标**：让数据库就绪、表结构就绪、必要种子数据就绪。这是后端能否启动的前提。

**侦察动作**：
1. **数据库选型确认**：从阶段二的指纹 + 配置文件（`application.yml`、`.env`、`config/database.yml`、`settings.py`、`ormconfig.json` 等）确认数据库类型与连接串。
2. **建库建表方式判定**：

| 场景 | 指纹 | 处理方式 |
|------|------|---------|
| 提供 SQL 脚本 | `*.sql`、`schema.sql`、`init.sql`、`db/init/*` | 手动导入：`mysql -u root < schema.sql` |
| ORM 迁移工具 | `prisma/migrations`、`migrations/`（Alembic）、`db/migrate`（Rails）、`flyway`、`liquibase` | 执行迁移命令（见下） |
| 自动建表 | TypeORM `synchronize:true`、GORM `AutoMigrate`、Sequelize `sync` | 启动时自动建表，无需手动 |
| Docker 初始化 | `docker-compose.yml` 中数据库服务挂载 `./init:/docker-entrypoint-initdb.d` | 容器首次启动自动执行 SQL |

3. **常见迁移命令速查**：

| 工具 | 命令 |
|------|------|
| Prisma | `npx prisma migrate deploy` / `npx prisma db push` |
| TypeORM | `npm run typeorm migration:run` |
| Sequelize | `npx sequelize-cli db:migrate` |
| Alembic (Python) | `alembic upgrade head` |
| Django | `python manage.py migrate` |
| Rails | `rails db:migrate` |
| Flyway (Java) | 通常随应用启动自动执行 |
| Spring Data JPA | `spring.jpa.hibernate.ddl-auto=update` 时自动 |

4. **种子数据**：查找 `seed` 相关脚本（`seed.sql`、`prisma/seed.ts`、`npm run seed`、`python manage.py loaddata`、`rails db:seed`）。

**结论产出**：
- 数据库准备步骤（装服务 / 用 Docker / 连远程）。
- 建表与种子数据的命令。

---

### 阶段五：配置与环境变量
**目标**：补齐项目运行所需的外部配置（密钥、连接串、第三方服务），这是"启动失败最高频"的环节。

**侦察动作**：
1. 查找配置模板：`.env.example`、`.env.sample`、`.env.local.example`、`config.example.json`、`application.yml.example`。
2. **复制为正式配置**：`.env.example` → `.env`，并按需填写。
3. 重点关注以下高敏感、高失败率的配置项：

| 类别 | 典型键名 | 风险 |
|------|---------|------|
| 数据库连接 | `DATABASE_URL` / `DB_HOST` / `MYSQL_URL` | 连不上 → 后端直接崩 |
| 端口 | `PORT` / `SERVER_PORT` | 端口冲突 → 启动失败 |
| 密钥 | `JWT_SECRET` / `SECRET_KEY` / `APP_KEY` | 缺失 → 报错或鉴权异常 |
| 第三方服务 | `REDIS_URL` / `OSS_*` / `SMTP_*` / 各种 API Key | 功能不可用 |
| 跨域 / 代理 | `VITE_PROXY_TARGET` / `CORS_ORIGIN` | 前端连后端报 CORS |

4. **前端 API 地址配置**：前端必须知道后端在哪。
   - Vite：`vite.config.*` 中的 `server.proxy`。
   - 前端 `.env` 中的 `VITE_API_BASE_URL` / `NEXT_PUBLIC_API_URL` / `VUE_APP_BASE_API`。
   - 确认该地址指向本地后端的端口。

**结论产出**：必须配置的环境变量清单 + 推荐取值。

---

### 阶段六：启动执行与验证
**目标**：按正确顺序启动，并确认每层真正就绪。

**6.1 标准启动顺序**（绝大多数项目遵循）：
```
数据库 / 中间件  →  后端 API  →  前端
```

**6.2 启动命令识别**：

| 层级 | 命令来源 |
|------|---------|
| Node.js 前端/后端 | `package.json` 的 `scripts`：`dev` / `start` / `serve` |
| Spring Boot | `mvn spring-boot:run` 或 `./mvnw spring-boot:run`；或打 jar 后 `java -jar app.jar` |
| Python | `python manage.py runserver` / `uvicorn main:app --reload` / `flask run` |
| Go | `go run main.go` 或 `go run cmd/server/main.go` |
| Docker 全家桶 | `docker-compose up -d`（**强烈推荐，最省心**） |
| Makefile | `make dev` / `make start`（聚合命令，优先看） |

**6.3 优先级建议**（给用户的推荐路径）：
1. 若有 `docker-compose.yml` 且用户环境支持 Docker → **优先用 `docker-compose up`**，可一键拉起全套服务。
2. 其次看 `Makefile` / 根 `package.json` 是否有聚合启动命令（如 `concurrently` 同时起前后端）。
3. 最后才是手动分层启动。

**6.4 验证启动成功**：
- 后端：控制台出现 `Started Application` / `Uvicorn running on http://...` / `Tomcat started on port(s)`；或 `curl http://localhost:<port>/health`、`/api/health` 返回 200。
- 前端：浏览器访问提示的地址（常为 `http://localhost:5173` / `3000` / `8080`），页面正常渲染。
- 数据库：用客户端连接或 `docker ps` 确认容器健康。

**结论产出**：完整的、按顺序的启动命令清单 + 验证方法 + 访问地址。

---

## 三、技术栈指纹速查表（综合）

将阶段一～二的识别结果汇总成"决策树"，便于快速定位命令：

```
根目录扫描
├── 有 docker-compose.yml ──→ 推荐: docker-compose up -d  (先看此路径)
├── 有 Makefile ────────────→ 先看 make help / make 目标
└── 无容器编排
    ├── Node.js 全栈 (package.json)
    │   ├── Next.js / Nuxt.js ─→ npm run dev (前后端一体)
    │   └── 前后端分离 ───────→ 分别在 前端目录 / 后端目录 npm run dev
    ├── Java (pom.xml) ──────→ mvn spring-boot:run + 前端 npm run dev
    ├── Python ─────────────→ manage.py runserver / uvicorn ... + 前端
    ├── Go ─────────────────→ go run . + 前端
    └── ...
```

---

## 四、常见陷阱与防御性检查

> 这些是项目启动**最容易翻车**的点。给出命令前，主动检查并提醒用户。

1. **执行目录错误**：monorepo 中很多命令必须在**特定子目录**或**根目录**执行，搞错则找不到配置。务必标注 `cd 到哪里`。
2. **端口冲突**：默认端口（3000/3306/6379/8080/5173 等）被占用 → 启动报 `EADDRINUSE`。提醒检查或换端口。
3. **环境变量缺失**：很多项目没有 `.env` 会静默用错误默认值或直接崩溃。**务必先建 `.env`**。
4. **数据库未初始化**：后端起来但表不存在 → 接口 500。提醒先跑迁移 / 导入 SQL。
5. **Node 版本不匹配**：`engines` 要求高版本但本机是旧版 → 依赖装不上或语法报错。提醒用 nvm 切版本。
6. **前后端 API 地址不对**：前端请求 404 / CORS 报错。提醒核对前端代理或 `BASE_URL` 指向后端端口。
7. **包管理器混用**：项目用 pnpm 却执行 `npm install`，可能产生 `package-lock` 冲突。按锁文件选择。
8. **私有依赖源**：`.npmrc` / `settings.xml` 指向内网源 → 公网环境装不上。提醒检查。
9. **Python 虚拟环境**：未激活 venv 导致全局污染或依赖缺失。建议先建虚拟环境。
10. **Maven Wrapper / Gradle Wrapper 缺失权限**：`mvnw` / `gradlew` 在某些系统无执行权限。提醒 `chmod +x`。
11. **Docker 资源不足 / 未启动**：`docker-compose up` 报连接拒绝。提醒确认 Docker Desktop 已运行。
12. **首次构建慢 / 看似卡住**：Maven/Gradle/npm 首次下载大量依赖，并非死机。提醒耐心等待。

---

## 五、标准输出模板

分析完成后，按以下模板输出，确保结构清晰、可执行、可验证。**这是必须遵守的输出格式。**

```markdown
# 项目启动指南：<项目名>

## 一、架构总览
- 项目拓扑：<单体 / 前后端分离 / monorepo / 全栈框架>
- 模块划分：
  | 层级 | 位置(目录) | 技术栈 |
  |------|-----------|--------|
  | 前端 | xxx/ | React + Vite |
  | 后端 | xxx/ | Spring Boot 3 |
  | 数据库 | — | MySQL 8 |
  | 缓存 | — | Redis |
- 模块间关系：<前端通过 /api 代理访问后端；后端连接 MySQL + Redis>

## 二、环境要求
- 运行时：Node.js >= 18、JDK 17、MySQL 8、Redis 7（或：Docker Desktop）
- 全局工具：pnpm、Maven（或用自带 wrapper）

## 三、启动步骤（按顺序执行）
### 第 0 步（可选，推荐）：Docker 一键启动
> 若已安装 Docker，可跳过后续手动步骤：
> ```bash
> docker-compose up -d
> ```

### 第 1 步：准备数据库
- 安装 / 启动 MySQL（或用 docker-compose 中的服务）
- 导入初始数据：
  ```bash
  mysql -u root -p < db/schema.sql
  ```

### 第 2 步：配置环境变量
- 后端：
  ```bash
  cd backend
  cp .env.example .env
  # 编辑 .env，填写数据库连接、密钥等
  ```
- 前端：
  ```bash
  cd frontend
  cp .env.example .env
  # 确认 VITE_API_BASE_URL 指向后端端口
  ```

### 第 3 步：安装依赖
- 后端：`cd backend && <安装命令>`
- 前端：`cd frontend && pnpm install`

### 第 4 步：启动后端
```bash
cd backend
<启动命令，如 mvn spring-boot:run>
```
- 成功标志：<控制台输出 / 健康检查地址>

### 第 5 步：启动前端
```bash
cd frontend
pnpm dev
```
- 成功标志：<访问地址>

## 四、访问地址
- 前端页面：http://localhost:xxxx
- 后端 API：http://localhost:xxxx
- 默认账号（如有）：<用户名 / 密码>

## 五、常见问题预案
- 端口冲突：……
- 数据库连不上：……
- CORS 报错：……

## 六、待确认项（假设与风险）
- 假设数据库密码为 xxx，请按实际修改。
- 未找到 xxx 配置，已用默认值，请核实。
```

---

## 六、执行约束（给大模型的硬性要求）

1. **不得跳过侦察直接给命令**。必须先读取关键文件（package.json、pom.xml、docker-compose.yml、README、`.env.example`、配置文件）再下结论。
2. **优先阅读项目自带 README / CONTRIBUTING / docs**。原作者的启动说明是第一手资料，若存在则以其为准，本 Skill 用于补充和校验。
3. **命令必须标注执行目录**。monorepo / 前后端分离项目，目录错则全错。
4. **遇到不确定，显式声明**。用"待确认项"列出假设，请用户核实，而不是默默猜测后给出可能错误的命令。
5. **推荐最优路径**：有 Docker 优先 Docker；有聚合脚本优先聚合脚本；降低用户的手动步骤数。
6. **输出必须可复制执行**。命令块用代码围栏，每步注明"成功标志"，让实习生能自己判断对不对。
