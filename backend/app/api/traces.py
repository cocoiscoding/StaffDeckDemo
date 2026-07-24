"""会话追踪（Traces）API 模块。

提供会话级别的执行轨迹查询接口，用于运维分析和调试。

- ``GET /api/enterprise/traces``：列出当前用户所有会话的轨迹摘要，
  包括最后一次路由决策、工具调用次数、最后消息等。
- ``GET /api/enterprise/traces/{session_id}``：获取指定会话的完整轨迹详情，
  复用 ``sessions.get_session_detail`` 的返回结构。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlmodel import Session, select

from app.api.sessions import get_session_detail
from app.db import get_session
from app.db.models import AgentEvent, ChatSession, Message, User
from app.security.auth import ensure_current_user_tenant, get_current_user
from app.security.tenant import ensure_tenant

router = APIRouter(
    prefix="/api/enterprise/traces",
    tags=["enterprise:traces"],
    dependencies=[Depends(get_current_user)],
)


@router.get("")
def list_traces(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[dict]:
    """列出当前用户所有会话的执行轨迹摘要。

    对每个会话，汇总其最后一次路由决策、工具调用次数和最后一条消息。

    Args:
        tenant_id: 租户 ID。
        current_user: 当前登录用户。
        db: 数据库会话。

    Returns:
        轨迹摘要列表，每个元素包含会话 ID、活跃技能/步骤、最后决策、
        工具调用次数等信息。
    """
    ensure_current_user_tenant(tenant_id, current_user)
    ensure_tenant(db, tenant_id)
    sessions = db.exec(
        select(ChatSession)
        .where(ChatSession.tenant_id == tenant_id, ChatSession.user_id == current_user.id)
        .order_by(ChatSession.updated_at.desc())
    ).all()
    traces: list[dict] = []
    for chat_session in sessions:
        events = db.exec(
            select(AgentEvent)
            .where(AgentEvent.tenant_id == tenant_id, AgentEvent.session_id == chat_session.id)
            .order_by(AgentEvent.created_at.desc())
        ).all()
        messages = db.exec(
            select(Message)
            .where(Message.tenant_id == tenant_id, Message.session_id == chat_session.id)
            .order_by(Message.created_at.desc())
        ).all()
        # 提取最后一次路由决策的 payload
        last_decision = next(
            (event.payload_json for event in events if event.event_type == "router_decision_created"),
            None,
        )
        # 统计已完成的工具调用次数
        tool_calls = len([event for event in events if event.event_type == "tool_call_finished"])
        traces.append(
            {
                "session_id": chat_session.id,
                "user_id": chat_session.user_id,
                "active_skill_id": chat_session.active_skill_id,
                "active_step_id": chat_session.active_step_id,
                "last_decision": last_decision,
                "last_message": messages[0].content if messages else None,
                "last_message_time": messages[0].created_at.isoformat() if messages else None,
                "tool_call_count": tool_calls,
                "status": chat_session.status,
                "updated_at": chat_session.updated_at.isoformat(),
            }
        )
    return traces


@router.get("/{session_id}")
def get_trace(
    session_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    """获取指定会话的完整执行轨迹详情。

    复用 ``sessions.get_session_detail`` 的逻辑，返回会话信息、消息列表和事件列表。

    Args:
        session_id: 会话 ID。
        tenant_id: 租户 ID。
        current_user: 当前登录用户。
        db: 数据库会话。

    Returns:
        包含会话详情、消息和 Agent 事件的字典。
    """
    return get_session_detail(
        session_id=session_id,
        tenant_id=tenant_id,
        current_user=current_user,
        db=db,
    )
