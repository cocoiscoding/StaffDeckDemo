"""查询 model_configs 表的模型配置记录。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db.database import engine
from sqlalchemy import text

with engine.connect() as conn:
    # 先看字段结构
    cols = conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='model_configs' ORDER BY ordinal_position"
    )).fetchall()
    print("model_configs 表的字段:")
    for c in cols:
        print(f"  - {c[0]}")

    # 查所有记录
    cnt = conn.execute(text("SELECT count(*) FROM model_configs")).scalar()
    print(f"\n记录数: {cnt}")

    if cnt > 0:
        rows = conn.execute(text(
            "SELECT id, name, base_url, model, enabled "
            "FROM model_configs ORDER BY created_at"
        )).fetchall()
        print("\n模型配置列表:")
        for r in rows:
            print(f"  - id={r[0]} name={r[1]} base_url={r[2]} model={r[3]} enabled={r[4]}")
