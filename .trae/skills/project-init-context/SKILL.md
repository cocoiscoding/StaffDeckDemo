---
name: "project-init-context"
description: "Analyze an unfamiliar IT project (frontend/backend) and generate a project context doc (CLAUDE.md / .cursorrules) that replaces /init. Invoke when /init is unavailable or fails, or when you need to generate/sync project context for AI-assisted development."
---

# Project Init Context / 项目初始化与上下文生成指南

> 本 Skill 用于指导大模型在 **AI 编码工具的 `/init` 命令不可用、失败或不存在** 的场景下，以**资深架构师 + 技术文档作者**的双重视角，对任意 IT 项目（前端 / 后端 / 全栈 / monorepo）进行**系统、严谨、可验证**的全面分析，最终沉淀出一份**结构化、可复用、对后续 AI 辅助开发有真实增益**的项目上下文文档（`CLAUDE.md` / `AGENTS.md` / `.cursorrules` / `copilot-instructions.md` 等），等效甚至优于原生 `/init` 命令的产出。

---

## 一、技能定位与适用场景

### 1.1 适用场景
- **`/init` 命令不可用**：当前 IDE / 编辑器 / Agent 环境未提供 `/init`，或该命令执行失败、超时、生成的文档质量不佳（遗漏关键信息、存在幻觉）。
- **新成员 / Agent 接手既有项目**：需要在极短时间内建立对项目的全局认知，避免"盲人摸象"。
- **团队规范化 / 上下文对齐**：需要一份被全体开发者和 AI Agent 共享、作为"事实真相（source of truth）"的项目上下文。
- **多 Agent 协作**：不同 AI Agent（编码、测试、评审、排障）需要共享同一份项目知识底座。
- **跨技术栈项目**：项目横跨前端 / 后端 / 移动端 / 基础设施，需要统一梳理。
- **遗留项目改造**：文档缺失或严重过期，需基于代码现状重建"活的"项目档案。

### 1.2 与"项目启动指南"的边界
本 Skill 聚焦于**全面理解项目并沉淀为可复用上下文文档**；不专门解决"如何把项目跑起来"（那属于 `project-bootstrap-guide` 的职责）。两者互补：本 Skill 生成的上下文文档**包含**启动/测试/构建命令，但更侧重架构、约定、规范、领域逻辑等长期知识。

### 1.3 目标产物
经过本 Skill 的分析后，应输出一份**结构化项目上下文文档**，至少包含：
1. **项目元信息**：名称、定位、一句话描述、当前状态。
2. **架构总览**：项目拓扑、模块划分、模块间关系、核心数据流。
3. **技术栈清单**：前端 / 后端 / 数据层 / 基础设施 / 工具链，含版本。
4. **代码组织与目录约定**：分层方式、目录职责、命名约定、入口点。
5. **命令体系**：安装 / 开发 / 构建 / 测试 / Lint / 部署的完整命令（含执行目录）。
6. **架构与设计模式**：分层架构、状态管理、鉴权机制、关键设计决策。
7. **编码规范与风格**：Linter / Formatter / 编辑器配置 / 提交规范。
8. **测试策略**：测试框架、分层、命令、覆盖率门槛。
9. **数据与 API 契约**：数据库设计、ORM、API 风格、接口文档位置。
10. **环境与部署**：环境变量、容器化、CI/CD、目标运行环境。
11. **AI 协作约定**：写给 AI Agent 的"红线与偏好"（写在哪里、改哪里、禁止做什么）。
12. **待确认项与风险**：基于假设的部分、需人工核实的内容。

### 1.4 核心原则
- **证据优先，零幻觉**：每一条结论必须能在代码或配置文件中找到出处。无法从文件确认的，**必须显式标注为"假设/待确认"**，绝不臆断。
- **从外到内，逐层下钻**：先 README → 目录结构 → 配置文件 → 入口代码 → 核心逻辑，避免一上来就陷入细节。
- **全面但有重点**：覆盖所有维度，但对"AI 协作时最易踩坑的点"（约定、禁忌、入口、命令）给予最高权重。
- **尊重既有约定**：项目里已有的规范、注释、文档是第一手事实；本 Skill 的任务是**发现并如实记录**，而非**发明或强加**规范。
- **可维护性**：生成的文档要写"为什么"和"在哪里能查证"，而非一次性快照，便于后续随项目演进更新。
- **最小侵入**：分析过程只读不写，绝不修改项目任何文件。

---

## 二、核心方法论：十一维度分析框架

面对任何陌生项目，严格按以下维度推进。每个维度给出**要找什么、看什么文件、得出什么结论**。维度之间有依赖：**一→三→二→四→五→六→七→八→九→十→十一** 是推荐顺序，但可按项目实际情况灵活回溯。

> 通用执行约束：先用 `LS` 列目录、`Glob` 按模式找文件、`Grep` 搜关键词、`Read` 读关键文件。**不要一次性 dump 全部文件**，按维度有目的地读取。

---

### 维度一：项目元信息与意图识别（"这个项目是什么"）
**目标**：用一句话说清项目是什么、解决什么问题、当前状态。

**侦察动作**：
1. 读 `README.md` / `README.zh-CN.md` / `readme.txt`（**最高优先级**，第一手的项目说明）。
2. 读 `package.json` 的 `name` / `description` / `version` / `keywords`；`pom.xml` 的 `<name>` / `<description>`；`pyproject.toml` 的 `[project]` 元信息；`go.mod` 的 `module` 路径。
3. 读 `LICENSE` 判断开源协议与合规要求；读 `CONTRIBUTING.md` / `CODE_OF_CONDUCT.md` 了解贡献规范。
4. 读 `CHANGELOG.md` / `VERSION` / Git tag 推断项目成熟度与版本演进。
5. 查 Git 远程信息（`.git/config`）了解仓库归属与协作模式。

**结论产出**：项目的一句话定位、目标用户、当前版本/状态、协议、维护方。

---

### 维度二：项目结构与拓扑识别（"项目长什么样"）
**目标**：建立项目的空间地图——单体 / 前后端分离 / monorepo / 全栈框架。

**侦察动作**：
1. `LS` 根目录，识别"结构指纹"：

| 文件 / 目录 | 含义 |
|-------------|------|
| `package.json` + 锁文件 | Node.js / 前端项目 |
| `lerna.json` / `nx.json` / `turbo.json` / `pnpm-workspace.yaml` / `pnpm-workspace.yml` | monorepo（多包仓库） |
| `package.json` 含 `"workspaces"` | npm / yarn workspace monorepo |
| `apps/` `packages/` `libs/` `services/` 目录 | 常见 monorepo 分层 |
| `pom.xml`（多 `<module>`）/ `settings.gradle`（多 `include`） | Java / Kotlin 多模块 |
| `requirements.txt` / `Pipfile` / `pyproject.toml` / `poetry.lock` | Python 项目 |
| `go.mod` | Go 项目 |
| `composer.json` | PHP 项目 |
| `Gemfile` | Ruby 项目 |
| `Cargo.toml`（多 `[[bin]]` / `[workspace]`） | Rust 项目 / workspace |
| `*.csproj` / `*.sln` | .NET 项目 |
| `pubspec.yaml` | Dart / Flutter |

2. 对每个一级子目录再 `LS`，递归建立 2~3 层的目录树（**不必全量展开，抓主干**）。
3. 识别前端 / 后端 / 共享代码的位置划分。

**结论产出**：
- 项目拓扑类型：`单体` / `前后端分离` / `monorepo` / `全栈框架（Next.js / Nuxt / Remix 等）`。
- 一张"模块地图"：哪个目录是什么职责。

---

### 维度三：技术栈与依赖指纹识别（"用了什么技术"）
**目标**：精确定位每一层技术栈，作为后续命令、规范、架构推断的依据。

**3.1 前端指纹**（读 `package.json` 的 `dependencies` / `devDependencies`）：

| 关键依赖 | 框架 / 工具 |
|----------|------------|
| `react`, `react-dom` | React |
| `vue` | Vue |
| `@angular/core` | Angular |
| `svelte`, `@sveltejs/kit` | Svelte / SvelteKit |
| `solid-js` | Solid |
| `next` | Next.js（React 全栈框架） |
| `nuxt` | Nuxt.js（Vue 全栈框架） |
| `remix` / `@remix-run/*` | Remix |
| `vite` | Vite 构建 |
| `webpack` | Webpack 构建 |
| `@vue/cli-service` | Vue CLI |
| `react-scripts` | Create React App |
| `@tanstack/*-query` / `redux` / `zustand` / `pinia` / `mobx` / `recoil` / `jotai` | 前端状态管理 |
| `tailwindcss` / `sass` / `less` / `styled-components` / `@emotion` | 样式方案 |
| `antd` / `element-plus` / `@mui` / `chakra-ui` / `ar-design` | UI 组件库 |
| `react-native` / `expo` / `@capacitor` / `electron` / `tauri` | 跨端（移动 / 桌面） |

**3.2 后端指纹**：

| 语言 / 生态 | 关键文件 / 依赖 | 典型框架 |
|------------|----------------|----------|
| Node.js | `express` / `koa` / `@nestjs/core` / `fastify` / `hapi` | Express / Koa / NestJS / Fastify |
| Java | `pom.xml` 含 `spring-boot-starter`；`application.yml` / `application.properties` | Spring Boot（最常见）/ Quarkus / Micronaut |
| Python | `django` / `flask` / `fastapi` / `tornado` / `sanic` | Django / Flask / FastAPI |
| Go | `go.mod` 含 `gin` / `echo` / `fiber` / `go-zero` / `kratos` | Gin / Echo / Fiber / go-zero |
| PHP | `composer.json` 含 `laravel/framework` / `symfony` / `webman` | Laravel / Symfony / Webman |
| Ruby | `Gemfile` 含 `rails` / `sinatra` / `grape` | Rails / Sinatra |
| .NET | `*.csproj` 含 `Microsoft.AspNetCore.*` | ASP.NET Core |
| Rust | `Cargo.toml` 含 `axum` / `actix-web` / `rocket` | Axum / Actix / Rocket |

**3.3 数据层 / 中间件 / ORM 指纹**：

| 指纹 | 服务 / 工具 |
|------|------------|
| `mysql2` / `pymysql` / `mysql-connector` / Spring JPA + MySQL 驱动 | MySQL |
| `pg` / `psycopg2` / `postgresql` 驱动 | PostgreSQL |
| `mongoose` / `MongoClient` / `motor` | MongoDB |
| `ioredis` / `redis` / `jedis` / `go-redis` | Redis |
| `amqplib` / `pika` / `kafka` / `rocketmq` 客户端 | RabbitMQ / Kafka / RocketMQ |
| `elasticsearch` / `meilisearch` / `typesense` 客户端 | 搜索引擎 |
| `sequelize` / `typeorm` / `prisma` / `@prisma/client` / `gorm` / `mybatis` / `hibernate` / `sqlalchemy` / `sqlmodel` / `tortoise` | ORM（影响迁移与查询方式） |

**3.4 工具链 / DevOps 指纹**：

| 文件 | 工具 |
|------|------|
| `Dockerfile` / `docker-compose.yml` | 容器化 |
| `.github/workflows/` | GitHub Actions CI/CD |
| `.gitlab-ci.yml` | GitLab CI |
| `Jenkinsfile` | Jenkins |
| `.circleci/` | CircleCI |
| `Makefile` / `justfile` / `Taskfile.yml` | 任务编排 |
| `husky` + `.husky/` / `lefthook` / `.pre-commit-config.yaml` | Git Hook |
| `pnpm` / `yarn` / `npm` 锁文件 | 包管理器判定 |

**结论产出**：一张"技术栈清单表"，列明前端 / 后端 / 数据库 / 缓存 / ORM / 工具链各用了什么，附关键版本。

---

### 维度四：命令体系梳理（"怎么操作这个项目"）
**目标**：穷尽所有高频命令——安装、开发、构建、测试、Lint、部署，且必须标注**执行目录**与**包管理器**。

**侦察动作**：
1. 读 `package.json` 的 `scripts` 字段（前端 / Node 后端命令的主要来源）。
2. 读 `Makefile` / `justfile` / `Taskfile.yml`（聚合命令的主要来源，**优先级高于散装命令**）。
3. 读 `pom.xml` / `build.gradle`（Java 构建命令：`mvn` / `gradle`）。
4. 读 `pyproject.toml` 的 `[tool.poetry.scripts]` / `[project.scripts]`、`manage.py`（Django）。
5. 读根 `package.json` 的 monorepo 编排命令（`turbo run` / `nx run` / `concurrently`）。
6. 识别测试命令（见维度八）。

**命令识别速查**：

| 场景 | 命令来源 | 示例 |
|------|---------|------|
| 安装依赖 | 锁文件决定包管理器 | `pnpm install` / `yarn` / `npm install` / `mvn install` / `pip install -r requirements.txt` / `poetry install` / `go mod download` / `composer install` |
| 本地开发 | `scripts.dev` / `scripts.start` | `pnpm dev` / `npm run dev` / `mvn spring-boot:run` / `python manage.py runserver` / `uvicorn main:app --reload` / `go run .` |
| 生产构建 | `scripts.build` | `pnpm build` / `npm run build` / `mvn package` / `gradle build` / `go build` |
| 启动生产 | — | `node dist/main.js` / `java -jar app.jar` / `gunicorn` / 二进制 |
| Docker 全家桶 | `docker-compose.yml` | `docker-compose up -d` |

**结论产出**：一份分模块、标注执行目录、可复制执行的命令清单。

---

### 维度五：架构与代码组织模式（"代码是怎么组织的"）
**目标**：理解分层架构、入口点、核心数据流，让 AI 知道"新功能该往哪儿放、改动会影响到哪里"。

**侦察动作**：
1. **定位入口点**：
   - 前端：`src/main.ts(x)` / `src/index.ts(x)` / `pages/_app.tsx`（Next.js）/ `app/layout.tsx`（App Router）/ `main.js`。
   - 后端：`src/main.ts`（NestJS）/ `Application.java` / `manage.py` / `main.go` / `index.js` / `app.py`。
2. **识别分层架构**（读目录命名即可判断）：

| 目录命名模式 | 架构风格 |
|-------------|---------|
| `controllers/` `services/` `models/` `repositories/` | 经典三层 / MVC |
| `modules/`（每模块含 controller/service/dto） | NestJS 模块化 / DDD 模块 |
| `domain/` `application/` `infrastructure/` `interfaces/` | DDD（领域驱动设计）分层 |
| `routes/` `handlers/` `db/` | 轻量路由式（常见于 Node / Go） |
| `usecases/` `entities/` | Clean Architecture |
| `components/` `pages/` `views/` `hooks/` `store/` `utils/` | 前端常见分层 |
| `app/`（含 `api/` `page.tsx`）`components/` `lib/` | Next.js App Router |
| `pkg/` `internal/` `cmd/` | Go 标准布局 |

3. **识别配置 / 装配方式**：IoC 容器（NestJS 依赖注入 / Spring `@Autowired` / `tsyringe`）、配置中心、中间件链路。
4. **追踪一条核心数据流**：从前端"一个用户操作"→ API 请求 → 后端路由 → service → 数据库，把链路上的关键文件串起来，作为"示例链路"写入文档。

**结论产出**：分层架构图（文字版）、入口文件清单、一条端到端示例数据流。

---

### 维度六：编码规范与风格约定（"代码该怎么写"）
**目标**：提取项目实际遵守的代码规范，让 AI 生成的代码与项目风格一致。

**侦察动作**：
1. **Linter / Formatter 配置**：

| 文件 | 工具 |
|------|------|
| `.eslintrc*` / `eslint.config.*` | ESLint |
| `.prettierrc*` / `prettier.config.*` | Prettier |
| `.stylelintrc*` | Stylelint |
| `tsconfig.json` | TypeScript 编译选项（`strict`、路径别名 `paths`） |
| `.editorconfig` | 编辑器统一配置（缩进、换行、文件尾） |
| `checkstyle.xml` / `spotbugs` / `.editorconfig` | Java 静态检查 |
| `ruff.toml` / `pyproject.toml [tool.ruff]` / `.flake8` / `mypy.ini` | Python Lint / 类型检查 |
| `.golangci.yml` | Go 静态检查 |
| `.php-cs-fixer.php` / `phpstan.neon` | PHP 规范 |

2. **Git 提交规范**：查 `commitlint.config.*` / `.commitlintrc*`（Conventional Commits）、`cz-*`（commitizen）、`.husky/commit-msg`。
3. **路径别名**：读 `tsconfig.json` 的 `compilerOptions.paths`、`vite.config.*` 的 `resolve.alias`、`jsconfig.json`——**AI 必须用项目别名 import，否则路径错乱**。
4. **命名约定抽样**：从已有代码抽样观察——文件命名（`kebab-case` / `PascalCase` / `camelCase`）、组件命名、API 函数命名、常量命名。
5. **目录约定**：观察"同类文件放哪"——组件在 `components/` 还是 `widgets/`、工具在 `utils/` 还是 `lib/`、类型在 `types/` 还是内联。

**结论产出**：一份"本项目代码风格速查"，含缩进、引号、分号、命名、别名、提交规范。

---

### 维度七：测试体系识别（"怎么验证质量"）
**目标**：识别测试框架、测试分层、运行命令、覆盖率要求。

**侦察动作**：
1. **框架指纹**（读依赖 + 配置文件）：

| 文件 / 依赖 | 测试框架 |
|------------|---------|
| `jest` / `vitest` + `*.test.ts` / `*.spec.ts` | Jest / Vitest（单元 / 集成） |
| `@playwright/test` / `playwright.config.*` | Playwright（E2E） |
| `cypress` + `cypress.config.*` | Cypress（E2E） |
| `@testing-library/*` | 组件测试 |
| `pytest` / `pytest.ini` / `conftest.py` / `tests/` | pytest |
| `junit` / `testng` / `src/test/` | Java 测试 |
| `go test` / `*_test.go` | Go 内置测试 |
| `phpunit.xml` / `tests/` | PHPUnit |

2. **测试目录结构**：`__tests__/` / `tests/` / `spec/` / `e2e/` / `__mocks__/`；测试是否与源码同置（colocated，`*.test.ts`）。
3. **运行命令**：`scripts.test` / `scripts.test:e2e` / `scripts.test:coverage`；覆盖率配置（`jest.config` 的 `coverageThreshold` / `c8` / `vite.config` 的 `test.coverage`）。
4. **测试策略**：是否有测试金字塔（单元多 / E2E 少）、Mock 方式（`msw` / `nock` / `vi.mock`）、固件（fixture / factory）。

**结论产出**：测试框架、测试命令（按层级）、测试文件放哪、覆盖率门槛。

---

### 维度八：数据层与状态管理（"数据怎么流动和存储"）
**目标**：理解持久化方案与状态管理，这是改动最易引入 bug 的区域。

**侦察动作**：
1. **数据库设计**：找 `schema.prisma` / `migrations/` / `*.sql` / `entity/` / `model/` / `db/`，理解核心实体与关系。
2. **ORM 用法**：查询是写在哪一层（repository / service / 直接 controller）、是否有 DAO 封装、迁移工具与命令。
3. **缓存**：Redis / 本地缓存的使用模式、缓存键命名约定。
4. **前端状态管理**：全局状态（Redux / Zustand / Pinia / Mobx）、服务端状态（TanStack Query / SWR）、表单状态（React Hook Form / VeeValidate）。
5. **数据获取模式**：`fetch` / `axios` 封装位置、请求拦截器、错误处理统一位置。

**结论产出**：数据流向图、状态管理分工、数据库核心实体清单。

---

### 维度九：API 与接口契约（"接口长什么样"）
**目标**：理解 API 风格、路由组织、鉴权方式、文档位置。

**侦察动作**：
1. **API 风格**：REST（`routes/` / `controllers/`）/ GraphQL（`*.graphql` / `resolvers/`）/ gRPC（`*.proto`）/ tRPC（`routers/` + 类型推断）。
2. **路由组织**：读路由注册文件——NestJS `*.controller.ts` / Express `app.use` / Spring `@RequestMapping` / FastAPI `APIRouter`。
3. **鉴权机制**：JWT（找 `jsonwebtoken` / `jjwt` / `pyjwt`）、Session、OAuth2（`passport` / `spring-security` / `authlib`）、API Key；找 `auth/` `middleware/` `guards/` `interceptors/`。
4. **请求校验**：`class-validator` / `zod` / `joi` / `yup` / `pydantic` 的 DTO 定义位置。
5. **接口文档**：Swagger / OpenAPI（`swagger.json` / `swagger-ui` / `@Api` 注解 / `FastAPI` 自动文档）、Postman collection、`docs/` 目录。

**结论产出**：API 风格、鉴权方式、路由约定、DTO 校验方式、接口文档位置。

---

### 维度十：环境、配置与部署（"怎么配置和发布"）
**目标**：厘清环境变量、运行环境、部署链路。

**侦察动作**：
1. **环境变量**：读 `.env.example` / `.env.sample` / `application.yml` / `config/`，区分必需项与可选项，标注敏感项。前端环境变量注意前缀（`VITE_` / `NEXT_PUBLIC_` / `VUE_APP_` / `REACT_APP_`，**前缀错则变量不注入**）。
2. **多环境配置**：Spring 的 `application-{profile}.yml`、Node 的 `.env.development` / `.env.production`、`config/` 多文件。
3. **容器化**：读 `Dockerfile`（基础镜像、构建阶段、暴露端口）、`docker-compose.yml`（服务依赖编排、健康检查）。
4. **CI/CD**：读 `.github/workflows/*.yml` / `.gitlab-ci.yml`，理解构建-测试-部署流水线、触发条件、产物。
5. **目标运行环境**：Node 运行时版本、JDK 版本、目标服务器（Vercel / Cloudflare / K8s / VPS）。

**结论产出**：环境变量清单（必需 / 可选 / 敏感）、部署链路、目标运行环境与版本。

---

### 维度十一：业务领域理解（"项目在解决什么业务问题"）
**目标**：超越代码，理解核心业务概念与流程，让 AI 改动贴合业务语义。

**侦察动作**：
1. **核心实体**：从数据库表 / ORM 模型 / 领域对象提取核心名词（用户、订单、商品、租户等）。
2. **核心流程**：从 service 层 / use case 追踪关键业务流程（注册、下单、支付、审批等）。
3. **领域术语**：从代码命名、注释、README 提取业务专有名词，建立"术语表"。
4. **权限模型**：RBAC / ABAC、多租户隔离、数据权限。

**结论产出**：核心业务实体表、关键业务流程描述、领域术语表、权限模型。

---

## 三、标准探索清单（执行动作汇总）

> 按"侦察动作"归类，作为大模型的执行检查表。**每完成一项勾掉一项，确保无遗漏。**

### 3.1 必读文件清单（按优先级）
1. `README.md`（及各语言变体）—— 项目意图、启动说明的第一手资料。
2. 根配置文件 —— `package.json` / `pom.xml` / `pyproject.toml` / `go.mod` / `Cargo.toml` / `composer.json` / `Gemfile`。
3. 入口文件 —— 见维度五。
4. `.env.example` / 配置模板 —— 见维度十。
5. `Makefile` / `Taskfile.yml` / 根 `package.json` 的 `scripts` —— 命令体系。
6. `docker-compose.yml` / `Dockerfile` —— 部署与依赖服务。
7. `CONTRIBUTING.md` / `docs/` —— 贡献规范与设计文档。
8. CI/CD 配置 —— `.github/workflows/` / `.gitlab-ci.yml`。
9. Lint / Format / 类型配置 —— 见维度六。

### 3.2 标准探索动作序列
```
1. LS 根目录                     → 识别拓扑（维度二）
2. Read README + 元信息文件       → 项目意图（维度一）
3. Read 依赖清单文件              → 技术栈（维度三）
4. Read 命令来源（scripts/Makefile）→ 命令体系（维度四）
5. Glob/Read 入口文件 + 分层目录  → 架构（维度五）
6. Read 规范配置文件              → 编码约定（维度六）
7. Glob 测试文件 + 配置           → 测试体系（维度七）
8. Read ORM/实体/迁移             → 数据层（维度八）
9. Read 路由/控制器/鉴权          → API 契约（维度九）
10. Read env/docker/ci           → 环境部署（维度十）
11. 抽样 service/领域代码         → 业务理解（维度十一）
```

---

## 四、常见技术栈"指纹速查表"（综合决策树）

```
根目录扫描
├── README/CONTRIBUTING ──────→ 先读，提取项目意图与作者约定
├── package.json
│   ├── workspaces / pnpm-workspace.yaml / nx.json / turbo.json
│   │   → monorepo：根目录装依赖，按 filter/turbo 启动子包
│   ├── next / nuxt / remix ──→ 全栈框架：前后端一体，npm run dev
│   ├── react/vue/svelte ─────→ 前端：Vite/Webpack，npm run dev + build
│   └── express/koa/nestjs/fastify ─→ Node 后端
├── pom.xml / build.gradle ───→ Java：Maven/Gradle，Spring Boot 居多
├── pyproject.toml / requirements.txt ─→ Python：pip/poetry/pipenv
├── go.mod ───────────────────→ Go：go run / go build
├── composer.json ────────────→ PHP：Laravel/Symfony
├── Gemfile ──────────────────→ Ruby：Rails/Sinatra
├── Cargo.toml ───────────────→ Rust：cargo
├── *.csproj/*.sln ───────────→ .NET：dotnet
├── Dockerfile/docker-compose ─→ 容器化：优先 docker-compose up
└── Makefile/Taskfile ────────→ 优先看聚合命令（make dev/test/build）
```

---

## 五、项目上下文文档：标准输出模板

分析完成后，**必须**按以下结构生成项目上下文文档（`CLAUDE.md` / `AGENTS.md` / `.cursorrules` 等，文件名由用户指定或默认 `CLAUDE.md`）。**这是必须遵守的输出格式。**

```markdown
# <项目名> — 项目上下文

> 一句话定位：<这是什么项目，解决什么问题>。
> 本文件由 <project-init-context skill> 基于代码现状自动生成，供人类开发者与 AI Agent 共享。

## 1. 技术栈
- 前端：<框架> <版本>，构建工具 <Vite/Webpack>，UI 库 <xxx>，状态管理 <xxx>
- 后端：<语言> <框架> <版本>
- 数据库：<MySQL/PostgreSQL/MongoDB> <版本>，ORM <xxx>
- 缓存/消息队列：<Redis/RabbitMQ/Kafka>
- 工具链：包管理器 <pnpm/yarn/npm/maven/poetry>，Node <版本>，JDK <版本>

## 2. 项目结构
<用树形或表格说明顶层目录职责>
- `src/`：xxx
- `apps/web`：xxx（monorepo 时标注子包）

## 3. 常用命令（注意执行目录）
- 安装依赖：`cd <dir> && <cmd>`
- 本地开发：`cd <dir> && <cmd>`
- 构建：`<cmd>`
- 测试：`<cmd>`（单元）/ `<cmd>`（E2E）
- Lint / 格式化：`<cmd>`
- Docker 启动：`docker-compose up -d`

## 4. 架构与代码组织
- 分层：<controller → service → repository> / <DDD 四层>
- 入口文件：<路径>
- 状态管理：<方案>
- 路径别名：`@/` → `src/`（举例）
- 端到端示例链路：<前端按钮 → /api/xxx → XxxService → 数据库表>

## 5. 编码规范
- Lint/格式化：<ESLint + Prettier，规则集 xxx>
- 缩进/引号/分号：<2 空格 / 单引号 / 无分号>
- 命名约定：<文件 kebab-case，组件 PascalCase>
- 提交规范：<Conventional Commits，husky + commitlint>
- 类型：<TypeScript strict>

## 6. 数据与 API
- 数据库核心实体：<User / Order / ...>
- ORM：<Prisma / TypeORM / JPA>，迁移命令 `<cmd>`
- API 风格：<REST / GraphQL>，路由注册于 <文件>
- 鉴权：<JWT / Session / OAuth2>，实现于 <文件>
- 接口文档：<Swagger 地址 / docs/api.md>

## 7. 环境变量（必需项）
| 变量 | 说明 | 示例 |
|------|------|------|
| DATABASE_URL | 数据库连接 | mysql://user:pass@host:3306/db |
| JWT_SECRET | 鉴权密钥 | （必填，自行生成）|

## 8. 部署
- 容器化：<Dockerfile + docker-compose.yml>
- CI/CD：<GitHub Actions，push 到 main 触发构建部署>
- 目标环境：<Vercel / K8s / VPS>

## 9. 业务领域（简要）
- 核心实体：<...>
- 关键流程：<注册 → 下单 → 支付>
- 权限模型：<RBAC，角色：admin/user>

## 10. AI 协作约定（写给 AI Agent）
- 新增 API：先加 DTO 校验，再写 service，最后注册路由。
- 禁止直接修改 `migrations/` 已有迁移文件，只新增。
- 前端 import 必须用 `@/` 别名。
- 测试：新增逻辑需补 `*.test.ts`，保持覆盖率 ≥ <阈值>。
- 提交信息遵循 Conventional Commits。
- 不确定处先问，不要臆造不存在的 API / 配置。

## 11. 待确认项（基于假设，需人工核实）
- 假设数据库为 <xxx>，未见明确配置，请核实。
- `scripts.xxx` 含义不明，已据惯例推断为 <xxx>。
```

---

## 六、质量校验清单（生成文档后必须逐项自检）

> 生成项目上下文文档后，**必须**对照本清单逐项核验，未通过的不得交付。

1. **[ ] 零幻觉**：所有技术栈、版本、命令均可追溯到具体文件；无文件支撑的已标注为"假设"。
2. **[ ] 命令可执行**：每条命令标注了执行目录与包管理器；安装/开发/构建/测试/Lint 均已覆盖。
3. **[ ] 入口明确**：前端与后端的入口文件路径准确。
4. **[ ] 路径别名**：若有 `paths` / `resolve.alias`，已记录（影响 AI 生成 import）。
5. **[ ] 环境变量**：必需项已列出，敏感项已标注，前端变量前缀正确。
6. **[ ] 测试命令**：测试框架与运行命令准确，测试文件位置已说明。
7. **[ ] 鉴权与 API**：鉴权方式与实现位置准确，API 风格与路由注册位置已说明。
8. **[ ] 部署链路**：Docker / CI/CD 的触发条件与产物已说明。
9. **[ ] AI 约定**：已给出具体的、可操作的 AI 协作规则（非空话套话）。
10. **[ ] 待确认项**：所有不确定处均已显式列出，请用户核实。
11. **[ ] 路径准确**：引用的目录 / 文件路径与实际结构一致（用 LS/Glob 复核）。
12. **[ ] 简洁有效**：无冗余废话，每条信息对"AI 后续开发"有实际增益。

---

## 七、常见陷阱与防御性检查

> 这些是生成项目上下文**最易翻车**的点，分析时主动规避。

1. **版本臆造**：未读版本约束文件就报版本号。**必须**读 `.nvmrc` / `engines` / `pom.xml` / `requires-python` / `go.mod` 后再写版本。
2. **命令目录错误**：monorepo 命令必须在特定目录执行。**必须**标注 `cd`，并用锁文件判定包管理器，不可混用。
3. **遗漏路径别名**：AI 不知别名会写出全路径 import，污染代码。**必须**读 `tsconfig.paths` / `vite alias`。
4. **前端环境变量前缀错**：`VITE_` / `NEXT_PUBLIC_` 前缀缺失则运行时取不到值。**必须**核对框架前缀。
5. **把"启动步骤"当全部**：本 Skill 产物是**长期上下文**，不只是启动命令；架构、约定、领域逻辑同样重要。
6. **忽略 README / CONTRIBUTING**：作者自己写的约定权重最高，跳过会丢失关键信息（甚至与作者意图冲突）。
7. **过度展开目录树**：把全部文件列出来既冗长又没用。抓主干（2~3 层 + 职责说明）即可。
8. **把假设当事实**：未见配置就用"惯例默认值"却不标注，误导后续开发。**一律标"待确认"**。
9. **忽略已有文档**：`docs/` / `ARCHITECTURE.md` / `CLAUDE.md`（已存在）若存在，应**优先采纳并校验**，而非另起炉灶。
10. **AI 约定写成空话**：如"写高质量代码""注意性能"毫无可操作性。**必须**给具体、可判定、与本项目挂钩的规则。
11. **测试命令遗漏分级**：只给 `test` 不区分单元 / E2E。**应**分层列出。
12. **入口文件猜错**：直接默认 `src/main.ts`，实际可能是 `server/index.ts` / `apps/api/src/main.ts`。**必须**用 Glob 验证。

---

## 八、执行约束（给大模型的硬性要求）

1. **只读不写**：分析全程仅读取项目文件，**绝不修改**任何项目文件。最终仅产出上下文文档（默认写到项目根 `CLAUDE.md`，或用户指定路径）。
2. **证据链完整**：每条结论可在文件中找到出处；无法确认的归入"待确认项"。
3. **遵循推荐顺序**：按维度一→十一推进，保证由表及里、依赖前置。
4. **优先采纳项目自有文档**：README / CONTRIBUTING / docs / 已有的上下文文件是第一手事实，存在则以之为准并校验时效。
5. **覆盖全维度**：不得因"看起来简单"跳过维度；无相关内容的维度写"本项目无 / 未检测到"。
6. **输出即模板**：严格按第五节模板输出，章节齐全，字段具体（禁止占位符残留）。
7. **交付前自检**：输出文档前，对照第六节质量校验清单逐项核验，未通过必返工。
8. **显式声明假设**：所有推断标"待确认"，请用户核实，绝不静默猜测后给出可能错误的信息。
9. **适配用户工具**：若用户指定目标文件名（`.cursorrules` / `AGENTS.md` / `copilot-instructions.md`），按其格式偏好调整（如 `.cursorrules` 偏好简洁指令体，`CLAUDE.md` 偏好结构化 Markdown）。
10. **诚实标注能力边界**：若某维度因信息不足无法分析（如代码被压缩、仓库不完整），明确告知用户而非编造。
