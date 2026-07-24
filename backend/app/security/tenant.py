"""租户校验模块。

本模块提供租户存在性校验的辅助函数，确保请求中引用的 tenant_id 对应的
租户在数据库中真实存在，防止操作不存在的租户资源。

与其他模块的关系
-----------------
- 依赖 :mod:`app.db.models` 的 :class:`Tenant` 模型；
- 被 :mod:`app.api` 下的各路由模块在业务逻辑中调用，作为租户级操作的
  前置校验。
"""

from fastapi import HTTPException
from sqlmodel import Session

from app.db.models import Tenant


def ensure_tenant(session: Session, tenant_id: str) -> Tenant:
    """校验指定租户是否存在，不存在则抛出 404。

    Args:
        session: 数据库会话。
        tenant_id: 待校验的租户 ID。

    Returns:
        存在的 :class:`Tenant` 实例。

    Raises:
        HTTPException: 租户不存在时抛出 404。
    """
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail=f"Tenant not found: {tenant_id}")
    return tenant
