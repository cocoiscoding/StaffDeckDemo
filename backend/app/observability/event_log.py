"""事件日志模块。

封装 ``AgentEvent`` 的写入操作，提供会话级别的 trace 追踪能力。
每次记录事件时可自动注入当前轮次 ID（``turn_id``）和客户端轮次 ID（``client_turn_id``），
便于在可观测性面板中按轮次过滤和关联事件。
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session

from app.db.models import AgentEvent


class EventLog:
    """事件日志记录器，负责向 ``AgentEvent`` 表写入追踪事件。

    每个 ``EventLog`` 实例可绑定一个轮次（turn）上下文，后续所有
    ``record`` 调用都会自动将该轮次的 ID 注入到事件 payload 中，
    实现端到端的请求链路追踪。

    Attributes:
        db: SQLModel 数据库会话。
    """

    def __init__(self, db: Session):
        """初始化事件日志记录器。

        Args:
            db: SQLModel 数据库会话，用于持久化事件记录。
        """
        self.db = db
        self._turn_id: str | None = None
        self._client_turn_id: str | None = None

    def bind_turn(self, turn_id: str, client_turn_id: str | None = None) -> None:
        """绑定当前请求轮次的追踪标识。

        绑定后，后续所有 ``record`` 调用都会在 payload 中自动注入这些 ID。

        Args:
            turn_id: 服务端生成的轮次唯一标识。
            client_turn_id: 客户端生成的轮次标识（可选），用于前后端对齐。
        """
        self._turn_id = str(turn_id or "").strip() or None
        self._client_turn_id = str(client_turn_id or "").strip() or None

    def record(self, tenant_id: str, session_id: str, event_type: str, payload: dict[str, Any]) -> AgentEvent:
        """记录一条 Agent 事件到数据库。

        如果已绑定轮次上下文，会自动将 ``turn_id``、``user_message_id``
        和 ``client_turn_id`` 注入到 payload 中（仅在原 payload 不包含这些字段时）。

        Args:
            tenant_id: 租户 ID。
            session_id: 会话 ID。
            event_type: 事件类型标识，如 ``"user_message_received"``、``"tool_call_finished"`` 等。
            payload: 事件负载字典，包含事件的具体数据。

        Returns:
            已创建并添加到数据库会话的 ``AgentEvent`` 对象（尚未 commit）。
        """
        traced_payload = dict(payload)
        if self._turn_id:
            traced_payload.setdefault("turn_id", self._turn_id)
            traced_payload.setdefault("user_message_id", self._turn_id)
        if self._client_turn_id:
            traced_payload.setdefault("client_turn_id", self._client_turn_id)
        event = AgentEvent(
            tenant_id=tenant_id,
            session_id=session_id,
            event_type=event_type,
            payload_json=traced_payload,
        )
        self.db.add(event)
        return event
