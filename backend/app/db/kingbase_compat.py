"""KingbaseES（人大金仓）PG 兼容模式适配层。

本模块为 KingbaseES 数据库提供与 SQLAlchemy 的兼容性补丁。

背景
----
KingbaseES 基于 PostgreSQL 内核深度定制，在 ``database_mode = pg`` 下
可被 psycopg3 驱动直连。但其返回的版本字符串形如 ``KingbaseES V009R001C010``，
不符合 SQLAlchemy PG dialect 期望的 ``PostgreSQL 16.0`` 格式，导致
``_get_server_version_info`` 解析失败抛出::

    Could not determine version from string 'KingbaseES V009R001C010'

解决方案
--------
对 ``PGDialect._get_server_version_info`` 做一层 try-except 包装：
原生解析成功则沿用，失败则回退到虚拟的 PG 16.0 版本。

安全性
--------
- **对原生 PostgreSQL 用户零影响**：仅在原生解析抛异常时才回退。
- **幂等**：多次 import 只会包装一次（通过 ``_PATCHED`` 标志位保证）。
- **可移除**：未来 SQLAlchemy 原生支持 KingbaseES 版本字符串后，
  删除本文件并移除 ``database.py`` 中的 import 即可。

使用方式
--------
在 ``app/db/database.py`` 顶部添加::

    from app.db import kingbase_compat  # noqa: F401  KingbaseES 兼容
"""

from __future__ import annotations

# 幂等标志：防止重复 patch
_PATCHED = False


def apply_kingbase_compat() -> None:
    """对 SQLAlchemy PG dialect 应用 KingbaseES 版本解析兼容补丁。

    该函数是幂等的，多次调用只会在首次生效。
    """
    global _PATCHED
    if _PATCHED:
        return

    from sqlalchemy.dialects.postgresql.base import PGDialect

    original = PGDialect._get_server_version_info

    # 核心逻辑：原生解析成功，返回原结果；失败，退回 PG 16.0版本
    def patched(self, connection):
        try:
            return original(self, connection)
        except Exception:
            # KingbaseES 版本字符串解析失败时，回退到虚拟的 PG 16.0
            # 该版本号影响 dialect 的部分 SQL 生成行为，16.0 足够新可启用全部特性
            return (16, 0, 0)

    PGDialect._get_server_version_info = patched
    _PATCHED = True


# 模块导入时自动应用，调用方只需 ``from app.db import kingbase_compat``
apply_kingbase_compat()
