#!/usr/bin/env python3
"""初始化 KingbaseES：建表 + 填充演示数据。

等价于后端正常启动时 main.py 的 startup 钩子做的事情：
  1. init_db() - 建出全部 35 张表
  2. seed_demo_data() - 写入演示租户、用户、技能、工具等
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    print("=" * 60)
    print("初始化 KingbaseES：建表 + 填充演示数据")
    print("=" * 60)

    # 1. 建表
    print("\n[1/3] 建表（init_db）...")
    from app.db.database import init_db
    init_db()
    print("  ✅ 35 张表已建出")

    # 2. 填充演示数据
    print("\n[2/3] 填充演示数据（seed_demo_data）...")
    from app.db.database import engine
    from app.db.seed import seed_demo_data
    from sqlmodel import Session
    with Session(engine) as db:
        seed_demo_data(db)
    print("  ✅ 演示数据已写入")

    # 3. 验证结果
    print("\n[3/3] 验证结果...")
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    tables = inspector.get_table_names()
    print(f"  表数量: {len(tables)}")

    with engine.connect() as conn:
        tenant_count = conn.execute(text("SELECT count(*) FROM tenants")).scalar()
        user_count = conn.execute(text("SELECT count(*) FROM users")).scalar()
        skill_count = conn.execute(text("SELECT count(*) FROM skills")).scalar()
        tool_count = conn.execute(text("SELECT count(*) FROM tools")).scalar()
        agent_count = conn.execute(text("SELECT count(*) FROM agent_profiles")).scalar()
    print(f"  租户数:   {tenant_count}")
    print(f"  用户数:   {user_count}")
    print(f"  技能数:   {skill_count}")
    print(f"  工具数:   {tool_count}")
    print(f"  员工数:   {agent_count}")

    print("\n" + "=" * 60)
    print("🎉 初始化完成，可在 DataGrip 刷新查看数据")
    print("=" * 60)
    print("\n初始账号（可用这些登录前端）:")
    print("  管理员: admin / admin")
    print("  演示用户: user_demo / user_demo")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
