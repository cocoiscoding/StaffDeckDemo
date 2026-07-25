"""聊天 API 模块 —— 整个系统最核心的交互入口。

本模块实现了完整的聊天会话生命周期管理，核心功能包括：

1. **SSE 流式对话（chat_stream）**：采用「数据库中继」(DB Relay) 模式实现流式响应。
   Worker 线程处理 AgentLoop 并将事件写入 AgentEvent 表，
   SSE 生成器轮询数据库将事件中继给前端，实现解耦的流式传输。
2. **会话管理**：聊天会话的创建、列表、重命名、删除。
3. **消息管理**：消息列表查询、消息反馈（点赞/点踩）。
4. **人工接管（Human Handoff）**：当 Agent 遇到无法处理的问题时，
   可以发起人工接管请求，人工回复后自动恢复 Agent 对话。
5. **定时任务草案**：在 scheduled_task 模式下，解析用户意图生成定时任务草案。
6. **会话标题自动摘要**：后台异步通过 LLM 生成会话标题。
7. **执行追踪（Trace）**：将 AgentEvent 转换为前端可展示的时间线追踪视图。
8. **Span 观测**：LLM 调用和知识库检索的细粒度 Span 记录。

核心设计模式 —— SSE 数据库中继：
    前端 ← SSE ← stream_events() ← AgentEvent(DB) ← run_stream_worker() ← AgentLoop
    Worker 线程和 SSE 生成器通过数据库表解耦，避免长连接占用和线程安全问题。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from datetime import timedelta

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import or_
from sqlmodel import Session, select

from app.agents.branching import model_for_agent
from app.core import AgentLoop
from app.core.cancellation import cancel_chat_turn
from app.db import engine, get_session
from app.db.models import (
    AgentEvent,
    AgentProfile,
    ChatSession,
    HumanHandoffRequest,
    KnowledgeChunk,
    KnowledgeConcept,
    Message,
    MessageFeedback,
    ScheduledTaskRun,
    Skill,
    SkillFeedback,
    User,
    new_id,
    utc_now,
)
from app.feedback import enqueue_feedback_analysis
from app.knowledge.citations import CITATION_EXCERPT_CHAR_LIMIT, compact_knowledge_citation_labels
from app.llm import LLMClient, LLMError
from app.observability.spans import (
    bind_span_sink,
    llm_operation,
    reset_span_sink,
    set_span_sink,
)
from app.security.auth import get_current_user
from app.security.permissions import agent_owned_by_user, is_admin_user
from app.security.tenant import ensure_tenant
from app.scheduled_tasks.schema import ScheduledTaskDraftRead
from app.scheduled_tasks.service import DEFAULT_TASK_TIME, detect_scheduled_task_draft
from app.session.attachments import parse_chat_attachment
from app.session.helpers import public_session
from app.session.session_schema import (
    ChatAttachmentRead,
    ChatSessionCreateRequest,
    ChatSessionRead,
    ChatSessionUpdateRequest,
    ChatTurnRequest,
    ChatTurnResponse,
    MessageFeedbackRequest,
    MessageRead,
)

# 路由前缀
router = APIRouter(prefix="/api/chat", tags=["chat"])

logger = logging.getLogger(__name__)

# 异常回复文案
CANCELLED_ASSISTANT_REPLY = "已停止生成"
INTERRUPTED_ASSISTANT_REPLY = "本次响应中断，请重试发送。"

# 流式回复配置
STREAM_REPLY_CHUNK_SIZE = 96  # SSE 流式回复的分块大小（字符数）
STREAM_RELAY_POLL_SECONDS = 0.08  # SSE 中继轮询数据库的间隔（秒）
STREAM_RELAY_HEARTBEAT_SECONDS = 5.0  # SSE 心跳发送间隔（秒）
STREAM_RELAY_IDLE_TIMEOUT_SECONDS = 660.0  # SSE 中继空闲超时（秒），超时后标记中断
STREAM_INTERRUPTED_TRACEBACK_CHAR_LIMIT = 6000  # 中断错误堆栈的最大字符数

# 会话附件配置
MAX_CHAT_ATTACHMENT_BYTES = 12 * 1024 * 1024  # 聊天附件最大字节数（12MB）
MAX_CHAT_ATTACHMENTS = 8  # 单次最多上传附件数
SESSION_TITLE_SUMMARY_EVENT = "session_title_summarized"  # 会话标题摘要事件类型标识
SCHEDULE_WEEKDAY_LABELS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
# AgentEvent payload 中属于元数据的键名集合（不归入 data 字段）
EVENT_PAYLOAD_META_KEYS = {"id", "event", "type", "event_type", "created_at", "data"}

# 事件类型分类集合，用于把 AgentEvent.event_type 分流
# 事件类型别名映射（数据库存储名 → 前端展示名）
STREAM_RELAY_EVENT_ALIASES = {
    "router_decision_created": "router_decision",
    "stream_status": "status",
}
# 终端事件集合 —— SSE 中继遇到这些事件后停止轮询
STREAM_RELAY_TERMINAL_EVENTS = {
    "complete",
    "error_occurred",
    "stream_cancelled",
    "stream_interrupted",
}
# Span 观测事件类型集合（不在常规 SSE 中继中发送，仅用于 /spans 端点）
SPAN_EVENT_TYPES = {
    "llm_call_started",
    "llm_call_finished",
    "llm_call_failed",
    "knowledge_span_started",
    "knowledge_span_finished",
    "knowledge_span_failed",
}
KNOWLEDGE_TRACE_PHASES = {
    "knowledge",
    "okf_route",
    "okf_only",
    "document_route",
    "document_route_lexical",
    "bucket_route",
    "bucket_route_lexical",
    "section_expand",
    "read_chunks",
    "evidence_pack",
    "no_visible_knowledge",
    "no_documents",
    "no_buckets",
}

# 会话标题摘要提示词
SESSION_TITLE_PROMPT = """你是任务派发台的会话标题编辑器。

根据首轮用户需求和员工回复，生成一个简短、可读、具体的中文标题。

要求：
- 输出 JSON object，格式为 {"title": "..."}。
- 直接输出标题 JSON，不输出分析、候选标题或解释。
- 标题 4 到 18 个中文字符优先，最多 24 个字符。
- 不要使用“新任务”“任务记录”“用户咨询”等空泛标题。
- 不要包含标点符号、引号、编号、员工名或用户称呼。
- 如果无法判断，就返回最能概括用户需求的短语。
"""
_session_title_summary_jobs: set[str] = set()
_session_title_summary_jobs_lock = threading.Lock()

# 继承pydantic.BaseModel，只定义了 HTTP 响应的 JSON 字段
# 作用：作为人工接管请求的"出参壳"，把DB实体转成这个DTO再返回给前端
class HumanHandoffRead(BaseModel):
    # 人工接管请求的读取模型，用于 API 响应。

    id: str  # 接管请求的全局唯一ID
    tenant_id: str  # 所属租户ID
    session_id: str  # 关联的聊天会话ID
    agent_id: str | None = None  # 处理该会话的智能体ID，可空
    requester_user_id: str | None = None  # 发起人工接管的用户ID，可空
    assignee_user_id: str | None = None  # 被指派处理的人工坐席ID，可空
    trigger_skill_id: str | None = None  # 触发接管的技能ID，可空
    trigger_step_id: str | None = None  # 触发接管的技能步骤ID，可空
    context_summary: str | None = None  # 上下文摘要，可空
    pending_question: str | None = None  # 待用户回答的问题，可空
    status: str  # 接管状态，如 pending/answered/closed
    human_reply: str | None = None  # 人工坐席的回复内容，可空
    metadata: dict[str, object]  # 扩展元数据字典
    created_at: str  # 创建时间（ISO字符串）
    updated_at: str  # 更新时间（ISO字符串）
    answered_at: str | None = None  # 人工回复时间（ISO字符串），可空

# 定义了HTTP 请求的字段
# 作用：作为取消聊天轮次端点的"入参壳"，前端发请求时JSON体要符合这个schema
class ChatTurnCancelRequest(BaseModel):
    # 取消聊天对话轮次的请求体模型。

    tenant_id: str  # 租户ID
    turn_id: str  # 要取消的对话轮次ID

# 定义了HTTP 请求的字段
# 作用：作为人工接管回复端点的"入参壳"，前端发请求时JSON体要符合这个schema
class HumanHandoffReplyRequest(BaseModel):
    # 人工接管回复的请求体模型。

    tenant_id: str  # 租户ID
    reply: str  # 人工坐席的回复内容

# DB 实体 → API DTO
def session_read(row: ChatSession, *, is_scheduled: bool = False) -> ChatSessionRead:
    """将数据库 ChatSession 对象转换为 API 响应模型。

    Args:
        row: 数据库聊天会话对象。row 就是一次聊天会话 = sessions 表里的一行
        is_scheduled: 该会话是否关联了定时任务。

    Returns:
        ChatSessionRead: 会话读取模型。
    """
    # 1. 只做字段映射和类型转换，不进行任何业务逻辑，只对外暴露了12个必要的字段
    # 1.1 基础ID字段（id/tenant_id/user_id/agent_id）
    return ChatSessionRead(
        id=row.id,
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        agent_id=row.agent_id,
        # 1.2 用户可见字段（title/状态/激活技能/总结/最近提问）
        title=row.title,
        active_skill_id=row.active_skill_id,
        active_step_id=row.active_step_id,
        status=row.status,
        summary=row.summary,
        last_agent_question=row.last_agent_question,
        # 1.3 跨表标志位（is_scheduled是查询时注入，不是DB字段）
        is_scheduled=is_scheduled,
        # 1.4 时间字段（datetime → ISO字符串）
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )

# DB 实体 → API DTO，一次只返回一条消息记录，多次调用就可拼接出完整的对话记录了
def message_read(
    row: Message,
    feedback_rating: str | None = None,
    turn_id: str | None = None,
    db: Session | None = None,
) -> MessageRead:
    """将数据库 Message 对象转换为 API 响应模型。

    对于 assistant 消息，会压缩知识库引用标签并填充引用内容。

    Args:
        row: 数据库消息对象。message表中的一行
        feedback_rating: 用户对该消息的反馈评分，赞/踩。
        turn_id: 对话轮次 ID。
        db: 数据库会话（可选，用于从 DB 填充引用内容）。

    Returns:
        MessageRead: 消息读取模型。
    """
    
    # 1. 富化元数据：从row.metadata_json中读取元数据，如果db参数也有传入，则要从knowledge_chunks表中查找知识
    metadata = _message_metadata_read(row, db)

    # 2. 对assistant消息压缩知识引用，即：知识引用的顺序按照在 content 中首次出现的顺序重新编号
    content = row.content
    if row.role == "assistant":
        # 只处理agent中的assistant消息
        # 处理逻辑大致为：正则扫描所有[N]、去重 → 重新建立索引 → 替换content里的标签 → 重排引用列表
        content, compacted_citations = compact_knowledge_citation_labels(
            content,  # 原content文本
            metadata.get("knowledge_citations"),  # 原引用列表
        )

        metadata = dict(metadata)  # 浅拷贝，避免直接修改原始字典

        if compacted_citations:
            metadata["knowledge_citations"] = compacted_citations  # 元数据中使用压缩后的
        else:
            metadata.pop("knowledge_citations", None)  # 没有压缩后的引用，删除原引用列表
            metadata.pop("knowledge_query", None)  # 没有压缩后的引用，删除原查询

    # 3. 兜底计算turn_id（对话轮次ID）
    # 把一次对话里的消息都归到一轮（用户提问+Agent回答=一轮），但是这个turn_id不在Message表的字段里
    # 如果元数据中有turn_id，就用它，没有就用user_message_id作为兜底，如果还没有就空串
    metadata_turn_id = str(metadata.get("turn_id") or metadata.get("user_message_id") or "").strip()
    
    # 4. 组装DTO字段，返回MessageRead模型实例，经过FastAPI的序列化后会返回JSON给前端
    return MessageRead(
        id=row.id,
        tenant_id=row.tenant_id,
        session_id=row.session_id,
        role=row.role,
        content=content,
        metadata=metadata,
        turn_id=turn_id or metadata_turn_id or None,  # 兜底
        created_at=row.created_at.isoformat(),
        feedback_rating=feedback_rating,
    )


def _message_metadata_read(row: Message, db: Session | None = None) -> dict:
    """读取消息的元数据，并可选地从数据库填充知识引用的内容片段。

    Args:
        row: 数据库消息对象。
        db: 数据库会话（可选，提供时填充引用内容）。

    Returns:
        dict: 消息元数据字典。
    """
    # 1. 先从消息表的 metadata_json 字段读取元数据（浅拷贝一份避免污染原字典）
    # 之所以要浅拷贝：避免后续修改影响原row对象，符合"读函数无副作用"的设计原则
    metadata = dict(row.metadata_json or {})

    # 2. 如果调用方没有传db会话（db=None），就跳过所有DB查询，直接返回元数据
    # 这是一个性能优化：调用方在不需要引用内容时可以不传db，减少查询开销
    if db is None:
        return metadata

    # 3. 从元数据中取出 knowledge_citations（知识引用列表）
    citations = metadata.get("knowledge_citations")

    # 4. 如果不是列表或者列表为空，说明这条消息没有知识引用，直接返回
    # 比如：用户消息、纯闲聊的assistant消息、没有引用的消息
    if not isinstance(citations, list) or not citations:
        return metadata

    # 5. 遍历每条引用，尝试从DB查出引用的具体内容并填回去（这一步叫"富化"）
    # hydrated: 富化后的引用列表
    # changed: 标记是否真的有内容被富化（用于决定是否整体替换原列表）
    hydrated: list[object] = []
    changed = False
    for citation in citations:
        # 5.1 如果引用本身不是字典（异常数据），原样保留即可
        if not isinstance(citation, dict):
            hydrated.append(citation)
            continue

        # 5.2 调用辅助函数从DB查这条引用的内容（按concept_id优先、chunk_id次之的顺序）
        content = _citation_content_from_db(db, row.tenant_id, citation)

        # 5.3 如果查到了内容，就浅拷贝这条引用并新增 content / excerpt 字段
        # CITATION_EXCERPT_CHAR_LIMIT 是6000字符，防止单个引用内容过长撑爆JSON
        if content:
            next_citation = dict(citation)  # 浅拷贝，避免直接修改原引用
            next_citation["content"] = content[:CITATION_EXCERPT_CHAR_LIMIT]  # content字段供前端显示
            next_citation["excerpt"] = content[:CITATION_EXCERPT_CHAR_LIMIT]  # excerpt字段是同义词，兼容前端不同组件
            hydrated.append(next_citation)
            changed = True  # 标记有内容被富化
        else:
            # 5.4 如果DB也没查到内容（比如引用已被删除），就原样保留这条引用
            hydrated.append(citation)

    # 6. 仅当有内容被富化时，才把原引用列表整体替换为富化后的列表
    # 这样做的目的：减少不必要的对象创建，降低GC压力
    if changed:
        metadata["knowledge_citations"] = hydrated

    # 7. 返回富化后的元数据字典（每条引用都补全了内容片段）
    return metadata


def _citation_content_from_db(db: Session, tenant_id: str, citation: dict) -> str:
    # 1. 优先按 concept_id 查 knowledge_concepts 表（OKF概念卡片，质量更高）
    concept_id = str(citation.get("concept_id") or "").strip()
    if concept_id:
        # 1.1 在同一个租户下查概念，兼容 concept_id 字段和 id 字段两种ID形式
        # or_ 是 SQLAlchemy 的或条件，知识库概念可能在不同列存不同形式的ID
        concept = db.exec(
            select(KnowledgeConcept).where(
                KnowledgeConcept.tenant_id == tenant_id,
                or_(KnowledgeConcept.concept_id == concept_id, KnowledgeConcept.id == concept_id),
            )
        ).first()
        # 1.2 查到概念后，剥离开头的YAML元数据（frontmatter），只保留正文
        if concept:
            content = _strip_okf_frontmatter(concept.content_md or "")
            if content:
                return content  # 找到内容就立刻返回，不继续往下查

    # 2. 退而求其次：按 chunk_id 查 knowledge_chunks 表（文档切片）
    chunk_id = str(citation.get("chunk_id") or "").strip()
    if chunk_id:
        # 2.1 用主键查chunk，同时校验租户归属（防止跨租户数据泄露）
        chunk = db.get(KnowledgeChunk, chunk_id)
        if chunk and chunk.tenant_id == tenant_id and chunk.content:
            return chunk.content  # 找到就返回

    # 3. 都没查到就返回空字符串（让调用方原样保留这条引用）
    return ""


def _strip_okf_frontmatter(value: str) -> str:
    # 1. 用正则把OKF文档开头的YAML元数据块（---...---）剥掉
    # ^--- 匹配开头的---；[\s\S]*? 是非贪婪匹配中间的所有字符；---\s* 匹配结尾的---和空白
    # count=1 表示只剥一次，防止正文里也有---被错误剥掉
    # 最后再strip()去掉首尾空白
    return re.sub(r"^---[\s\S]*?---\s*", "", value or "", count=1).strip()


def human_handoff_read(row: HumanHandoffRequest) -> HumanHandoffRead:
    # 1. 把 HumanHandoffRequest 数据库实体（人工接管表的一行）转成 HumanHandoffRead DTO
    # 跟 session_read 一样：DB 实体 → API DTO 的纯转换函数
    return HumanHandoffRead(
        id=row.id,                                  # 接管请求ID
        tenant_id=row.tenant_id,                    # 租户ID
        session_id=row.session_id,                  # 关联的会话ID
        agent_id=row.agent_id,                      # 关联的Agent ID
        requester_user_id=row.requester_user_id,    # 发起人ID
        assignee_user_id=row.assignee_user_id,      # 处理人ID
        trigger_skill_id=row.trigger_skill_id,      # 触发接管的技能ID
        trigger_step_id=row.trigger_step_id,        # 触发接管的步骤ID
        context_summary=row.context_summary,        # 上下文摘要
        pending_question=row.pending_question,      # 待用户回答的问题
        status=row.status,                          # 状态
        human_reply=row.human_reply,                # 人工回复内容
        # metadata_json 是DB里存的JSON字段，转DTO时要兜底空字典
        metadata=row.metadata_json or {},
        created_at=row.created_at.isoformat(),  # datetime → ISO字符串
        updated_at=row.updated_at.isoformat(),  # datetime → ISO字符串
        # answered_at 可能为None（还没回复），是None就直接传None
        answered_at=row.answered_at.isoformat() if row.answered_at else None,
    )


def _user_message_metadata(request: ChatTurnRequest) -> dict[str, object]:
    # 1. 初始化一个空字典，用来存用户消息的元数据
    metadata: dict[str, object] = {}
    # 2. 如果前端发了 client_turn_id（前端用来跟踪轮次的ID），就存到元数据里
    if request.client_turn_id:
        metadata["client_turn_id"] = request.client_turn_id
    # 3. 如果交互模式是定时任务模式（scheduled_task），标记一下，后面会走不同的逻辑
    if request.interaction_mode == "scheduled_task":
        metadata["interaction_mode"] = "scheduled_task"
    # 4. 如果指定了模型配置ID（前端要求用某个模型），也存进去
    if request.model_config_id:
        metadata["model_config_id"] = request.model_config_id
    # 5. 如果用户上传了附件，把每个附件转成JSON可序列化的字典存进去
    # model_dump(mode="json") 是Pydantic的标准方法，确保datetime等字段转为JSON兼容格式
    if request.attachments:
        metadata["attachments"] = [item.model_dump(mode="json") for item in request.attachments]
    # 6. 返回组装好的元数据字典（用于写入Message表的metadata_json字段）
    return metadata


def _schedule_session_title_summary(
    tenant_id: str,
    user_id: str,
    session_id: str,
    agent_id: str | None,
) -> None:
    """异步安排会话标题摘要任务（后台线程）。

    使用去重锁防止同一会话的并发摘要任务。

    Args:
        tenant_id: 租户ID。
        user_id: 用户 ID。
        session_id: 会话 ID。
        agent_id: Agent ID（用于模型配置解析）。
    """
    # 1. 防御性检查：session_id为空就直接返回（避免构造空job_key）
    if not session_id:
        return

    # 2. 构造任务唯一标识：租户+用户+会话的三元组
    job_key = f"{tenant_id}:{user_id}:{session_id}"

    # 3. 加锁检查任务去重：防止同一会话的并发摘要任务（避免重复调LLM浪费资源）
    with _session_title_summary_jobs_lock:
        # 3.1 如果这个会话已经有任务在跑了，就直接返回（去重）
        if job_key in _session_title_summary_jobs:
            return
        # 3.2 否则把任务标记为"已加入"
        _session_title_summary_jobs.add(job_key)

    # 4. 定义后台线程的执行函数
    # try-finally 保证无论执行成功还是异常，都会从任务集合里移除job_key
    def run() -> None:
        try:
            _summarize_session_title_once(tenant_id, user_id, session_id, agent_id)
        finally:
            with _session_title_summary_jobs_lock:
                _session_title_summary_jobs.discard(job_key)

    # 5. 创建并启动后台线程（daemon=True 让线程随主进程退出而退出）
    thread = threading.Thread(
        target=run,
        daemon=True,
    )
    thread.start()


def _summarize_session_title_once(
    tenant_id: str,
    user_id: str,
    session_id: str,
    agent_id: str | None,
) -> None:
    """执行一次会话标题摘要（含重试逻辑）。

    通过 LLM 生成简短的会话标题。如果消息尚未就绪会等待重试（最多 8 次）。
    生成失败时回退到用户首条消息作为标题。

    Args:
        tenant_id: 租户ID。
        user_id: 用户 ID。
        session_id: 会话 ID。
        agent_id: Agent ID。
    """
    try:
        # 1. 最多重试 8 次：消息可能还没持久化就调用了摘要任务，需要等消息就绪
        for attempt in range(8):
            # 2. 准备本轮迭代的局部变量
            messages: list[Message] = []
            model_config = None
            effective_agent_id = agent_id

            # 3. 在独立DB会话中查询会话状态、消息、模型配置
            # 独立Session是为了线程安全（这是后台线程，不能共享主线程的Session）
            with Session(engine) as db:
                # 3.1 查出该会话（同时校验租户和用户归属）
                session = db.exec(
                    select(ChatSession).where(
                        ChatSession.id == session_id,
                        ChatSession.tenant_id == tenant_id,
                        ChatSession.user_id == user_id,
                    )
                ).first()
                # 3.2 会话不存在就退出
                if not session:
                    return
                # 3.3 会话已经有标题了就退出（避免重复生成）
                if (session.title or "").strip():
                    return
                # 3.4 已经生成过标题摘要事件了也退出（防止重复写AgentEvent）
                existing = db.exec(
                    select(AgentEvent).where(
                        AgentEvent.tenant_id == tenant_id,
                        AgentEvent.session_id == session_id,
                        AgentEvent.event_type == SESSION_TITLE_SUMMARY_EVENT,
                    )
                ).first()
                if existing:
                    return
                # 3.5 取出该会话最早的6条消息（按时间正序）
                messages = db.exec(
                    select(Message)
                    .where(Message.tenant_id == tenant_id, Message.session_id == session_id)
                    .order_by(Message.created_at)
                    .limit(6)
                ).all()
                # 3.6 如果这6条消息里没有任何用户消息，说明Agent还没回复，不生成标题
                if not any(row.role == "user" for row in messages):
                    messages = []
                else:
                    # 3.7 有用户消息：确定实际生效的Agent ID + 加载对应模型配置
                    effective_agent_id = agent_id or session.agent_id
                    model_config = model_for_agent(db, tenant_id, effective_agent_id)

            # 4. 如果没拿到消息（消息还没持久化完），睡 0.25s 再重试
            # 最多重试8次（attempt 0~7），最后一次（attempt=7）就不再重试
            if not messages:
                if attempt < 7:
                    time.sleep(0.25)
                    continue
                return

            # 5. 构造给LLM的payload：把首轮消息列表传过去，限制每条content最多1200字符
            payload = {
                "current_title": "",
                "messages": [
                    {"role": row.role, "content": row.content[:1200]}
                    for row in messages
                    if row.role in {"user", "assistant"}  # 只保留用户和助手消息
                ],
            }
            # 6. 初始化标题和来源标记：默认走 fallback（首条用户消息截取）
            title = ""
            title_source = "first_user_fallback"

            # 7. 如果模型配置可用，调用LLM生成标题
            if model_config:
                try:
                    # 7.1 找到触发本轮的第一条用户消息ID（用于写AgentEvent时关联轮次）
                    title_turn_id = next((row.id for row in messages if row.role == "user"), "")

                    # 7.2 定义一个span sink回调：把LLM调用的事件持久化到AgentEvent表
                    def persist_title_span(
                        event_type: str, event_payload: dict[str, object]
                    ) -> None:
                        # 7.2.1 浅拷贝payload避免污染原对象
                        traced_payload = dict(event_payload)
                        # 7.2.2 把turn_id和user_message_id补到payload里（前端用来关联轮次）
                        if title_turn_id:
                            traced_payload.setdefault("turn_id", title_turn_id)
                            traced_payload.setdefault("user_message_id", title_turn_id)
                        # 7.2.3 在独立Session中写入AgentEvent表
                        with Session(engine) as span_db:
                            _persist_relay_only_event(
                                span_db,
                                tenant_id,
                                session_id,
                                event_type,
                                traced_payload,
                            )

                    # 7.3 用span上下文管理器包住LLM调用，调用JSON模式生成标题
                    with bind_span_sink(persist_title_span), llm_operation("session.title"):
                        raw = LLMClient(model_config).generate_json(SESSION_TITLE_PROMPT, payload)
                    # 7.4 规范化LLM返回的标题（去引号、限长24字符等）
                    title = _normalize_auto_title(str(raw.get("title") or ""))
                    # 7.5 如果LLM成功生成标题，更新来源标记
                    if title:
                        title_source = "first_turn_summary"
                except LLMError:
                    # 7.6 LLM调用出错时，标题置空（会走到下面的fallback）
                    title = ""

            # 8. 如果LLM没生成出标题（失败或返回空），降级用首条用户消息作为标题
            if not title:
                title = _fallback_session_title(messages)
            # 9. 兜底都拿不到标题就退出（不写空标题）
            if not title:
                return

            # 10. 再开一个独立Session，重复校验 + 写库（防止重试期间被别人写过）
            with Session(engine) as db:
                # 10.1 再次查出该会话
                session = db.exec(
                    select(ChatSession).where(
                        ChatSession.id == session_id,
                        ChatSession.tenant_id == tenant_id,
                        ChatSession.user_id == user_id,
                    )
                ).first()
                if not session:
                    return
                if (session.title or "").strip():
                    return
                # 10.2 再次校验标题事件是否已存在
                existing = db.exec(
                    select(AgentEvent).where(
                        AgentEvent.tenant_id == tenant_id,
                        AgentEvent.session_id == session_id,
                        AgentEvent.event_type == SESSION_TITLE_SUMMARY_EVENT,
                    )
                ).first()
                if existing:
                    return
                # 10.3 写入标题到ChatSession表
                session.title = title
                db.add(session)
                # 10.4 同时写一条AgentEvent，记录标题+来源+AgentID（前端展示用）
                db.add(
                    AgentEvent(
                        tenant_id=tenant_id,
                        session_id=session_id,
                        event_type=SESSION_TITLE_SUMMARY_EVENT,
                        payload_json={
                            "title": title,
                            "source": title_source,
                            "agent_id": effective_agent_id,
                        },
                    )
                )
                # 10.5 提交事务（标题+事件一起写入）
                db.commit()
                return
    except (LLMError, Exception):
        # 11. 顶层兜底：任何异常（LLM错误、DB错误等）都静默吞掉
        # 标题生成失败不应该影响主对话流程
        return


def _session_title_summary_payload(db: Session, tenant_id: str, session_id: str) -> dict[str, str] | None:
    # 1. 查最新的"会话标题已生成"事件（按时间倒序，取最新的一条）
    event = db.exec(
        select(AgentEvent)
        .where(
            AgentEvent.tenant_id == tenant_id,
            AgentEvent.session_id == session_id,
            AgentEvent.event_type == SESSION_TITLE_SUMMARY_EVENT,
        )
        .order_by(AgentEvent.created_at.desc())
        .limit(1)
    ).first()
    # 2. 取出事件里的payload（含title、source、agent_id等元信息）
    payload = event.payload_json if event else None
    # 3. 从payload中取title字段，兼容不是dict的异常情况
    title = payload.get("title") if isinstance(payload, dict) else None
    # 4. 如果title不是字符串或者为空，返回None（调用方据此决定是否回退）
    if not isinstance(title, str) or not title.strip():
        return None
    # 5. 返回给前端的载荷：sessionId用camelCase是前端约定
    return {"sessionId": session_id, "title": title.strip()}


def _normalize_auto_title(value: str) -> str:
    # 1. 去掉首尾空白
    title = value.strip()
    # 2. 去掉首尾的成对引号（中文引号和英文引号都去掉，LLM有时会加引号）
    title = title.strip("\"'“”‘’`")
    # 3. 把标题里常见的标点（冒号、句号、逗号、分号、换行制表符）替换成空格
    # 标题里通常不需要这些符号，避免LLM生成"差旅费: 报销"这种带冒号的
    for token in ("\n", "\r", "\t", "：", ":", "。", "，", ",", "；", ";"):
        title = title.replace(token, " ")
    # 4. 按空白分词后重组：去掉空串、把多空白压缩成单空格
    title = " ".join(part for part in title.split() if part)
    # 5. 截断到最多24字符（标题长度限制，由调用方的提示词决定）
    return title[:24]


def _fallback_session_title(messages: list[Message]) -> str:
    # 1. 在消息列表里找第一条非空的用户消息
    first_user = next((row.content for row in messages if row.role == "user" and row.content.strip()), "")
    # 2. 没找到就返回空串（让调用方决定要不要放弃标题）
    if not first_user:
        return ""
    # 3. 用首条用户消息作为标题的素材，再走一遍 normalize 规范化
    return _normalize_auto_title(first_user)


def _normalized_session_event_payload(row: AgentEvent) -> dict[str, object]:
    # 1. 浅拷贝payload_json（避免直接修改DB实体）
    payload = dict(row.payload_json or {})
    # 2. 确定事件名：优先用payload里的event字段，其次type字段，最后用row.event_type
    event_name = str(payload.get("event") or payload.get("type") or row.event_type)
    # 3. 构造data字段：默认从payload里剔除元数据键
    # data里应该只放业务数据，不放id/created_at这些元信息（前端会单独取这些字段）
    data = payload.get("data")
    if not isinstance(data, dict):
        # EVENT_PAYLOAD_META_KEYS 是不归入data的元数据键白名单
        data = {key: value for key, value in payload.items() if key not in EVENT_PAYLOAD_META_KEYS}
    # 4. 组装标准化后的事件payload：把元信息（id/event/type/event_type/created_at）补齐到顶层
    normalized: dict[str, object] = {
        **payload,
        "id": str(payload.get("id") or row.id),                # 事件ID（用字符串类型）
        "event": event_name,                                    # 事件名（前端按此路由）
        "type": str(payload.get("type") or event_name),         # 兼容type字段
        "event_type": str(payload.get("event_type") or event_name),  # 兼容event_type字段
        "created_at": str(payload.get("created_at") or row.created_at.isoformat()),  # ISO时间
        "data": data,                                            # 业务数据
    }
    # 5. 如果顶层没有run_id但data里有，把run_id提到顶层（前端SSE中继需要）
    if "run_id" not in normalized and data.get("run_id"):
        normalized["run_id"] = str(data.get("run_id"))
    # 6. 返回标准化后的事件payload
    return normalized


def _resume_human_handoff_async(handoff_id: str) -> None:
    # 1. 启动一个后台线程执行恢复Worker（daemon=True 随主进程退出而退出）
    # args=(handoff_id,) 是单参数线程函数的固定传参语法（逗号不可少，否则被识别为generator）
    thread = threading.Thread(target=_resume_human_handoff_worker, args=(handoff_id,), daemon=True)
    # 2. 启动线程后立即返回（异步语义，调用方不等Worker完成）
    thread.start()


def _resume_human_handoff_worker(handoff_id: str) -> None:
    """人工接管恢复 Worker：在后台线程中恢复 Agent 对话。

    当人工回复后，使用人工回复作为新的用户消息重新触发 AgentLoop。
    包含完整的错误处理和状态持久化。

    Args:
        handoff_id: 人工接管请求 ID。
    """
    # 1. 顶层try-except：任何异常都进底部的失败处理分支，确保状态可观测
    try:
        # 2. 在独立DB Session中执行整个恢复流程（后台线程不能共享主线程的Session）
        with Session(engine) as db:
            # 2.1 查出接管记录
            handoff = db.get(HumanHandoffRequest, handoff_id)
            # 2.2 校验：接管记录不存在，或状态不是"已回复"，或没有回复内容，就直接返回
            # 这些是异常状态，不该进入恢复流程
            if not handoff or handoff.status != "answered" or not handoff.human_reply:
                return
            # 2.3 查出关联的会话
            chat_session = db.get(ChatSession, handoff.session_id)
            # 2.4 校验：会话不存在，或会话和接管的租户不一致（防止跨租户数据泄露），直接返回
            if not chat_session or chat_session.tenant_id != handoff.tenant_id:
                return
            # 2.5 浅拷贝metadata_json
            metadata = dict(handoff.metadata_json or {})
            # 2.6 检查是否已经标记过"开始恢复"（防止并发重复启动恢复Worker）
            if metadata.get("resume_started_at"):
                return
            # 2.7 记录开始时间戳（用UTC时间），并写回metadata
            now = utc_now()
            metadata["resume_started_at"] = now.isoformat()
            handoff.metadata_json = metadata
            db.add(handoff)
            # 2.8 写一条"恢复已开始"事件到AgentEvent表（前端可以感知状态变化）
            db.add(
                AgentEvent(
                    tenant_id=handoff.tenant_id,
                    session_id=handoff.session_id,
                    event_type="human_handoff_resume_started",
                    payload_json={
                        "handoff_id": handoff.id,                  # 关联的接管ID
                        "agent_id": handoff.agent_id,              # 关联的Agent
                        "trigger_skill_id": handoff.trigger_skill_id,   # 触发接管的技能
                        "trigger_step_id": handoff.trigger_step_id,     # 触发接管的步骤
                    },
                    created_at=now,
                )
            )
            # 2.9 提交：把"开始恢复"的状态和事件持久化
            db.commit()

            # 3. 构造一个假的ChatTurnRequest，把人工回复当作用户的新消息
            request = ChatTurnRequest(
                tenant_id=handoff.tenant_id,                                # 租户
                session_id=handoff.session_id,                              # 会话ID
                agent_id=handoff.agent_id or chat_session.agent_id,          # Agent（优先用接管的）
                user_id=handoff.requester_user_id or chat_session.user_id or "",  # 用户
                message=handoff.human_reply,                                # 用人工回复作为消息内容
                channel="human_handoff_resume",                             # 标记是"接管恢复"渠道
                debug=False,                                                # 非debug模式
            )
            # 4. 调AgentLoop处理这一轮（核心：让Agent用人工回复继续对话）
            AgentLoop(db).handle_turn(request)
            # 5. AgentLoop完成后，记录完成时间戳
            metadata = dict(handoff.metadata_json or {})
            metadata["resume_finished_at"] = utc_now().isoformat()
            handoff.metadata_json = metadata
            db.add(handoff)
            # 6. 提交：把"恢复已完成"的状态持久化
            db.commit()
    except Exception as exc:
        # 7. 异常兜底：任何上述步骤抛出的异常都进这里
        with Session(engine) as db:
            # 7.1 重新查出接管记录（异常可能发生在事务里，需要新Session）
            handoff = db.get(HumanHandoffRequest, handoff_id)
            # 7.2 接管记录已被删除就放弃（无法记录失败状态）
            if not handoff:
                return
            # 7.3 记录失败时间戳和错误信息（截断到300字符防止超长）
            metadata = dict(handoff.metadata_json or {})
            metadata["resume_failed_at"] = utc_now().isoformat()
            metadata["resume_error"] = str(exc)[:300]
            # 7.4 把状态改为failed，更新时间戳
            handoff.status = "failed"
            handoff.metadata_json = metadata
            handoff.updated_at = utc_now()
            db.add(handoff)
            # 7.5 写一条"恢复失败"事件（前端可看到错误状态）
            db.add(
                AgentEvent(
                    tenant_id=handoff.tenant_id,
                    session_id=handoff.session_id,
                    event_type="human_handoff_resume_failed",
                    payload_json={"handoff_id": handoff.id, "error": str(exc)[:300]},
                )
            )
            # 7.6 提交失败状态
            db.commit()


def _maybe_handle_scheduled_task_request(
    db: Session,
    request: ChatTurnRequest,
    chat_session: ChatSession,
) -> tuple[ChatTurnResponse, ScheduledTaskDraftRead] | None:
    """检测并处理定时任务模式的请求。

    在 scheduled_task 交互模式下，解析用户消息生成定时任务草案，
    如果草案需要创建则构建完整的回复（含状态事件）。

    Args:
        db: 数据库会话。
        request: 聊天轮次请求体。
        chat_session: 聊天会话对象。

    Returns:
        包含 (回复响应, 草案) 的元组，如果不是定时任务模式或不需要创建则返回 None。
    """
    if request.interaction_mode != "scheduled_task" or not request.agent_id:
        # 1. 早返回：不是定时任务模式，或没指定Agent，就不走这个分支
        return None
    # 2. 调detect_scheduled_task_draft识别用户消息里的定时任务意图
    #    传入chat_session.id和client_timezone（用于解析时间相关的草稿）
    draft = detect_scheduled_task_draft(
        db,
        request.tenant_id,
        request.agent_id,
        request.user_id,
        request.message,
        chat_session.id,
        request.client_timezone,
    )
    # 3. 没识别到草稿，或草稿不需要创建（should_create=False），就返回
    if not draft or not draft.should_create:
        return None

    # 4. 把草案渲染成给用户看的回复文本
    reply = _scheduled_task_draft_reply(draft)
    # 5. 构造一系列递增时间戳（相隔1微秒）用于事件排序
    #    用now + 1us/2us/... 是为了保证事件按写入顺序排列，避免同毫秒内时序错乱
    now = utc_now()
    intent_time = now + timedelta(microseconds=1)        # "识别意图"事件时间
    parse_time = now + timedelta(microseconds=2)         # "解析计划"事件时间
    draft_status_time = now + timedelta(microseconds=3)  # "生成草案"事件时间
    event_time = now + timedelta(microseconds=4)         # "草案已创建"事件时间
    assistant_time = now + timedelta(microseconds=5)     # assistant消息时间
    state_time = now + timedelta(microseconds=6)         # session状态变更事件时间
    # 6. 更新ChatSession的updated_at和summary（summary让前端列表页可看到摘要）
    chat_session.updated_at = assistant_time
    chat_session.summary = f"最近回复：{reply[:120]}"
    # 7. 构造用户消息实体（不是真Agent回复，是定时任务模式下的用户消息）
    user_message = Message(
        tenant_id=request.tenant_id,
        session_id=chat_session.id,
        role="user",                                       # 角色是user
        content=request.message,                           # 内容是用户发的原文本
        metadata_json=_user_message_metadata(request),    # 元数据用_user_message_metadata构造
        created_at=now,                                    # 用户消息时间用now
    )
    db.add(user_message)
    # 8. 把草案转成可JSON序列化的字典（后面多处复用）
    draft_payload = draft.model_dump(mode="json")
    # 9. 写"用户消息已收到"事件到AgentEvent表（前端SSE中继能感知消息入栈）
    db.add(
        AgentEvent(
            tenant_id=request.tenant_id,
            session_id=chat_session.id,
            event_type="user_message_received",
            payload_json={
                "message_id": user_message.id,            # 用户消息ID
                "client_turn_id": request.client_turn_id, # 前端轮次跟踪ID
                "message": request.message,               # 用户原文本
                "channel": request.channel,               # 渠道
                "user_id": request.user_id,               # 用户ID
            },
            created_at=now,
        )
    )
    # 10. 写3条流式状态事件：意图识别 → 计划解析 → 草案生成
    #     这3条事件是给前端做"实时进度条"用的
    _add_stream_status_event(
        db, request.tenant_id, chat_session.id, user_message.id,
        "scheduled_task_intent", "识别定时任务需求",     # 步骤1：识别意图
        created_at=intent_time,
    )
    _add_stream_status_event(
        db, request.tenant_id, chat_session.id, user_message.id,
        "scheduled_task_parse", "解析执行计划",          # 步骤2：解析计划
        created_at=parse_time,
    )
    _add_stream_status_event(
        db, request.tenant_id, chat_session.id, user_message.id,
        "scheduled_task_draft", "生成定时任务草案",       # 步骤3：生成草案
        extra=draft_payload,                             # extra字段把draft详情也带上
        created_at=draft_status_time,
    )
    # 11. 构造assistant消息（"定时任务草案已就绪"这条回复）
    assistant_message = Message(
        tenant_id=request.tenant_id,
        session_id=chat_session.id,
        role="assistant",                                # 角色是assistant
        content=reply,                                   # 内容是渲染好的草案文本
        metadata_json={
            "scheduled_task_draft": draft_payload,       # 草案详情存进metadata
            "user_message_id": user_message.id,          # 关联触发的用户消息
            "turn_id": user_message.id,                  # turn_id就是用户消息ID（一轮 = 一问一答）
        },
        created_at=assistant_time,
    )
    db.add(assistant_message)
    # 12. 写"草案已创建"事件（前端用来跳转到草稿详情页）
    db.add(
        AgentEvent(
            tenant_id=request.tenant_id,
            session_id=chat_session.id,
            event_type="scheduled_task_draft_created",
            payload_json={**draft_payload, "user_message_id": user_message.id, "turn_id": user_message.id},
            created_at=event_time,
        )
    )
    # 13. 写"assistant消息已创建"事件（SSE中继推到前端用）
    db.add(
        AgentEvent(
            tenant_id=request.tenant_id,
            session_id=chat_session.id,
            event_type="assistant_message_created",
            payload_json={
                "message_id": assistant_message.id,       # assistant消息ID
                "assistant_message_id": assistant_message.id,  # 兼容字段名
                "user_message_id": user_message.id,       # 关联用户消息
                "turn_id": user_message.id,              # 轮次ID
                "reply": reply,                           # 回复文本
                "scheduled_task_draft": draft_payload,   # 草案详情
            },
            created_at=assistant_time,
        )
    )
    # 14. 把chat_session转成公开的DTO（去敏感字段）
    state = public_session(chat_session)
    # 15. 写"会话状态已变更"事件（前端列表页可看到最近更新）
    db.add(
        AgentEvent(
            tenant_id=request.tenant_id,
            session_id=chat_session.id,
            event_type="session_state_changed",
            payload_json=state.model_dump(),              # DTO → 字典
            created_at=state_time,
        )
    )
    # 16. 一次性提交所有写操作
    db.commit()
    # 17. 刷新chat_session，拿到DB最新字段（包括DB侧自动更新的字段）
    db.refresh(chat_session)
    # 18. 构造API响应（同步返回，不需要走SSE中继）
    response = ChatTurnResponse(
        reply=reply,
        session_id=chat_session.id,
        session_state=public_session(chat_session),  # 再调一次public_session拿到最新状态
    )
    # 19. 返回 (响应, 草案)，调用方拿到draft后可能会写到ScheduledTaskDraft表
    return response, draft


def _add_stream_status_event(
    db: Session,
    tenant_id: str,
    session_id: str,
    user_message_id: str,
    phase: str,
    text: str,
    *,
    extra: dict | None = None,
    created_at=None,
) -> None:
    """添加一个 stream_status 类型的 AgentEvent（用于前端展示处理进度）。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        session_id: 会话 ID。
        user_message_id: 关联的用户消息 ID。
        phase: 处理阶段标识。
        text: 展示文本。
        extra: 额外的负载字段。
        created_at: 创建时间（可选）。
    """
    # 1. 构造stream_status事件的payload：基础字段 + 调用方传入的extra（dict合并）
    payload = {
        "phase": phase,                            # 处理阶段标识（如"识别意图"）
        "text": text,                              # 展示文本（前端进度条用）
        "user_message_id": user_message_id,        # 关联的用户消息ID
        "turn_id": user_message_id,                # turn_id和user_message_id保持一致
        **(extra or {}),                           # 合并extra（额外信息，比如draft详情）
    }
    # 2. 写入AgentEvent表：event_type固定为"stream_status"
    #    created_at默认当前UTC时间，允许调用方传入自定义时间（用于事件排序）
    db.add(
        AgentEvent(
            tenant_id=tenant_id,
            session_id=session_id,
            event_type="stream_status",            # 事件类型固定
            payload_json=payload,                  # 上面的payload
            created_at=created_at or utc_now(),    # 时间戳（默认当前）
        )
    )


def _scheduled_task_draft_reply(draft: ScheduledTaskDraftRead) -> str:
    # 1. 构造5行文本（多行Markdown风格的回复）
    lines = [
        "我已按你选择的定时项目整理成自动任务草案。",                     # 引导行
        f"任务：{draft.title}",                                            # 任务标题
        f"计划：{_format_draft_schedule(draft)}",                          # 计划描述（用_format_draft_schedule渲染）
        f"执行内容：{draft.prompt}",                                       # 执行内容
        "确认下方卡片后才会启用；确认前不会创建自动任务。",                 # 确认提示
    ]
    return "\n".join(lines)


def _format_draft_schedule(draft: ScheduledTaskDraftRead) -> str:
    # 1. 委托给_format_scheduled_task_schedule渲染：传入draft的schedule_type和schedule字典
    return _format_scheduled_task_schedule(draft.schedule_type, draft.schedule or {})


def _format_once_schedule(schedule: dict) -> str:
    # 1. 一次性任务：渲染"一次性 {时间}"（run_at是ISO时间字符串，没有就用"待确认时间"）
    return f"一次性 {schedule.get('run_at') or '待确认时间'}"


def _format_weekly_schedule(schedule: dict) -> str:
    # 1. 每周任务：渲染"每周 {周几} {时间}"
    #    weekdays是数字列表（0-6），用_format_weekday_labels转成中文"周一、周二"等
    #    time是"HH:MM"格式字符串，没有就用DEFAULT_TASK_TIME（默认任务时间）
    return f"每周 {_format_weekday_labels(schedule.get('weekdays'))} {schedule.get('time') or DEFAULT_TASK_TIME}"


def _format_monthly_schedule(schedule: dict) -> str:
    # 1. 每月任务：渲染"每月 {几号} 号 {时间}"
    #    day_of_month是数字，没有默认1号
    return f"每月 {schedule.get('day_of_month') or 1} 号 {schedule.get('time') or DEFAULT_TASK_TIME}"


def _format_daily_schedule(schedule: dict) -> str:
    # 1. 每天任务：渲染"每天 {时间}"（最简单的一种）
    return f"每天 {schedule.get('time') or DEFAULT_TASK_TIME}"


# 调度类型 → 对应渲染函数的映射表
# 作用：根据schedule_type字符串，路由到对应的format函数
# 是典型的"注册表模式"（registry pattern），新增调度类型只需往这里加一行
SCHEDULE_TEXT_FORMATTERS: dict[str, Callable[[dict], str]] = {
    "once": _format_once_schedule,        # 一次性任务
    "weekly": _format_weekly_schedule,     # 每周任务
    "monthly": _format_monthly_schedule,   # 每月任务
    "daily": _format_daily_schedule,       # 每天任务
}


def _format_scheduled_task_schedule(schedule_type: object, schedule_value: object) -> str:
    # 1. 把schedule_value兜底成字典（如果传入的是None或非dict，按空字典处理）
    schedule = schedule_value if isinstance(schedule_value, dict) else {}
    # 2. 把schedule_type转字符串兜底"daily"（默认值）
    schedule_type_text = str(schedule_type or "daily")
    # 3. 从注册表里找对应的格式化函数，找不到就兜底用每天
    formatter = SCHEDULE_TEXT_FORMATTERS.get(schedule_type_text, _format_daily_schedule)
    # 4. 调格式化函数返回最终的字符串
    return formatter(schedule)


def _format_weekday_labels(value: object) -> str:
    # 1. 非列表（None、字符串等）就默认返回"周一"（SCHEDULE_WEEKDAY_LABELS[0]）
    if not isinstance(value, list):
        return SCHEDULE_WEEKDAY_LABELS[0]
    # 2. 遍历value，把数字0-6转成中文"周一"~"周日"，用"、"连接
    labels: list[str] = []
    for item in value:
        # 2.1 把item转字符串去空白
        text = str(item).strip()
        # 2.2 必须全是数字（0-6），否则跳过（防御性检查，防止LLM输出非数字）
        if not text.isdigit():
            continue
        # 2.3 转int，检查范围在SCHEDULE_WEEKDAY_LABELS长度内
        day = int(text)
        if 0 <= day < len(SCHEDULE_WEEKDAY_LABELS):
            labels.append(SCHEDULE_WEEKDAY_LABELS[day])
    # 3. 用"、"连接；如果一个标签都没有（空列表），兜底返回"周一"
    return "、".join(labels) or SCHEDULE_WEEKDAY_LABELS[0]


def _scheduled_task_trace_detail(payload: dict) -> str | None:
    # 1. 取出payload里的title，转字符串去空白
    title = str(payload.get("title") or "").strip()
    # 2. 渲染调度计划文本（用前面定义的格式化函数）
    schedule = _format_scheduled_task_schedule(payload.get("schedule_type"), payload.get("schedule"))
    # 3. 用" · "把三段拼起来（标题、计划、提示），自动跳过空字符串
    detail = " · ".join(part for part in (title, schedule, "等待确认后启用") if part)
    # 4. 如果三段都为空就返回None（前端据此判断要不要渲染）
    return detail or None


def _scheduled_task_trace_lines(payload: dict, *, state: str = "completed") -> list[dict]:
    # 1. 渲染调度计划文本（前端trace展示用）
    schedule = _format_scheduled_task_schedule(payload.get("schedule_type"), payload.get("schedule"))
    # 2. 返回3行trace（前端按这个结构展示3个步骤）
    #    kind="decision" 表示这是"决策类"步骤（前端用决策图标渲染）
    #    state控制样式：默认completed（已完成），调用方可传running/failed等
    return [
        {
            "id": "scheduled_task_intent",            # 步骤1：识别意图
            "kind": "decision",
            "text": "识别定时任务需求",
            "detail": "用户选择了创建定时任务模式",
            "state": "completed",                       # 第一二步永远completed（已发生）
        },
        {
            "id": "scheduled_task_parse",              # 步骤2：解析计划
            "kind": "decision",
            "text": "解析执行计划",
            "detail": f"计划：{schedule}" if schedule else None,  # 计划文本，没有就None
            "state": "completed",
        },
        {
            "id": "scheduled_task_draft",              # 步骤3：生成草案（state由调用方决定）
            "kind": "decision",
            "text": "生成定时任务草案",
            "detail": _scheduled_task_trace_detail(payload),  # 复用上面的detail函数
            "state": state,                             # 默认completed，调用方可传running/failed
        },
    ]


def _persist_scheduled_task_draft(
    db: Session,
    tenant_id: str,
    session_id: str,
    draft: ScheduledTaskDraftRead,
) -> None:
    # 1. session_id为空就早返回（无法定位要写哪条会话的草案）
    if not session_id:
        return
    # 2. 把draft转成可JSON序列化的字典
    payload = draft.model_dump(mode="json")
    # 3. 查出该会话最新的assistant消息（草案的载体一般是最后一条assistant回复）
    latest_assistant = db.exec(
        select(Message)
        .where(Message.tenant_id == tenant_id, Message.session_id == session_id, Message.role == "assistant")
        .order_by(Message.created_at.desc())  # 倒序取最新
    ).first()
    # 4. 如果找到了最新assistant消息，把draft详情塞进它的metadata
    #    这样前端拿assistant消息时能直接看到关联的草案
    if latest_assistant:
        metadata = dict(latest_assistant.metadata_json or {})  # 浅拷贝metadata
        metadata["scheduled_task_draft"] = payload              # 写入draft
        latest_assistant.metadata_json = metadata              # 写回消息
        db.add(latest_assistant)                                # 标记修改
    # 5. 写一条"草案已创建"事件到AgentEvent表（前端SSE中继感知用）
    db.add(
        AgentEvent(
            tenant_id=tenant_id,
            session_id=session_id,
            event_type="scheduled_task_draft_created",
            payload_json=payload,
            created_at=utc_now(),
        )
    )
    # 6. 提交事务（更新message + 写event 一起）
    db.commit()


def _reply_chunks(reply: str) -> Iterator[str]:
    """将回复文本按 STREAM_REPLY_CHUNK_SIZE 大小分块，用于 SSE 流式传输。"""
    # 1. 用range按STREAM_REPLY_CHUNK_SIZE（96字符）步长切片
    #    range(0, len(reply), 96) 生成 [0, 96, 192, ...] 这种起点
    for index in range(0, len(reply), STREAM_REPLY_CHUNK_SIZE):
        # 2. yield一个96字符的切片（生成器模式，调用方for循环时按需生成）
        yield reply[index : index + STREAM_REPLY_CHUNK_SIZE]


@router.post("/attachments", response_model=list[ChatAttachmentRead])
async def upload_chat_attachments(
    tenant_id: str = Query(...),
    files: list[UploadFile] = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[ChatAttachmentRead]:
    # 1. 鉴权：校验租户ID属于当前用户（防止跨租户访问）
    _ensure_request_tenant(tenant_id, current_user)
    # 2. 再次校验租户存在（依赖注入的db已确保租户表有记录）
    ensure_tenant(db, tenant_id)
    # 3. 校验至少上传了一个文件
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")
    # 4. 校验文件数量不超过上限（MAX_CHAT_ATTACHMENTS=8）
    if len(files) > MAX_CHAT_ATTACHMENTS:
        raise HTTPException(status_code=400, detail=f"最多一次上传 {MAX_CHAT_ATTACHMENTS} 个文件")
    # 5. 逐个解析上传的文件，转成ChatAttachmentRead返回
    parsed: list[ChatAttachmentRead] = []
    for file in files:
        # 5.1 异步读取文件二进制内容（FastAPI的UploadFile是异步流式读取）
        data = await file.read()
        # 5.2 校验文件大小不超过单文件上限（MAX_CHAT_ATTACHMENT_BYTES=12MB）
        if len(data) > MAX_CHAT_ATTACHMENT_BYTES:
            raise HTTPException(status_code=413, detail=f"{file.filename or '文件'} 超过上传大小限制")
        # 5.3 调parse_chat_attachment解析附件：拿到内容、文本、附件类型等
        parsed.append(parse_chat_attachment(file.filename or "uploaded-file", file.content_type, data))
    # 6. 返回所有解析好的附件列表
    return parsed


@router.post("/turn", response_model=ChatTurnResponse)
def chat_turn(
    request: ChatTurnRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ChatTurnResponse:
    """处理非流式聊天对话轮次。

    同步执行 AgentLoop 并返回完整回复。如果当前为定时任务模式，
    会先检测是否需要生成定时任务草案。

    Args:
        request: 聊天轮次请求体。
        current_user: 当前登录用户。
        db: 数据库会话依赖。

    Returns:
        ChatTurnResponse: 包含回复内容和会话状态的响应。

    Raises:
        HTTPException 400: 消息为空。
        HTTPException 403: 租户不匹配。
    """
    # 1. 鉴权：校验请求的tenant_id与当前用户匹配
    _ensure_request_tenant(request.tenant_id, current_user)
    # 2. 强制覆盖user_id为当前用户（防止前端伪造用户ID）
    request = request.model_copy(update={"user_id": current_user.id})
    # 3. 处理会话上下文：有session_id就查会话+绑定agent；没session_id就确保agent可用
    if request.session_id:
        # 3.1 有session_id：先查出或创建该会话
        chat_session = _ensure_chat_session_available(db, request.tenant_id, current_user.id, request.session_id)
        # 3.2 把请求绑定到该会话的agent（如果请求没指定agent就沿用会话的）
        request = _bind_request_to_session_agent(db, request, chat_session, current_user)
    else:
        # 3.3 没session_id：确保指定的agent存在且对当前用户可见
        _ensure_chat_agent_available(db, request.tenant_id, request.agent_id, current_user)
    # 4. 校验租户存在
    ensure_tenant(db, request.tenant_id)
    # 5. 校验消息非空：纯文本消息strip后不能为空，且至少要有文本或附件
    if not request.message.strip() and not request.attachments:
        raise HTTPException(status_code=400, detail="Message cannot be empty")
    # 6. 如果是定时任务模式，先尝试生成定时任务草案
    #    走这条路径会直接返回response，不再调AgentLoop
    if request.session_id:
        scheduled_response = _maybe_handle_scheduled_task_request(db, request, chat_session)
        if scheduled_response:
            # 6.1 拿到草案响应，拆出response和draft（draft用不到用_占位）
            response, _draft = scheduled_response
            # 6.2 异步安排会话标题摘要（LLM生成标题，耗时操作放后台）
            _schedule_session_title_summary(request.tenant_id, request.user_id, response.session_id, request.agent_id)
            # 6.3 直接返回草案响应
            return response
    # 7. 调AgentLoop处理这一轮对话（核心：让Agent生成完整回复）
    response = AgentLoop(db).handle_turn(request)
    # 8. 异步安排会话标题摘要
    _schedule_session_title_summary(request.tenant_id, request.user_id, response.session_id, request.agent_id)
    if request.interaction_mode == "scheduled_task" and request.agent_id:
        # 9. 如果是定时任务模式且指定了agent，再走一次草案检测
        #    这是为了防止_maybe_handle_scheduled_task_request没识别出来时（should_create=False）的兜底
        draft = detect_scheduled_task_draft(
            db,
            request.tenant_id,
            request.agent_id,
            request.user_id,
            request.message,
            response.session_id,            # 注意：用response.session_id（AgentLoop已自动创建会话）
            request.client_timezone,
        )
        # 10. 如果识别出需要创建的草案，就持久化到DB
        if draft and draft.should_create:
            _persist_scheduled_task_draft(db, request.tenant_id, response.session_id, draft)
    # 11. 返回AgentLoop生成的response
    return response


@router.post("/stream")
def chat_stream(
    request: ChatTurnRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> StreamingResponse:
    """处理 SSE 流式聊天对话（数据库中继模式）。

    这是整个系统的核心流式接口，采用「数据库中继」架构：
    1. 启动 Worker 线程执行 AgentLoop，将事件写入 AgentEvent 表。
    2. 返回 StreamingResponse，生成器轮询数据库中继事件给前端。
    3. Worker 和 SSE 生成器通过共享状态（relay_ready/worker_done/worker_terminal）协调。

    支持的流程：
    - 定时任务草案：检测到 scheduled_task 模式时直接生成草案事件。
    - 正常对话：通过 AgentLoop.handle_turn_stream 产生流式事件。
    - 错误处理：Worker 异常时持久化中断事件，SSE 端检测并发送。

    Args:
        request: 聊天轮次请求体。
        current_user: 当前登录用户。
        db: 数据库会话依赖。

    Returns:
        StreamingResponse: SSE 流式响应（media_type="text/event-stream"）。

    Raises:
        HTTPException 400: 消息为空。
        HTTPException 403: 租户不匹配。
    """
    # 1. 鉴权：校验租户ID匹配 + 覆盖user_id为当前用户（防伪造）
    _ensure_request_tenant(request.tenant_id, current_user)
    request = request.model_copy(update={"user_id": current_user.id})
    # 2. 校验租户存在
    ensure_tenant(db, request.tenant_id)
    # 3. 处理会话上下文：有session_id就查会话+绑定agent；没session_id就校验agent
    if request.session_id:
        chat_session = _ensure_chat_session_available(db, request.tenant_id, current_user.id, request.session_id)
        request = _bind_request_to_session_agent(db, request, chat_session, current_user)
    else:
        _ensure_chat_agent_available(db, request.tenant_id, request.agent_id, current_user)
    # 4. 校验消息非空
    if not request.message.strip() and not request.attachments:
        raise HTTPException(status_code=400, detail="Message cannot be empty")

    # 5. 准备SSE中继所需的线程同步原语
    #    relay_ready: 通知SSE生成器Worker已经知道session_id了
    relay_ready = threading.Event()
    #    worker_done: 通知SSE生成器Worker已退出（成功或失败）
    worker_done = threading.Event()
    #    source_session_id: Worker和SSE共享的"当前会话ID"（包装成dict避免nonlocal）
    #    使用dict是因为闭包里需要修改，但nonlocal声明在闭包内才能用，dict更简单
    source_session_id = {"value": request.session_id or ""}
    #    worker_terminal: Worker是否已发送过终止事件（complete/error/cancelled/interrupted）
    worker_terminal = {"seen": False}
    # 6. 拿到"继续会话"时的事件游标（前端SSE能续上之前的进度）
    #    如果是新会话（没session_id）就没游标
    initial_cursor = _latest_event_cursor(db, request.tenant_id, request.session_id) if request.session_id else None

    # 7. 内部辅助函数：设置当前会话ID + 触发relay_ready
    def set_source_session(session_id: str) -> None:
        # 7.1 空session_id直接忽略（不更新）
        if not session_id:
            return
        # 7.2 更新共享session_id（dict的value，闭包可写）
        source_session_id["value"] = session_id
        # 7.3 触发relay_ready，通知SSE生成器可以开始轮询
        relay_ready.set()

    # 8. 如果请求有session_id（继续会话场景），立刻触发relay_ready
    if source_session_id["value"]:
        relay_ready.set()

    # 9. 定义后台Worker线程的执行函数
    def run_stream_worker() -> None:
        # 9.1 用于在finally中重置span sink（避免泄漏到下次调用）
        span_sink_token = None
        # 9.2 顶层try包裹整段执行，最后用finally确保清理
        try:
            # 9.3 Worker独立DB会话（不能共享主线程的Session，跨线程不安全）
            with Session(engine) as worker_db:
                # 9.4 记录当前turn_id的局部变量（AgentLoop生成的事件会设置它）
                span_turn_id = {"value": ""}

                # 9.5 定义span事件持久化回调：把span观测事件写到AgentEvent表
                def persist_span(event_type: str, payload: dict[str, object]) -> None:
                    # 9.5.1 解析出当前session_id（优先用已知的，缺省用request的）
                    session_id = source_session_id["value"] or request.session_id or ""
                    if not session_id:
                        return
                    # 9.5.2 取出turn_id（前面定义的可变容器）
                    turn_id = span_turn_id["value"]
                    # 9.5.3 浅拷贝payload避免污染原对象
                    event_payload = dict(payload)
                    # 9.5.4 如果有turn_id，setdefault到payload（不覆盖已有的）
                    if turn_id:
                        event_payload.setdefault("turn_id", turn_id)
                        event_payload.setdefault("user_message_id", turn_id)
                    # 9.5.5 如果有client_turn_id也补上
                    if request.client_turn_id:
                        event_payload.setdefault("client_turn_id", request.client_turn_id)
                    # 9.5.6 写一条relay_only事件（仅给SSE中继用，不进入业务流）
                    _persist_relay_only_event(
                        worker_db,
                        request.tenant_id,
                        session_id,
                        event_type,
                        event_payload,
                    )

                # 9.6 设置全局span sink（任何LLM调用都会通过persist_span写事件）
                span_sink_token = set_span_sink(persist_span)
                # 9.7 校验租户存在
                ensure_tenant(worker_db, request.tenant_id)
                # 9.8 如果是继续会话（有session_id），重新查会话
                if request.session_id:
                    chat_session = _ensure_chat_session_available(
                        worker_db,
                        request.tenant_id,
                        request.user_id,
                        request.session_id,
                    )
                    # 9.9 如果是定时任务模式，先发2条进度事件（让前端立刻看到进度条）
                    if request.interaction_mode == "scheduled_task":
                        _persist_relay_only_event(
                            worker_db, request.tenant_id, chat_session.id,
                            "stream_status",
                            {"phase": "scheduled_task_intent", "text": "识别定时任务需求"},
                        )
                        _persist_relay_only_event(
                            worker_db, request.tenant_id, chat_session.id,
                            "stream_status",
                            {"phase": "scheduled_task_parse", "text": "解析执行计划"},
                        )
                    # 9.10 尝试生成定时任务草案（可能直接走完流程不调AgentLoop）
                    scheduled_response = _maybe_handle_scheduled_task_request(worker_db, request, chat_session)
                    if scheduled_response:
                        # 9.10.1 草案生成成功：拿response和draft
                        response, draft = scheduled_response
                        # 9.10.2 把session_id同步到SSE共享状态
                        set_source_session(response.session_id)
                        # 9.10.3 解析当前轮的turn_id（事件流里的turn_id用于分组消息）
                        message_id, client_turn_id = _resolve_turn_ids_from_events(
                            worker_db, request.tenant_id, response.session_id, request.client_turn_id or "",
                        )
                        # 9.10.4 构造turn_payload（用于后面所有事件）
                        turn_payload = {
                            "turn_id": message_id,                   # turn_id用首条消息ID
                            "user_message_id": message_id,           # 兼容字段名
                            "client_turn_id": client_turn_id or None, # 前端的轮次跟踪ID
                        }
                        # 9.10.5 写"生成草案"状态事件
                        _persist_relay_only_event(
                            worker_db, request.tenant_id, response.session_id,
                            "stream_status",
                            {
                                "phase": "scheduled_task_draft",
                                "text": "生成定时任务草案",
                                **draft.model_dump(mode="json"),     # 草案详情
                                **turn_payload,                       # turn信息
                            },
                        )
                        # 9.10.6 写"草案内容"事件（前端用来展示草案卡片）
                        _persist_relay_only_event(
                            worker_db, request.tenant_id, response.session_id,
                            "scheduled_task_draft",
                            {**draft.model_dump(mode="json"), **turn_payload},
                        )
                        # 9.10.7 把回复文本按96字符切片，按stream_delta事件推送（模拟流式）
                        for chunk in _reply_chunks(response.reply):
                            _persist_relay_only_event(
                                worker_db, request.tenant_id, response.session_id,
                                "stream_delta",
                                {"content": chunk, **turn_payload},   # 每个chunk都是delta
                            )
                        # 9.10.8 写"流结束"事件
                        _persist_relay_only_event(
                            worker_db, request.tenant_id, response.session_id,
                            "stream_end", turn_payload,
                        )
                        # 9.10.9 写"对话完成"事件（最终响应）
                        _persist_relay_only_event(
                            worker_db, request.tenant_id, response.session_id,
                            "complete",
                            {**response.model_dump(mode="json"), **turn_payload},
                        )
                        # 9.10.10 标记已发终止事件（防止finally误判为中断）
                        worker_terminal["seen"] = True
                        # 9.10.11 异步安排标题摘要
                        _schedule_session_title_summary(
                            request.tenant_id, request.user_id, response.session_id, request.agent_id,
                        )
                        # 9.10.12 草案流程完毕，直接退出Worker
                        return
                # 9.11 正常对话流：调AgentLoop.handle_turn_stream逐事件处理
                for item in AgentLoop(worker_db).handle_turn_stream(request):
                    # 9.11.1 解析事件名和data
                    event_name = str(item["event"])
                    data = item["data"] if isinstance(item.get("data"), dict) else {}
                    # 9.11.2 确定事件的session_id（事件里可能有，缺省用request里的）
                    item_session_id = str(data.get("sessionId") or request.session_id or source_session_id["value"] or "")
                    # 9.11.3 把session_id同步到SSE共享状态
                    if item_session_id:
                        set_source_session(item_session_id)
                    # 9.11.4 处理"会话已创建"事件（首次创建会话时AgentLoop发出）
                    if event_name == "session_created" and item_session_id:
                        _persist_relay_only_event(worker_db, request.tenant_id, item_session_id, event_name, data)
                    # 9.11.5 处理"对话完成"事件：写入 + 标记终止
                    elif event_name == "complete" and item_session_id:
                        _persist_relay_only_event(worker_db, request.tenant_id, item_session_id, event_name, data)
                        worker_terminal["seen"] = True
                    # 9.11.6 处理终止类事件（无需重复写，AgentLoop已写）
                    elif event_name in {"stream_cancelled", "stream_interrupted", "error", "error_occurred"}:
                        worker_terminal["seen"] = True
                    # 9.11.7 特殊处理"用户消息已收到"事件：记录turn_id + 安排标题
                    if item["event"] == "user_message_received":
                        event_source_session_id = str(item["data"].get("sessionId") or request.session_id or "")
                        set_source_session(event_source_session_id)
                        # 设置当前turn_id（后面的span事件会用这个ID）
                        span_turn_id["value"] = str(
                            data.get("turn_id")
                            or data.get("user_message_id")
                            or data.get("message_id")
                            or ""
                        )
                        # 异步安排标题摘要
                        _schedule_session_title_summary(
                            request.tenant_id, request.user_id, event_source_session_id, request.agent_id,
                        )
                        continue    # 这个事件不需要再走下面的complete处理
                    # 9.11.8 特殊处理"对话完成"事件：触发标题摘要 + 写摘要事件
                    if item["event"] == "complete":
                        event_source_session_id = str(item["data"].get("sessionId") or request.session_id or "")
                        _schedule_session_title_summary(
                            request.tenant_id, request.user_id, event_source_session_id, request.agent_id,
                        )
                        # 把已有标题摘要通过SSE推到前端
                        if event_source_session_id:
                            summary_payload = _session_title_summary_payload(worker_db, request.tenant_id, event_source_session_id)
                            if summary_payload:
                                _persist_relay_only_event(
                                    worker_db, request.tenant_id, event_source_session_id,
                                    SESSION_TITLE_SUMMARY_EVENT,
                                    summary_payload,
                                )
                        # 9.11.9 定时任务模式兜底：complete事件后再检查一次草案
                        if request.interaction_mode != "scheduled_task" or not request.agent_id:
                            continue
                        draft = detect_scheduled_task_draft(
                            worker_db, request.tenant_id, request.agent_id, request.user_id,
                            request.message, event_source_session_id or None, request.client_timezone,
                        )
                        if draft and draft.should_create:
                            _persist_scheduled_task_draft(worker_db, request.tenant_id, event_source_session_id, draft)
                            _persist_relay_only_event(
                                worker_db, request.tenant_id, event_source_session_id,
                                "scheduled_task_draft", draft.model_dump(mode="json"),
                            )
        # 10. 普通异常处理（程序错误等）
        except Exception as exc:
            # 10.1 记录异常日志
            logger.exception("chat stream worker failed")
            # 10.2 拿到当前session_id
            session_id = source_session_id["value"] or request.session_id or ""
            if session_id:
                # 10.3 新DB会话里持久化中断事件（当前Session可能已损坏）
                with Session(engine) as error_db:
                    chat_session = error_db.get(ChatSession, session_id)
                    if chat_session:
                        _persist_chat_turn_interrupted(
                            error_db, request.tenant_id, chat_session,
                            request.client_turn_id or "",
                            str(exc) or "stream worker failed",          # 错误信息
                            error_details={
                                "error_type": exc.__class__.__name__,    # 错误类型
                                "error_traceback": traceback.format_exc()[-STREAM_INTERRUPTED_TRACEBACK_CHAR_LIMIT:],  # 截断堆栈
                            },
                        )
                        error_db.commit()
                        worker_terminal["seen"] = True   # 标记终止
                        set_source_session(session_id)    # 同步session_id
        # 11. 基础异常处理（包括KeyboardInterrupt、SystemExit等）
        except BaseException as exc:
            logger.exception("chat stream worker stopped with base exception")
            session_id = source_session_id["value"] or request.session_id or ""
            if session_id:
                with Session(engine) as error_db:
                    chat_session = error_db.get(ChatSession, session_id)
                    if chat_session:
                        _persist_chat_turn_interrupted(
                            error_db, request.tenant_id, chat_session,
                            request.client_turn_id or "",
                            exc.__class__.__name__,                       # 错误类型
                            error_details={
                                "error_type": exc.__class__.__name__,
                                "error_traceback": traceback.format_exc()[-STREAM_INTERRUPTED_TRACEBACK_CHAR_LIMIT:],
                            },
                        )
                        error_db.commit()
                        worker_terminal["seen"] = True
                        set_source_session(session_id)
            # 11.1 Ctrl+C / 系统退出需要再抛出去，不能被吞掉
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
        # 12. finally：无论成功失败都执行的清理
        finally:
            # 12.1 重置span sink（避免污染后续调用）
            if span_sink_token is not None:
                reset_span_sink(span_sink_token)
            # 12.2 检查是否需要补一次"中断"事件（Worker没发终止就退了）
            session_id = source_session_id["value"] or request.session_id or ""
            if session_id and not worker_terminal["seen"]:
                with Session(engine) as final_db:
                    chat_session = final_db.get(ChatSession, session_id)
                    if chat_session:
                        changed = _persist_chat_turn_interrupted(
                            final_db, request.tenant_id, chat_session,
                            request.client_turn_id or "",
                            "stream worker ended before terminal event",  # 异常退出标记
                        )
                        if changed:
                            final_db.commit()
                            set_source_session(session_id)
            # 12.3 通知SSE生成器Worker已退出
            worker_done.set()

    # 13. 启动Worker线程（daemon=True 随主进程退出）
    threading.Thread(target=run_stream_worker, daemon=True).start()

    # 14. 定义SSE事件流生成器（FastAPI每次yield时推一个chunk到客户端）
    def stream_events() -> Iterator[str]:
        # 14.1 允许在闭包内修改initial_cursor（每次循环要更新到最新）
        nonlocal initial_cursor
        # 14.2 等Worker给出session_id（最多等15秒），超时则按source_session_id继续
        relay_ready.wait(15)
        # 14.3 设置空闲超时deadline（每次有新事件会重置）
        deadline = time.monotonic() + STREAM_RELAY_IDLE_TIMEOUT_SECONDS
        # 14.4 上次心跳时间（防止中间网关断连）
        last_heartbeat_at = time.monotonic()
        # 14.5 是否已发送过终止事件（防止重复推）
        terminal_sent = False
        # 14.6 轮询循环：每秒约12次（80ms一次）
        while True:
            # 14.6.1 读当前session_id（Worker可能刚更新过）
            session_id = source_session_id["value"]
            # 14.6.2 标记本轮是否真正推过事件
            emitted = False
            # 14.6.3 如果有session_id，轮询AgentEvent表拿新增事件
            if session_id:
                with Session(engine) as relay_db:    # 每次轮询独立Session
                    rows = _events_after_cursor(relay_db, request.tenant_id, session_id, initial_cursor)
                # 14.6.4 遍历所有新增事件，转成SSE格式推给前端
                for row in rows:
                    event_name, data = _relay_event_payload(row)   # 解码事件
                    initial_cursor = (row.created_at, row.id)       # 更新游标到当前事件
                    emitted = True
                    yield _sse(event_name, data, row.id)            # 推送一个SSE块
                    # 14.6.5 如果是终止事件，标记一下（Worker可能还会发更多）
                    if event_name in STREAM_RELAY_TERMINAL_EVENTS:
                        terminal_sent = True
                # 14.6.6 有新事件时重置超时和心跳时间
                if emitted:
                    deadline = time.monotonic() + STREAM_RELAY_IDLE_TIMEOUT_SECONDS
                    last_heartbeat_at = time.monotonic()
            # 14.6.7 终止事件已发 且 Worker已退出 且 本轮没新事件 → 流结束
            if terminal_sent and worker_done.is_set() and not emitted:
                return
            # 14.6.8 Worker已退出 且 本轮没新事件 → 流结束（即使没发终止事件也退出）
            if worker_done.is_set() and not emitted:
                return
            # 14.6.9 空闲超时（11分钟没新事件）：补一条"中断"事件，继续等
            if time.monotonic() > deadline:
                if session_id:
                    with Session(engine) as timeout_db:
                        chat_session = timeout_db.get(ChatSession, session_id)
                        if chat_session:
                            _persist_chat_turn_interrupted(
                                timeout_db, request.tenant_id, chat_session,
                                request.client_turn_id or "",
                                "stream relay timed out waiting for terminal event",
                            )
                            timeout_db.commit()
                    continue    # 超时后继续下一轮循环（不死循环）
                return            # 没session_id就直接退出
            # 14.6.10 发送心跳（防中间网关断连）
            now = time.monotonic()
            if now - last_heartbeat_at >= STREAM_RELAY_HEARTBEAT_SECONDS:
                last_heartbeat_at = now
                yield _sse(
                    "heartbeat",                              # 事件名固定为heartbeat
                    {
                        "phase": "relay",                    # 标记是"中继"心跳
                        "sessionId": session_id or request.session_id or "",
                    },
                )
            # 14.6.11 睡80ms再下一轮（CPU友好 + 准实时）
            time.sleep(STREAM_RELAY_POLL_SECONDS)

    # 15. 包装成SSE流式响应返回给前端
    return StreamingResponse(stream_events(), media_type="text/event-stream")


@router.post("/sessions/{session_id}/cancel")
def cancel_chat_turn_endpoint(
    session_id: str,
    request: ChatTurnCancelRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, bool]:
    """取消正在进行的聊天对话轮次。

    通过 cancel_chat_turn 发送取消信号，并持久化取消事件。

    Args:
        session_id: 会话 ID。
        request: 取消请求体（包含 turn_id）。
        current_user: 当前登录用户。
        db: 数据库会话依赖。

    Returns:
        dict[str, bool]: 操作结果 ``{"ok": True}``。
    """
    # 1. 鉴权：校验租户匹配
    _ensure_request_tenant(request.tenant_id, current_user)
    # 2. 校验会话存在且当前用户可访问
    chat_session = _ensure_chat_session_available(db, request.tenant_id, current_user.id, session_id)
    # 3. 发送取消信号到全局的Cancel Registry（AgentLoop内部会监听）
    cancel_chat_turn(session_id, request.turn_id)
    # 4. 持久化"已取消"事件到AgentEvent表（前端SSE能感知）
    _persist_chat_turn_cancelled(db, request.tenant_id, chat_session, request.turn_id, current_user.id)
    # 5. 提交事务
    db.commit()
    # 6. 返回成功
    return {"ok": True}


def _persist_chat_turn_cancelled(
    db: Session,
    tenant_id: str,
    chat_session: ChatSession,
    requested_turn_id: str,
    cancelled_by_user_id: str | None = None,
) -> bool:
    """持久化聊天轮次取消事件，并创建对应的取消回复消息。

    通过事件历史匹配 turn_id，确保取消事件关联到正确的用户消息。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        chat_session: 聊天会话对象。
        requested_turn_id: 请求取消的轮次 ID。
        cancelled_by_user_id: 发起取消的用户 ID。

    Returns:
        True 表示成功创建了取消事件。
    """
    # 1. 防御性检查：requested_turn_id去空白后为空就直接返回False
    requested_turn_id = requested_turn_id.strip()
    if not requested_turn_id:
        return False

    # 2. 取出该会话的所有AgentEvent（按时间正序）
    events = db.exec(
        select(AgentEvent)
        .where(AgentEvent.tenant_id == tenant_id, AgentEvent.session_id == chat_session.id)
        .order_by(AgentEvent.created_at)
    ).all()
    # 3. 反向遍历找请求要取消的那条用户消息事件
    #    从最新的事件开始找，能更快找到最近一次匹配的turn
    message_id = ""
    client_turn_id = ""
    for event in reversed(events):
        # 3.1 只看用户消息事件（其他事件不含turn信息）
        if event.event_type != "user_message_received":
            continue
        # 3.2 取出候选的message_id和client_turn_id
        payload = event.payload_json or {}
        candidate_message_id = str(payload.get("message_id") or payload.get("user_message_id") or "").strip()
        candidate_client_turn_id = str(payload.get("client_turn_id") or "").strip()
        # 3.3 如果requested_turn_id匹配这条用户消息的message_id或client_turn_id
        if requested_turn_id in {candidate_message_id, candidate_client_turn_id}:
            message_id = candidate_message_id
            client_turn_id = candidate_client_turn_id
            break    # 找到最近的匹配就退出
    # 4. 如果没找到匹配的用户消息（极端情况），把requested_turn_id当message_id用
    if not message_id:
        message_id = requested_turn_id
        client_turn_id = requested_turn_id

    # 5. 构造turn_id集合（用于匹配后面事件里的turn信息）
    turn_ids = {message_id}
    if client_turn_id:
        turn_ids.add(client_turn_id)
    # 6. 再次遍历所有事件，看是否已存在"已取消"或"assistant已创建"事件
    for event in events:
        # 6.1 只看这两类事件
        if event.event_type not in {"assistant_message_created", "stream_cancelled"}:
            continue
        # 6.2 收集事件里的所有候选turn_id
        payload = event.payload_json or {}
        event_turn_ids = {
            str(payload.get("turn_id") or "").strip(),
            str(payload.get("user_message_id") or "").strip(),
            str(payload.get("message_id") or "").strip(),
            str(payload.get("client_turn_id") or "").strip(),
        }
        # 6.3 匹配判断：事件里的turn_id包含我们要取消的message_id或client_turn_id
        matches_message = bool(message_id and message_id in event_turn_ids)
        matches_client_turn = bool(client_turn_id and client_turn_id in event_turn_ids)
        if not matches_message and not matches_client_turn:
            continue
        # 6.4 如果已经存在"流已取消"事件：直接创建取消的assistant消息，不重复发事件
        if event.event_type == "stream_cancelled":
            return _ensure_cancelled_assistant_message(
                db, tenant_id, chat_session, message_id, client_turn_id,
                event.created_at + timedelta(microseconds=1),    # 时间比事件晚1us
            )
        # 6.5 如果存在"assistant消息已创建"事件：说明Agent已发完这次回复，不需要取消
        return False

    # 7. 没找到已存在的取消/完成事件：创建新的"流已取消"事件
    now = utc_now()
    db.add(
        AgentEvent(
            tenant_id=tenant_id,
            session_id=chat_session.id,
            event_type="stream_cancelled",                       # 事件类型：流已取消
            payload_json={
                "turn_id": message_id,                            # turn_id
                "user_message_id": message_id,                    # 兼容字段
                "client_turn_id": client_turn_id or None,         # 前端轮次跟踪ID
                "phase": "cancelled",                             # 阶段标记
                "text": "已停止生成",                              # 展示文本
                "cancelled_by_user_id": cancelled_by_user_id,     # 取消发起人
            },
            created_at=now,
        )
    )
    # 8. 创建"已停止生成"的assistant消息（让前端消息列表显示这条）
    _ensure_cancelled_assistant_message(
        db, tenant_id, chat_session, message_id, client_turn_id,
        now + timedelta(microseconds=1),                          # 晚1us，保证在取消事件之后
    )
    # 9. 把chat_session状态重置回active（取消后可以继续对话）
    chat_session.status = "active"
    chat_session.updated_at = now
    db.add(chat_session)
    # 10. 返回True表示成功创建
    return True


def _ensure_cancelled_assistant_message(
    db: Session,
    tenant_id: str,
    chat_session: ChatSession,
    user_message_id: str,
    client_turn_id: str,
    created_at,
) -> bool:
    # 1. 校验：用户消息必须存在、属于该租户、属于该会话、且角色是user
    user_message = db.get(Message, user_message_id)
    if not user_message or user_message.tenant_id != tenant_id or user_message.session_id != chat_session.id:
        return False
    if user_message.role != "user":
        return False

    # 2. 构造turn_id集合（用于匹配已有assistant消息）
    turn_ids = {user_message_id}
    if client_turn_id:
        turn_ids.add(client_turn_id)
    # 3. 取出该会话所有assistant消息（按时间正序）
    messages = db.exec(
        select(Message)
        .where(Message.tenant_id == tenant_id, Message.session_id == chat_session.id, Message.role == "assistant")
        .order_by(Message.created_at)
    ).all()
    # 4. 如果已经有assistant消息关联到同一turn，就不再创建（避免重复）
    for message_row in messages:
        metadata = message_row.metadata_json or {}
        # 4.1 收集已有消息的turn_id
        row_turn_ids = {
            str(metadata.get("turn_id") or "").strip(),
            str(metadata.get("user_message_id") or "").strip(),
            str(metadata.get("client_turn_id") or "").strip(),
        }
        # 4.2 如果集合有交集，说明已经发过这一轮的assistant了
        if turn_ids & row_turn_ids:
            return False

    # 5. 创建"已停止生成"的assistant消息
    assistant_message = Message(
        tenant_id=tenant_id,
        session_id=chat_session.id,
        role="assistant",                                  # 角色是assistant
        content=CANCELLED_ASSISTANT_REPLY,                  # 内容是常量"已停止生成"
        metadata_json={
            "turn_id": user_message_id,                    # turn_id
            "user_message_id": user_message_id,            # 兼容字段
            "client_turn_id": client_turn_id or None,      # 前端轮次ID
            "status": "cancelled",                         # 状态标记
        },
        created_at=created_at,
    )
    db.add(assistant_message)
    # 6. 写"assistant消息已创建"事件到AgentEvent表
    db.add(
        AgentEvent(
            tenant_id=tenant_id,
            session_id=chat_session.id,
            event_type="assistant_message_created",        # 事件类型固定
            payload_json={
                "message_id": assistant_message.id,         # 新消息ID
                "assistant_message_id": assistant_message.id,  # 兼容字段
                "user_message_id": user_message_id,        # 触发的用户消息ID
                "turn_id": user_message_id,                # turn_id
                "client_turn_id": client_turn_id or None,  # 前端轮次ID
                "reply": CANCELLED_ASSISTANT_REPLY,         # 回复内容
                "status": "cancelled",                     # 状态
            },
            created_at=created_at,
        )
    )
    # 7. 更新chat_session的summary和updated_at（让列表页能看到"已停止生成"）
    chat_session.summary = f"最近回复：{CANCELLED_ASSISTANT_REPLY}"
    chat_session.updated_at = created_at
    db.add(chat_session)
    # 8. 返回True表示成功创建
    return True


def _persist_chat_turn_interrupted(
    db: Session,
    tenant_id: str,
    chat_session: ChatSession,
    requested_turn_id: str,
    reason: str,
    error_details: dict[str, object] | None = None,
) -> bool:
    """持久化聊天轮次中断事件（用于 Worker 异常/超时场景）。

    检查是否已有终端事件，如果没有则创建中断事件和中断回复消息。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        chat_session: 聊天会话对象。
        requested_turn_id: 中断的轮次 ID。
        reason: 中断原因描述。
        error_details: 错误详情（错误类型、堆栈等）。

    Returns:
        True 表示成功创建了中断事件。
    """
    # 1. 解析要中断的turn：从事件流里反查turn_id对应的message_id和client_turn_id
    message_id, client_turn_id = _resolve_turn_ids_from_events(db, tenant_id, chat_session.id, requested_turn_id)
    # 2. 兜底：解析不到message_id时把requested_turn_id（去空白）当message_id用
    if not message_id:
        message_id = requested_turn_id.strip()
    # 3. 兜底：还是空就放弃处理（无法定位中断目标）
    if not message_id:
        return False
    # 4. 校验这一轮是否已经有终止事件：有了就不再重复创建中断
    if _turn_has_terminal_event(db, tenant_id, chat_session.id, message_id, client_turn_id):
        return False

    # 5. 构造"流已中断"事件的payload
    now = utc_now()
    # 5.1 基础字段：turn_id、阶段、文本、原因（原因截断到2000字符防超长）
    payload = {
        "turn_id": message_id,
        "user_message_id": message_id,
        "client_turn_id": client_turn_id or None,
        "phase": "interrupted",
        "text": "响应生成中断",
        "reason": reason[:2000],
    }
    # 5.2 合并错误详情（如错误类型、堆栈等）
    if error_details:
        payload.update(error_details)
    # 6. 写"流已中断"事件到AgentEvent表
    db.add(
        AgentEvent(
            tenant_id=tenant_id,
            session_id=chat_session.id,
            event_type="stream_interrupted",
            payload_json=payload,
            created_at=now,
        )
    )
    # 7. 创建"本次响应中断"的assistant消息
    _ensure_interrupted_assistant_message(
        db,
        tenant_id,
        chat_session,
        message_id,
        client_turn_id,
        now + timedelta(microseconds=1),
    )
    # 8. 会话状态重置为active（中断后用户可以继续发消息）
    chat_session.status = "active"
    chat_session.updated_at = now
    db.add(chat_session)
    # 9. 返回True表示成功处理
    return True


def _resolve_turn_ids_from_events(
    db: Session,
    tenant_id: str,
    session_id: str,
    requested_turn_id: str,
) -> tuple[str, str]:
    # 1. 兜底空字符串：去空白后为空就返回空元组（调用方据此放弃处理）
    requested_turn_id = requested_turn_id.strip()
    if not requested_turn_id:
        return "", ""
    # 2. 取出该会话所有AgentEvent（按时间正序）
    events = db.exec(
        select(AgentEvent)
        .where(AgentEvent.tenant_id == tenant_id, AgentEvent.session_id == session_id)
        .order_by(AgentEvent.created_at)
    ).all()
    # 3. 反向遍历找请求要查询的那条用户消息（最近的优先）
    for event in reversed(events):
        # 3.1 只看"用户消息已收到"事件（其他事件不含turn信息）
        if event.event_type != "user_message_received":
            continue
        # 3.2 取出候选的message_id和client_turn_id
        payload = event.payload_json or {}
        candidate_message_id = str(payload.get("message_id") or payload.get("user_message_id") or "").strip()
        candidate_client_turn_id = str(payload.get("client_turn_id") or "").strip()
        # 3.3 如果requested_turn_id匹配这条用户消息：返回(message_id, client_turn_id)
        if requested_turn_id in {candidate_message_id, candidate_client_turn_id}:
            return candidate_message_id, candidate_client_turn_id
    # 4. 没找到匹配：兜底把requested_turn_id同时当message_id和client_turn_id返回
    return requested_turn_id, requested_turn_id


def _turn_has_terminal_event(
    db: Session,
    tenant_id: str,
    session_id: str,
    message_id: str,
    client_turn_id: str = "",
) -> bool:
    # 1. 构造turn_id集合（用于事件匹配）：{message_id} + 可选{client_turn_id}
    turn_ids = {message_id}
    if client_turn_id:
        turn_ids.add(client_turn_id)
    # 2. 取出该会话所有AgentEvent（按时间正序）
    events = db.exec(
        select(AgentEvent)
        .where(AgentEvent.tenant_id == tenant_id, AgentEvent.session_id == session_id)
        .order_by(AgentEvent.created_at)
    ).all()
    # 3. 遍历所有事件，看是否已有终止类事件
    #    终止事件：assistant已创建/对话完成/出错/已取消/已中断（5种）
    for event in events:
        # 3.1 只看这5种终止类事件，其他事件跳过
        if event.event_type not in {
            "assistant_message_created",    # Agent回复已发完
            "complete",                       # 对话轮次正常完成
            "error_occurred",                 # 出错
            "stream_cancelled",               # 流被取消
            "stream_interrupted",             # 流被中断
        }:
            continue
        # 3.2 收集事件里的所有候选turn_id
        payload = event.payload_json or {}
        event_turn_ids = {
            str(payload.get("turn_id") or "").strip(),
            str(payload.get("user_message_id") or "").strip(),
            str(payload.get("message_id") or "").strip(),
            str(payload.get("client_turn_id") or "").strip(),
        }
        # 3.3 如果事件turn_id集合和我们查询的turn_id集合有交集：这一轮已终止
        if turn_ids & event_turn_ids:
            return True
    # 4. 没找到终止事件
    return False


def _ensure_interrupted_assistant_message(
    db: Session,
    tenant_id: str,
    chat_session: ChatSession,
    user_message_id: str,
    client_turn_id: str,
    created_at,
) -> bool:
    # 1. 校验：用户消息必须存在、属于该租户、属于该会话、且角色是user
    user_message = db.get(Message, user_message_id)
    if not user_message or user_message.tenant_id != tenant_id or user_message.session_id != chat_session.id:
        return False
    if user_message.role != "user":
        return False

    # 2. 构造turn_id集合（用于匹配已有assistant消息）
    turn_ids = {user_message_id}
    if client_turn_id:
        turn_ids.add(client_turn_id)
    # 3. 取出该会话所有assistant消息（按时间正序）
    messages = db.exec(
        select(Message)
        .where(Message.tenant_id == tenant_id, Message.session_id == chat_session.id, Message.role == "assistant")
        .order_by(Message.created_at)
    ).all()
    # 4. 如果已有assistant消息关联到同一turn，就不重复创建
    for message_row in messages:
        metadata = message_row.metadata_json or {}
        # 4.1 收集已有消息的turn_id
        row_turn_ids = {
            str(metadata.get("turn_id") or "").strip(),
            str(metadata.get("user_message_id") or "").strip(),
            str(metadata.get("client_turn_id") or "").strip(),
        }
        # 4.2 集合有交集说明同一turn已经发过assistant了
        if turn_ids & row_turn_ids:
            return False

    # 5. 创建"本次响应中断"的assistant消息
    assistant_message = Message(
        tenant_id=tenant_id,
        session_id=chat_session.id,
        role="assistant",                                  # 角色是assistant
        content=INTERRUPTED_ASSISTANT_REPLY,               # 内容是常量"本次响应中断，请重试发送。"
        metadata_json={
            "turn_id": user_message_id,                    # turn_id
            "user_message_id": user_message_id,            # 兼容字段
            "client_turn_id": client_turn_id or None,      # 前端轮次ID
            "status": "interrupted",                       # 状态标记
        },
        created_at=created_at,
    )
    db.add(assistant_message)
    # 6. 写"assistant消息已创建"事件
    db.add(
        AgentEvent(
            tenant_id=tenant_id,
            session_id=chat_session.id,
            event_type="assistant_message_created",
            payload_json={
                "message_id": assistant_message.id,         # 新消息ID
                "assistant_message_id": assistant_message.id,  # 兼容字段
                "user_message_id": user_message_id,        # 触发的用户消息ID
                "turn_id": user_message_id,                # turn_id
                "client_turn_id": client_turn_id or None,  # 前端轮次ID
                "reply": INTERRUPTED_ASSISTANT_REPLY,      # 回复内容
                "status": "interrupted",                   # 状态
            },
            created_at=created_at,
        )
    )
    # 7. 更新chat_session的summary和updated_at（让列表页能看到"本次响应中断"）
    chat_session.summary = f"最近回复：{INTERRUPTED_ASSISTANT_REPLY}"
    chat_session.updated_at = created_at
    db.add(chat_session)
    # 8. 返回True表示成功创建
    return True


def _persist_relay_only_event(
    db: Session,
    tenant_id: str,
    session_id: str,
    event_type: str,
    payload: dict[str, object],
) -> None:
    """将中继事件持久化到 AgentEvent 表并立即提交。

    这是 SSE 数据库中继模式的核心写入函数，
    Worker 线程通过此函数将事件写入数据库供 SSE 生成器读取。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        session_id: 会话 ID。
        event_type: 事件类型标识。
        payload: 事件负载数据。
    """
    # 1. 直接追加AgentEvent到db
    db.add(
        AgentEvent(
            tenant_id=tenant_id,
            session_id=session_id,
            event_type=event_type,
            payload_json=payload,
        )
    )
    # 2. 立即提交（让SSE生成器可以立刻轮询到这条事件）
    db.commit()


def _relay_event_payload(row: AgentEvent) -> tuple[str, dict[str, object]]:
    # 1. 浅拷贝payload避免污染原行
    payload = dict(row.payload_json or {})
    # 2. 解析事件名：优先查别名映射（DB名 → 前端名），没有就用原event_type
    event_name = STREAM_RELAY_EVENT_ALIASES.get(row.event_type, row.event_type)
    # 3. 构造data字段（合并标准字段 + 业务payload）
    data: dict[str, object] = {
        "kind": event_name,                                # 事件名
        "sessionId": row.session_id,                        # 关联的会话ID
        "timestamp": row.created_at.isoformat(),            # ISO时间
        "provider": "skill",                                # 来源（前端用来识别skill系统事件）
        **payload,                                          # 合并业务payload
    }
    # 4. 返回(event_name, data)供SSE生成器用
    return event_name, data


def _events_after_cursor(
    db: Session,
    tenant_id: str,
    session_id: str,
    cursor: tuple[object, str] | None,
) -> list[AgentEvent]:
    # 1. 构造基础查询：按租户+会话过滤，排除Span事件（Span不通过SSE中继）
    statement = select(AgentEvent).where(
        AgentEvent.tenant_id == tenant_id,
        AgentEvent.session_id == session_id,
        AgentEvent.event_type.notin_(SPAN_EVENT_TYPES),     # notin_是SQLAlchemy的NOT IN
    )
    # 2. 如果有游标：追加复合游标条件（created_at, id）确保严格顺序
    #    用or_是因为同一时间戳可能有多条事件，需要用id做二级排序
    if cursor:
        last_created_at, last_id = cursor
        statement = statement.where(
            or_(
                AgentEvent.created_at > last_created_at,   # 严格大于时间戳
                (AgentEvent.created_at == last_created_at) & (AgentEvent.id > last_id),  # 同时戳+大id
            )
        )
    # 3. 排序+限制200条返回
    return db.exec(statement.order_by(AgentEvent.created_at, AgentEvent.id).limit(200)).all()


def _latest_event_cursor(db: Session, tenant_id: str, session_id: str) -> tuple[object, str] | None:
    # 1. 查出该会话最新的一条事件（按时间+id倒序，取第一条）
    row = db.exec(
        select(AgentEvent)
        .where(AgentEvent.tenant_id == tenant_id, AgentEvent.session_id == session_id)
        .order_by(AgentEvent.created_at.desc(), AgentEvent.id.desc())
        .limit(1)
    ).first()
    # 2. 没事件就返回None（前端SSE从头开始）
    if not row:
        return None
    # 3. 返回(created_at, id)作为续传起点
    return row.created_at, row.id


def _sse(event: object, data: object, event_id: str | None = None) -> str:
    # 1. 把data序列化成JSON字符串（ensure_ascii=False保留中文）
    payload = json.dumps(data, ensure_ascii=False)
    # 2. 如果有event_id就拼"id: xxx\n"行（SSE客户端断连重连时用Last-Event-ID续传）
    id_line = f"id: {event_id}\n" if event_id else ""
    # 3. 拼成SSE规范格式：id(可选) + event + data + 两个换行（消息分隔）
    return f"{id_line}event: {event}\ndata: {payload}\n\n"


@router.post("/sessions", response_model=ChatSessionRead)
def create_chat_session(
    request: ChatSessionCreateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ChatSessionRead:
    """创建新的聊天会话。

    Args:
        request: 会话创建请求体。
        current_user: 当前登录用户。
        db: 数据库会话依赖。

    Returns:
        ChatSessionRead: 新创建的会话信息。
    """
    # 1. 鉴权：校验租户匹配
    _ensure_request_tenant(request.tenant_id, current_user)
    # 2. 校验租户存在
    ensure_tenant(db, request.tenant_id)
    # 3. 校验Agent存在且对当前用户可见
    _ensure_chat_agent_available(db, request.tenant_id, request.agent_id, current_user)
    # 4. 规范化title（去引号、限长等）
    title = _normalize_title(request.title)
    # 5. 构造新的ChatSession实体
    row = ChatSession(
        id=new_id("session"),                    # 生成以"session"为前缀的唯一ID
        tenant_id=request.tenant_id,             # 租户ID
        user_id=current_user.id,                 # 当前用户ID
        agent_id=request.agent_id,               # 关联的Agent
        title=title,                             # 规范化后的标题
    )
    # 6. 写入DB
    db.add(row)
    db.commit()
    db.refresh(row)    # 刷新拿到DB侧字段
    # 7. 转DTO返回
    return session_read(row)


@router.get("/sessions", response_model=list[ChatSessionRead])
def list_chat_sessions(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[ChatSessionRead]:
    # 1. 鉴权：校验租户匹配
    _ensure_request_tenant(tenant_id, current_user)
    # 2. 校验租户存在
    ensure_tenant(db, tenant_id)
    # 3. 查出该租户+当前用户的所有会话（按更新时间倒序，最新的在前）
    rows = db.exec(
        select(ChatSession)
        .where(ChatSession.tenant_id == tenant_id, ChatSession.user_id == current_user.id)
        .order_by(ChatSession.updated_at.desc())
    ).all()
    # 4. 清理已过期/失效的"已完成"会话（孤立状态恢复）
    _cleanup_stale_completed_sessions(db, tenant_id, rows)
    # 5. 批量查出所有定时任务关联的session_id集合（避免N+1查询）
    scheduled_session_ids = {
        session_id
        for session_id in db.exec(
            select(ScheduledTaskRun.session_id).where(
                ScheduledTaskRun.tenant_id == tenant_id,
                ScheduledTaskRun.user_id == current_user.id,
                ScheduledTaskRun.session_id.is_not(None),    # 排除null值
            )
        ).all()
        if session_id    # 再过滤一次None
    }
    # 6. 逐个转DTO返回（用set查O(1)判断is_scheduled）
    return [session_read(row, is_scheduled=row.id in scheduled_session_ids) for row in rows]


@router.put("/sessions/{session_id}", response_model=ChatSessionRead)
def rename_chat_session(
    session_id: str,
    request: ChatSessionUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> ChatSessionRead:
    # 1. 鉴权：校验租户匹配
    _ensure_request_tenant(request.tenant_id, current_user)
    # 2. 查出该会话（同时校验用户归属）
    row = _get_user_chat_session(db, request.tenant_id, current_user.id, session_id)
    # 3. 更新title（先规范化再赋值）
    row.title = _normalize_title(request.title)
    row.updated_at = utc_now()    # 更新时间戳
    db.add(row)
    # 4. 提交并刷新
    db.commit()
    db.refresh(row)
    # 5. 查询该会话是否关联了定时任务（用于DTO is_scheduled字段）
    is_scheduled = db.exec(
        select(ScheduledTaskRun.id).where(
            ScheduledTaskRun.tenant_id == request.tenant_id,
            ScheduledTaskRun.user_id == current_user.id,
            ScheduledTaskRun.session_id == row.id,
        )
    ).first() is not None    # 存在就是True
    # 6. 转DTO返回
    return session_read(row, is_scheduled=is_scheduled)


@router.delete("/sessions/{session_id}")
def delete_chat_session(
    session_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict[str, str]:
    # 1. 鉴权：校验租户匹配
    _ensure_request_tenant(tenant_id, current_user)
    # 2. 查出该会话（同时校验用户归属）
    row = _get_user_chat_session(db, tenant_id, current_user.id, session_id)
    # 3. 级联删除4类关联数据：消息、AgentEvent、消息反馈、Skill反馈
    # 3.1 删除所有消息
    messages = db.exec(
        select(Message).where(Message.tenant_id == tenant_id, Message.session_id == session_id)
    ).all()
    # 3.2 删除所有AgentEvent（流式事件、Span等）
    events = db.exec(
        select(AgentEvent).where(AgentEvent.tenant_id == tenant_id, AgentEvent.session_id == session_id)
    ).all()
    # 3.3 删除所有消息级反馈（赞/踩）
    feedback_rows = db.exec(
        select(MessageFeedback).where(MessageFeedback.tenant_id == tenant_id, MessageFeedback.session_id == session_id)
    ).all()
    # 3.4 删除所有技能级反馈
    skill_feedback_rows = db.exec(
        select(SkillFeedback).where(SkillFeedback.tenant_id == tenant_id, SkillFeedback.session_id == session_id)
    ).all()
    # 4. 逐个删除（先删依赖，最后删主表）
    for message in messages:
        db.delete(message)
    for event in events:
        db.delete(event)
    for feedback in feedback_rows:
        db.delete(feedback)
    for feedback in skill_feedback_rows:
        db.delete(feedback)
    # 5. 最后删主表（ChatSession本身）
    db.delete(row)
    # 6. 一次性提交
    db.commit()
    # 7. 返回成功
    return {"status": "deleted"}


@router.get("/sessions/{session_id}/messages", response_model=list[MessageRead])
def list_chat_messages(
    session_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[MessageRead]:
    # 1. 鉴权：校验租户匹配
    _ensure_request_tenant(tenant_id, current_user)
    # 2. 校验该会话对当前用户可读（不一定是自己创建的——可能共享给他人）
    chat_session = _get_readable_chat_session(db, tenant_id, current_user, session_id)
    # 3. 清理可能失效的"已完成"状态
    _cleanup_stale_completed_sessions(db, tenant_id, [chat_session])
    # 4. 取出该会话所有消息（按时间正序）
    rows = db.exec(
        select(Message)
        .where(Message.tenant_id == tenant_id, Message.session_id == session_id)
        .order_by(Message.created_at)
    ).all()
    # 5. 取出"用户消息已收到"和"assistant消息已创建"两类事件（用于反查turn_id）
    events = db.exec(
        select(AgentEvent)
        .where(
            AgentEvent.tenant_id == tenant_id,
            AgentEvent.session_id == session_id,
            AgentEvent.event_type.in_(["user_message_received", "assistant_message_created"]),  # type: ignore[attr-defined]
        )
        .order_by(AgentEvent.created_at)
    ).all()
    # 6. 从事件流反查每条消息的turn_id
    turn_ids_by_message = _message_turn_ids_from_events(events)
    # 7. 批量查出当前用户对所有消息的赞/踩（避免N+1查询）
    feedback_by_message = _feedback_by_message(db, tenant_id, current_user.id, [row.id for row in rows])
    # 8. 逐条调message_read转DTO返回（带turn_id、feedback、引用富化）
    return [message_read(row, feedback_by_message.get(row.id), turn_ids_by_message.get(row.id), db) for row in rows]


@router.get("/sessions/{session_id}/events")
def list_chat_session_events(
    session_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[dict]:
    # 1. 鉴权：校验租户匹配
    _ensure_request_tenant(tenant_id, current_user)
    # 2. 校验会话可读
    _get_readable_chat_session(db, tenant_id, current_user, session_id)
    # 3. 取出该会话所有事件（按时间正序，最多500条防超长）
    rows = db.exec(
        select(AgentEvent)
        .where(
            AgentEvent.tenant_id == tenant_id,
            AgentEvent.session_id == session_id,
        )
        .order_by(AgentEvent.created_at)
        .limit(500)
    ).all()
    # 4. 逐个事件标准化后返回（统一事件名、id、data等字段格式）
    return [_normalized_session_event_payload(row) for row in rows]


@router.get("/handoffs", response_model=list[HumanHandoffRead])
def list_human_handoffs(
    tenant_id: str = Query(...),
    status: str = Query("pending"),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[HumanHandoffRead]:
    # 1. 鉴权：校验租户匹配
    _ensure_request_tenant(tenant_id, current_user)
    # 2. 校验租户存在
    ensure_tenant(db, tenant_id)
    # 3. 构造基础查询：按租户过滤
    stmt = select(HumanHandoffRequest).where(HumanHandoffRequest.tenant_id == tenant_id)
    # 4. 按状态过滤（默认pending；"all"表示全部）
    if status != "all":
        stmt = stmt.where(HumanHandoffRequest.status == status)
    # 5. 权限过滤：非管理员只能看到自己负责的
    if not is_admin_user(current_user):
        if status == "pending":
            # 5.1 待办列表：自己被指派的 + 无人指派的
            stmt = stmt.where(
                or_(
                    HumanHandoffRequest.assignee_user_id == current_user.id,
                    HumanHandoffRequest.assignee_user_id.is_(None),
                )
            )
        else:
            # 5.2 其他状态列表：自己被指派的 + 自己发起的
            stmt = stmt.where(
                or_(
                    HumanHandoffRequest.assignee_user_id == current_user.id,
                    HumanHandoffRequest.requester_user_id == current_user.id,
                )
            )
    # 6. 按更新时间倒序查200条
    rows = db.exec(stmt.order_by(HumanHandoffRequest.updated_at.desc()).limit(200)).all()
    # 7. 逐个转DTO返回
    return [human_handoff_read(row) for row in rows]


@router.post("/handoffs/{handoff_id}/reply", response_model=HumanHandoffRead)
def reply_human_handoff(
    handoff_id: str,
    request: HumanHandoffReplyRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> HumanHandoffRead:
    """回复人工接管请求。

    将人工回复写入数据库，更新会话状态，并异步恢复 Agent 对话。

    Args:
        handoff_id: 接管请求 ID。
        request: 回复请求体。
        current_user: 当前登录用户。
        db: 数据库会话依赖。

    Returns:
        HumanHandoffRead: 更新后的接管请求信息。

    Raises:
        HTTPException 404: 接管请求或关联会话不存在。
        HTTPException 403: 当前用户无权回复该接管请求。
        HTTPException 400: 回复内容为空。
        HTTPException 409: 接管请求不在 pending 状态。
    """
    # 1. 鉴权：校验租户匹配
    _ensure_request_tenant(request.tenant_id, current_user)
    # 2. 校验接管请求存在 + 租户归属（否则404）
    row = db.get(HumanHandoffRequest, handoff_id)
    if not row or row.tenant_id != request.tenant_id:
        raise HTTPException(status_code=404, detail="Handoff request not found")
    # 3. 权限校验：非管理员只能回复分配给自己的（否则403）
    if not is_admin_user(current_user) and row.assignee_user_id not in {None, current_user.id}:
        raise HTTPException(status_code=403, detail="Handoff request not assigned to current user")
    # 4. 校验回复内容非空（去空白后）
    reply = request.reply.strip()
    if not reply:
        raise HTTPException(status_code=400, detail="Reply is required")
    # 5. 校验状态必须是pending（已回复/已关闭的不能再回复，否则409）
    if row.status != "pending":
        raise HTTPException(status_code=409, detail="Handoff request is not pending")
    # 6. 校验关联的会话还存在（否则409）
    chat_session = db.get(ChatSession, row.session_id)
    if not chat_session or chat_session.tenant_id != request.tenant_id:
        raise HTTPException(status_code=409, detail="Original handoff session is not available")

    # 7. 更新接管请求状态为answered + 写回复内容
    now = utc_now()
    row.status = "answered"                              # 状态：已回复
    row.human_reply = reply                              # 人工回复内容
    row.answered_at = now                                # 回复时间
    row.updated_at = now                                 # 更新时间
    # 7.1 把"是谁回复的"记入resume_payload（供AgentLoop恢复时使用）
    row.resume_payload_json = {**(row.resume_payload_json or {}), "answered_by_user_id": current_user.id}
    db.add(row)

    # 8. 把会话状态重置回active（清除待输入状态）
    chat_session.status = "active"                       # 状态：active
    chat_session.awaiting_input_json = None              # 清除待输入标记
    chat_session.summary = f"最近回复：{reply[:120]}"   # 更新列表页摘要
    chat_session.updated_at = now
    db.add(chat_session)

    # 9. 写"接管已回复"事件
    db.add(
        AgentEvent(
            tenant_id=request.tenant_id,
            session_id=row.session_id,
            event_type="human_handoff_answered",          # 事件类型
            payload_json={
                "handoff_id": row.id,                    # 接管ID
                "agent_id": row.agent_id,                # Agent
                "trigger_skill_id": row.trigger_skill_id, # 触发的技能
                "trigger_step_id": row.trigger_step_id,   # 触发的步骤
                "answered_by_user_id": current_user.id,  # 回复人
                "reply_preview": reply[:180],            # 回复预览（截断180字符）
            },
            created_at=now,
        )
    )
    # 10. 一次性提交
    db.commit()
    db.refresh(row)
    # 11. 异步启动Agent恢复（用人工回复作为新一轮用户消息，重新调AgentLoop）
    _resume_human_handoff_async(row.id)
    # 12. 转DTO返回
    return human_handoff_read(row)


@router.post("/messages/{message_id}/feedback")
def upsert_message_feedback(
    message_id: str,
    request: MessageFeedbackRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    # 1. 鉴权：校验租户匹配
    _ensure_request_tenant(request.tenant_id, current_user)
    # 2. 校验消息存在 + 用户可访问
    message_row = _get_feedback_target_message(db, request.tenant_id, current_user.id, message_id)
    # 3. 查现有反馈（同一用户对同一消息的反馈应该只有一条）
    existing = db.exec(
        select(MessageFeedback).where(
            MessageFeedback.tenant_id == request.tenant_id,
            MessageFeedback.message_id == message_id,
            MessageFeedback.user_id == current_user.id,
        )
    ).first()
    # 4. 准备时间戳（用于本轮所有写操作）
    now = utc_now()
    # 5. 决定是更新还是新建
    if existing:
        # 5.1 更新现有反馈：只改rating和分析状态（清空旧分析结果）
        existing.rating = request.rating
        existing.analysis_status = "pending"          # 重置为待分析
        existing.analysis_bucket = None               # 清空分析桶
        existing.analysis_reason = None               # 清空分析原因
        existing.analysis_summary = None              # 清空分析摘要
        existing.analysis_confidence = None           # 清空分析置信度
        existing.analysis_json = {}                   # 清空分析详情
        existing.analyzed_at = None                   # 清空分析时间
        existing.updated_at = now
        row = existing
    else:
        # 5.2 新建反馈：初始状态为待分析
        row = MessageFeedback(
            tenant_id=request.tenant_id,
            session_id=message_row.session_id,
            message_id=message_row.id,
            user_id=current_user.id,
            rating=request.rating,
            analysis_status="pending",
            analysis_json={},
            created_at=now,
            updated_at=now,
        )
    # 6. 写入反馈
    db.add(row)
    # 7. 同步upsert技能级反馈（一个消息可能来自某个技能，技能也要记录）
    _upsert_skill_feedback_for_message(db, request.tenant_id, current_user.id, message_row, request.rating, now)
    # 8. 写"反馈已变更"事件
    db.add(
        AgentEvent(
            tenant_id=request.tenant_id,
            session_id=message_row.session_id,
            event_type="message_feedback_changed",
            payload_json={"message_id": message_row.id, "rating": request.rating, "user_id": current_user.id},
        )
    )
    # 9. 提交并刷新
    db.commit()
    db.refresh(row)
    # 10. 异步入队反馈分析任务（LLM分析为什么赞/踩）
    enqueue_feedback_analysis(row.tenant_id, row.id, row.session_id)
    # 11. 返回简化DTO（包含分析状态供前端轮询）
    return {
        "id": row.id,
        "tenant_id": row.tenant_id,
        "session_id": row.session_id,
        "message_id": row.message_id,
        "rating": row.rating,
        "analysis_status": row.analysis_status,
        "updated_at": row.updated_at.isoformat(),
    }


@router.delete("/messages/{message_id}/feedback")
def delete_message_feedback(
    message_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> dict:
    # 1. 鉴权：校验租户匹配
    _ensure_request_tenant(tenant_id, current_user)
    # 2. 校验消息存在 + 用户可访问
    message_row = _get_feedback_target_message(db, tenant_id, current_user.id, message_id)
    # 3. 查现有反馈（同upsert逻辑：同一用户对同一消息的反馈应唯一）
    existing = db.exec(
        select(MessageFeedback).where(
            MessageFeedback.tenant_id == tenant_id,
            MessageFeedback.message_id == message_id,
            MessageFeedback.user_id == current_user.id,
        )
    ).first()
    # 4. 没反馈就直接返回（删除操作幂等）
    if not existing:
        return {"status": "deleted"}
    # 5. 删消息级反馈
    db.delete(existing)
    # 6. 同步删技能级反馈（保证两级反馈一致）
    _delete_skill_feedback_for_message(db, tenant_id, current_user.id, message_row)
    # 7. 写"反馈已变更"事件（rating=None表示"已撤销"）
    db.add(
        AgentEvent(
            tenant_id=tenant_id,
            session_id=message_row.session_id,
            event_type="message_feedback_changed",
            payload_json={"message_id": message_row.id, "rating": None, "user_id": current_user.id},
        )
    )
    # 8. 提交
    db.commit()
    # 9. 返回成功
    return {"status": "deleted"}


@router.get("/sessions/{session_id}/trace")
def list_chat_session_trace(
    session_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[dict]:
    """获取聊天会话的执行追踪视图。

    将会话中的消息和事件转换为前端可展示的时间线追踪视图，
    展示 Agent 的每一步决策和操作过程。

    Args:
        session_id: 会话 ID。
        tenant_id: 租户ID。
        current_user: 当前登录用户。
        db: 数据库会话依赖。

    Returns:
        list[dict]: 追踪视图列表。
    """
    # 1. 鉴权：校验租户匹配
    _ensure_request_tenant(tenant_id, current_user)
    # 2. 校验会话可读
    _get_readable_chat_session(db, tenant_id, current_user, session_id)
    # 3. 取出该会话所有消息（按时间正序）
    messages = db.exec(
        select(Message)
        .where(Message.tenant_id == tenant_id, Message.session_id == session_id)
        .order_by(Message.created_at)
    ).all()
    # 4. 取出该会话所有事件（按时间正序）
    events = db.exec(
        select(AgentEvent)
        .where(AgentEvent.tenant_id == tenant_id, AgentEvent.session_id == session_id)
        .order_by(AgentEvent.created_at)
    ).all()
    # 5. 一次性查出该租户所有技能的 name（用于事件→技能名映射）
    skills = db.exec(select(Skill).where(Skill.tenant_id == tenant_id)).all()
    skill_names = {skill.skill_id: skill.name for skill in skills}    # skill_id → name 字典
    # 6. 构建追踪视图（消息+事件+技能名 → 时间线条目）
    return _build_turn_traces(messages, events, skill_names)


@router.get("/sessions/{session_id}/spans")
def list_chat_session_spans(
    session_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[dict[str, object]]:
    # 1. 鉴权：校验租户匹配
    _ensure_request_tenant(tenant_id, current_user)
    # 2. 校验会话可读
    _get_readable_chat_session(db, tenant_id, current_user, session_id)
    # 3. 取出该会话所有Span事件（按时间+id正序，确保顺序严格）
    rows = db.exec(
        select(AgentEvent)
        .where(
            AgentEvent.tenant_id == tenant_id,
            AgentEvent.session_id == session_id,
            AgentEvent.event_type.in_(SPAN_EVENT_TYPES),    # 只取Span事件（LLM调用/知识检索埋点）
        )
        .order_by(AgentEvent.created_at, AgentEvent.id)
    ).all()
    # 4. 展平每个事件为 dict：基础字段 + payload_json 字段（避免嵌套）
    return [
        {
            "event_id": row.id,                             # AgentEvent 主键
            "event_type": row.event_type,                   # 事件类型
            "created_at": row.created_at.isoformat(),       # ISO 时间
            **dict(row.payload_json or {}),                 # 合并业务字段
        }
        for row in rows
    ]


def _get_user_chat_session(db: Session, tenant_id: str, user_id: str, session_id: str) -> ChatSession:
    # 1. 校验租户存在
    ensure_tenant(db, tenant_id)
    # 2. 按主键查ChatSession
    row = db.get(ChatSession, session_id)
    # 3. 校验存在 + 租户归属 + 用户归属（任意不符都按404处理，避免信息泄露）
    if not row or row.tenant_id != tenant_id or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="Session not found")
    # 4. 返回会话实体
    return row


def _get_readable_chat_session(db: Session, tenant_id: str, current_user: User, session_id: str) -> ChatSession:
    # 1. 校验租户存在
    ensure_tenant(db, tenant_id)
    # 2. 按主键查ChatSession
    row = db.get(ChatSession, session_id)
    # 3. 校验租户归属（不对就404，避免信息泄露）
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Session not found")
    # 4. 自己是创建者：直接可读
    if row.user_id == current_user.id:
        return row
    # 5. 不是创建者：检查是否人工接管过他（管理员全部可见，非管理员只能看被分配的）
    if _user_can_read_handoff_session(db, tenant_id, current_user, session_id):
        return row
    # 6. 都不符合：404
    raise HTTPException(status_code=404, detail="Session not found")


def _user_can_read_handoff_session(db: Session, tenant_id: str, current_user: User, session_id: str) -> bool:
    # 1. 构造查询：该租户+该会话的所有接管请求
    statement = select(HumanHandoffRequest).where(
        HumanHandoffRequest.tenant_id == tenant_id,
        HumanHandoffRequest.session_id == session_id,
    )
    # 2. 非管理员追加权限过滤：只看被分配给自己的 + 无人分配的 + 自己发起的
    if not is_admin_user(current_user):
        statement = statement.where(
            or_(
                HumanHandoffRequest.assignee_user_id == current_user.id,    # 分配给自己
                HumanHandoffRequest.assignee_user_id.is_(None),             # 无人分配
                HumanHandoffRequest.requester_user_id == current_user.id,   # 自己发起的
            )
        )
    # 3. 存在一条就返回True
    return db.exec(statement).first() is not None


def _ensure_chat_agent_available(
    db: Session,
    tenant_id: str,
    agent_id: str | None,
    current_user: User,
) -> AgentProfile:
    """验证聊天 Agent 可用（活跃、非整体、用户可见）。"""
    # 1. 校验agent_id非空
    if not agent_id:
        raise HTTPException(status_code=400, detail="Agent is required")
    # 2. 校验租户存在
    ensure_tenant(db, tenant_id)
    # 3. 查Agent并多维校验：存在 + 同租户 + 状态active + 非"整体"Agent（chat 只能用具体Agent）
    row = db.get(AgentProfile, agent_id)
    if not row or row.tenant_id != tenant_id or row.status != "active" or row.is_overall:
        raise HTTPException(status_code=404, detail="Agent not available")    # 不存在/未激活/是整体Agent → 404
    # 4. 校验当前用户能否看到这个Agent（按可见性字段）
    if not _chat_agent_visible_to_user(row, current_user):
        raise HTTPException(status_code=403, detail="Agent not available")    # 不可见 → 403
    # 5. 返回Agent实体
    return row


def _bind_request_to_session_agent(
    db: Session,
    request: ChatTurnRequest,
    chat_session: ChatSession,
    current_user: User,
) -> ChatTurnRequest:
    # 1. 如果会话已经绑定Agent：校验请求的agent_id和会话的一致
    if chat_session.agent_id:
        if request.agent_id and request.agent_id != chat_session.agent_id:
            raise HTTPException(status_code=409, detail="Session is already bound to another agent")    # 不匹配 → 409
        # 1.1 匹配/没传：用会话已有的Agent
        return request.model_copy(update={"agent_id": chat_session.agent_id})

    # 2. 会话还没绑定Agent：校验请求的agent_id可用（不存在/未激活/整体Agent都不可用）
    agent = _ensure_chat_agent_available(db, request.tenant_id, request.agent_id, current_user)
    # 3. 绑定到会话（写DB）
    chat_session.agent_id = agent.id
    chat_session.updated_at = utc_now()
    db.add(chat_session)
    db.commit()
    # 4. 返回绑定后的request（拷贝并替换agent_id）
    return request.model_copy(update={"agent_id": agent.id})


def _ensure_chat_session_available(db: Session, tenant_id: str, user_id: str, session_id: str) -> ChatSession:
    """验证聊天会话可用并返回会话对象。"""
    # 1. 校验租户存在
    ensure_tenant(db, tenant_id)
    # 2. 按主键查ChatSession
    row = db.get(ChatSession, session_id)
    # 3. 多维校验：存在 + 租户归属 + 用户归属（任一不符 → 404）
    if not row or row.tenant_id != tenant_id or row.user_id != user_id:
        raise HTTPException(status_code=404, detail="Session not found")    # 不存在/不是你的会话 → 404
    # 4. 返回会话实体
    return row


def _get_feedback_target_message(db: Session, tenant_id: str, user_id: str, message_id: str) -> Message:
    """获取用户可反馈的目标消息（必须是 assistant 消息且属于当前用户会话）。"""
    # 1. 校验租户存在
    ensure_tenant(db, tenant_id)
    # 2. 按主键查Message
    row = db.get(Message, message_id)
    # 3. 多维校验：存在 + 租户归属 + 角色是assistant（用户消息不允许反馈）
    if not row or row.tenant_id != tenant_id or row.role != "assistant":
        raise HTTPException(status_code=404, detail="Message not found")    # 不存在/不是assistant → 404
    # 4. 校验所属会话的归属
    chat_session = db.get(ChatSession, row.session_id)
    if not chat_session or chat_session.tenant_id != tenant_id or chat_session.user_id != user_id:
        raise HTTPException(status_code=404, detail="Message not found")    # 会话不是你的 → 404
    # 5. 返回消息实体
    return row


def _feedback_by_message(
    db: Session,
    tenant_id: str,
    user_id: str,
    message_ids: list[str],
) -> dict[str, str]:
    # 1. 防御：空消息ID列表直接返回空dict（避免不必要的查询）
    if not message_ids:
        return {}
    # 2. 批量查反馈：按租户+用户+消息ID集合一次查完
    rows = db.exec(
        select(MessageFeedback).where(
            MessageFeedback.tenant_id == tenant_id,                       # 租户过滤
            MessageFeedback.user_id == user_id,                           # 用户过滤
            MessageFeedback.message_id.in_(message_ids),  # type: ignore[attr-defined]   # 批量消息ID
        )
    ).all()
    # 3. 转成 {message_id: rating} 字典（前端按消息ID取评分）
    return {row.message_id: row.rating for row in rows}


def _cleanup_stale_completed_sessions(
    db: Session,
    tenant_id: str,
    rows: list[ChatSession],
) -> None:
    # 1. 筛选候选会话：只处理有"激活技能"的会话（没有则不需要清理）
    candidates = [row for row in rows if row.active_skill_id]
    if not candidates:
        return
    # 2. 一次性查出该租户所有"已发布"状态的技能
    skills = list(
        db.exec(
            select(Skill).where(Skill.tenant_id == tenant_id, Skill.status == "published")
        ).all()
    )
    if not skills:
        return
    # 3. 构造AgentLoop（用于调用其私有清理方法）
    loop = AgentLoop(db)
    changed = False    # 标记是否有会话状态变化
    # 4. 遍历候选会话，逐个调用清理方法
    for row in candidates:
        # 4.1 记录清理前的状态快照
        before = (
            row.active_skill_id,
            row.active_step_id,
            json.dumps(row.slots_json or {}, sort_keys=True, ensure_ascii=False),
        )
        # 4.2 调用AgentLoop的私有方法清理"已完成但还挂着"的技能
        loop._finish_stale_completed_skill(tenant_id, row, skills)
        # 4.3 记录清理后的状态
        after = (
            row.active_skill_id,
            row.active_step_id,
            json.dumps(row.slots_json or {}, sort_keys=True, ensure_ascii=False),
        )
        # 4.4 任一会话有变化就标记changed
        changed = changed or before != after
    # 5. 如果有变化：提交DB并刷新每个候选行
    if changed:
        db.commit()
        for row in candidates:
            db.refresh(row)


def _upsert_skill_feedback_for_message(
    db: Session,
    tenant_id: str,
    user_id: str,
    message_row: Message,
    rating: str,
    now,
) -> None:
    # 1. 解析消息的"激活技能上下文"（如果消息不来自技能就跳过）
    skill_context = _active_skill_context_for_assistant_message(db, tenant_id, message_row)
    if not skill_context:
        return    # 消息不是技能产出的，不写技能级反馈
    # 2. 从上下文中抽出skill_id、skill_version、step_id
    skill_id = skill_context["skill_id"]    # 技能ID（必有）
    skill_version = skill_context.get("skill_version")    # 技能版本（可空）
    step_id = skill_context.get("node_id") or skill_context.get("step_id")    # 节点ID
    # 3. 查现有技能反馈（同用户对同一消息的反馈应唯一）
    existing = db.exec(
        select(SkillFeedback).where(
            SkillFeedback.tenant_id == tenant_id,
            SkillFeedback.message_id == message_row.id,
            SkillFeedback.user_id == user_id,
        )
    ).first()
    # 4. 有就更新
    if existing:
        existing.skill_id = skill_id
        existing.skill_version = skill_version
        existing.step_id = step_id
        existing.rating = rating
        existing.updated_at = now
        db.add(existing)
        return
    # 5. 没有就新建
    db.add(
        SkillFeedback(
            tenant_id=tenant_id,
            skill_id=skill_id,
            skill_version=skill_version,
            step_id=step_id,
            session_id=message_row.session_id,
            message_id=message_row.id,
            user_id=user_id,
            rating=rating,
            created_at=now,
            updated_at=now,
        )
    )


def _delete_skill_feedback_for_message(
    db: Session,
    tenant_id: str,
    user_id: str,
    message_row: Message,
) -> None:
    # 1. 查现有技能反馈（按消息+用户定位）
    existing = db.exec(
        select(SkillFeedback).where(
            SkillFeedback.tenant_id == tenant_id,
            SkillFeedback.message_id == message_row.id,
            SkillFeedback.user_id == user_id,
        )
    ).first()
    # 2. 有就删除（删除操作幂等，没有直接跳过）
    if existing:
        db.delete(existing)


def _active_skill_for_assistant_message(db: Session, tenant_id: str, message_row: Message) -> str | None:
    # 1. 委托给_active_skill_context_for_assistant_message拿完整上下文
    context = _active_skill_context_for_assistant_message(db, tenant_id, message_row)
    # 2. 有上下文就返回skill_id；没有就返回None
    return context["skill_id"] if context else None


def _active_skill_context_for_assistant_message(
    db: Session, tenant_id: str, message_row: Message
) -> dict[str, str | None] | None:
    """从事件历史中重建 assistant 消息对应的技能上下文。

    通过回溯用户消息和关联的 AgentEvent 事件，推断出该 assistant 回复
    是由哪个技能、哪个步骤、哪个版本产生的。用于消息反馈的技能归属。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        message_row: 目标 assistant 消息。

    Returns:
        包含 skill_id/skill_version/node_id 的上下文字典，无法推断时返回 None。
    """
    # 1. 取出该会话所有消息（按时间正序）
    messages = db.exec(
        select(Message)
        .where(Message.tenant_id == tenant_id, Message.session_id == message_row.session_id)
        .order_by(Message.created_at)
    ).all()
    # 2. 在消息列表中找到目标消息的索引（用于反查"上一次用户消息"）
    target_index = next((index for index, item in enumerate(messages) if item.id == message_row.id), -1)
    if target_index < 0:
        return None    # 目标消息不在该会话 → 无法推断
    # 3. 在目标消息之前，找最近的一条user消息（assistant 回复总对应"上一次user提问"）
    user_message = next(
        (item for item in reversed(messages[:target_index]) if item.role == "user"),
        None,
    )
    if not user_message:
        return None    # 前面没有user消息 → 没有触发源 → 无法推断

    # 4. 取出该会话所有AgentEvent（按时间正序）
    events = db.exec(
        select(AgentEvent)
        .where(AgentEvent.tenant_id == tenant_id, AgentEvent.session_id == message_row.session_id)
        .order_by(AgentEvent.created_at)
    ).all()
    # 5. 状态变量
    collecting = False                                       # 是否已找到目标user_message，开始收集事件
    last_context: dict[str, str | None] | None = None        # 最近一次推断出的技能上下文
    skill_hint: str | None = None                            # 技能ID提示（用于跨事件关联）

    # 6. 遍历事件，追踪到目标assistant消息为止
    for event in events:
        payload = event.payload_json or {}
        # 6.1 user_message_received事件：标记是否开始收集
        if event.event_type == "user_message_received":
            event_message_id = str(payload.get("message_id") or payload.get("user_message_id") or "").strip()
            collecting = bool(event_message_id and event_message_id == user_message.id)
            # 重置状态：进入新一轮时清掉旧上下文和hint
            last_context = None if collecting else last_context
            skill_hint = None if collecting else skill_hint
            continue
        if not collecting:
            continue
        # 6.2 router_decision_created事件：尝试提取目标技能作为hint
        if event.event_type == "router_decision_created":
            target_skill_id = str(payload.get("target_skill_id") or "").strip()
            if target_skill_id:
                skill_hint = target_skill_id
        # 6.3 解析事件上下文
        event_context = _skill_context_from_event(event, skill_hint=skill_hint)
        if event_context:
            last_context = event_context
            # 更新hint为最新的skill_id（用于后续事件推断）
            if event_context.get("skill_id"):
                skill_hint = event_context["skill_id"]
        # 6.4 assistant_message_created事件：检查是不是目标assistant消息
        if event.event_type == "assistant_message_created":
            assistant_message_id = str(
                payload.get("message_id") or payload.get("assistant_message_id") or ""
            ).strip()
            if assistant_message_id == message_row.id:
                return _fill_skill_context_version(db, tenant_id, last_context)
            continue
    # 7. 循环结束都没找到：返回最后推断的上下文（兜底）
    return _fill_skill_context_version(db, tenant_id, last_context)


def _skill_id_from_event(event: AgentEvent) -> str | None:
    context = _skill_context_from_event(event)
    return context["skill_id"] if context else None


def _skill_context_from_event(event: AgentEvent, skill_hint: str | None = None) -> dict[str, str | None] | None:
    # 1. 取出payload（默认空dict避免NoneType）
    payload = event.payload_json or {}
    # 2. 技能切换类事件（开始/恢复/换步骤）：3种
    if event.event_type in {"skill_started", "skill_resumed", "skill_step_changed"}:
        # 2.1 抽出skill_id（to→from→hint三级兜底）
        skill_id = str(payload.get("to_skill_id") or payload.get("from_skill_id") or skill_hint or "") or None
        if not skill_id:
            return None
        # 2.2 抽出skill_version
        skill_version = str(payload.get("to_skill_version") or payload.get("from_skill_version") or "") or None
        # 2.3 抽出node_id（兼容多种字段名）
        node_id = str(
            payload.get("to_node_id")
            or payload.get("from_node_id")
            or payload.get("to_step_id")
            or payload.get("from_step_id")
            or ""
        ) or None
        return {"skill_id": skill_id, "skill_version": skill_version, "node_id": node_id}
    # 3. 技能完成事件：skill_completed
    if event.event_type == "skill_completed":
        skill_id = str(payload.get("skill_id") or "") or None
        if not skill_id:
            return None
        return {
            "skill_id": skill_id,
            "skill_version": str(payload.get("skill_version") or "") or None,
            "node_id": str(payload.get("node_id") or payload.get("step_id") or "") or None,
        }
    # 4. 反思决策事件：reflection_decision_created（反射也可以切技能）
    if event.event_type == "reflection_decision_created":
        skill_id = str(payload.get("target_skill_id") or "") or None
        if not skill_id:
            return None
        return {
            "skill_id": skill_id,
            "skill_version": str(payload.get("target_skill_version") or "") or None,
            "node_id": str(payload.get("target_node_id") or payload.get("target_step_id") or "") or None,
        }
    # 5. 其他事件类型不携带技能上下文
    return None


def _fill_skill_context_version(
    db: Session, tenant_id: str, context: dict[str, str | None] | None
) -> dict[str, str | None] | None:
    # 1. 防御：context为空或已有版本号 → 直接返回
    if not context or context.get("skill_version"):
        return context
    # 2. 防御：context没有skill_id → 无法补版本
    skill_id = context.get("skill_id")
    if not skill_id:
        return context
    # 3. 按skill_id查Skill实体，补全版本号
    skill = db.exec(select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id == skill_id)).first()
    if skill:
        return {**context, "skill_version": skill.version}    # 找到技能：用Skill.version填充
    # 4. 没找到技能：保持context原样返回
    return context


def _trace_payload_text(value: object) -> str:
    # 1. None或空字符串 → 返回空字符串
    if value is None or value == "":
        return ""
    # 2. 字符串类型：尝试解析成JSON重新格式化（缩进2，便于追踪展示）
    if isinstance(value, str):
        try:
            return json.dumps(json.loads(value), ensure_ascii=False, indent=2)    # JSON字符串 → 格式化展示
        except Exception:
            return value    # 不是合法JSON → 原样返回
    # 3. 其他类型（dict/list等）：直接序列化为格式化JSON
    return json.dumps(value, ensure_ascii=False, indent=2)


def _trace_payload_language(value: str) -> str:
    # 1. 空字符串 → 视为纯文本
    if not value.strip():
        return "text"
    # 2. 尝试解析为JSON：成功就标记为json
    try:
        json.loads(value)
        return "json"
    except Exception:
        return "text"    # 解析失败 → 纯文本


def _general_skill_trace_detail(payload: dict, phase: str) -> str | None:
    # 1. 取出review字段（必须为dict，否则当空dict）
    review = payload.get("review") if isinstance(payload.get("review"), dict) else {}
    # 2. 反思类阶段：展示reason + repair_hint（两个都用"·"分隔）
    if phase.startswith("reflection_"):
        parts = [
            str(review.get("reason") or "").strip(),       # 反思原因
            str(review.get("repair_hint") or "").strip(),  # 修复建议
        ]
        text = " · ".join(part for part in parts if part)
        return text or None    # 全空就返回None
    # 3. 普通阶段：取rationale或text作为主展示
    detail = str(payload.get("rationale") or payload.get("text") or "").strip()
    # 4. 失败阶段：额外追加error/stderr_preview
    if _general_skill_trace_failed(phase):
        error = str(payload.get("error") or payload.get("stderr_preview") or "").strip()
        if error and error not in detail:
            detail = f"{detail} · {error}" if detail else error    # 不重复追加
    return detail or None


def _general_skill_trace_failed(phase: str) -> bool:
    # 判断阶段是否表示失败：含"failed" / 是"code_timeout" / 以"_error"结尾
    return "failed" in phase or phase == "code_timeout" or phase.endswith("_error")


def _error_trace_text(payload: dict, *, interrupted: bool = False) -> str:
    # 1. 取出错误码
    code = str(payload.get("code") or "").strip()
    # 2. LLM调用失败 → 固定文案
    if code == "LLM_ERROR":
        return "模型调用失败"
    # 3. 被打断 → 固定文案
    if interrupted:
        return "响应生成中断"
    # 4. 有错误码 → 用错误码描述
    if code:
        return f"执行失败 {code}"
    # 5. 没错误码但有错误类型 → 用错误类型描述
    error_type = str(payload.get("error_type") or "").strip()
    if error_type:
        return f"执行失败 {error_type}"
    # 6. 兜底：只返回"执行失败"
    return "执行失败"


def _error_trace_detail(payload: dict) -> str | None:
    # 1. 抽出三个字段：code / error_type / message（按优先级取 message）
    code = str(payload.get("code") or "").strip()
    error_type = str(payload.get("error_type") or "").strip()
    message = str(payload.get("message") or payload.get("reason") or payload.get("text") or "").strip()
    # 2. 用"·"连接非空部分
    parts = [code, error_type, message]
    detail = " · ".join(part for part in parts if part)
    # 3. 截断到2000字符（防超长）；全空就返回None
    return detail[:2000] if detail else None


def _general_skill_trace_output(payload: dict, phase: str) -> dict[str, str]:
    # 1. 标准输出片段
    if phase == "stdout_chunk":
        output = _trace_payload_text(payload.get("stdout_preview") or payload.get("text"))
        return {
            "output": output,
            "outputLanguage": _trace_payload_language(output),
            "outputTitle": "查看运行输出",        # 标题
        } if output else {}    # 空就返回空dict
    # 2. 错误输出片段
    if phase == "stderr_chunk":
        output = _trace_payload_text(payload.get("stderr_preview") or payload.get("text"))
        return {
            "output": output,
            "outputLanguage": _trace_payload_language(output),
            "outputTitle": "查看错误输出",
        } if output else {}
    # 3. 代码执行完成 / 超时
    if phase in {"code_finished", "code_timeout"}:
        result: dict[str, object] = {}
        # 3.1 依次收集4种可选字段：return_code / structured_result / stdout / stderr
        if "return_code" in payload:
            result["return_code"] = payload.get("return_code")
        if "structured_result" in payload:
            result["structured_result"] = payload.get("structured_result")
        if str(payload.get("stdout_preview") or "").strip():
            result["stdout"] = payload.get("stdout_preview")
        if str(payload.get("stderr_preview") or "").strip():
            result["stderr"] = payload.get("stderr_preview")
        # 3.2 result为空时退回用单独的stdout/stderr
        output = _trace_payload_text(result if result else payload.get("stdout_preview") or payload.get("stderr_preview"))
        return {
            "output": output,
            "outputLanguage": _trace_payload_language(output),
            "outputTitle": "查看超时结果" if phase == "code_timeout" else "查看执行结果",
        } if output else {}
    # 4. 反思类阶段
    if phase.startswith("reflection_"):
        result: dict[str, object] = {}
        # 4.1 收集4种可选字段：structured_result / review / stdout / stderr
        if "structured_result" in payload:
            result["structured_result"] = payload.get("structured_result")
        if "review" in payload:
            result["review"] = payload.get("review")
        if str(payload.get("stdout_preview") or "").strip():
            result["stdout"] = payload.get("stdout_preview")
        if str(payload.get("stderr_preview") or "").strip():
            result["stderr"] = payload.get("stderr_preview")
        output = _trace_payload_text(result)
        return {
            "output": output,
            "outputLanguage": _trace_payload_language(output),
            "outputTitle": "查看校验详情",
        } if result and output else {}    # result非空且output非空才展示
    # 5. 其他阶段不返回输出
    return {}


def _ensure_request_tenant(tenant_id: str, current_user: User) -> None:
    """验证请求的租户 ID 与当前用户的租户 ID 匹配。"""
    # 1. 不匹配就 403（避免跨租户越权访问）
    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")


def _chat_agent_visible_to_user(row: AgentProfile, user: User) -> bool:
    """判断 Agent 在聊天侧是否对用户可见（管理员、拥有者或已发布到画廊）。"""
    # 1. 管理员对所有Agent可见
    if is_admin_user(user):
        return True
    # 2. 非管理员：必须是Agent拥有者 或 Agent已发布到画廊（这两种之一即可）
    metadata = row.metadata_json or {}
    return agent_owned_by_user(row, user) or metadata.get("published_to_gallery") is True


def _normalize_title(value: str | None) -> str | None:
    # 1. None原样返回（允许不传title）
    if value is None:
        return None
    # 2. 去空白
    title = value.strip()
    # 3. 空字符串（含空白字符）报错（避免"无标题"会话）
    if not title:
        raise HTTPException(status_code=400, detail="Session title cannot be empty")
    # 4. 截断到80字符（防止超长title撑爆DB）
    return title[:80]


def _message_turn_ids_from_events(events: list[AgentEvent]) -> dict[str, str]:
    # 1. 初始化空字典（key=assistant_message_id, value=turn_id）
    turn_ids: dict[str, str] = {}
    # 2. 遍历所有事件，按类型分别处理
    for event in events:
        payload = event.payload_json or {}
        # 2.1 user_message_received事件：记录 user_message_id → 自己（user的消息自身就是turn起点）
        if event.event_type == "user_message_received":
            message_id = str(payload.get("message_id") or payload.get("user_message_id") or "").strip()
            if message_id:
                turn_ids[message_id] = message_id
            continue
        # 2.2 assistant_message_created事件：记录 assistant_message_id → 该assistant对应的turn_id
        if event.event_type == "assistant_message_created":
            assistant_message_id = str(
                payload.get("message_id") or payload.get("assistant_message_id") or ""
            ).strip()
            explicit_turn_id = str(payload.get("turn_id") or payload.get("user_message_id") or "").strip()
            if assistant_message_id and explicit_turn_id:
                turn_ids[assistant_message_id] = explicit_turn_id
    return turn_ids


def _build_turn_traces(
    messages: list[Message],
    events: list[AgentEvent],
    skill_names: dict[str, str],
) -> list[dict]:
    """从消息和事件列表构建对话轮次追踪视图。

    将 AgentEvent 按对话轮次（turn）分组，每个轮次包含一个时间线（lines），
    展示 Agent 的决策过程（路由判断、SOP 执行、工具调用、知识检索等）。

    Args:
        messages: 会话中的所有消息。
        events: 会话中的所有 AgentEvent。
        skill_names: 技能 ID 到名称的映射。

    Returns:
        list[dict]: 追踪视图列表，每条包含 turn_id、时间戳和 lines。
    """
    # 1. 没事件直接返回空（空会话没有追踪）
    if not events:
        return []

    # 2. 准备状态变量
    # 2.1 user_message字典：按message_id快速查"对应的user消息"
    user_messages_by_id = {message.id: message for message in messages if message.role == "user"}
    # 2.2 traces：所有轮次追踪的列表（顺序保留）
    traces: list[dict] = []
    # 2.3 traces_by_turn_id：按turn_id查追踪（O(1)查找）
    traces_by_turn_id: dict[str, dict] = {}
    # 2.4 skill_hints_by_turn_id：按turn_id记录最近使用的skill_id（用于跨事件关联）
    skill_hints_by_turn_id: dict[str, str | None] = {}
    # 2.5 active_turn_id：当前正在进行的轮次ID（用户发新消息时更新）
    active_turn_id: str | None = None

    # 3. 遍历事件，分类处理
    for event in events:
        payload = event.payload_json or {}
        # 3.1 user_message_received事件：开启新一轮
        if event.event_type == "user_message_received":
            text = str(payload.get("message") or "")
            message_id = str(payload.get("message_id") or payload.get("user_message_id") or "").strip()
            user_message = user_messages_by_id.get(message_id) if message_id else None
            turn_id = message_id or event.id
            active_turn_id = turn_id
            current = {
                "turn_id": turn_id,
                "user_message_id": message_id or None,
                "_user_message_content": user_message.content if user_message else text,
                "started_at": event.created_at.isoformat(),
                "completed_at": None,
                "lines": [],
            }
            traces.append(current)
            traces_by_turn_id[turn_id] = current
            skill_hints_by_turn_id[turn_id] = None
            continue

        # 3.2 普通事件：反查它属于哪个轮次
        target_turn_id = _event_trace_turn_id(event, active_turn_id)
        if not target_turn_id:
            continue    # 不属于任何已知轮次 → 跳过
        current = traces_by_turn_id.get(target_turn_id)
        if not current:
            continue    # 找不到对应轮次 → 跳过
        # 3.3 router_decision_created事件：更新该轮的skill_hint
        if event.event_type == "router_decision_created":
            target_skill_id = str(payload.get("target_skill_id") or "").strip()
            if target_skill_id:
                skill_hints_by_turn_id[target_turn_id] = target_skill_id

        # 3.4 取出当前skill_hint + 当前追踪是否已结束
        skill_hint = skill_hints_by_turn_id.get(target_turn_id)
        trace_was_completed = bool(current.get("completed_at"))
        # 4. 把事件转成追踪行
        lines = _event_trace_lines(event, skill_names, skill_hint)
        # 4.1 逐行插入或更新；轮次已结束但行还是running → 标记completed
        for line in lines:
            if trace_was_completed and line.get("state") == "running":
                line = {**line, "state": "completed"}
            _upsert_trace_line(current["lines"], line)
        # 4.2 更新skill_hint（用最近推断出的skill_id）
        event_context = _skill_context_from_event(event, skill_hint=skill_hint)
        if event_context and event_context.get("skill_id"):
            skill_hints_by_turn_id[target_turn_id] = event_context["skill_id"]
        # 5. 处理"轮次结束"类事件
        # 5.1 assistant_message_created：正常结束
        if event.event_type == "assistant_message_created":
            if not current.get("completed_at"):
                current["completed_at"] = event.created_at.isoformat()
            _complete_trace_lines(current["lines"])    # 所有running行 → completed
            if active_turn_id == target_turn_id:
                active_turn_id = None    # 清空活动轮次
        # 5.2 取消/中断/出错：异常结束
        elif event.event_type in {"stream_cancelled", "stream_interrupted", "error_occurred"}:
            if not current.get("completed_at"):
                current["completed_at"] = event.created_at.isoformat()
            _finish_trace_if_needed(current, event.created_at)
            if active_turn_id == target_turn_id:
                active_turn_id = None

    # 6. 兜底处理：所有未完成的轮次都用最后事件时间作为完成时间
    fallback_time = events[-1].created_at if events else None
    open_turn_id = active_turn_id    # 当前仍开放的轮次（不强行收尾）
    for current in traces:
        # 6.1 当前开放的轮次不强制结束
        if open_turn_id and current.get("turn_id") == open_turn_id and not current.get("completed_at"):
            continue
        # 6.2 其他未完成的轮次：用fallback_time收尾
        _finish_trace_if_needed(current, fallback_time)

    # 7. 清理临时字段（_user_message_content只是构造时用的，不需要返回）
    for trace in traces:
        trace.pop("_user_message_content", None)
    # 8. 最后合并"定时任务草案"产生的额外追踪行
    return _with_scheduled_draft_message_traces(traces, messages)


def _event_trace_turn_id(event: AgentEvent, _active_turn_id: str | None) -> str | None:
    # 1. 取出payload
    payload = event.payload_json or {}
    # 2. user_message_received事件：用message_id作为turn_id（兜底用event.id）
    if event.event_type == "user_message_received":
        return str(payload.get("message_id") or payload.get("user_message_id") or "").strip() or event.id
    # 3. 普通事件：用payload里的turn_id或user_message_id
    explicit_turn_id = str(payload.get("turn_id") or payload.get("user_message_id") or "").strip()
    if explicit_turn_id:
        return explicit_turn_id
    # 4. 没找到turn_id：返回None（让调用方跳过）
    return None


def _with_scheduled_draft_message_traces(traces: list[dict], messages: list[Message]) -> list[dict]:
    # 1. 收集已追踪的turn_id集合（避免重复）
    traced_turn_ids = {str(trace.get("turn_id") or "") for trace in traces}
    # 2. 复制原traces列表（不修改原对象）
    next_traces = list(traces)
    # 3. 遍历所有消息，找出"含定时任务草案"的assistant消息并补全追踪
    previous_user: Message | None = None    # 上一条user消息（每轮开始时更新）
    for message in messages:
        # 3.1 user消息：更新previous_user
        if message.role == "user":
            previous_user = message
            continue
        # 3.2 非user/assistant消息：跳过
        if message.role != "assistant" or not previous_user:
            continue
        # 3.3 取出metadata中的draft（必须是dict）
        metadata = message.metadata_json or {}
        draft = metadata.get("scheduled_task_draft") if isinstance(metadata, dict) else None
        # 3.4 没有draft 或 已经被追踪过 → 跳过
        if not isinstance(draft, dict) or previous_user.id in traced_turn_ids:
            continue
        # 3.5 追加"定时任务草案"轮次的追踪
        next_traces.append(
            {
                "turn_id": previous_user.id,                          # 用上一条user消息作为turn_id
                "user_message_id": previous_user.id,
                "started_at": previous_user.created_at.isoformat(),
                "completed_at": message.created_at.isoformat(),
                "lines": _scheduled_task_trace_lines(draft),         # 草案专用追踪行
            }
        )
        traced_turn_ids.add(previous_user.id)    # 标记为已追踪
    # 4. 按started_at排序（保证顺序稳定）
    next_traces.sort(key=lambda item: str(item.get("started_at") or ""))
    return next_traces


def _event_trace_lines(event: AgentEvent, skill_names: dict[str, str], skill_hint: str | None = None) -> list[dict]:
    # 1. 委托给_event_trace_line处理单个事件（可能返回None/单行/多行）
    line = _event_trace_line(event, skill_names, skill_hint)
    if not line:
        return []    # 不产生追踪行
    # 2. 规范化：list多行 / 单行 → 都包成list
    lines = line if isinstance(line, list) else [line]
    # 3. 每行设置默认的icon（按事件类型 + line kind 综合判断）
    for item in lines:
        item.setdefault("icon", _event_trace_icon(event, item))
    return lines


def _event_trace_icon(event: AgentEvent, line: dict) -> str:
    # 1. 取出event_type和payload.phase
    event_type = event.event_type
    payload = event.payload_json or {}
    phase = str(payload.get("phase") or "").strip()
    # 2. 路由类事件 → judge图标（"判断中"）
    if event_type in {"router_decision_created", "general_skill_intent_checked"}:
        return "judge"
    # 3. 流状态事件：路由中/意图识别中 → judge
    if event_type == "stream_status" and phase in {"routing", "scheduled_task_intent"}:
        return "judge"
    # 4. general_skill_trace事件：按阶段细分
    if event_type == "general_skill_trace":
        # 4.1 代码生成/运行类阶段 → generated
        if phase in {
            "plan_created", "attempt_started", "running_code",
            "stdout_chunk", "stderr_chunk",
            "code_finished", "code_timeout", "plan_failed",
        }:
            return "generated"
        # 4.2 反思/重试类阶段 → loading
        if phase.startswith("reflection_") or phase == "repair_planning":
            return "loading"
        # 4.3 其他阶段 → advance
        return "advance"
    # 5. Agent循环/反思/异常类事件 → loading
    if event_type in {
        "agent_loop_continued", "agent_loop_completed",
        "reflection_decision_created", "reflection_decision",
        "reflection_skipped", "reflection_retry_started",
        "stream_cancelled", "stream_interrupted", "error_occurred",
    }:
        return "loading"
    # 6. 兜底：按line.kind判断
    kind = str(line.get("kind") or "")
    if kind == "tool":
        return "tool"
    if kind == "code":
        return "generated"
    if kind == "thinking":
        return "loading"
    # 7. 最终兜底
    return "advance"


def _event_trace_line(
    event: AgentEvent, skill_names: dict[str, str], skill_hint: str | None = None
) -> dict | list[dict] | None:
    """将单个 AgentEvent 转换为追踪时间线的一行或多行。

    根据 event_type 和 payload 内容，构建包含 id/kind/text/detail/state 的追踪行。
    不同事件类型有不同的展示逻辑（SOP 状态、路由决策、工具调用、知识检索等）。

    Args:
        event: AgentEvent 对象。
        skill_names: 技能 ID 到名称的映射。
        skill_hint: 当前技能提示（用于补充缺失的技能 ID）。

    Returns:
        dict（单行）、list[dict]（多行）或 None（不展示该事件）。
    """
    # 1. 取出payload（默认空dict，避免NoneType）
    payload = event.payload_json or {}
    # 2. 流状态事件：按phase分派
    if event.event_type == "stream_status":
        phase = str(payload.get("phase") or "").strip()
        text = str(payload.get("text") or "").strip()
        # 2.1 定时任务意图识别阶段
        if phase == "scheduled_task_intent":
            return {
                "id": "scheduled_task_intent",
                "kind": "decision",
                "text": text or "识别定时任务需求",
                "detail": "用户选择了创建定时任务模式",
                "state": "running",
            }
        # 2.2 定时任务计划解析阶段
        if phase == "scheduled_task_parse":
            return {
                "id": "scheduled_task_parse",
                "kind": "decision",
                "text": text or "解析执行计划",
                "detail": None,
                "state": "running",
            }
        # 2.3 定时任务草案生成阶段
        if phase == "scheduled_task_draft":
            return {
                "id": "scheduled_task_draft",
                "kind": "decision",
                "text": text or "生成定时任务草案",
                "detail": _scheduled_task_trace_detail(payload),
                "state": "running",
            }
        # 2.4 意图路由阶段
        if phase == "routing":
            return {
                "id": "decision_router",
                "kind": "decision",
                "text": "判断意图",
                "detail": None,
                "state": "running",
            }
        # 2.5 流状态中的错误
        if phase == "error":
            code = str(payload.get("code") or payload.get("error_type") or "status").strip()
            return {
                "id": f"error_{code}",
                "kind": "decision",
                "text": _error_trace_text(payload),
                "detail": _error_trace_detail(payload),
                "state": "failed",
            }
        # 2.6 正在回复阶段：不展示
        if phase == "responding":
            return None
        # 2.7 决定下一步：main=正常推进，其他=重试
        if phase == "stepping":
            repair_reason = str(payload.get("repair_reason") or "main").strip()
            iteration = payload.get("iteration")
            iteration_suffix = (
                f"_{iteration}" if isinstance(iteration, (int, float, str)) else ""
            )
            return {
                "id": f"decision_stepping_{repair_reason}{iteration_suffix}",
                "kind": "decision",
                "text": "决定下一步" if repair_reason == "main" else "重新分析",
                "detail": None,
                "state": "running",
            }
        # 2.8 正在反思
        if phase == "reflecting":
            return {
                "id": "reflection",
                "kind": "decision",
                "text": "正在反思",
                "detail": None,
                "state": "running",
            }
        # 2.9 知识检索阶段：按命中/未命中展示详情
        if phase in KNOWLEDGE_TRACE_PHASES:
            query = payload.get("query") if isinstance(payload.get("query"), dict) else {}
            detail_parts = [
                f"查询：{query['query']}" if query.get("query") else "",
                f"命中知识图谱 {payload['selected_count']} 个"
                if isinstance(payload.get("selected_count"), int)
                else "",
                f"候选 {payload['candidate_count']} 个"
                if isinstance(payload.get("candidate_count"), int)
                else "",
                f"读取 {payload['chunk_count']} 个片段"
                if isinstance(payload.get("chunk_count"), int)
                else "",
                f"整理 {payload['evidence_count']} 条证据"
                if isinstance(payload.get("evidence_count"), int)
                else "",
            ]
            return {
                "id": _knowledge_trace_line_id(payload),
                "kind": "knowledge",
                "text": text or "检索知识库",
                "detail": " · ".join(part for part in detail_parts if part) or None,
                "state": "completed"
                if phase == "evidence_pack" or phase.startswith("no_") or phase == "okf_only"
                else "running",
            }
        # 2.10 工具调用阶段
        if phase == "tool" and payload.get("tool_name"):
            tool_name = str(payload["tool_name"])
            tool_call_id = str(payload.get("tool_call_id") or tool_name)
            return {
                "id": f"tool_{tool_call_id}",
                "kind": "tool",
                "text": f"正在调用 {tool_name}",
                "detail": None,
                "state": "running",
            }
        # 2.11 其他phase：通用展示
        if phase and phase != "received":
            return {
                "id": f"decision_status_{phase}",
                "kind": "decision",
                "text": text or phase,
                "detail": None,
                "state": "running",
            }
        # 2.12 phase为空或"received"：不展示
        return None
    if event.event_type == "stream_cancelled":
        # 3. 流被取消事件：标记已完成
        return {
            "id": "generation_stopped",
            "kind": "decision",
            "text": "用户已停止生成",
            "detail": None,
            "state": "completed",
        }
    # 4. 流被中断事件：标记失败
    if event.event_type == "stream_interrupted":
        return {
            "id": "generation_interrupted",
            "kind": "thinking",
            "text": _error_trace_text(payload, interrupted=True),
            "detail": _error_trace_detail(payload),
            "state": "failed",
        }
    # 5. 通用技能已选择事件
    if event.event_type == "general_skill_selected":
        skill_name = str(payload.get("skill_name") or payload.get("skill_slug") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        return {
            "id": f"general_skill_selected_{event.id}",
            "kind": "skill",
            "text": f"选择通用技能 {skill_name}" if skill_name else "选择通用技能",
            "detail": reason or None,
            "state": "completed",
        }
    # 6. 通用技能意图识别事件
    if event.event_type == "general_skill_intent_checked":
        skill_name = str(payload.get("skill_name") or payload.get("skill_slug") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        return {
            "id": f"general_skill_intent_{event.id}",
            "kind": "decision",
            "text": "判断意图" if not skill_name else f"判断意图 {skill_name}",
            "detail": reason or None,
            "state": "completed",
        }
    # 7. 通用技能追踪事件：按phase细分
    if event.event_type == "general_skill_trace":
        message = str(payload.get("message") or "").strip()
        phase = str(payload.get("phase") or "").strip()
        # 7.1 正在回复阶段：不展示
        if phase == "replying":
            return None
        # 7.2 其他phase：通用追踪行
        detail = _general_skill_trace_detail(payload, phase)
        output = _general_skill_trace_output(payload, phase)
        code = str(payload.get("code") or "").strip()
        runtime = str(payload.get("runtime") or "").strip().lower()
        code_phases = {
            "plan_created",
            "attempt_started",
            "running_code",
            "stdout_chunk",
            "stderr_chunk",
            "code_finished",
            "code_timeout",
            "plan_failed",
        }
        return {
            "id": f"general_skill_trace_{event.id}",
            "kind": "code" if code or phase in code_phases else "decision",
            "text": message or phase or "执行通用技能",
            "detail": detail or None,
            "code": code or None,
            "language": "bash" if code and runtime == "bash" else "python" if code else None,
            "state": "failed" if _general_skill_trace_failed(phase) else "completed",
            "collapsible": bool(code or output.get("output")),
            **output,
        }
    # 8. 通用技能运行完成事件
    if event.event_type == "general_skill_run_finished":
        success = bool(payload.get("success"))
        return {
            "id": f"general_skill_finished_{event.id}",
            "kind": "skill",
            "text": "通用技能运行完成" if success else "通用技能运行失败",
            "detail": str(payload.get("skill_slug") or "") or None,
            "state": "completed" if success else "failed",
        }
    # 9. SOP技能状态事件（可能返回多行）
    if event.event_type == "skill_state":
        lines = []
        runtime_decision = str(payload.get("runtimeDecision") or "").strip()
        from_skill_id = str(payload.get("fromSkillId") or "").strip()
        to_skill_id = str(payload.get("toSkillId") or "").strip()
        for index, entry in enumerate(payload.get("currentSkills") or []):
            if not isinstance(entry, dict):
                continue
            skill_id = str(entry.get("skillId") or "").strip()
            if not skill_id:
                continue
            name = str(entry.get("name") or skill_id).strip()
            state = str(entry.get("state") or "active").strip()
            # 9.1 按运行时决策决定label（决定SOP图标/文案）
            if state == "suspended":
                label = "挂起SOP"
            elif state == "pending":
                label = "等待SOP"
            elif runtime_decision in {"start_skill", "start_new_task"}:
                label = "选择SOP"
            elif runtime_decision == "suspend_current_and_start_new_skill":
                label = "切换SOP"
            elif (
                runtime_decision
                in {"answer_related_question_then_resume", "answer_chitchat_then_resume"}
                and from_skill_id
                and to_skill_id
                and from_skill_id != to_skill_id
            ):
                label = "切换SOP"
            elif runtime_decision == "exit_current_skill":
                label = "恢复SOP"
            else:
                label = "推进SOP"
            # 9.2 构造追踪行
            step_id = str(entry.get("stepId") or "").strip()
            state_key = step_id or str(index)
            lines.append(
                {
                    "id": f"skill_state_{skill_id}_{state}_{state_key}",
                    "kind": "skill",
                    "text": f"{label} {name}",
                    "detail": f"当前步骤 {step_id}" if step_id else None,
                    "state": "completed" if state == "suspended" else "running",
                }
            )
        # 9.3 没构造出任何行就返回None（不展示）
        return lines or None
    # 10. 定时任务草案创建事件
    if event.event_type == "scheduled_task_draft_created":
        return _scheduled_task_trace_lines(payload)
    # 11. 路由决策事件
    if event.event_type == "router_decision_created":
        intent = str(payload.get("user_intent") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        return {
            "id": "decision_router",
            "kind": "decision",
            "text": f"判断意图 {intent}" if intent else "完成SOP判断",
            "detail": reason or None,
            "state": "completed",
        }
    # 12. 步骤结果事件：决定下一步动作（调工具/查知识/直接推进）
    if event.event_type == "step_result":
        tool_call = payload.get("tool_call") if isinstance(payload.get("tool_call"), dict) else {}
        knowledge_query = payload.get("knowledge_query") if isinstance(payload.get("knowledge_query"), dict) else {}
        next_step_id = str(payload.get("next_step_id") or "").strip()
        reply = str(payload.get("reply") or "").strip()
        raw_tool_name = tool_call.get("name") if isinstance(tool_call, dict) else ""
        raw_knowledge_query = knowledge_query.get("query") if isinstance(knowledge_query, dict) else ""
        tool_name = str(raw_tool_name or "").strip()
        knowledge_query_text = str(raw_knowledge_query or "").strip()
        # 12.1 构造共用detail：下一节点/查询内容/简短回复
        detail = " · ".join(
            part
            for part in (
                f"下一节点 {next_step_id}" if next_step_id else "",
                f"查询：{knowledge_query_text}" if knowledge_query_text else "",
                reply[:80] if not tool_name and not knowledge_query_text and reply else "",
            )
            if part
        )
        # 12.2 决定调用工具
        if tool_name:
            return {
                "id": f"decision_step_tool_{tool_name}",
                "kind": "decision",
                "text": f"决定调用工具 {tool_name}",
                "detail": detail or None,
                "state": "running",
            }
        # 12.3 决定查询知识库
        if knowledge_query_text:
            return {
                "id": "decision_step_knowledge",
                "kind": "decision",
                "text": "决定查询知识库",
                "detail": detail or None,
                "state": "running",
            }
        # 12.4 兜底：决定下一步/完成步骤
        return {
            "id": "decision_step_result",
            "kind": "decision",
            "text": "决定下一步" if next_step_id else "完成步骤判断",
            "detail": detail or None,
            "state": "completed",
        }
    # 13. SOP技能切换事件：选择/恢复/推进
    if event.event_type in {"skill_started", "skill_resumed", "skill_step_changed"}:
        to_skill_id = str(payload.get("to_skill_id") or "")
        from_skill_id = str(payload.get("from_skill_id") or "")
        # 13.1 skill_step_changed且步骤ID未变 → 跳过（重复事件）
        if (
            event.event_type == "skill_step_changed"
            and from_skill_id == to_skill_id
            and str(payload.get("from_step_id") or "")
            == str(payload.get("to_step_id") or "")
        ):
            return None
        # 13.2 取出skill_id（to→from→hint三级兜底）
        skill_id = to_skill_id or from_skill_id or (skill_hint or "")
        if not skill_id:
            return None
        # 13.3 按事件类型选label
        label = {
            "skill_started": "选择SOP",
            "skill_resumed": "恢复SOP",
            "skill_step_changed": "推进SOP",
        }[event.event_type]
        # 13.4 构造detail：切换源 + 当前步骤
        detail_parts = []
        if from_skill_id and from_skill_id != to_skill_id:
            detail_parts.append(f"from {skill_names.get(from_skill_id, from_skill_id)}")
        if payload.get("to_step_id"):
            detail_parts.append(f"step {payload['to_step_id']}")
        step_id = str(payload.get("to_step_id") or payload.get("from_step_id") or "").strip()
        state_key = step_id or "0"
        return {
            "id": f"skill_state_{skill_id}_active_{state_key}",
            "kind": "skill",
            "text": f"{label} {skill_names.get(skill_id, skill_id)}",
            "detail": " · ".join(detail_parts) or None,
            "state": "completed",
        }
    # 14. SOP技能完成事件
    if event.event_type == "skill_completed":
        skill_id = str(payload.get("skill_id") or "")
        return {
            "id": f"skill_{event.id}",
            "kind": "skill",
            "text": f"完成SOP {skill_names.get(skill_id, skill_id)}" if skill_id else "完成SOP",
            "detail": str(payload.get("reason") or "") or None,
            "state": "completed",
        }
    # 15. 工具调用开始事件
    if event.event_type == "tool_call_started":
        name = str(payload.get("name") or "")
        tool_call_id = str(payload.get("tool_call_id") or name or event.id)
        if not name:
            return None    # 没工具名 → 不展示
        return {
            "id": f"tool_{tool_call_id}",
            "kind": "tool",
            "text": f"调用工具 {name}",
            "detail": None,
            "state": "running",
        }
    # 16. 知识查询开始事件
    if event.event_type == "knowledge_query_started":
        query = payload.get("query") if isinstance(payload.get("query"), dict) else {}
        text = str(query.get("query") if isinstance(query, dict) else payload.get("text") or "").strip()
        return {
            "id": _knowledge_trace_line_id(payload),
            "kind": "knowledge",
            "phase": "query",
            "text": "查询业务资料",
            "detail": text or None,
            "state": "running",
        }
    # 17. 知识查询完成/结果事件
    if event.event_type in {"knowledge_query_finished", "knowledge_result"}:
        chunks = payload.get("chunks") if isinstance(payload.get("chunks"), list) else []
        buckets = payload.get("selected_buckets") if isinstance(payload.get("selected_buckets"), list) else []
        concepts = payload.get("selected_concepts") if isinstance(payload.get("selected_concepts"), list) else []
        evidence = payload.get("evidence_pack") if isinstance(payload.get("evidence_pack"), list) else []
        # 17.1 构造4段统计信息
        parts = [
            f"命中 Wiki {len(concepts)} 个" if concepts else "",
            f"展开 {len(buckets)} 个知识桶" if buckets else "",
            f"读取 {len(chunks)} 个片段" if chunks else "",
            f"生成 {len(evidence)} 条引用候选" if evidence else "",
        ]
        return {
            "id": _knowledge_trace_line_id(payload),
            "kind": "knowledge",
            "phase": "result",
            "text": "读取业务资料",
            "detail": " · ".join(part for part in parts if part),
            "state": "completed",
        }
    # 18. 工具结果事件（标准协议）
    if event.event_type == "tool_result":
        content = payload.get("content") if isinstance(payload.get("content"), dict) else {}
        raw_name = str(
            payload.get("rawToolName")
            or payload.get("toolId")
            or content.get("tool_name")
            or ""
        ).strip()
        display_name = str(payload.get("toolName") or raw_name).strip()
        tool_call_id = str(payload.get("toolCallId") or raw_name or event.id)
        success = payload.get("success")
        is_error = bool(payload.get("isError")) if success is None else not bool(success)
        detail_payload = content if isinstance(content, dict) else payload
        return {
            "id": f"tool_{tool_call_id}",
            "kind": "tool",
            "text": f"{'工具调用失败' if is_error else '调用工具'} {display_name}",
            "detail": _tool_trace_detail(detail_payload),
            "state": "failed" if is_error else "completed",
        }
    # 19. 工具调用完成事件（轻量版）
    if event.event_type == "tool_call_finished":
        name = str(payload.get("tool_name") or "")
        tool_call_id = str(payload.get("tool_call_id") or name or event.id)
        success = bool(payload.get("success"))
        return {
            "id": f"tool_{tool_call_id}",
            "kind": "tool",
            "text": f"{'调用工具' if success else '工具调用失败'} {name}",
            "detail": _tool_trace_detail(payload),
            "state": "completed" if success else "failed",
        }
    # 20. Agent循环继续事件：决定继续调用工具
    if event.event_type == "agent_loop_continued":
        iteration = str(payload.get("iteration") or event.id)
        target_tool = str(payload.get("target_tool_name") or "").strip()
        return {
            "id": f"decision_stepping_tool_continuation_{iteration}",
            "kind": "decision",
            "text": "重新分析执行动作",
            "detail": f"决定继续调用工具 {target_tool}" if target_tool else "决定继续调用工具",
            "state": "completed",
        }
    # 21. Agent循环完成事件：判断无需继续
    if event.event_type == "agent_loop_completed":
        iteration = str(payload.get("iteration") or event.id)
        return {
            "id": f"decision_stepping_tool_continuation_{iteration}",
            "kind": "decision",
            "text": "重新分析执行动作",
            "detail": "判断无需继续调用工具",
            "state": "completed",
        }
    # 22. 反思决策事件：继续尝试/反思通过
    if event.event_type in {"reflection_decision_created", "reflection_decision"}:
        needs_retry = bool(payload.get("needs_retry"))
        return {
            "id": "reflection",
            "kind": "decision",
            "text": "反思后继续尝试" if needs_retry else "反思通过",
            "detail": _reflection_trace_detail(payload),
            "state": "completed",
        }
    # 23. 反思跳过事件
    if event.event_type == "reflection_skipped":
        return {
            "id": "reflection",
            "kind": "decision",
            "text": "反思已关闭",
            "detail": str(payload.get("reason") or "") or None,
            "state": "completed",
        }
    # 24. 反思重试开始事件
    if event.event_type == "reflection_retry_started":
        mode = str(payload.get("mode") or "").strip()
        target_tool = str(payload.get("target_tool_name") or "").strip()
        target_skill = str(payload.get("target_skill_id") or "").strip()
        target = target_tool or skill_names.get(target_skill, target_skill)
        return {
            "id": "reflection",
            "kind": "decision",
            "text": f"重试{ '工具' if mode == 'tool' else 'SOP' } {target}".strip(),
            "detail": str(payload.get("reason") or "") or None,
            "state": "completed",
        }
    # 25. 错误事件
    if event.event_type == "error_occurred":
        code = str(payload.get("code") or payload.get("error_type") or event.id).strip()
        return {
            "id": f"error_{code}",
            "kind": "decision",
            "text": _error_trace_text(payload),
            "detail": _error_trace_detail(payload),
            "state": "failed",
        }
    # 26. 未识别的event_type：不展示
    return None


def _tool_trace_detail(payload: dict) -> str | None:
    # 1. 取出data字段（必须为dict，否则当空dict）
    data = payload.get("data")
    data_dict = data if isinstance(data, dict) else {}
    # 2. 构造5段可选描述
    parts = [
        "已复用此前成功结果" if payload.get("idempotent_replay") or data_dict.get("idempotent_replay") else "",  # 是否幂等重放
        str(data_dict.get("source") or "").strip(),                # 数据源
        "未命中" if data_dict.get("found") is False else "已命中" if data_dict.get("found") is True else "",    # 是否命中
        str(data_dict.get("miss_reason") or "").strip(),            # 未命中原因
        str(data_dict.get("recommendation") or "").strip(),         # 建议
    ]
    # 3. 用"·"连接非空段；全空就返回None
    text = " · ".join(part for part in parts if part)
    return text or None


def _reflection_trace_detail(payload: dict) -> str | None:
    # 1. 构造4段可选描述
    parts = [
        str(payload.get("reason") or "").strip(),                                       # 反思原因
        f"工具 {payload['target_tool_name']}" if payload.get("target_tool_name") else "",  # 目标工具
        f"技能 {payload['target_skill_id']}" if payload.get("target_skill_id") else "",     # 目标技能
        f"步骤 {payload['target_step_id']}" if payload.get("target_step_id") else "",     # 目标步骤
    ]
    # 2. 用"·"连接非空段
    text = " · ".join(part for part in parts if part)
    return text or None


def _knowledge_trace_line_id(payload: dict) -> str:
    # 1. 取出query字段（兼容嵌套dict）
    raw_query = payload.get("query")
    if isinstance(raw_query, dict):
        raw_query = raw_query.get("query")
    # 2. 规范化query（合并多余空白）
    query = " ".join(str(raw_query or "").split())
    # 3. 有query就附加到id末尾（让同类查询归到同一行）；没query就用通用id
    return f"knowledge_lookup_{query}" if query else "knowledge_lookup"


def _upsert_trace_line(lines: list[dict], line: dict) -> None:
    # 1. 遍历现有行：按id去重，相同id就更新（用最新内容覆盖）
    for index, item in enumerate(lines):
        if item.get("id") == line.get("id"):
            lines[index] = line
            return
    # 2. 没找到相同id就追加新行
    lines.append(line)


def _complete_trace_lines(lines: list[dict]) -> None:
    # 1. 把所有running状态的行 → completed
    for line in lines:
        if line.get("state") == "running":
            line["state"] = "completed"
    # 2. 特殊处理"思考"行：更新文本+状态
    thinking = next((line for line in lines if line.get("id") == "thinking"), None)
    if thinking:
        thinking["text"] = "已完成思考"
        thinking["state"] = "completed"


def _finish_trace_if_needed(trace: dict, fallback_time) -> None:
    # 1. 没设置completed_at且有fallback_time → 用fallback_time填充
    if not trace.get("completed_at") and fallback_time:
        trace["completed_at"] = fallback_time.isoformat()
    # 2. 完成所有未完成的行
    _complete_trace_lines(trace["lines"])
