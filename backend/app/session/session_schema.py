"""会话数据模型模块。

定义会话（Session）和聊天（Chat）相关的所有 Pydantic 数据模型，
包括：

- **任务与路由**：``TaskFrame``、``PendingTask``、``TaskUpdate``、``RouterDecision``
  等，描述 Agent 的任务栈和路由决策。
- **步骤执行**：``StepAgentResult``、``KnowledgeQuery``，描述单步执行的动作和结果。
- **会话状态**：``SessionPublic``、``ChatSessionRead``、``ChatSessionCreateRequest``
  等，用于会话的创建、读取和更新。
- **消息**：``MessageRead``、``ChatTurnRequest``、``ChatTurnResponse``，
  描述用户消息和 Agent 回复的交互结构。
- **附件**：``ChatAttachmentRead``，描述上传附件的解析结果。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.tools.tool_schema import ToolCall, ToolResult


RouterDecisionValue = Literal[
    "continue_active",
    "switch_to_pending",
    "create_pending",
    "update_pending",
    "complete_task",
    "start_new_task",
    "answer_only",
    "handoff_human",
    "clarify",
]
MessageFeedbackValue = Literal["up", "down"]


class TaskFrame(BaseModel):
    """任务栈帧，描述一个正在执行或挂起的任务的状态。"""

    task_id: Optional[str] = None
    status: str = "pending"
    skill_id: Optional[str] = None
    step_id: Optional[str] = None
    slots: dict[str, Any] = Field(default_factory=dict)
    intent_summary: Optional[str] = None
    source_turn_id: Optional[str] = None
    source_message: Optional[str] = None
    parent_task_id: Optional[str] = None
    resume_policy: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class PendingTask(BaseModel):
    """待处理任务，描述 Router 识别到的用户意图和目标技能。"""

    task_id: Optional[str] = None
    decision: Optional[str] = None
    target_skill_id: Optional[str] = None
    target_step_id: Optional[str] = None
    confidence: float = 0.0
    user_intent: Optional[str] = None
    reason: Optional[str] = None
    source_message: Optional[str] = None
    slot_hints: dict[str, Any] = Field(default_factory=dict)


class TaskUpdate(BaseModel):
    task_id: str
    status: Optional[str] = None
    target_skill_id: Optional[str] = None
    target_step_id: Optional[str] = None
    user_intent: Optional[str] = None
    reason: Optional[str] = None
    source_message: Optional[str] = None
    slot_hints: dict[str, Any] = Field(default_factory=dict)
    remove: bool = False


class AwaitingInput(BaseModel):
    task_id: Optional[str] = None
    skill_id: Optional[str] = None
    step_id: Optional[str] = None
    expected_fields: list[str] = Field(default_factory=list)
    question_summary: Optional[str] = None
    turn_id: Optional[str] = None


class RouterDecision(BaseModel):
    decision: RouterDecisionValue
    selected_task_id: Optional[str] = None
    target_skill_id: Optional[str] = None
    target_step_id: Optional[str] = None
    confidence: float = 0.0
    user_intent: Optional[str] = None
    general_intent: Optional[str] = None
    reason: Optional[str] = None
    source_message: Optional[str] = None
    clarification_question: Optional[str] = None
    slot_hints: dict[str, Any] = Field(default_factory=dict)
    task_frames: list[PendingTask] = Field(default_factory=list)
    pending_tasks: list[PendingTask] = Field(default_factory=list)
    task_updates: list[TaskUpdate] = Field(default_factory=list)
    created_tasks: list[PendingTask] = Field(default_factory=list)
    awaiting_input: Optional[AwaitingInput] = None


class KnowledgeQuery(BaseModel):
    """知识检索查询，描述 Agent 发起的知识库检索请求。"""

    query: str
    reason: Optional[str] = None
    scope: dict[str, Any] = Field(default_factory=dict)
    max_chunks: int = 6
    query_type: Literal["answer", "policy_check", "tool_discovery", "skill_discovery"] = "answer"
    desired_evidence: Optional[str] = None
    max_depth: int = 2


class StepAgentResult(BaseModel):
    action: Optional[
        Literal[
            "ask_user",
            "clarify",
            "reply",
            "advance",
            "call_tool",
            "query_knowledge",
            "handoff",
        ]
    ] = None
    reply: Optional[str] = None
    slot_updates: dict[str, Any] = Field(default_factory=dict)
    tool_call: Optional[ToolCall] = None
    knowledge_query: Optional[KnowledgeQuery] = None
    knowledge_results: list[dict[str, Any]] = Field(default_factory=list)
    next_step_id: Optional[str] = None
    is_step_completed: bool = False
    handoff: bool = False


class SessionPublic(BaseModel):
    session_id: str
    tenant_id: str
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    title: Optional[str] = None
    active_skill_id: Optional[str] = None
    active_step_id: Optional[str] = None
    slots: dict[str, Any] = Field(default_factory=dict)
    pending_tasks: list[dict[str, Any]] = Field(default_factory=list)
    awaiting_input: Optional[dict[str, Any]] = None
    knowledge_context: list[dict[str, Any]] = Field(default_factory=list)
    summary: Optional[str] = None
    last_agent_question: Optional[str] = None
    status: str = "active"


class ChatTurnRequest(BaseModel):
    """用户对话轮次请求，包含用户消息和会话上下文。"""

    tenant_id: str
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    model_config_id: Optional[str] = None
    client_turn_id: Optional[str] = None
    user_id: Optional[str] = None
    message: str
    attachments: list["ChatAttachmentRead"] = Field(default_factory=list)
    channel: str = "web"
    interaction_mode: Literal["normal", "scheduled_task"] = "normal"
    client_timezone: Optional[str] = None
    debug: bool = False


class ChatAttachmentRead(BaseModel):
    id: str
    filename: str
    content_type: str
    size: int
    kind: Literal["text", "pdf", "image", "binary"] = "binary"
    text: Optional[str] = None
    preview: Optional[str] = None
    data_url: Optional[str] = None
    python_summary: Optional[str] = None
    error: Optional[str] = None


class ChatTurnResponse(BaseModel):
    """对话轮次响应，包含 Agent 回复和完整的会话状态快照。"""

    reply: str
    session_id: str
    router_decision: Optional[RouterDecision] = None
    step_result: Optional[StepAgentResult] = None
    tool_result: Optional[ToolResult] = None
    session_state: SessionPublic


class ChatSessionCreateRequest(BaseModel):
    tenant_id: str
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    title: Optional[str] = None


class ChatSessionUpdateRequest(BaseModel):
    tenant_id: str
    user_id: Optional[str] = None
    title: str


class ChatSessionRead(BaseModel):
    id: str
    tenant_id: str
    user_id: Optional[str]
    agent_id: Optional[str] = None
    title: Optional[str]
    active_skill_id: Optional[str]
    active_step_id: Optional[str]
    status: str
    summary: Optional[str]
    last_agent_question: Optional[str]
    is_scheduled: bool = False
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class MessageRead(BaseModel):
    """聊天消息读取响应模型。"""

    id: str
    tenant_id: str
    session_id: str
    role: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    turn_id: Optional[str] = None
    created_at: str
    feedback_rating: Optional[MessageFeedbackValue] = None

    model_config = ConfigDict(from_attributes=True)


class MessageFeedbackRequest(BaseModel):
    tenant_id: str
    rating: MessageFeedbackValue
