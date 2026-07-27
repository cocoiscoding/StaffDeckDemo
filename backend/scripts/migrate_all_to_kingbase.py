"""把 PostgreSQL (5432) 的全部缺失数据迁移到 KingbaseES (54321)。

迁移策略：
  1. 按依赖顺序处理（先无外键依赖的表，后有依赖的）
  2. 跳过目标库已存在的 id（幂等）
  3. JSON 列用 psycopg Json 包装，避免 dict 无法序列化
  4. 迁移后做一次完整对比验证
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text
from psycopg.types.json import Json


def patch_kingbase_compat():
    """让 SQLAlchemy 能识别 KingbaseES 版本字符串。"""
    from sqlalchemy.dialects.postgresql.base import PGDialect
    _orig = PGDialect._get_server_version_info
    def _patched(self, conn):
        try: return _orig(self, conn)
        except Exception: return (16, 0, 0)
    PGDialect._get_server_version_info = _patched


def get_table_columns(engine, table_name):
    """获取表的全部列名。"""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :t AND table_schema = 'public' "
            "ORDER BY ordinal_position"
        ), {"t": table_name}).fetchall()
    return [r[0] for r in rows]


def get_json_columns(engine, table_name):
    """识别 JSON/JSONB 列。"""
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :t AND table_schema = 'public' "
            "AND data_type IN ('json', 'jsonb')"
        ), {"t": table_name}).fetchall()
    return {r[0] for r in rows}


def migrate_table(src_engine, dst_engine, table_name, verbose=True):
    """迁移单张表（跳过已存在的 id）。"""
    cols = get_table_columns(src_engine, table_name)
    json_cols = get_json_columns(src_engine, table_name)

    # 读取源库全部数据
    with src_engine.connect() as conn:
        rows = conn.execute(text(f'SELECT * FROM "{table_name}"')).fetchall()

    if not rows:
        if verbose:
            print(f"  ⏭️  {table_name:<35} 源库无数据，跳过")
        return 0, 0

    # 目标库已有 id（用于跳过）
    with dst_engine.connect() as conn:
        existing = {r[0] for r in conn.execute(text(f'SELECT id FROM "{table_name}"')).fetchall()}

    cols_str = ",".join(f'"{c}"' for c in cols)
    placeholders = ",".join(f":{c}" for c in cols)
    insert_sql = text(f'INSERT INTO "{table_name}" ({cols_str}) VALUES ({placeholders})')

    inserted = 0
    skipped = 0
    with dst_engine.begin() as conn:
        for row in rows:
            row_id = row._mapping["id"]
            if row_id in existing:
                skipped += 1
                continue
            data = {}
            for c in cols:
                val = row._mapping[c]
                if c in json_cols and val is not None:
                    val = Json(val)
                data[c] = val
            conn.execute(insert_sql, data)
            inserted += 1

    if verbose:
        print(f"  {'✅' if inserted else '⏭️'} {table_name:<35} 插入 {inserted} 行，跳过 {skipped} 行")
    return inserted, skipped


def main() -> int:
    patch_kingbase_compat()

    src_url = "postgresql+psycopg://staffdeck:staffdeck123@localhost:5432/staffdeck"
    dst_url = "postgresql+psycopg://staffdeck:staffdeck123@localhost:54321/staffdeck"
    src_engine = create_engine(src_url)
    dst_engine = create_engine(dst_url)

    print("=" * 70)
    print("全量数据迁移：PostgreSQL (5432) → KingbaseES (54321)")
    print("=" * 70)

    # 按依赖顺序迁移全部 35 张表（已存在的会跳过）
    # 顺序：租户/用户 → 资源（技能/知识/工具/模型） → 员工 → 绑定关系 → 会话/消息/事件
    tables_ordered = [
        # 基础
        "tenants", "users",
        # 资源
        "skills", "skill_versions",
        "general_skills",
        "knowledge_bases", "knowledge_base_versions",
        "knowledge_documents", "knowledge_buckets", "knowledge_chunks",
        "knowledge_concepts", "knowledge_discovery_suggestions",
        "knowledge_ingest_jobs",
        "model_configs", "persona_configs", "ui_configs",
        "tools", "mcp_servers", "mock_orders",
        # 员工与绑定
        "agent_profiles", "agent_usages",
        "agent_model_bindings", "agent_resource_bindings",
        "agent_skill_branches", "agent_skill_branch_versions",
        "agent_knowledge_branches",
        # 会话与运行时
        "sessions", "messages", "agent_events",
        "message_feedback", "skill_feedback",
        "human_handoff_requests",
        "scheduled_tasks", "scheduled_task_runs",
        "memories",
    ]

    print("\n[迁移开始]")
    total_inserted = 0
    total_skipped = 0
    for table in tables_ordered:
        try:
            ins, skip = migrate_table(src_engine, dst_engine, table)
            total_inserted += ins
            total_skipped += skip
        except Exception as exc:
            print(f"  ❌ {table:<35} 迁移失败: {exc}")

    print(f"\n{'=' * 70}")
    print(f"迁移完成：共插入 {total_inserted} 行，跳过 {total_skipped} 行（已存在）")

    # 最终验证
    print(f"\n[最终对比验证]")
    with src_engine.connect() as conn:
        src_tables = sorted([r[0] for r in conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE'"
        )).fetchall()])

    print(f"\n{'表名':<35} {'PG(5432)':>10} {'KES(54321)':>12} {'差异':>8}")
    print("-" * 67)

    all_match = True
    for t in src_tables:
        with src_engine.connect() as conn:
            s = conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar()
        with dst_engine.connect() as conn:
            d = conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar()
        diff = d - s
        marker = " ✅" if diff == 0 else f" ❌ ({diff:+d})"
        if diff != 0:
            all_match = False
        print(f"{t:<35} {s:>10} {d:>12} {diff:>+6}{marker}")

    print(f"\n{'=' * 70}")
    if all_match:
        print("🎉 全部 35 张表数据完全一致")
    else:
        print("⚠️  仍有部分表数据不一致，请检查上面的标记")
    print("=" * 70)

    return 0 if all_match else 1


if __name__ == "__main__":
    raise SystemExit(main())
