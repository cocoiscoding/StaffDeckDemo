"""用户认证与账户管理 API 模块。

提供用户登录（JWT 令牌签发）、当前用户信息查询、以及管理员对用户账户的 CRUD 操作。
所有接口均基于多租户隔离，确保用户只能操作所属租户内的资源。
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app.db import get_session
from app.db.models import User, utc_now
from app.security.auth import create_access_token, get_current_user, hash_password, verify_password
from app.security.permissions import MEMBER_ROLE, is_admin_user
from app.security.tenant import ensure_tenant


router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    """用户登录请求体模型。"""

    tenant_id: str
    username: str
    password: str


class UserCreateRequest(BaseModel):
    """创建用户账户的请求体模型。

    管理员通过此模型创建新用户，可指定角色（admin 或 member）。
    """

    tenant_id: str
    username: str
    password: str
    display_name: Optional[str] = None
    role: Literal["admin", "member"] = MEMBER_ROLE


class UserUpdateRequest(BaseModel):
    """更新用户信息的请求体模型。

    所有字段可选，支持更新显示名称、密码和角色。
    """

    tenant_id: str
    display_name: Optional[str] = None
    password: Optional[str] = None
    role: Optional[Literal["admin", "member"]] = None


class UserRead(BaseModel):
    """用户信息读取模型，用于 API 响应。

    不包含密码哈希等敏感字段。
    """

    id: str
    tenant_id: str
    username: str
    display_name: Optional[str] = None
    role: Literal["admin", "member"]
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class LoginResponse(BaseModel):
    """登录成功后的响应模型，包含 JWT 令牌和用户基本信息。"""

    token: str
    user: UserRead


@router.post("/login", response_model=LoginResponse)
def login(request: LoginRequest, db: Session = Depends(get_session)) -> LoginResponse:
    """用户登录接口，验证凭据并签发 JWT 令牌。

    Args:
        request: 包含租户ID、用户名和密码的登录请求体。
        db: 数据库会话依赖。

    Returns:
        LoginResponse: 包含 JWT 令牌和用户信息的登录响应。

    Raises:
        HTTPException 400: 用户名或密码为空。
        HTTPException 401: 用户名或密码不正确。
    """
    ensure_tenant(db, request.tenant_id)
    username = request.username.strip()
    if not username or not request.password:
        raise HTTPException(status_code=400, detail="Username and password are required")

    user = db.exec(
        select(User).where(User.tenant_id == request.tenant_id, User.username == username)
    ).first()
    if not user or not verify_password(request.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    return LoginResponse(token=create_access_token(user), user=_user_read(user))


@router.get("/me", response_model=UserRead)
def me(user: User = Depends(get_current_user)) -> UserRead:
    """获取当前登录用户的信息。

    Args:
        user: 通过 JWT 令牌解析出的当前用户对象。

    Returns:
        UserRead: 当前用户的信息模型。
    """
    return _user_read(user)


@router.post("/users", response_model=UserRead)
def create_user(
    request: UserCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> UserRead:
    """创建新用户账户（仅管理员可用）。

    Args:
        request: 包含新用户信息的创建请求体。
        current_user: 当前登录用户（用于权限验证）。
        db: 数据库会话依赖。

    Returns:
        UserRead: 新创建用户的信息模型。

    Raises:
        HTTPException 403: 当前用户不是管理员或试图为其他租户创建账户。
        HTTPException 400: 用户名或密码为空。
        HTTPException 409: 用户名已存在。
    """
    if not is_admin_user(current_user):
        raise HTTPException(status_code=403, detail="Only administrator can create accounts")
    if request.tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Cannot create accounts for another tenant")
    username = request.username.strip()
    if not username or not request.password:
        raise HTTPException(status_code=400, detail="Username and password are required")
    existing = db.exec(
        select(User).where(User.tenant_id == request.tenant_id, User.username == username)
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Account already exists")
    user = User(
        tenant_id=request.tenant_id,
        username=username,
        display_name=(request.display_name or username).strip()[:80],
        role=request.role,
        password_hash=hash_password(request.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return _user_read(user)


@router.get("/users", response_model=list[UserRead])
def list_users(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[UserRead]:
    """获取指定租户下的所有用户列表（仅管理员可用）。

    Args:
        tenant_id: 租户ID。
        current_user: 当前登录用户（用于权限验证）。
        db: 数据库会话依赖。

    Returns:
        list[UserRead]: 用户信息列表，按创建时间倒序排列。

    Raises:
        HTTPException 403: 当前用户不是管理员或租户不匹配。
    """
    _require_admin(current_user, tenant_id)
    rows = db.exec(
        select(User).where(User.tenant_id == tenant_id).order_by(User.created_at.desc())
    ).all()
    return [_user_read(row) for row in rows]


@router.put("/users/{user_id}", response_model=UserRead)
def update_user(
    user_id: str,
    request: UserUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> UserRead:
    """更新指定用户的信息（仅管理员可用）。

    支持更新显示名称、密码和角色。角色变更不允许自己修改自己的角色。

    Args:
        user_id: 目标用户ID。
        request: 包含更新字段的请求体。
        current_user: 当前登录用户（用于权限验证）。
        db: 数据库会话依赖。

    Returns:
        UserRead: 更新后的用户信息模型。

    Raises:
        HTTPException 403: 当前用户不是管理员或租户不匹配。
        HTTPException 404: 目标用户不存在。
        HTTPException 400: 试图修改自己的角色。
    """
    _require_admin(current_user, request.tenant_id)
    user = db.get(User, user_id)
    if not user or user.tenant_id != request.tenant_id:
        raise HTTPException(status_code=404, detail="Account not found")
    if request.display_name is not None:
        display_name = request.display_name.strip()[:80]
        user.display_name = display_name or user.username
    if request.password is not None:
        password = request.password.strip()
        if password:
            user.password_hash = hash_password(password)
    if request.role is not None and request.role != user.role:
        if user.id == current_user.id:
            raise HTTPException(status_code=400, detail="Cannot change your own account role")
        user.role = request.role
    user.updated_at = utc_now()
    db.add(user)
    db.commit()
    db.refresh(user)
    return _user_read(user)


@router.delete("/users/{user_id}")
def delete_user(
    user_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, bool]:
    """删除指定用户账户（仅管理员可用）。

    不允许删除自己或任何管理员账户。

    Args:
        user_id: 目标用户ID。
        tenant_id: 租户ID。
        current_user: 当前登录用户（用于权限验证）。
        db: 数据库会话依赖。

    Returns:
        dict[str, bool]: 操作结果，``{"ok": True}`` 表示删除成功。

    Raises:
        HTTPException 403: 当前用户不是管理员或租户不匹配。
        HTTPException 404: 目标用户不存在。
        HTTPException 400: 试图删除自己或管理员账户。
    """
    _require_admin(current_user, tenant_id)
    user = db.get(User, user_id)
    if not user or user.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Account not found")
    if user.id == current_user.id or is_admin_user(user):
        raise HTTPException(status_code=400, detail="Administrator account cannot be deleted")
    db.delete(user)
    db.commit()
    return {"ok": True}


def _user_read(user: User) -> UserRead:
    """将数据库 User 对象转换为 API 响应模型 UserRead。

    Args:
        user: 数据库用户对象。

    Returns:
        UserRead: 转换后的用户信息读取模型。
    """
    return UserRead(
        id=user.id,
        tenant_id=user.tenant_id,
        username=user.username,
        display_name=user.display_name,
        role=user.role,
        created_at=user.created_at.isoformat() if user.created_at else None,
        updated_at=user.updated_at.isoformat() if user.updated_at else None,
    )


def _require_admin(user: User, tenant_id: str) -> None:
    """验证当前用户是否为指定租户的管理员。

    Args:
        user: 当前登录用户。
        tenant_id: 请求操作的租户ID。

    Raises:
        HTTPException 403: 当前用户不是管理员或租户不匹配。
    """
    if not is_admin_user(user):
        raise HTTPException(status_code=403, detail="Only administrator can manage accounts")
    if user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Cannot manage accounts for another tenant")
