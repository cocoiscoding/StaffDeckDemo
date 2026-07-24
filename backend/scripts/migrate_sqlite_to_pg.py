"""SQLite → PostgreSQL 一次性数据迁移脚本。

用途
=====
    将 backend/skill_agent_loop.db（SQLite）中的全部业务数据导入到当前配置的
    PostgreSQL 数据库（由 app.config.Settings.database_url 指定）。

前置条件
=========
    1. PostgreSQL 中已通过 ``init_db()`` 创建了全部 35 张表（见 database.py）。
    2. PostgreSQL 目标库应为空（或至少相关表为空），避免主键冲突。
       本脚本会在导入前 TRUNCATE 每张表（含级联），保证可重复执行。

使用方法
=========
    在 backend 目录下执行：

        # 默认 dry-run（只统计不写入）
        .venv\\Scripts\\python.exe scripts\\migrate_sqlite_to_pg.py

        # 确认无误后正式执行
        .venv\\Scripts\\python.exe scripts\\migrate_sqlite_to_pg.py --apply

        # 自定义 SQLite 源库路径
        .venv\\Scripts\\python.exe scripts\\migrate_sqlite_to_pg.py --sqlite path/to/source.db --apply

类型转换策略
=============
    SQLite 是动态类型数据库，部分类型与 PostgreSQL 存在差异，脚本会按列类型
    做显式转换：
      - Boolean：SQLite 存 0/1 → Python bool
      - DateTime：SQLite 存 ISO 字符串 → Python datetime
      - JSON（含 JSON/JSONB）：SQLite 存 TEXT → 解析为 Python dict/list
      - 其他类型（str/int/float）：原样透传

外键说明
=========
    SQLModel 模型未声明 SQLAlchemy ForeignKey 约束（仅用字符串字段表示逻辑关联），
    因此 PG 表中没有物理外键，迁移顺序不影响。脚本按表名字母序处理。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# 确保能 import app.* —— 脚本可能从 backend/ 目录运行
BACKEND_DIR = Path(__file__).resolve().parent.parent
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import Table, inspect, text  # noqa: E402
from sqlalchemy.types import JSON, Boolean, DateTime  # noqa: E402
from sqlmodel import Session  # noqa: E402

from app.db.database import engine  # noqa: E402
from app.db.models import *  # noqa: F401,F403  触发全部模型注册到 metadata


# 默认 SQLite 源数据库路径（backend/skill_agent_loop.db）
DEFAULT_SQLITE_PATH = BACKEND_DIR / "skill_agent_loop.db"


def coerce_value(value: Any, column_type: Any) -> Any:
    """根据目标列类型将 SQLite 读出的原始值转换为 PG 兼容值。

    Args:
        value: SQLite 原始值（可能是 int/str/bytes/None）。
        column_type: SQLAlchemy 列类型实例（Boolean/DateTime/JSON 等）。

    Returns:
        转换后的 Python 值，可直接交给 SQLAlchemy insert。
    """
    if value is None:
        return None

    # 布尔：SQLite 用 0/1 整数存储，PG 需要 Python bool
    if isinstance(column_type, Boolean):
        return bool(value)

    # 日期时间：SQLite 存 ISO 字符串，PG 需要 datetime 对象
    if isinstance(column_type, DateTime):
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            # 兼容带/不带时区、带 'T' / 空格 分隔的多种 ISO 写法
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                # 极少数历史脏数据：保留原值让 PG 报错，便于人工排查
                return value
        return value

    # JSON：SQLite 存 TEXT，需解析为 Python dict/list
    if isinstance(column_type, JSON):
        if isinstance(value, str):
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                # 解析失败回退为空 dict，避免整行失败
                return {}
        # 已是 dict/list（sqlite3 启用 json1 扩展时可能出现）直接返回
        return value

    # 其他类型原样返回
    return value


def fetch_sqlite_rows(sqlite_path: Path, table_name: str) -> list[dict[str, Any]]:
    """从 SQLite 读取指定表的全部行，以字段名为键的字典列表返回。

    Args:
        sqlite_path: SQLite 数据库文件路径。
        table_name: 表名。

    Returns:
        list[dict]: 行数据列表（字段名 → 值）。
    """
    conn = sqlite3.connect(str(sqlite_path))
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(f"SELECT * FROM {table_name}")
        return [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()


def truncate_target_table(session: Session, table: Table) -> None:
    """清空 PG 目标表，保证脚本可重复执行。

    使用 TRUNCATE ... CASCADE 以避免外键依赖（尽管本项目无物理外键，
    CASCADE 仍是安全的兜底）。

    Args:
        session: SQLModel 会话。
        table: SQLAlchemy Table 对象。
    """
    session.execute(text(f'TRUNCATE TABLE "{table.name}" RESTART IDENTITY CASCADE'))


def migrate_table(
    session: Session,
    table: Table,
    sqlite_rows: list[dict[str, Any]],
    apply: bool,
) -> int:
    """把 SQLite 行数据转换后批量插入 PG 目标表。

    Args:
        session: SQLModel 会话。
        table: 目标 SQLAlchemy Table 对象（含列类型信息）。
        sqlite_rows: SQLite 读出的原始行字典列表。
        apply: 是否真正写入；False 仅返回待插入行数。

    Returns:
        实际（或计划）插入的行数。
    """
    if not sqlite_rows:
        return 0

    # 预先收集每列的类型实例，用于 coerce_value 的类型分支判断
    column_types = {col.name: col.type for col in table.columns}

    # SQLite 行字段可能比 PG 列少（历史演进新增列）或多（被删除列），按 PG 列为准过滤
    pg_column_names = set(column_types.keys())
    converted_rows: list[dict[str, Any]] = []
    for raw in sqlite_rows:
        row: dict[str, Any] = {}
        for col_name, col_type in column_types.items():
            if col_name in raw:
                row[col_name] = coerce_value(raw[col_name], col_type)
            else:
                row[col_name] = None
        converted_rows.append(row)

    if apply:
        session.execute(table.insert(), converted_rows)

    return len(converted_rows)


def main() -> int:
    """脚本主入口：解析参数 → 连接源/目标 → 逐表迁移。

    Returns:
        进程退出码（0 成功，非 0 失败）。
    """
    parser = argparse.ArgumentParser(description="SQLite → PostgreSQL 数据迁移")
    parser.add_argument(
        "--sqlite",
        type=Path,
        default=DEFAULT_SQLITE_PATH,
        help=f"SQLite 源数据库路径（默认：{DEFAULT_SQLITE_PATH}）",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正执行写入；不带此参数仅 dry-run（只统计不写入）",
    )
    args = parser.parse_args()

    sqlite_path: Path = args.sqlite
    apply: bool = args.apply

    # 1. 检查源 SQLite 文件存在
    if not sqlite_path.exists():
        print(f"[ERROR] SQLite 源库不存在：{sqlite_path}", file=sys.stderr)
        return 2

    print(f"[INFO] 模式：{'APPLY（写入）' if apply else 'DRY-RUN（只统计）'}")
    print(f"[INFO] SQLite 源：{sqlite_path}")
    print(f"[INFO] PG 目标：{engine.url}")
    print()

    # 2. 从 SQLAlchemy metadata 获取所有已注册的表（按名字排序）
    inspector = inspect(engine)
    pg_tables = set(inspector.get_table_names())
    total_rows = 0

    with Session(engine) as session:
        for table_name in sorted(engine.dialect.get_table_names(engine.connect())):
            pass
        # 上面循环只为演示，实际直接遍历 metadata
        metadata_tables = sorted(engine.dialect.get_table_names(engine.connect()))

    # 直接使用 SQLModel.metadata 的表（与 init_db 建表来源一致）
    sorted_tables = sorted(
        (name, table) for name, table in __import__("sqlmodel").SQLModel.metadata.tables.items()
    )

    print(f"[INFO] 待迁移表数：{len(sorted_tables)}")
    print()

    with Session(engine) as session:
        for table_name, table in sorted_tables:
            # 跳过 SQLAlchemy/SQLModel 内部表（若有）
            if table_name not in pg_tables:
                print(f"[SKIP] {table_name}（PG 中不存在，跳过）")
                continue

            # SQLite 端读源数据
            try:
                sqlite_rows = fetch_sqlite_rows(sqlite_path, table_name)
            except sqlite3.OperationalError as exc:
                print(f"[SKIP] {table_name}（SQLite 中不存在：{exc}）")
                continue

            # PG 端清空 + 插入
            if apply and sqlite_rows:
                truncate_target_table(session, table)

            inserted = migrate_table(session, table, sqlite_rows, apply)
            total_rows += inserted
            print(f"[{'OK' if apply and sqlite_rows else 'PLAN'}] {table_name:<40} {inserted:>6} rows")

        if apply:
            session.commit()

    print()
    print(f"[DONE] 总计 {'写入' if apply else '计划写入'} {total_rows} 行")
    if not apply:
        print("[HINT] 确认无误后加上 --apply 参数正式执行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
