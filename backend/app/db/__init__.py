"""数据库基础设施包。

负责数据库引擎创建、会话管理与表结构初始化，是整个后端持久层的统一入口。

导出对象:
    engine: SQLAlchemy/SQLModel 数据库引擎实例。
    get_session: 依赖注入函数，用于获取数据库会话。
    init_db: 初始化数据库表结构。
"""

from app.db.database import engine, get_session, init_db

__all__ = ["engine", "get_session", "init_db"]
