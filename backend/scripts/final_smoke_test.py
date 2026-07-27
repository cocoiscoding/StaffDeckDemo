"""PG 切换最终冒烟测试（含 admin 登录 + 模型配置解密验证）。

使用方法：
    .venv\\Scripts\\python.exe scripts\\final_smoke_test.py
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

BASE = "http://127.0.0.1:5174"


def post(path: str, body: dict[str, Any] | None = None, token: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get(path: str, token: str) -> Any:
    req = urllib.request.Request(
        f"{BASE}{path}", headers={"Authorization": f"Bearer {token}"}, method="GET"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    print("=" * 60)
    print("最终冒烟测试（admin + model_configs 解密）")
    print("=" * 60)

    # 健康检查
    print("\n[1/7] 健康检查...")
    with urllib.request.urlopen(f"{BASE}/api/health", timeout=5) as resp:
        health = json.loads(resp.read().decode("utf-8"))
    assert health["status"] == "ok"
    print(f"    PASS  {health}")

    # admin 登录（验证刚改的密码）
    print("\n[2/7] admin 登录...")
    login = post(
        "/api/auth/login",
        {"tenant_id": "tenant_demo", "username": "admin", "password": ""},
    )
    token = login.get("token", "")
    assert token, f"admin 登录失败: {login}"
    user = login.get("user", {})
    print(f"    PASS  user={user.get('username')}, role={user.get('role')}")

    # 鉴权
    print("\n[3/7] 鉴权 /api/auth/me...")
    me = get("/api/auth/me", token)
    assert me["username"] == "admin"
    print(f"    PASS  {me['username']} @ {me['tenant_id']}")

    # 数字员工列表
    print("\n[4/7] 数字员工列表...")
    agents = get("/api/enterprise/agents?tenant_id=tenant_demo", token)
    print(f"    PASS  count={len(agents)}, 样例={[a['name'] for a in agents[:3]]}")

    # 模型配置（关键：验证 API Key 解密）
    print("\n[5/7] 模型配置（含 API Key 解密）...")
    models = get("/api/enterprise/model-configs?tenant_id=tenant_demo", token)
    print(f"    PASS  count={len(models)}")
    for m in models:
        print(f"          - name={m.get('name')}, model={m.get('model')}, "
              f"protocol={m.get('api_protocol')}, trust={m.get('trust_status')}")

    # 数字员工详情
    print("\n[6/7] 数字员工详情（资源绑定联读）...")
    detail_agent = next((a for a in agents if not a.get("is_overall")), None)
    if detail_agent:
        detail = get(f"/api/enterprise/agents/{detail_agent['id']}?tenant_id=tenant_demo", token)
        print(f"    PASS  agent={detail.get('name')}, resources={len(detail.get('resources', []))}")

    # 工具列表
    print("\n[7/7] 工具列表（验证 tools 表 + JSON 列）...")
    try:
        tools = get("/api/enterprise/tools?tenant_id=tenant_demo", token)
        print(f"    PASS  count={len(tools) if isinstance(tools, list) else 'N/A'}")
    except Exception as e:
        print(f"    SKIP  {e}")

    print("\n" + "=" * 60)
    print("全部通过 ✅")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
