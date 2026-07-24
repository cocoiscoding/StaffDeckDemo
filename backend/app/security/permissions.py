"""权限与角色控制模块。

本模块在 :mod:`app.security.auth` 提供的身份认证基础之上，实现基于角色的
访问控制（RBAC）和 Agent（数字员工）粒度的资源权限管理。

核心概念
---------
- **角色 (role)**：用户角色分为 ``admin``（管理员）和 ``member``（普通成员），
  管理员可管理租户级设置和所有 Agent。
- **Agent 作用域**：每个 Agent 可属于某用户私有、全局可见（``is_overall``）
  或已发布到 Gallery（``published_to_gallery``）。权限校验依据 Agent 的归属
  和可见范围决定当前用户是否有权查看或管理。

权限校验层级
-------------
1. 租户隔离（委托给 :func:`app.security.auth.ensure_current_user_tenant`）；
2. 角色校验（管理员 / 普通成员）；
3. Agent 级别的作用域校验（所有者 / 全局 / Gallery 可见性）。

与其他模块的关系
-----------------
- 复用 :mod:`app.security.auth` 完成身份认证与租户校验；
- 依赖 :mod:`app.db.models` 的 :class:`AgentProfile` 和 :class:`User` 模型；
- 被 :mod:`app.api` 下的各路由模块作为 FastAPI 依赖注入使用。
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Query
from sqlmodel import Session

from app.db import get_session
from app.db.models import AgentProfile, User
from app.security.auth import ensure_current_user_tenant, get_current_user

# 角色常量定义
ADMIN_ROLE = "admin"  # 管理员角色标识
MEMBER_ROLE = "member"  # 普通成员角色标识
USER_ROLES = {ADMIN_ROLE, MEMBER_ROLE}  # 合法角色集合


def is_admin_user(current_user: User) -> bool:
    """判断当前用户是否为管理员角色。

    Args:
        current_user: 当前已认证的用户实例。

    Returns:
        是管理员返回 ``True``，否则返回 ``False``。
    """
    return current_user.role == ADMIN_ROLE


def ensure_tenant_admin(tenant_id: str, current_user: User) -> User:
    """确保当前用户是指定租户的管理员。

    校验顺序：先验证租户归属（防止跨租户访问），再验证管理员角色。

    Args:
        tenant_id: 请求路径或查询参数中的租户 ID。
        current_user: 当前已认证的用户。

    Returns:
        通过校验的当前用户（管理员）。

    Raises:
        HTTPException: 租户不匹配（403）、非管理员角色（403）。
    """
    ensure_current_user_tenant(tenant_id, current_user)  # 先做租户隔离校验
    if not is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="Only administrator can manage tenant settings")
    return current_user


def require_tenant_admin(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
) -> User:
    """FastAPI 依赖：要求当前用户是指定租户的管理员。

    适用于管理租户级配置、成员管理等仅限管理员操作的路由。

    Args:
        tenant_id: 查询参数 ``tenant_id``（自动注入）。
        current_user: 通过 :func:`get_current_user` 注入的当前用户。

    Returns:
        通过校验的管理员用户。

    Raises:
        HTTPException: 身份、租户或角色校验失败时抛出（由内部调用传递）。
    """
    return ensure_tenant_admin(tenant_id, current_user)


def require_agent_scope_viewer(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> User:
    """FastAPI 依赖：要求当前用户有权查看指定 Agent。

    可见性规则（满足任一即可查看）：
      - 用户是管理员；
      - Agent 标记为全局可见（``is_overall``）；
      - Agent 属于当前用户（通过 ``metadata_json.owner_user_id`` 判断）；
      - Agent 已发布到 Gallery（``metadata_json.published_to_gallery == True``）。

    Args:
        tenant_id: 查询参数 ``tenant_id``（自动注入）。
        agent_id: 查询参数 ``agent_id``（可选，为 ``None`` 时仅做租户校验）。
        current_user: 通过 :func:`get_current_user` 注入的当前用户。
        db: 自动注入的数据库会话。

    Returns:
        通过校验的当前用户。

    Raises:
        HTTPException: Agent 不存在或不属于该租户（404）、无权查看（403）。
    """
    ensure_current_user_tenant(tenant_id, current_user)  # 先做租户隔离校验
    if not agent_id:
        # 未指定 agent_id 时，仅校验租户归属即可
        return current_user
    row = db.get(AgentProfile, agent_id)
    if not row or row.tenant_id != tenant_id:
        # Agent 不存在或跨租户引用
        raise HTTPException(status_code=404, detail="Agent not found")
    if (
        is_admin_user(current_user)  # 管理员可查看所有 Agent
        or row.is_overall  # 全局可见的 Agent
        or agent_owned_by_user(row, current_user)  # Agent 的创建者
        or (row.metadata_json or {}).get("published_to_gallery") is True  # 已发布到 Gallery
    ):
        return current_user
    raise HTTPException(status_code=403, detail="Cannot access this staff")


def ensure_open_gallery_admin(tenant_id: str, current_user: User) -> None:
    """确保当前用户是管理员，用于 Gallery 开放操作的权限校验。

    目前直接复用 :func:`ensure_tenant_admin`，保留独立函数以便未来扩展
    更细粒度的 Gallery 管理权限。

    Args:
        tenant_id: 请求路径或查询参数中的租户 ID。
        current_user: 当前已认证的用户。

    Raises:
        HTTPException: 非管理员角色时抛出（由内部调用传递）。
    """
    ensure_tenant_admin(tenant_id, current_user)


def ensure_agent_scope_manager(
    db: Session,
    tenant_id: str,
    agent_id: str | None,
    current_user: User,
) -> AgentProfile | None:
    """确保当前用户有权管理（编辑/删除）指定 Agent。

    管理权限规则：
      - 管理员可管理所有 Agent；
      - 普通用户只能管理自己创建的 Agent（``metadata_json.owner_user_id`` 匹配）；
      - 全局 Agent（``is_overall``）仅管理员可管理。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。
        agent_id: Agent ID（为 ``None`` 时仅做租户校验，返回 ``None``）。
        current_user: 当前已认证的用户。

    Returns:
        通过校验的 :class:`AgentProfile` 实例；若 ``agent_id`` 为 ``None`` 则返回 ``None``。

    Raises:
        HTTPException: Agent 不存在或不属于该租户（404）、无管理权限（403）。
    """
    ensure_current_user_tenant(tenant_id, current_user)  # 先做租户隔离校验
    if not agent_id:
        # 未指定 agent_id 时，仅校验租户归属
        return None
    row = db.get(AgentProfile, agent_id)
    if not row or row.tenant_id != tenant_id:
        # Agent 不存在或跨租户引用
        raise HTTPException(status_code=404, detail="Agent not found")
    if is_admin_user(current_user):
        # 管理员可管理所有 Agent
        return row
    if row.is_overall:
        # 全局 Agent 仅管理员可管理
        raise HTTPException(status_code=403, detail="Only administrator can manage overall agent")
    if agent_owned_by_user(row, current_user):
        # Agent 的创建者可管理自己的 Agent
        return row
    raise HTTPException(status_code=403, detail="Only the creator or administrator can manage this staff")


def agent_owned_by_user(row: AgentProfile, user: User) -> bool:
    """判断 Agent 是否属于指定用户（通过 metadata 中的 owner_user_id 判定）。

    Args:
        row: Agent 档案实例。
        user: 当前用户实例。

    Returns:
        Agent 属于该用户返回 ``True``，否则返回 ``False``。
    """
    metadata = row.metadata_json or {}
    return metadata.get("owner_user_id") == user.id
