"""FastAPI 应用入口模块。

本模块创建并配置 FastAPI 应用实例，注册所有路由模块、CORS 中间件，
以及应用启动/关闭时的生命周期钩子。

核心概念
---------
- **应用生命周期**：
    - 启动时初始化数据库表结构、填充演示数据、启动后台调度工作器；
    - 关闭时停止后台调度工作器、关闭异步任务线程池。
- **路由注册**：所有业务路由模块通过 ``app.include_router`` 挂载到
  主应用上，每个模块负责自己的 URL 前缀和依赖注入。
- **安全配置**：关闭了内置的 Swagger / ReDoc / OpenAPI 文档端点
  （``docs_url=None`` 等），生产环境不暴露 API 文档。

与其他模块的关系
-----------------
- 依赖 :mod:`app.config` 获取全局配置；
- 依赖 :mod:`app.db` 进行数据库初始化和演示数据填充；
- 依赖 :mod:`app.async_jobs` 管理后台任务线程池；
- 依赖 :mod:`app.scheduled_tasks.worker` 管理定时任务后台工作器；
- 聚合 :mod:`app.api` 下的所有路由模块。
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, SQLModel
from sqlalchemy import inspect

from app.api import (
    agents,
    auth,
    chat,
    feedback,
    general_skills,
    knowledge,
    knowledge_bases,
    memories,
    mock,
    model_configs,
    persona,
    scheduled_tasks,
    sessions,
    skills,
    tools,
    traces,
    ui_config,
)
from app.async_jobs import shutdown_async_jobs
from app.config import get_settings
from app.db import engine, init_db
from app.db.seed import seed_demo_data
from app.scheduled_tasks.worker import start_background_worker, stop_background_worker

# 加载全局配置（单例模式），用于配置 FastAPI 实例和 CORS 中间件
settings = get_settings()

# 创建 FastAPI 应用实例
app = FastAPI(
    title=settings.app_name,
    version="0.1.0",
    docs_url=None,  # 禁用 Swagger UI 文档端点
    redoc_url=None,  # 禁用 ReDoc 文档端点
    openapi_url=None,  # 禁用 OpenAPI JSON 端点
)

# 配置 CORS 中间件，允许前端跨域访问
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,  # 从配置读取允许的前端来源列表
    allow_credentials=True,  # 允许携带 Cookie
    allow_methods=["*"],  # 允许所有 HTTP 方法
    allow_headers=["*"],  # 允许所有请求头
)


@app.on_event("startup")
def on_startup() -> None:
    """应用启动钩子。
    
    执行以下初始化流程：
      1. 初始化数据库表结构（``init_db``）；
      2. 填充演示种子数据（``seed_demo_data``）；
      3. 启动定时任务后台工作器（``start_background_worker``）。
    """
    # 原代码为每次启动应用时都执行初始化数据库，但此操作是幂等的，已创建的表不会再重建或修改。
    # init_db()  # 创建数据库表（如果不存在）

    if settings.auto_init_db:
        existing = set(inspect(engine).get_table_names())
        required = set(SQLModel.metadata.tables.keys())
        if not required.issubset(existing):
            init_db()  # 仅在缺表时才建
        
        with Session(engine) as db:
            seed_demo_data(db)  # 填充演示数据（仅首次启动时写入）

    start_background_worker()  # 启动定时任务调度工作线程


@app.on_event("shutdown")
def on_shutdown() -> None:
    """应用关闭钩子。

    执行以下清理流程：
      1. 停止定时任务后台工作器（``stop_background_worker``）；
      2. 关闭异步任务线程池（``shutdown_async_jobs``）。
    """
    stop_background_worker()  # 停止定时任务后台工作器（
    shutdown_async_jobs()  # 关闭异步任务线程池（


@app.get("/api/health", tags=["health"])
def health() -> dict[str, str]:
    # 健康检查端点
    return {
        "status": "ok", 
        "app": settings.app_name
    }


# --- 注册所有业务路由模块 ---

# 对话与聊天相关路由
app.include_router(chat.router)
app.include_router(agents.chat_router)
app.include_router(ui_config.chat_router)

# 认证路由
app.include_router(auth.router)

# Agent（数字员工）管理路由
app.include_router(agents.scope_router)
app.include_router(agents.enterprise_router)

# 技能与知识库路由
app.include_router(general_skills.router)
app.include_router(knowledge_bases.router)
app.include_router(knowledge.router)
app.include_router(skills.router)

# 模型配置路由
app.include_router(model_configs.router)

# 记忆与反馈路由
app.include_router(memories.router)
app.include_router(feedback.router)

# 人设路由
app.include_router(persona.router)

# 定时任务路由（企业管理 / 聊天 / 聊天草稿三个子路由）
app.include_router(scheduled_tasks.enterprise_router)
app.include_router(scheduled_tasks.chat_router)
app.include_router(scheduled_tasks.chat_draft_router)

# UI 配置企业管理路由
app.include_router(ui_config.enterprise_router)

# 工具与 MCP 路由
app.include_router(tools.router)
app.include_router(tools.mcp_router)

# 会话与链路追踪路由
app.include_router(sessions.router)
app.include_router(traces.router)

# 内部 Mock 服务路由
app.include_router(mock.router)
