"""对比 PostgreSQL (5432) 和 KingbaseES (54321) 的数据差异。

逐表对比行数，找出 KingbaseES 缺失的数据。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import create_engine, text


def patch_kingbase_compat():
    from sqlalchemy.dialects.postgresql.base import PGDialect
    _orig = PGDialect._get_server_version_info
    def _patched(self, conn):
        try: return _orig(self, conn)
        except Exception: return (16, 0, 0)
    PGDialect._get_server_version_info = _patched


def count_rows(engine, tables):
    """返回 {表名: 行数} 字典。"""
    result = {}
    with engine.connect() as conn:
        for t in tables:
            try:
                cnt = conn.execute(text(f'SELECT count(*) FROM "{t}"')).scalar()
                result[t] = cnt
            except Exception as e:
                result[t] = f"ERROR: {e}"
    return result


def main() -> int:
    patch_kingbase_compat()

    src_url = "postgresql+psycopg://staffdeck:staffdeck123@localhost:5432/staffdeck"
    dst_url = "postgresql+psycopg://staffdeck:staffdeck123@localhost:54321/staffdeck"

    src_engine = create_engine(src_url)
    dst_engine = create_engine(dst_url)

    # 1. 获取源库所有表
    with src_engine.connect() as conn:
        src_tables = sorted([r[0] for r in conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE'"
        )).fetchall()])

    with dst_engine.connect() as conn:
        dst_tables = sorted([r[0] for r in conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='public' AND table_type='BASE TABLE'"
        )).fetchall()])

    print("=" * 70)
    print("PostgreSQL (5432) vs KingbaseES (54321) 数据对比")
    print("=" * 70)

    print(f"\n源库（PG 5432）表数量: {len(src_tables)}")
    print(f"目标库（KES 54321）表数量: {len(dst_tables)}")

    only_src = set(src_tables) - set(dst_tables)
    only_dst = set(dst_tables) - set(src_tables)
    if only_src:
        print(f"\n⚠️  KingbaseES 缺失的表: {sorted(only_src)}")
    if only_dst:
        print(f"\nℹ️  KingbaseES 多出的表: {sorted(only_dst)}")

    # 2. 逐表对比行数
    common = sorted(set(src_tables) & set(dst_tables))
    print(f"\n{'表名':<40} {'PG(5432)':>10} {'KES(54321)':>12} {'差异':>10}")
    print("-" * 74)

    src_counts = count_rows(src_engine, common)
    dst_counts = count_rows(dst_engine, common)

    missing_data = []
    for t in common:
        s = src_counts[t]
        d = dst_counts[t]
        if isinstance(s, int) and isinstance(d, int):
            diff = d - s
            marker = ""
            if s > 0 and d == 0:
                marker = " ❌ 全缺"
                missing_data.append((t, s, d))
            elif s > d:
                marker = " ⚠️  部分缺"
                missing_data.append((t, s, d))
            elif s == 0 and d == 0:
                marker = ""
            else:
                marker = " ✅"
            print(f"{t:<40} {s:>10} {d:>12} {diff:>+8}{marker}")
        else:
            print(f"{t:<40} {str(s):>10} {str(d):>12}      ERROR")

    # 3. 汇总
    print("\n" + "=" * 70)
    print("汇总")
    print("=" * 70)
    if not missing_data:
        print("✅ 所有表数据一致，无缺失")
    else:
        print(f"⚠️  发现 {len(missing_data)} 张表数据不完整：")
        total_missing_rows = 0
        for t, s, d in missing_data:
            gap = s - d
            total_missing_rows += gap
            print(f"  - {t}: PG 有 {s} 行，KES 有 {d} 行，缺 {gap} 行")
        print(f"\n共缺失约 {total_missing_rows} 行数据")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
