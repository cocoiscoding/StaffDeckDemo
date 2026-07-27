"""把原 PostgreSQL 容器里的 model_configs 迁移到 KingbaseES。

从 staffdeck-postgres（5432）读取全部模型配置，写入 kingbase-pg（54321）。
API Key 用加密形式存储，直接原样复制密文即可（只要 APP_SECRET 一致就能解密）。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text


def main() -> int:
    print("=" * 60)
    print("迁移 model_configs：PostgreSQL (5432) → KingbaseES (54321)")
    print("=" * 60)

    # 1. 连接两个数据库
    src_url = "postgresql+psycopg://staffdeck:staffdeck123@localhost:5432/staffdeck"
    dst_url = "postgresql+psycopg://staffdeck:staffdeck123@localhost:54321/staffdeck"

    # 对 KingbaseES 应用版本解析 patch
    from sqlalchemy.dialects.postgresql.base import PGDialect
    _orig = PGDialect._get_server_version_info
    def _patched(self, conn):
        try: return _orig(self, conn)
        except Exception: return (16, 0, 0)
    PGDialect._get_server_version_info = _patched

    src_engine = create_engine(src_url)
    dst_engine = create_engine(dst_url)

    # 2. 从源库读取全部模型配置
    print("\n[1/3] 读取源库（PostgreSQL 5432）的模型配置...")
    with src_engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT * FROM model_configs ORDER BY created_at"
        )).fetchall()
    print(f"  读取到 {len(rows)} 条模型配置")

    if not rows:
        print("  源库无模型配置，无需迁移")
        return 0

    # 3. 检查目标库是否已有同 id 记录
    print("\n[2/3] 检查目标库（KingbaseES 54321）是否已有数据...")
    with dst_engine.connect() as conn:
        existing_ids = {r[0] for r in conn.execute(text("SELECT id FROM model_configs")).fetchall()}
    print(f"  目标库已有 {len(existing_ids)} 条记录")

    # 4. 写入目标库
    print(f"\n[3/3] 迁移到 KingbaseES...")
    # 获取所有列名
    cols = [c for c in rows[0]._mapping.keys()]
    placeholders = ",".join(f":{c}" for c in cols)
    cols_str = ",".join(cols)
    insert_sql = text(f"INSERT INTO model_configs ({cols_str}) VALUES ({placeholders})")

    # JSON 列需要特殊处理（psycopg3 不能直接传 dict，用 Json 包装）
    from psycopg.types.json import Json
    json_cols = {"extra_body_json", "protocol_options_json", "legacy_unmapped_options_json"}

    inserted = 0
    skipped = 0
    with dst_engine.begin() as conn:
        for row in rows:
            row_id = row._mapping["id"]
            if row_id in existing_ids:
                print(f"  ⏭️  跳过 {row_id}（已存在）")
                skipped += 1
                continue
            data = {}
            for c in cols:
                val = row._mapping[c]
                if c in json_cols and val is not None:
                    val = Json(val)
                data[c] = val
            conn.execute(insert_sql, data)
            print(f"  ✅ 插入 {row_id} ({row._mapping['name']} → {row._mapping['model']})")
            inserted += 1

    # 5. 验证
    print("\n" + "=" * 60)
    with dst_engine.connect() as conn:
        total = conn.execute(text("SELECT count(*) FROM model_configs")).scalar()
        for r in conn.execute(text(
            "SELECT id, name, base_url, model, enabled FROM model_configs ORDER BY created_at"
        )).fetchall():
            print(f"  KingbaseES: {r[1]} | {r[3]} | base_url={r[2]} | enabled={r[4]}")

    print(f"\n🎉 迁移完成：插入 {inserted} 条，跳过 {skipped} 条，KingbaseES 共 {total} 条模型配置")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
