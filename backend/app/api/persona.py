"""数字员工人设（Persona）配置 API 模块。

提供租户级别的系统提示词（system_prompt）管理接口，
管理员可以读取和更新数字员工的全局人设设定。
人设配置以租户 ID 为主键，每个租户只有一份。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict
from fastapi import APIRouter, Depends, Query
from sqlmodel import Session

from app.db import get_session
from app.db.models import PersonaConfig, User, utc_now
from app.db.seed import DEFAULT_PERSONA_PROMPT
from app.security.auth import get_current_user, require_current_tenant
from app.security.permissions import ensure_tenant_admin
from app.security.tenant import ensure_tenant

router = APIRouter(
    prefix="/api/enterprise/persona",
    tags=["enterprise:persona"],
    dependencies=[Depends(get_current_user)],
)


class PersonaRead(BaseModel):
    """人设配置读取响应模型。"""

    tenant_id: str
    system_prompt: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class PersonaUpdateRequest(BaseModel):
    """人设配置更新请求模型。"""

    tenant_id: str
    system_prompt: str


def persona_read(row: PersonaConfig) -> PersonaRead:
    """将数据库人设配置行转换为 API 响应模型。

    Args:
        row: ``PersonaConfig`` 数据库模型实例。

    Returns:
        ``PersonaRead`` 响应对象。
    """
    return PersonaRead(
        tenant_id=row.tenant_id,
        system_prompt=row.system_prompt,
        updated_at=row.updated_at.isoformat(),
    )


@router.get("", response_model=PersonaRead, dependencies=[Depends(require_current_tenant)])
def get_persona(tenant_id: str = Query(...), db: Session = Depends(get_session)) -> PersonaRead:
    """获取指定租户的人设配置。

    如果租户尚未配置人设，则使用默认提示词创建一条记录。

    Args:
        tenant_id: 租户 ID。
        db: 数据库会话（由 FastAPI 依赖注入）。

    Returns:
        人设配置响应对象。
    """
    ensure_tenant(db, tenant_id)
    row = db.get(PersonaConfig, tenant_id)
    if not row:
        row = PersonaConfig(tenant_id=tenant_id, system_prompt=DEFAULT_PERSONA_PROMPT)
        db.add(row)
        db.commit()
        db.refresh(row)
    return persona_read(row)


@router.put("", response_model=PersonaRead)
def update_persona(
    request: PersonaUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> PersonaRead:
    """更新指定租户的人设配置（系统提示词）。

    仅租户管理员可执行此操作。如果配置不存在则创建。

    Args:
        request: 包含租户 ID 和新系统提示词的请求体。
        db: 数据库会话。
        current_user: 当前登录用户（由依赖注入提供）。

    Returns:
        更新后的人设配置响应对象。

    Raises:
        HTTPException: 如果当前用户不是租户管理员，返回 403。
    """
    ensure_tenant_admin(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    row = db.get(PersonaConfig, request.tenant_id)
    if not row:
        row = PersonaConfig(tenant_id=request.tenant_id, system_prompt=request.system_prompt)
    else:
        row.system_prompt = request.system_prompt
        row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return persona_read(row)
