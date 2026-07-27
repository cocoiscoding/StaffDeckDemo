#!/usr/bin/env python3
"""KingbaseES 兼容性验证脚本。

验证 StaffDeck 后端能否连接 KingbaseES (PG 兼容模式) 并正常建表。
执行方式：
    cd backend
    .venv\Scripts\python.exe scripts\test_kingbase_compat.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# 让脚本能 import app 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _patch_sqlalchemy_version_parsing() -> None:
    """Monkey-patch SQLAlchemy 的 PostgreSQL dialect 版本解析。

    KingbaseES 返回的版本字符串形如 'KingbaseES V009R001C010'，
    而 SQLAlchemy 的 PG dialect 期望 PostgreSQL 风格（如 'PostgreSQL 16.0'）。
    这里在解析失败时回退到一个虚拟的 PG 16.0 版本，让 dialect 正常工作。
    """
    from sqlalchemy.dialects.postgresql.base import PGDialect

    original = PGDialect._get_server_version_info

    def patched(self, connection):
        try:
            return original(self, connection)
        except Exception:
            # KingbaseES 解析失败时，回退到 PG 16.0
            return (16, 0, 0)

    PGDialect._get_server_version_info = patched


def main() -> int:
    print("=" * 60)
    print("KingbaseES PG 兼容模式 - 兼容性验证")
    print("=" * 60)

    # 0. 预处理：patch SQLAlchemy 版本解析
    _patch_sqlalchemy_version_parsing()
    print("[0/5] 已 patch SQLAlchemy 版本解析（回退到 PG 16.0）")

    # 1. 读取配置，确认连接目标
    from app.config import get_settings
    settings = get_settings()
    print(f"\n[1/5] 配置检查")
    print(f"  DATABASE_URL = {settings.database_url}")
    print(f"  AUTO_INIT_DB = {settings.auto_init_db}")

    # 2. 测试连接
    print(f"\n[2/5] 测试数据库连接...")
    from sqlalchemy import create_engine, text
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as conn:
            version = conn.execute(text("SELECT version();")).scalar()
            db_mode = conn.execute(text("SHOW database_mode;")).scalar()
            db_name = conn.execute(text("SELECT current_database();")).scalar()
            current_user = conn.execute(text("SELECT current_user;")).scalar()
        print(f"  ✅ 连接成功")
        print(f"  版本       : {version}")
        print(f"  兼容模式   : {db_mode}")
        print(f"  当前数据库 : {db_name}")
        print(f"  当前用户   : {current_user}")
    except Exception as exc:
        print(f"  ❌ 连接失败: {exc}")
        return 1

    # 3. 建表前：确认目标库为空（或只有我们刚建的测试表）
    print(f"\n[3/5] 建表前状态检查...")
    from sqlalchemy import inspect
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    print(f"  现有表数量: {len(existing_tables)}")
    if existing_tables:
        print(f"  现有表: {sorted(existing_tables)[:10]}...")

    # 4. 执行 init_db() - 这是项目的建表逻辑
    print(f"\n[4/5] 执行 init_db() 建表...")
    from app.db.database import init_db
    try:
        init_db()
        print(f"  ✅ init_db() 执行成功")
    except Exception as exc:
        print(f"  ❌ init_db() 失败: {exc}")
        import traceback
        traceback.print_exc()
        return 1

    # 5. 验证建表结果
    print(f"\n[5/5] 建表结果验证...")
    inspector = inspect(engine)
    final_tables = set(inspector.get_table_names())
    new_tables = final_tables - existing_tables

    # 项目应建的 35 张表（从 SQLModel.metadata 读取期望值）
    import app.db.models  # noqa: F401 - 触发模型注册
    from sqlmodel import SQLModel
    expected_tables = set(SQLModel.metadata.tables.keys())

    print(f"  期望表数量: {len(expected_tables)}")
    print(f"  实际表数量: {len(final_tables - existing_tables)}（新建）")

    missing = expected_tables - final_tables
    extra = (final_tables - existing_tables) - expected_tables

    if not missing:
        print(f"  ✅ 全部 {len(expected_tables)} 张期望表均已建出")
    else:
        print(f"  ❌ 缺失 {len(missing)} 张表: {sorted(missing)}")
    if extra:
        print(f"  ℹ️  额外表（非项目定义）: {sorted(extra)}")

    # 抽样检查关键表的列结构（验证 JSON 列类型）
    print(f"\n[抽样] 关键表结构检查...")
    key_tables = ["users", "agent_profiles", "skills", "chat_sessions", "messages"]
    for table_name in key_tables:
        if table_name in final_tables:
            columns = inspector.get_columns(table_name)
            json_cols = [c for c in columns if "json" in str(c.get("type", "")).lower()]
            print(f"  {table_name}: {len(columns)} 列, JSON 列 {len(json_cols)} 个")
        else:
            print(f"  {table_name}: ❌ 不存在")

    print("\n" + "=" * 60)
    if not missing:
        print("🎉 兼容性验证通过：KingbaseES PG 模式可承载 StaffDeck 建表")
        print("=" * 60)
        return 0
    else:
        print("⚠️  兼容性验证存在缺失项，请检查上述输出")
        print("=" * 60)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
