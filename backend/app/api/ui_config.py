"""UI 配置 API 模块。

提供租户级别的 UI 行为配置管理，包括：

- 思维链（thinking trace）显示开关
- 技能执行轨迹（skill trace）显示开关
- 工具调用轨迹（tool trace）显示开关
- 反思轮数上限（reflection_max_rounds）
- Agent 循环最大动作数（agent_loop_max_actions）

分为企业端（enterprise）和聊天端（chat）两组路由：
企业端需要租户管理员权限才能修改，聊天端仅提供只读查询。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session

from app.db import get_session
from app.db.models import UIConfig, User, utc_now
from app.security.auth import get_current_user, require_current_tenant
from app.security.permissions import ensure_tenant_admin
from app.security.tenant import ensure_tenant

enterprise_router = APIRouter(
    prefix="/api/enterprise/ui-config",
    tags=["enterprise:ui-config"],
    dependencies=[Depends(get_current_user)],
)
chat_router = APIRouter(prefix="/api/chat/ui-config", tags=["chat:ui-config"])


class UIConfigRead(BaseModel):
    """UI 配置读取响应模型。"""

    tenant_id: str
    show_thinking_trace: bool
    show_skill_trace: bool
    show_tool_trace: bool
    reflection_max_rounds: int
    agent_loop_max_actions: int
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class UIConfigUpdateRequest(BaseModel):
    """UI 配置更新请求模型。"""

    tenant_id: str
    show_thinking_trace: bool = True
    show_skill_trace: bool = True
    show_tool_trace: bool = True
    reflection_max_rounds: int = Field(default=1, ge=0, le=5)
    agent_loop_max_actions: int = Field(default=6, ge=1, le=20)


def ui_config_read(row: UIConfig) -> UIConfigRead:
    """将数据库 UI 配置行转换为 API 响应模型。

    Args:
        row: ``UIConfig`` 数据库模型实例。

    Returns:
        ``UIConfigRead`` 响应对象。
    """
    return UIConfigRead(
        tenant_id=row.tenant_id,
        show_thinking_trace=row.show_thinking_trace,
        show_skill_trace=row.show_skill_trace,
        show_tool_trace=row.show_tool_trace,
        reflection_max_rounds=row.reflection_max_rounds,
        agent_loop_max_actions=row.agent_loop_max_actions,
        updated_at=row.updated_at.isoformat(),
    )


def get_or_create_ui_config(db: Session, tenant_id: str) -> UIConfig:
    """获取或创建租户的 UI 配置。

    如果租户尚无 UI 配置记录，则使用默认值创建一条。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。

    Returns:
        ``UIConfig`` 数据库模型实例。
    """
    ensure_tenant(db, tenant_id)
    row = db.get(UIConfig, tenant_id)
    if not row:
        row = UIConfig(tenant_id=tenant_id)
        db.add(row)
        db.commit()
        db.refresh(row)
    return row


@enterprise_router.get("", response_model=UIConfigRead, dependencies=[Depends(require_current_tenant)])
def get_enterprise_ui_config(
    tenant_id: str = Query(...), db: Session = Depends(get_session)
) -> UIConfigRead:
    """获取企业端 UI 配置。

    Args:
        tenant_id: 租户 ID。
        db: 数据库会话。

    Returns:
        UI 配置响应对象。
    """
    return ui_config_read(get_or_create_ui_config(db, tenant_id))


@enterprise_router.put("", response_model=UIConfigRead)
def update_enterprise_ui_config(
    request: UIConfigUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> UIConfigRead:
    """更新企业端 UI 配置。

    仅租户管理员可执行此操作。

    Args:
        request: UI 配置更新请求体。
        db: 数据库会话。
        current_user: 当前登录用户。

    Returns:
        更新后的 UI 配置响应对象。

    Raises:
        HTTPException: 如果当前用户不是租户管理员，返回 403。
    """
    ensure_tenant_admin(request.tenant_id, current_user)
    row = get_or_create_ui_config(db, request.tenant_id)
    row.show_thinking_trace = request.show_thinking_trace
    row.show_skill_trace = request.show_skill_trace
    row.show_tool_trace = request.show_tool_trace
    row.reflection_max_rounds = request.reflection_max_rounds
    row.agent_loop_max_actions = request.agent_loop_max_actions
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return ui_config_read(row)


@chat_router.get("", response_model=UIConfigRead)
def get_chat_ui_config(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> UIConfigRead:
    """获取聊天端 UI 配置（只读）。

    验证当前用户属于指定租户后返回该租户的 UI 配置。

    Args:
        tenant_id: 租户 ID。
        current_user: 当前登录用户。
        db: 数据库会话。

    Returns:
        UI 配置响应对象。

    Raises:
        HTTPException: 如果租户不匹配，返回 403。
    """
    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")
    return ui_config_read(get_or_create_ui_config(db, tenant_id))
