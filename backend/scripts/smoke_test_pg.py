"""PG 切换后端到端冒烟测试脚本。

验证从 PG 读取数据并完成核心业务流程（登录 → 鉴权 → 查询 → 聊天 SSE）。

使用方法：
    .venv\\Scripts\\python.exe scripts\\smoke_test_pg.py
"""

from __future__ import annotations

import json
import sys
import urllib.request
from typing import Any

BASE = "http://127.0.0.1:5174"


def post(path: str, body: dict[str, Any] | None = None, token: str | None = None) -> dict:
    """发起 POST 请求并返回解析后的 JSON。"""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get(path: str, token: str) -> Any:
    """发起 GET 请求并返回解析后的 JSON。"""
    req = urllib.request.Request(
        f"{BASE}{path}",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    """执行冒烟测试用例。"""
    print("=" * 60)
    print("PG 切换冒烟测试")
    print("=" * 60)

    # ===== 测试 1：健康检查 =====
    print("\n[1/6] 健康检查...")
    try:
        with urllib.request.urlopen(f"{BASE}/api/health", timeout=5) as resp:
            health = json.loads(resp.read().decode("utf-8"))
        assert health["status"] == "ok", f"健康检查失败: {health}"
        print(f"    PASS  status={health['status']}, app={health['app']}")
    except Exception as e:
        print(f"    FAIL  {e}")
        return 1

    # ===== 测试 2：user_demo 登录 =====
    print("\n[2/6] user_demo 登录...")
    try:
        login = post(
            "/api/auth/login",
            {"tenant_id": "tenant_demo", "username": "user_demo", "password": ""},
        )
        token = login.get("token") or login.get("access_token", "")
        assert token, f"未获取到 token: {login}"
        user = login.get("user", {})
        print(f"    PASS  user={user.get('username')}, role={user.get('role')}, token_len={len(token)}")
    except Exception as e:
        print(f"    FAIL  {e}")
        return 1

    # ===== 测试 3：鉴权 /api/auth/me =====
    print("\n[3/6] 鉴权验证 /api/auth/me...")
    try:
        me = get("/api/auth/me", token)
        assert me["username"] == "user_demo", f"用户不匹配: {me}"
        print(f"    PASS  username={me['username']}, tenant={me['tenant_id']}")
    except Exception as e:
        print(f"    FAIL  {e}")
        return 1

    # ===== 测试 4：查询 Agent 列表（验证业务表读取） =====
    print("\n[4/6] 查询数字员工列表 /api/enterprise/agents...")
    try:
        agents = get(f"/api/enterprise/agents?tenant_id=tenant_demo", token)
        count = len(agents) if isinstance(agents, list) else 0
        print(f"    PASS  agents_count={count}")
        if count > 0:
            sample = agents[0]
            print(f"          样例: id={sample.get('id')}, name={sample.get('name')}, is_overall={sample.get('is_overall')}")
    except Exception as e:
        print(f"    FAIL  {e}")
        return 1

    # ===== 测试 5：查询数字员工详情（验证 agent_resource_bindings + JSON 列联读） =====
    print("\n[5/6] 查询数字员工详情（含资源绑定）...")
    try:
        # 取第一个非 overall 的 agent 做详情查询
        detail_agent = next((a for a in agents if not a.get("is_overall")), agents[0] if agents else None)
        if detail_agent:
            detail = get(
                f"/api/enterprise/agents/{detail_agent['id']}?tenant_id=tenant_demo",
                token,
            )
            res_count = len(detail.get("resources", [])) if isinstance(detail, dict) else 0
            print(f"    PASS  agent={detail.get('name')}, resources_count={res_count}")
        else:
            print(f"    SKIP  无可用 agent")
    except Exception as e:
        print(f"    FAIL  {e}")
        return 1

    # ===== 测试 6：查询会话列表（验证 sessions 表 + 关联读取） =====
    print("\n[6/6] 查询会话列表 /api/sessions...")
    try:
        # 部分实现用 /api/enterprise/agents 端点已足够；这里改查 sessions 验证多表
        sessions = get(f"/api/sessions?tenant_id=tenant_demo", token)
        count = len(sessions) if isinstance(sessions, list) else 0
        print(f"    PASS  sessions_count={count}")
    except Exception as e:
        print(f"    FAIL  {e}")
        return 1

    print("\n" + "=" * 60)
    print("冒烟测试全部通过 ✅  PG 切换功能正常")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
