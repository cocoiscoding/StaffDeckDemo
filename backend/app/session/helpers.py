"""会话辅助工具模块。

提供将数据库 ``ChatSession`` 模型转换为 ``SessionPublic`` 传输对象的便捷函数，
用于在 API 响应中向前端暴露会话的公开状态信息。
"""

from __future__ import annotations

from app.db.models import ChatSession
from app.session.session_schema import SessionPublic


def public_session(session: ChatSession) -> SessionPublic:
    """将数据库会话模型转换为前端可读的公开传输对象。

    从 ``ChatSession`` 中提取会话标识、活跃技能/步骤、槽位、待办任务、
    知识上下文等字段，组装为 ``SessionPublic``。

    Args:
        session: 数据库中的会话行对象。

    Returns:
        包含会话公开状态信息的 ``SessionPublic`` 实例。
    """
    return SessionPublic(
        session_id=session.id,
        tenant_id=session.tenant_id,
        user_id=session.user_id,
        agent_id=session.agent_id,
        title=session.title,
        active_skill_id=session.active_skill_id,
        active_step_id=session.active_step_id,
        slots=session.slots_json or {},
        pending_tasks=session.pending_tasks_json or [],
        awaiting_input=session.awaiting_input_json,
        knowledge_context=session.knowledge_context_json or [],
        summary=session.summary,
        last_agent_question=session.last_agent_question,
        status=session.status,
    )
