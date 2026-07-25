"""数据库初始化与会话管理模块（PostgreSQL 版）。

本模块负责 StaffDeck 平台的数据库底层管理，职责精简为三部分：

1. 引擎与会话管理
    根据配置（``app.config.get_settings``）构建 SQLAlchemy 引擎，并提供
    ``get_session`` 生成器供 FastAPI 依赖注入使用。
    默认数据库为 PostgreSQL（通过 psycopg3 驱动连接）。

2. 建表初始化
    ``init_db`` 调用 SQLModel 的 ``create_all`` 创建全部模型表（表结构定义见
    ``app.db.models``）。该方式适合首次启动或 schema 未变场景；
    后续 schema 变更建议引入 Alembic 进行规范化迁移管理。

3. 连接池调优
    PostgreSQL 是网络数据库（区别于嵌入式的 SQLite），需要合理配置连接池：
      - ``pool_pre_ping``：每次取连接前做一次轻量探测，避免使用已断开的连接；
      - ``pool_recycle``：连接最大存活时间，防止数据库端或中间网络设备提前关闭；
      - ``pool_size`` / ``max_overflow``：基础连接数与突发溢出数。

历史说明
---------
    原版本针对 SQLite 实现了大量渐进式迁移逻辑（``_migrate_sqlite_skill_schema``
    及一系列辅助函数，约 2000 行），用于在无成熟迁移工具的环境下幂等的
    补齐缺失列、迁移旧数据、规范化技能图谱等。切换到 PostgreSQL 后，这些
    SQLite 方言代码已整体移除。如需保留历史实现，请查阅 git 历史记录。

本模块与 ``models.py``（表结构）、``seed.py``（演示数据）共同构成数据层。
"""

# 1. 标准库：类型注解（Generator 用于 get_session 的返回类型）
from collections.abc import Generator

# 2. 第三方：SQLAlchemy 引擎类型 + 连接池实现
from sqlalchemy import Engine
from sqlalchemy.pool import QueuePool
# 3. 第三方：SQLModel 核心 API（Session 会话 / SQLModel 基类 / create_engine 引擎工厂）
from sqlmodel import Session, SQLModel, create_engine

# 4. 项目内：全局配置单例（提供 database_url 等参数）
from app.config import get_settings


# 1. 加载全局配置（单例），用于读取数据库连接字符串等参数。
settings = get_settings()

# 2. 全局数据库 URL。
#    默认指向本地 Docker 启动的 PostgreSQL（见 app.config.Settings.database_url）。
#    生产环境应通过 backend/.env 的 DATABASE_URL 环境变量覆盖。
database_url: str = settings.database_url

# 3. 创建 SQLAlchemy 引擎。
#    参数说明：
#      - echo=False：关闭 SQL 日志输出（调试时可临时设为 True）；
#      - pool_pre_ping=True：取连接前做一次 ping，避免使用已被服务端关闭的连接，
#        对 PostgreSQL 这类长连接网络数据库尤其重要；
#      - pool_recycle=3600：连接最多复用 1 小时，防止 PG 端 idle 超时或中间网关
#        断连导致的 "server closed the connection unexpectedly"；
#      - pool_size=10 / max_overflow=20：基础 10 个连接，突发可再借 20 个；
#        FastAPI + SQLModel 默认使用线程池，连接数需与并发线程数匹配。
engine: Engine = create_engine(
    database_url,
    echo=False,                # 不打印 SQL（调试时改 True）
    poolclass=QueuePool,       # 使用队列连接池（默认即此类型，显式声明便于阅读）
    pool_pre_ping=True,        # 取连接前 ping 一次
    pool_recycle=3600,         # 连接最大存活 1 小时
    pool_size=10,              # 基础连接数
    max_overflow=20,           # 突发可溢出的连接数
)


# 0. 函数说明：初始化数据库（创建全部缺失的表，幂等）
#    URL：无（启动期基础设施，由 app.main 的 startup 钩子调用）
def init_db() -> None:
    """初始化数据库：创建全部缺失的表。

    流程：
      1. 导入模型模块（触发表元数据注册到 ``SQLModel.metadata``）；
      2. 调用 ``SQLModel.metadata.create_all`` 创建所有尚不存在的表。
         ``create_all`` 是幂等的——已存在的表不会被重建或修改，因此对
         运行中的生产库安全。

    注意：
      - 本函数仅负责"建表"，不包含跨版本 schema 迁移。如需修改已存在表的
        列定义、约束或索引，应使用 Alembic 或等价迁移工具。
      - 应用启动时由 ``app.main`` 的 ``@app.on_event("startup")`` 调用一次。
    """
    # 1. 导入模型模块（仅副作用：让 SQLModel 把所有表类注册到 metadata）
    import app.db.models  # noqa: F401  触发 SQLModel 表元数据注册

    # 2. 执行 CREATE TABLE IF NOT EXISTS（已存在的表不会被重建）
    SQLModel.metadata.create_all(engine)


# 0. 函数说明：FastAPI 依赖：提供一个 Session 上下文并在用完后自动关闭
#    URL：无（依赖注入函数，配合 Depends(get_session) 使用）
def get_session() -> Generator[Session, None, None]:
    """FastAPI 依赖：提供一个 ``Session`` 上下文并在用完后自动关闭。

    典型用法（路由函数签名）：
        def handler(db: Session = Depends(get_session)): ...

    Yields:
        Session: 已绑定到全局引擎的 SQLModel 会话。
    """
    # 1. 用 with 上下文管理 Session（离开 with 块时自动 close）
    with Session(engine) as session:
        # 1.1 yield 把 session 注入到路由函数，路由执行完后回到这里
        yield session
