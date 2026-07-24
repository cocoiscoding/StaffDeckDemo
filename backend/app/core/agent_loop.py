"""Agent 编排引擎核心模块。

本模块是整个多智能体对话系统的**中枢神经**，定义了 :class:`AgentLoop` 类，
负责将用户的一轮对话（turn）编排为完整的执行管道。

管道流程
========

``handle_turn`` / ``handle_turn_stream`` 是两个对等的主入口，分别用于
**非流式**和**流式**场景，两者共享同一套编排逻辑：

    1. **准备阶段** (:meth:`AgentLoop._prepare_turn`)
       - 获取 / 创建会话、追加用户消息
       - 加载模型配置、可用技能、工具列表
       - 调用 :class:`Router` 进行意图路由，产出 :class:`RouterDecision`
       - 若路由判定走通用技能 (general skill)，直接执行并返回
       - 应用技能决策 (:class:`SkillRuntime`)，推进到目标步骤
       - 调用 :class:`StepAgent` 产出步骤结果 (:class:`StepAgentResult`)
       - 执行知识查询循环 (:meth:`_execute_knowledge_query_cycle`)
       - 执行工具调用循环 (:meth:`_execute_tool_action_cycle`)
       - 执行反思重试 (:meth:`_run_reflection_rounds`)
       - 自动推进技能图 (:meth:`_auto_progress_skill_graph`)

    2. **回复生成** (:meth:`_generate_reply_segment` / :meth:`_generate_reply_stream_segment`)
       - 将路由决策、步骤结果、工具结果等整合，调用
         :class:`ResponseGenerator` 生成最终回复文本

    3. **收尾阶段** (:meth:`_finalize_execution_after_reply` / :meth:`_finalize_turn`)
       - 判定技能是否完成、是否需要人工接管
       - 持久化 assistant 消息、记录事件、入队记忆采集

路由决策类型（9 种，定义于 :class:`RouterDecisionValue`）
========================================================

    - ``continue_active``  —— 继续当前活跃任务
    - ``switch_to_pending`` —— 切换到某个待处理任务
    - ``create_pending`` —— 创建并暂存新的待处理任务
    - ``update_pending`` —— 更新已有待处理任务
    - ``complete_task`` —— 完成当前任务
    - ``start_new_task`` —— 启动新任务
    - ``answer_only`` —— 直接回答（不涉及技能流程）
    - ``handoff_human`` —— 转交人工处理
    - ``clarify`` —— 向用户澄清意图

步骤动作类型（7 种，定义于 :class:`StepAgentResult.action`）
==========================================================

    - ``ask_user`` —— 向用户提问以收集信息
    - ``clarify`` —— 澄清模糊意图
    - ``reply`` —— 直接回复
    - ``advance`` —— 推进到下一步骤
    - ``call_tool`` —— 调用外部工具
    - ``query_knowledge`` —— 检索知识库
    - ``handoff`` —— 请求人工接管

与子组件的关系
==============

    - :class:`Router` — 意图路由，将用户消息映射为路由决策
    - :class:`StepAgent` — 步骤推理，在技能的某个节点上决定下一步动作
    - :class:`SkillRuntime` — 技能运行时，管理技能生命周期、步骤流转、槽位
    - :class:`ReflectionAgent` — 反思代理，评估步骤/工具结果质量并决定是否重试
    - :class:`ResponseGenerator` — 回复生成器，产出最终面向用户的自然语言文本
    - :class:`GeneralSkillRunner` / :class:`GeneralSkillSelector` — 通用技能选择与执行
    - :class:`ToolExecutor` — 工具执行器
    - :class:`MemoryService` — 记忆服务，提供长期记忆上下文
"""

from __future__ import annotations

import json
import queue
import re
import threading
import traceback
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from time import sleep
from types import SimpleNamespace
from typing import Any, Literal

from sqlmodel import Session, select

from app.agents.branching import (
    is_bound_resource_visible_for_agent,
    is_open_gallery_resource,
    model_for_agent,
    visible_knowledge_base_ids,
    visible_published_skills,
    visible_skill,
    visible_tool_rows,
)
from app.core.conversation_context import build_conversation_context
from app.core.cancellation import clear_chat_turn_cancelled, is_chat_turn_cancelled
from app.core.reflection_agent import ReflectionAgent, ReflectionDecision, action_needs_reflection
from app.core.response_generator import (
    FALLBACK_REPLY,
    ResponseGenerator,
    format_runtime_failure_reply,
    model_failure_suggestion,
)
from app.core.router import Router
from app.core.skill_runtime import SkillRuntime
from app.core.step_agent import StepAgent
from app.db.models import (
    AgentEvent,
    AgentProfile,
    AgentResourceBinding,
    ChatSession,
    GeneralSkill,
    HumanHandoffRequest,
    Message,
    ModelConfig,
    PersonaConfig,
    Skill,
    Tool,
    UIConfig,
    User,
    new_id,
    utc_now,
)
from app.general_skills import GeneralSkillRunner, GeneralSkillSelector
from app.general_skills.schema import GeneralSkillRunResponse, GeneralSkillSelection
from app.knowledge import KnowledgeService
from app.knowledge.citations import (
    compact_knowledge_citation_labels,
    knowledge_citations_from_results,
)
from app.knowledge.schema import KnowledgeSearchRequest, KnowledgeSearchResponse
from app.llm import LLMClient, LLMError
from app.llm.model_config_resolver import (
    resolve_model_config_for_runtime,
)
from app.llm.stage_protocol import stage_payload, unified_system_prompt
from app.observability.spans import llm_operation
from app.memory.jobs import enqueue_memory_capture
from app.memory.service import MemoryService, memory_read
from app.observability import EventLog
from app.session.attachments import (
    message_content_with_attachment_context,
    message_images_from_metadata,
)
from app.session.helpers import public_session
from app.session.session_schema import (
    ChatTurnRequest,
    ChatTurnResponse,
    KnowledgeQuery,
    PendingTask,
    RouterDecision,
    StepAgentResult,
)
from app.tools import ToolExecutor
from app.tools.tool_schema import ToolCall, ToolError, ToolResult


# 状态回调类型：(事件类型, 负载字典) → 无返回值；用于流式推送中间状态。
StatusCallback = Callable[[str, dict[str, object]], None]
# 流式 token 推送的节流间隔（秒），45ms 兼顾实时感与前端渲染压力。
STREAM_CHUNK_INTERVAL_SECONDS = 0.045
# 反思（reflection）默认最大轮数；1 表示首次结果不满意时再重试一次。
DEFAULT_REFLECTION_MAX_ROUNDS = 1
# 反思最大轮数的硬上限，防止配置过大导致无限重试消耗 token。
REFLECTION_MAX_ROUNDS_LIMIT = 5
# 单轮对话中工具调用动作的最大次数，防止循环调用耗尽资源。
MAX_TOOL_ACTIONS_PER_TURN = 6
# 槽位键名：存储历史工具调用记录，供去重与幂等性判断使用。
TOOL_CALL_HISTORY_SLOT = "_tool_call_history"
# 槽位键名：存储工具执行结果，供后续步骤引用。
TOOL_RESULTS_SLOT = "_tool_results"
# 槽位键名：存储技能图中待执行的后续步骤 ID 列表（图编排模式专用）。
GRAPH_PENDING_STEPS_SLOT = "_graph_pending_steps"
# 通用技能工具名前缀，用于区分通用技能工具与普通外部工具。
GENERAL_SKILL_TOOL_PREFIX = "general_skill."
# 用户取消生成时，持久化到 assistant 消息的占位回复文本。
CANCELLED_ASSISTANT_REPLY = "已停止生成"
# 产生副作用的 HTTP 写方法集合，用于幂等性重放判断。
IDEMPOTENT_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# 错误事件中 traceback 字段的最大字符数，防止超大日志撑爆存储。
ERROR_TRACEBACK_CHAR_LIMIT = 6000
# Agent persona 元数据字段映射表：(JSON key → 中文标签)，用于构建身份提示词。
# 同一中文标签可对应多个 key（如"岗位"对应 role_name/position/job_title），去重时仅保留首个。
AGENT_PERSONA_METADATA_FIELDS: tuple[tuple[str, str], ...] = (
    ("role_name", "岗位"),
    ("position", "岗位"),
    ("job_title", "岗位"),
    ("role", "角色"),
    ("title", "职务"),
    ("department", "部门"),
    ("team", "团队"),
    ("work_styles", "工作风格"),
    ("expertise_tags", "擅长领域"),
    ("work_modes", "工作方式"),
)

# 执行收尾状态字面量类型：continued（继续活跃）/ completed（已完成）/ handoff（转人工）。
ExecutionFinalizeState = Literal["continued", "completed", "handoff"]


def _agent_identity_prompt(agent: AgentProfile) -> str:
    """构建 Agent（数字员工）身份提示词。

    将员工的名称、描述、岗位、部门等元数据组织为系统提示词的一部分，
    供 LLM 在回复时保持角色一致性。

    Args:
        agent: Agent 配置档案，包含名称、描述、persona 等。

    Returns:
        多行身份提示词文本。
    """
    metadata = agent.metadata_json if isinstance(agent.metadata_json, dict) else {}
    lines = [
        "你正在扮演一个企业数字员工。请始终以该员工的身份、岗位和职责口径回复用户，不要自称其他员工。",
        f"员工名称：{_single_line_text(agent.name)}",
    ]
    description = _single_line_text(agent.description)
    if description:
        lines.append(f"员工描述：{description}")
    # 遍历 persona 元数据字段，按标签去重（"岗位"标签仅保留首个有效值）
    seen_labels: set[str] = set()
    for key, label in AGENT_PERSONA_METADATA_FIELDS:
        value = _metadata_prompt_text(metadata.get(key))
        if not value:
            continue
        # "岗位"标签可能由多个 key 提供（role_name/position/job_title），仅保留首个
        if label in seen_labels and label in {"岗位"}:
            continue
        seen_labels.add(label)
        lines.append(f"{label}：{value}")
    # 追加自定义角色补充要求（如有）
    persona = str(agent.persona_prompt or "").strip()
    if persona:
        lines.append("")
        lines.append("员工角色补充要求：")
        lines.append(persona)
    return "\n".join(lines)


def _metadata_prompt_text(value: object) -> str:
    """将任意类型的元数据值转换为适合放入提示词的单行文本。

    支持字符串、数字、布尔值和列表；列表元素以顿号连接。
    """
    if isinstance(value, str):
        return _single_line_text(value)
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        items = [_single_line_text(item) for item in value]
        return "、".join(item for item in items if item)
    return ""


def _single_line_text(value: object) -> str:
    """将任意值转换为单行文本，折叠所有空白字符为一个空格。

    用于在构建提示词或事件负载时，避免换行/制表符破坏单行字段格式。

    Args:
        value: 任意输入值（可为 None、字符串、数字等），None 视为空串。

    Returns:
        去除首尾空白、内部连续空白折叠为单个空格后的字符串。
    """
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _normalize_action(action: object) -> str:
    """规范化步骤动作字符串，去除多余引号和空白。

    特别处理 ``call_tool:工具名`` 形式，统一去除工具名两侧的引号。
    对空输入返回空串。

    Args:
        action: 原始动作值（通常来自 LLM 输出或配置）。

    Returns:
        规范化后的动作字符串（如 ``"call_tool:search"`` 或 ``"reply"``）；
        若输入为空则返回 ``""``。
    """
    text = str(action or "").strip().strip("`'\"").strip()
    if not text:
        return ""
    # 特殊处理 call_tool:工具名 形式，统一格式
    if text.startswith("call_tool:"):
        tool_name = text.split(":", 1)[1].strip().strip("`'\"").strip()
        return f"call_tool:{tool_name}" if tool_name else ""
    return text


def _slot_has_value(slots: dict[str, Any], field: str) -> bool:
    """检查槽位字典中某个字段是否已有有效值。

    判定规则：值不为 None、不为空字符串、不为空列表时视为有值。

    Args:
        slots: 槽位字典（如会话的 ``slots_json``）。
        field: 要检查的字段名。

    Returns:
        若字段存在且值有效则返回 ``True``，否则 ``False``。
    """
    value = slots.get(field)
    return value is not None and value != "" and value != []


def _skill_expected_fields(skill: Skill) -> set[str]:
    """从技能定义中提取所有期望收集的用户信息字段名集合。

    扫描技能的 ``required_info`` 列表和各节点的 ``expected_user_info``，
    返回去重后的字段名集合。
    """
    content = skill.content_json or {}
    fields: set[str] = set()
    required_info = content.get("required_info")
    if isinstance(required_info, list):
        fields.update(str(item) for item in required_info if str(item).strip())
    nodes = content.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if not isinstance(node, dict):
                continue
            expected = node.get("expected_user_info")
            if isinstance(expected, list):
                fields.update(str(item) for item in expected if str(item).strip())
    return fields


def _profile_name_from_memory(memory_context: list[dict[str, object]]) -> str:
    """从记忆上下文中提取用户偏好的称呼名称。

    遍历记忆列表，查找 ``kind`` 为 ``profile`` 且 ``metadata.key`` 为
    ``preferred_name`` 的记忆条目，返回其内容作为用户称呼。

    Args:
        memory_context: 记忆上下文列表，每个元素为一条记忆字典。

    Returns:
        找到的称呼名称（截断到 40 字符）；若未找到则返回空字符串。
    """
    for memory in memory_context:
        # 仅处理 profile 类型的记忆
        if memory.get("kind") != "profile":
            continue
        metadata = memory.get("metadata")
        key = metadata.get("key") if isinstance(metadata, dict) else None
        content = str(memory.get("content") or "").strip()
        # 只提取 preferred_name（偏好称呼）字段
        if key != "preferred_name":
            continue
        if content:
            return content[:40]
    return ""


def _node_as_step(node: dict[str, Any]) -> dict[str, Any]:
    """将技能图节点（node）映射为统一格式的步骤（step）字典。

    技能定义中使用 ``nodes`` 描述步骤，但 StepAgent 等组件期望使用
    ``step_id`` / ``node_id`` 等标准化字段名。本函数做字段名适配和
    默认值填充。

    Args:
        node: 技能定义中的节点字典。

    Returns:
        标准化后的步骤字典，包含 step_id、type、name、instruction、
        optional、condition、expected_user_info、allowed_actions、
        knowledge_scope、retry_policy、metadata 等字段。
    """
    return {
        "step_id": node.get("node_id"),
        "node_id": node.get("node_id"),
        "type": node.get("type"),
        "name": node.get("name"),
        "instruction": node.get("instruction"),
        "optional": node.get("optional", False),
        "condition": node.get("condition"),
        "expected_user_info": node.get("expected_user_info") or [],
        "allowed_actions": node.get("allowed_actions") or [],
        "knowledge_scope": node.get("knowledge_scope") or {},
        "retry_policy": node.get("retry_policy") or {},
        "metadata": node.get("metadata") or {},
    }


class AgentLoopPreconditionError(Exception):
    """Agent 编排前置条件不满足时抛出的异常。

    与一般运行时异常不同，此类异常表示编排引擎在开始处理前就检测到
    缺少必要的配置或资源（如没有默认模型配置），属于可预期的业务错误，
    会直接返回面向用户的错误提示而非触发通用错误处理流程。

    Attributes:
        code: 错误代码（如 ``missing_model_config``），用于前端区分处理。
        message: 面向用户的错误描述文本。
    """

    def __init__(self, code: str, message: str):
        """初始化前置条件异常。

        Args:
            code: 机器可读的错误代码（如 ``missing_model_config``）。
            message: 人类可读的错误描述。
        """
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class PreparedTurn:
    """``_prepare_turn`` 的产出，封装一轮对话准备阶段的全部中间结果。

    该数据类作为准备阶段与回复生成阶段之间的数据传递载体，将路由决策、
    步骤结果、工具结果、记忆上下文等聚合为一个对象，避免方法间传递过多参数。

    Attributes:
        chat_session: 准备阶段获取或创建的聊天会话。
        model_config: 本轮使用的模型配置。
        active_skill: 当前活跃技能（若无则为 None）。
        router_decision: 路由器产出的路由决策。
        step_result: StepAgent 产出的步骤结果。
        tool_result: 工具执行结果（若本轮调用了工具）。
        memory_context: 记忆服务提供的长期记忆上下文。
        conversation_context: 对话上下文（历史消息摘要等）。
        general_response: 若通用技能已直接生成完整响应则非 None，调用方应直接返回。
        reply_override: 若非 None，跳过回复生成直接使用此文本作为回复。
        user_message_id: 用户消息的数据库 ID。
    """

    chat_session: ChatSession
    model_config: ModelConfig
    active_skill: Skill | None
    router_decision: RouterDecision
    step_result: StepAgentResult
    tool_result: ToolResult | None
    memory_context: list[dict[str, object]]
    conversation_context: dict[str, object]
    general_response: ChatTurnResponse | None = None
    reply_override: str | None = None
    user_message_id: str | None = None


@dataclass
class QueuedTaskContinuation:
    """排队的待处理任务（pending task）在主回复完成后继续执行的产出。

    当一轮对话的主任务完成后，若存在可接续的 pending 任务，
    编排器会尝试继续执行并将结果封装到此结构中。

    Attributes:
        reply: 续接任务生成的回复文本。
        task_results: 续接任务的结果上下文列表。
        active_skill: 续接任务执行时的活跃技能。
        router_decision: 续接任务的路由决策。
        step_result: 续接任务的步骤结果。
        tool_result: 续接任务的工具结果。
    """

    reply: str
    task_results: list[dict[str, object]]
    active_skill: Skill | None
    router_decision: RouterDecision
    step_result: StepAgentResult
    tool_result: ToolResult | None


class AgentLoop:
    """Agent 编排引擎，整个多智能体对话系统的核心编排入口。

    :class:`AgentLoop` 组合了路由 (:class:`Router`)、步骤推理
    (:class:`StepAgent`)、技能运行时 (:class:`SkillRuntime`)、反思代理
    (:class:`ReflectionAgent`)、回复生成器 (:class:`ResponseGenerator`)、
    通用技能 (:class:`GeneralSkillRunner` / :class:`GeneralSkillSelector`)、
    工具执行 (:class:`ToolExecutor`)、记忆 (:class:`MemoryService`) 等子组件，
    将它们协调为一条完整的对话处理管道。

    典型用法::

        with Session(engine) as db:
            loop = AgentLoop(db)
            response = loop.handle_turn(request)       # 非流式
            # 或：
            for chunk in loop.handle_turn_stream(req):  # 流式
                ...

    每个实例绑定一个数据库会话 (:class:`sqlmodel.Session`)，
    在一轮对话处理完毕后由调用方负责提交事务。
    """

    def __init__(self, db: Session):
        """初始化编排引擎及其所有子组件。

        Args:
            db: 数据库会话，所有子组件共享此会话进行持久化操作。
        """
        self.db = db
        self.events = EventLog(db)
        self.router = Router()
        self.runtime = SkillRuntime()
        self.step_agent = StepAgent()
        self.reflection_agent = ReflectionAgent()
        self.response_generator = ResponseGenerator()
        self.general_skill_selector = GeneralSkillSelector()
        self.general_skill_runner = GeneralSkillRunner()
        self.tool_executor = ToolExecutor(db)
        self.memory = MemoryService(db)
        self._validated_general_skill_calls: set[tuple[str, str, str]] = set()

    def _turn_payload(self, payload: dict[str, Any], user_message_id: str | None) -> dict[str, Any]:
        """为事件记录构建附带轮次标识的 payload。

        在传入的 payload 基础上，补充 ``user_message_id`` 和 ``turn_id`` 字段，
        以便后续按轮次检索事件。
        """
        data = dict(payload)
        if user_message_id:
            data.setdefault("user_message_id", user_message_id)
            data.setdefault("turn_id", user_message_id)
        return data

    def _hydrate_router_decision_from_context(
        self,
        chat_session: ChatSession,
        router_decision: RouterDecision,
        skills: list[Skill],
        memory_context: list[dict[str, object]],
    ) -> dict[str, Any]:
        """从会话上下文和记忆中为路由决策补充（hydrate）缺失的槽位。

        路由器产出的 ``RouterDecision`` 可能缺少一些可从记忆或已有槽位推导的
        信息（如用户名称）。本方法遍历主任务和所有子任务的槽位，尝试用记忆
        上下文中的数据填充缺失字段，并裁剪已被满足的等待输入字段。

        Args:
            chat_session: 当前聊天会话（包含已有槽位 ``slots_json``）。
            router_decision: 路由器产出的原始决策。
            skills: 当前可用的技能列表（用于查找目标技能定义）。
            memory_context: 记忆上下文列表（用于推导槽位值）。

        Returns:
            补充记录字典，包含 ``primary``（主任务补丁）、
            ``awaiting_input_expected_fields``（裁剪后的等待字段）和
            ``tasks``（各子任务的补丁列表）；若无补充则为空字典。
        """
        # 构建技能 ID → 技能对象的映射，便于按 ID 查找技能定义
        skills_by_id = {skill.skill_id: skill for skill in skills}
        hydrated: dict[str, Any] = {}

        # —— 主任务槽位补充 ——
        # 确定目标技能：优先用路由决策指定的，其次用会话当前活跃的
        target_skill = skills_by_id.get(
            router_decision.target_skill_id or chat_session.active_skill_id or ""
        )
        # 合并会话已有槽位和路由决策提供的槽位提示
        base_slots = dict(chat_session.slots_json or {})
        base_slots.update(dict(router_decision.slot_hints or {}))
        # 尝试用记忆上下文补充缺失槽位
        patch = self._slot_hydration_patch(target_skill, base_slots, memory_context)
        if patch:
            router_decision.slot_hints = {**dict(router_decision.slot_hints or {}), **patch}
            hydrated["primary"] = patch
        # 裁剪已被满足的等待输入字段
        remaining_awaiting = self._trim_satisfied_awaiting_fields(
            router_decision, {**base_slots, **patch}
        )
        if remaining_awaiting is not None:
            hydrated["awaiting_input_expected_fields"] = remaining_awaiting

        # —— 子任务槽位补充 ——
        # 遍历所有任务帧（task_frames / pending_tasks / created_tasks）
        task_patches: list[dict[str, Any]] = []
        for task in [
            *router_decision.task_frames,
            *router_decision.pending_tasks,
            *router_decision.created_tasks,
        ]:
            task_skill = skills_by_id.get(task.target_skill_id or "")
            task_slots = dict(task.slot_hints or {})
            task_patch = self._slot_hydration_patch(task_skill, task_slots, memory_context)
            if task_patch:
                task.slot_hints = {**task_slots, **task_patch}
                task_patches.append(
                    {
                        "task_id": task.task_id,
                        "target_skill_id": task.target_skill_id,
                        "slots": task_patch,
                    }
                )
        if task_patches:
            hydrated["tasks"] = task_patches
        return hydrated

    def _slot_hydration_patch(
        self,
        skill: Skill | None,
        slots: dict[str, Any],
        memory_context: list[dict[str, object]],
    ) -> dict[str, Any]:
        """为单个技能计算缺失槽位的补充补丁。

        目前仅支持从记忆上下文中推导 ``user_name`` 槽位：当技能期望收集
        用户名称而槽位中尚无值时，从记忆的偏好称呼中提取。

        Args:
            skill: 目标技能定义（若无则返回空补丁）。
            slots: 当前已有槽位字典。
            memory_context: 记忆上下文列表。

        Returns:
            补丁字典（键为槽位名，值为补充的值）；若无补充则为空字典。
        """
        if not skill:
            return {}
        # 获取该技能期望收集的所有用户信息字段
        expected_fields = _skill_expected_fields(skill)
        patch: dict[str, Any] = {}
        # 当技能期望收集 user_name 且槽位中尚无值时，从记忆中提取偏好称呼
        if "user_name" in expected_fields and not _slot_has_value(slots, "user_name"):
            profile_name = _profile_name_from_memory(memory_context)
            if profile_name:
                patch["user_name"] = profile_name
        return patch

    def _trim_satisfied_awaiting_fields(
        self, router_decision: RouterDecision, slots: dict[str, Any]
    ) -> list[str] | None:
        """裁剪 ``awaiting_input`` 中已被满足的期望字段。

        当某些期望字段在槽位中已有值时，从 ``awaiting_input.expected_fields``
        中移除它们。若全部满足则将 ``awaiting_input`` 置为 None。

        Returns:
            剩余未满足字段列表；若无变化则返回 None。
        """
        if not router_decision.awaiting_input:
            return None
        # 保存原始字段列表用于比较
        original = list(router_decision.awaiting_input.expected_fields)
        # 过滤出仍在等待（槽位中无值）的字段
        remaining = [
            field
            for field in router_decision.awaiting_input.expected_fields
            if not _slot_has_value(slots, field)
        ]
        # 若无变化，返回 None 表示未做任何裁剪
        if remaining == original:
            return None
        # 有剩余字段：更新 expected_fields
        if remaining:
            router_decision.awaiting_input.expected_fields = remaining
        # 全部已满足：清除 awaiting_input
        else:
            router_decision.awaiting_input = None
        return remaining

    def handle_turn(self, request: ChatTurnRequest) -> ChatTurnResponse:
        """处理一轮对话（非流式入口）。

        这是 Agent 编排引擎的**同步主入口**。完整执行"准备→执行→回复→持久化"
        管道后返回最终响应。内部流程概述：

        1. 调用 :meth:`_prepare_turn` 完成路由、技能应用、步骤执行、
           知识查询、工具调用、反思重试等全部准备逻辑。
        2. 若准备阶段已产出通用技能响应（``general_response``），直接返回。
        3. 否则根据是否存在待续接的任务帧 (turn follow-up frames)，
           分别走"先完成主任务再续接"或"直接生成回复"两条路径。
        4. 对定时任务 (scheduled_task) 模式，额外尝试续接 pending 任务。
        5. 调用 :meth:`_generate_reply_segment` 生成回复文本。
        6. 调用 :meth:`_finalize_turn` 持久化 assistant 消息。
        7. 异步入队记忆采集 (:meth:`_enqueue_memory_capture`)。

        异常处理：捕获 :class:`AgentLoopPreconditionError`（前置条件不满足）、
        :class:`LLMError`（模型调用失败）和其他异常，转换为面向用户的错误回复。

        Args:
            request: 包含用户消息、租户 ID、会话 ID 等的请求对象。

        Returns:
            包含回复文本、会话状态、路由决策、步骤结果等的响应对象。
        """
        router_decision: RouterDecision | None = None
        step_result = StepAgentResult()
        tool_result: ToolResult | None = None
        chat_session: ChatSession | None = None
        memory_model_config: ModelConfig | None = None
        prepared_user_message_id: str | None = None
        try:
            prepared = self._prepare_turn(request)
            prepared_user_message_id = prepared.user_message_id
            # 通用技能已直接处理完毕，跳过后续编排
            if prepared.general_response:
                return prepared.general_response
            chat_session = prepared.chat_session
            memory_model_config = prepared.model_config
            router_decision = prepared.router_decision
            # 检查路由决策中是否携带需要在本轮立即续接的任务帧
            turn_followup_frames = self._turn_followup_task_frames(router_decision)
            step_result = prepared.step_result
            tool_result = prepared.tool_result
            memory_context = prepared.memory_context
            conversation_context = prepared.conversation_context
            if prepared.reply_override is not None:
                reply = prepared.reply_override
            else:
                if turn_followup_frames:
                    task_results = [
                        self._task_response_context(
                            chat_session,
                            prepared.active_skill,
                            router_decision,
                            step_result,
                            tool_result,
                        )
                    ]
                    primary_router_decision = router_decision
                    primary_step_result = step_result
                    primary_tool_result = tool_result
                    # 判定主任务的收尾状态（继续/完成/人工接管）
                    finalize_state = self._finalize_execution_after_reply(
                        request.tenant_id,
                        chat_session,
                        prepared.active_skill,
                        router_decision,
                        step_result,
                        tool_result,
                    )
                    # 若主任务尚未完成且有活跃技能，先挂起以便续接任务使用会话
                    paused_primary = None
                    if finalize_state == "continued" and prepared.active_skill:
                        paused_primary = self.runtime.suspend_current_skill(chat_session)
                    # 尝试执行待续接的任务帧
                    continuation = self._try_continue_pending_after_completion(
                        request,
                        chat_session,
                        prepared.model_config,
                        self._list_published_skills(request.tenant_id, chat_session.agent_id),
                        self._tools_with_general_skills(
                            request.tenant_id,
                            self._list_enabled_tools(
                                request.tenant_id, chat_session.agent_id
                            ),
                            chat_session.agent_id,
                        ),
                        self._get_persona_prompt(
                            request.tenant_id, chat_session.agent_id
                        ),
                        memory_context,
                        conversation_context,
                        "",
                        turn_task_frames=turn_followup_frames,
                    )
                    if continuation:
                        task_results.extend(continuation.task_results)
                    # 恢复之前挂起的主技能（若续接任务改变了活跃技能）
                    if paused_primary:
                        # 先挂起续接任务引入的技能，再恢复主技能
                        if chat_session.active_skill_id:
                            self.runtime.suspend_current_skill(chat_session, enqueue=True)
                        self.runtime.restore_task_frame(chat_session, paused_primary)
                        self.db.commit()
                        self.db.refresh(chat_session)
                        # 回复基于主任务的结果
                        router_decision = primary_router_decision
                        step_result = primary_step_result
                        tool_result = primary_tool_result
                    elif continuation:
                        # 无挂起的主技能：回复基于续接任务的结果
                        router_decision = continuation.router_decision
                        step_result = continuation.step_result
                        tool_result = continuation.tool_result
                    # 选择用于生成回复的活跃技能
                    response_active_skill = (
                        prepared.active_skill
                        if paused_primary or not continuation
                        else continuation.active_skill
                    )
                    reply = self._generate_reply_segment(
                        request.message,
                        chat_session,
                        response_active_skill,
                        router_decision,
                        step_result,
                        tool_result,
                        prepared.model_config,
                        self._get_persona_prompt(request.tenant_id, chat_session.agent_id),
                        memory_context,
                        conversation_context,
                        task_results,
                    )
                else:
                    reply = self._generate_reply_segment(
                        request.message,
                        chat_session,
                        prepared.active_skill,
                        router_decision,
                        step_result,
                        tool_result,
                        prepared.model_config,
                        self._get_persona_prompt(request.tenant_id, chat_session.agent_id),
                        memory_context,
                        conversation_context,
                    )
                    finalize_state = self._finalize_execution_after_reply(
                        request.tenant_id,
                        chat_session,
                        prepared.active_skill,
                        router_decision,
                        step_result,
                        tool_result,
                    )
                # 定时任务模式：主任务完成后尝试自动续接 pending 任务
                if (
                    not turn_followup_frames
                    and finalize_state == "completed"
                    and request.interaction_mode == "scheduled_task"
                ):
                    continuation = self._try_continue_pending_after_completion(
                        request,
                        chat_session,
                        prepared.model_config,
                        self._list_published_skills(request.tenant_id, chat_session.agent_id),
                        self._tools_with_general_skills(
                            request.tenant_id,
                            self._list_enabled_tools(request.tenant_id, chat_session.agent_id),
                            chat_session.agent_id,
                        ),
                        self._get_persona_prompt(request.tenant_id, chat_session.agent_id),
                        memory_context,
                        conversation_context,
                        reply,
                    )
                    if continuation:
                        # 拼接主回复与续接回复
                        reply = "\n\n".join(
                            part
                            for part in (reply.strip(), continuation.reply.strip())
                            if part
                        )
                        router_decision = continuation.router_decision
                        step_result = continuation.step_result
                        tool_result = continuation.tool_result
                    elif chat_session.pending_tasks_json:
                        # 有待处理任务但无法续接：记录等待状态
                        self.events.record(
                            request.tenant_id,
                            chat_session.id,
                            "pending_tasks_waiting",
                            {"pending_tasks": chat_session.pending_tasks_json or []},
                        )

        except AgentLoopPreconditionError as exc:
            # 前置条件不满足（如缺少模型配置）：直接返回错误响应
            chat_session = chat_session or self._get_or_create_session(request)
            return self._finish_with_error(chat_session, exc.code, exc.message)
        except LLMError as exc:
            # 模型调用失败：记录错误并生成面向用户的失败回复
            chat_session = chat_session or self._get_or_create_session(request)
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "error_occurred",
                {"code": "LLM_ERROR", "message": str(exc)},
            )
            reply = format_runtime_failure_reply(
                "模型调用失败", exc, "LLM_ERROR", model_failure_suggestion(exc)
            )
        except Exception as exc:
            # 其他未预期异常：记录错误并生成通用失败回复
            chat_session = chat_session or self._get_or_create_session(request)
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "error_occurred",
                {"code": "AGENT_LOOP_ERROR", "message": str(exc)},
            )
            reply = format_runtime_failure_reply(
                "Agent Loop 出错",
                exc,
                "AGENT_LOOP_ERROR",
                "请查看执行记录或服务日志定位具体原因。",
            )

        # —— 收尾阶段：持久化、提交、记忆采集 ——
        if not chat_session:
            chat_session = self._get_or_create_session(request)
        self._finalize_turn(
            chat_session,
            request.tenant_id,
            reply,
            step_result,
            request.message,
            user_message_id=prepared_user_message_id,
        )
        self.db.commit()
        self.db.refresh(chat_session)
        if memory_model_config:
            self._enqueue_memory_capture(
                request,
                chat_session,
                step_result,
                tool_result,
                memory_model_config,
            )
        return ChatTurnResponse(
            reply=reply,
            session_id=chat_session.id,
            router_decision=router_decision,
            step_result=step_result,
            tool_result=tool_result,
            session_state=public_session(chat_session),
        )

    def _try_handle_general_skill_after_scene_router(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        model_config: ModelConfig,
        router_decision: RouterDecision,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        user_message_id: str | None = None,
        capability: tuple[GeneralSkill | None, GeneralSkillSelection] | None = None,
    ) -> ChatTurnResponse | None:
        """在场景路由判定为延迟到通用技能时，尝试用通用技能处理用户消息。

        当 Router 将决策推迟到通用技能层 (general skill) 时，选择并执行
        匹配的通用技能，生成回复后直接持久化返回。

        Args:
            request: 用户请求。
            chat_session: 当前会话。
            model_config: 模型配置。
            router_decision: 场景路由决策。
            memory_context: 记忆上下文。
            conversation_context: 对话上下文。
            user_message_id: 用户消息 ID。
            capability: 预先选定的通用技能能力（可选，避免重复选择）。

        Returns:
            若成功匹配并执行通用技能则返回完整响应；否则返回 None。
        """
        if not self._scene_router_deferred_to_general(router_decision):
            return None
        capability = capability or self._select_general_capability(
            request.message,
            model_config,
            chat_session.agent_id,
            conversation_context,
            memory_context,
        )
        skill, selection = capability
        if skill is None:
            return None
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "general_skill_intent_checked",
            self._turn_payload(
                {
                    "skill_slug": skill.slug,
                    "skill_name": skill.name,
                    "confidence": selection.confidence,
                    "reason": selection.reason,
                    "scene_router_decision": router_decision.model_dump(mode="json"),
                },
                user_message_id,
            ),
        )
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "general_skill_selected",
            self._turn_payload(
                {
                    "skill_slug": skill.slug,
                    "skill_name": skill.name,
                    "confidence": selection.confidence,
                    "reason": selection.reason,
                    "scene_router_decision": router_decision.model_dump(mode="json"),
                },
                user_message_id,
            ),
        )
        run_response = self.general_skill_runner.run(
            skill,
            request.message,
            model_config,
            request.user_id,
            conversation_context=conversation_context,
            memory_context=memory_context,
        )
        self._record_general_skill_run_events(
            request.tenant_id, chat_session, run_response, user_message_id
        )
        step_result, tool_result = self._general_skill_agent_outputs(run_response)
        knowledge_step = self._auto_knowledge_step_result(
            request,
            chat_session,
            model_config,
            router_decision,
            selection,
        )
        self._merge_capability_knowledge(step_result, knowledge_step)
        active_skill = self._get_active_skill(
            request.tenant_id, chat_session.active_skill_id, chat_session.agent_id
        )
        reply = self._generate_reply_segment(
            request.message,
            chat_session,
            active_skill,
            router_decision,
            step_result,
            tool_result,
            model_config,
            self._get_persona_prompt(request.tenant_id, chat_session.agent_id),
            memory_context or [],
            (
                conversation_context
                if conversation_context is not None
                else self._conversation_context(chat_session)
            ),
        )
        self._finalize_turn(
            chat_session,
            request.tenant_id,
            reply,
            step_result,
            request.message,
            user_message_id=user_message_id,
        )
        self.db.commit()
        self.db.refresh(chat_session)
        self._enqueue_memory_capture(
            request,
            chat_session,
            step_result,
            tool_result,
            model_config,
        )
        return ChatTurnResponse(
            reply=reply,
            session_id=chat_session.id,
            router_decision=router_decision,
            step_result=step_result,
            tool_result=tool_result,
            session_state=public_session(chat_session),
        )

    def _general_skill_agent_outputs(
        self, run_response: GeneralSkillRunResponse
    ) -> tuple[StepAgentResult, ToolResult]:
        """将通用技能运行结果转换为标准步骤结果和工具结果。

        把 ``GeneralSkillRunResponse`` 适配为编排引擎内部统一的
        ``StepAgentResult`` 和 ``ToolResult``，使通用技能与场景技能
        在后续流程中使用相同的接口。

        Args:
            run_response: 通用技能执行器的运行结果。

        Returns:
            元组 ``(step_result, tool_result)``：
            - step_result: 含通用技能回复文本和完成标记。
            - tool_result: 含通用技能结构化结果或错误信息。
        """
        # 判定成功：结构化结果标记 success 为真且无 stderr 输出
        success = (
            bool(run_response.structured_result.get("success", True))
            and not run_response.stderr.strip()
        )
        data = {
            "skill_slug": run_response.skill_slug,
            "reply": run_response.reply,
            "structured_result": run_response.structured_result,
            "stdout": run_response.stdout,
            "stderr": run_response.stderr,
        }
        # 将通用技能结果封装为 ToolResult，使用统一前缀的虚拟工具名
        tool_result = ToolResult(
            tool_name=f"{GENERAL_SKILL_TOOL_PREFIX}{run_response.skill_slug}",
            success=success,
            data=data if success else None,
            error=None
            if success
            else ToolError(
                code="GENERAL_SKILL_FAILED", message=run_response.stderr or run_response.reply
            ),
        )
        step_result = StepAgentResult(
            reply=run_response.reply,
            is_step_completed=success,
            tool_call=None,
        )
        return step_result, tool_result

    @staticmethod
    def _merge_capability_knowledge(
        step_result: StepAgentResult,
        knowledge_step: StepAgentResult,
    ) -> None:
        """将知识检索结果合并到步骤结果中。

        当通用技能执行后还触发了自动知识检索时，将知识查询和结果
        从 knowledge_step 合并到主 step_result 上。

        Args:
            step_result: 主步骤结果（将被原地修改）。
            knowledge_step: 知识检索步骤结果。
        """
        if knowledge_step.knowledge_query is not None:
            step_result.knowledge_query = knowledge_step.knowledge_query
        if knowledge_step.knowledge_results:
            step_result.knowledge_results = knowledge_step.knowledge_results

    def _scene_router_deferred_to_general(self, router_decision: RouterDecision) -> bool:
        """判断场景路由是否应延迟到通用技能层处理。

        当路由决策为 ``answer_only`` 或 ``clarify``（即不涉及具体技能流程），
        且没有任何任务帧或已选定的任务时，表示可尝试用通用技能回答。

        Args:
            router_decision: 路由器产出的决策。

        Returns:
            若应延迟到通用技能层则返回 ``True``，否则 ``False``。
        """
        # 若已选定具体任务，不走通用技能
        if router_decision.selected_task_id:
            return False
        # 若存在任务帧/待处理任务/已创建任务/任务更新，不走通用技能
        if (
            router_decision.task_frames
            or
            router_decision.pending_tasks
            or router_decision.created_tasks
            or router_decision.task_updates
        ):
            return False
        # 仅当决策为直接回答或澄清时，可尝试通用技能
        return router_decision.decision in {
            "answer_only",
            "clarify",
        }

    def _should_run_step_agent(
        self, router_decision: RouterDecision, active_skill: Skill | None
    ) -> bool:
        """判断是否需要调用 StepAgent 进行步骤推理。

        若有活跃技能且路由决策未明确指示跳过，则需要运行 StepAgent。
        """
        if active_skill is None:
            return False
        return router_decision.decision not in {
            "answer_only",
            "clarify",
            "create_pending",
            "update_pending",
            "complete_task",
            "handoff_human",
        }

    def _stream_general_skill_response(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        model_config: ModelConfig,
        selected_general_skill: tuple[GeneralSkill, GeneralSkillSelection],
        router_decision: RouterDecision | None = None,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        persona_prompt: str | None = None,
        user_message_id: str | None = None,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> Iterator[dict[str, object]]:
        """以流式方式执行通用技能并产出事件块。

        在独立线程中运行通用技能，同时通过队列将 trace 事件实时推送给前端。
        完成后生成回复、持久化并产出 ``complete`` 事件。

        Yields:
            流式事件字典（status / general_skill_trace / token / complete 等）。
        """
        skill, selection = selected_general_skill
        # 记录并推送意图检查与技能选择事件
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "general_skill_intent_checked",
            self._turn_payload(
                {
                    "skill_slug": skill.slug,
                    "skill_name": skill.name,
                    "confidence": selection.confidence,
                    "reason": selection.reason,
                    "scene_router_decision": router_decision.model_dump(mode="json")
                    if router_decision
                    else None,
                },
                user_message_id,
            ),
        )
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "general_skill_selected",
            self._turn_payload(
                {
                    "skill_slug": skill.slug,
                    "skill_name": skill.name,
                    "confidence": selection.confidence,
                    "reason": selection.reason,
                    "scene_router_decision": router_decision.model_dump(mode="json")
                    if router_decision
                    else None,
                },
                user_message_id,
            ),
        )
        yield self._stream_status(
            chat_session,
            "general_skill_intent",
            "正在判断意图",
            {"skill_slug": skill.slug, "skill_name": skill.name, "reason": selection.reason},
            user_message_id=user_message_id,
        )
        yield self._stream_event(
            "general_skill_trace",
            chat_session,
            self._turn_payload(
                {
                    "phase": "intent_checked",
                    "message": "判断意图",
                    "skill_slug": skill.slug,
                    "skill_name": skill.name,
                    "reason": selection.reason,
                    "confidence": selection.confidence,
                },
                user_message_id,
            ),
        )
        # 推送前端状态序列：意图判断→技能选择→技能运行
        yield self._stream_status(
            chat_session,
            "general_skill_routing",
            "正在选择通用技能",
            {"skill_slug": skill.slug, "skill_name": skill.name},
            user_message_id=user_message_id,
        )
        yield self._stream_event(
            "general_skill_state",
            chat_session,
            self._turn_payload(
                {"skillSlug": skill.slug, "skillName": skill.name, "state": "selected"},
                user_message_id,
            ),
        )
        yield self._stream_status(
            chat_session,
            "general_skill_running",
            "正在运行通用技能",
            {"skill_slug": skill.slug, "skill_name": skill.name},
            user_message_id=user_message_id,
        )
        general_skill_events: queue.Queue[tuple[str, Any] | None] = queue.Queue()
        # 创建技能快照（SimpleNamespace），使子线程不依赖 SQLAlchemy 对象的生命周期
        skill_snapshot = SimpleNamespace(
            slug=skill.slug,
            name=skill.name,
            description=skill.description,
            homepage=skill.homepage,
            skill_markdown=skill.skill_markdown,
            skill_files_json=skill.skill_files_json or [],
            metadata_json=skill.metadata_json or {},
            permissions_json=skill.permissions_json or {},
            runtime_config_json=skill.runtime_config_json or {},
            status=skill.status,
        )
        model_snapshot = model_config

        def general_skill_sink(trace_item: dict[str, Any]) -> None:
            """通用技能 trace 事件接收器，将事件推入队列供主线程消费。"""
            general_skill_events.put(("trace", trace_item))

        def general_skill_worker() -> None:
            """在独立线程中执行通用技能运行，将结果推入事件队列。"""
            try:
                response = GeneralSkillRunner().run(
                    skill_snapshot,
                    request.message,
                    model_snapshot,
                    request.user_id,
                    event_sink=general_skill_sink,
                    conversation_context=conversation_context,
                    memory_context=memory_context,
                )
                # 成功：将最终结果推入队列
                general_skill_events.put(("complete", response))
            except Exception as exc:  # pragma: no cover - defensive stream boundary
                # 失败：将异常推入队列，由主线程重新抛出
                general_skill_events.put(("error", exc))
            finally:
                # 哨兵值 None 通知主线程：工作线程已结束
                general_skill_events.put(None)

        # 在守护线程中启动通用技能执行，与主线程通过队列通信
        threading.Thread(target=general_skill_worker, daemon=True).start()
        run_response: GeneralSkillRunResponse | None = None
        streamed_trace_count = 0
        # 主线程事件消费循环：从队列取出事件并 yield 给前端
        while True:
            # 每次迭代检查是否被用户取消
            if is_cancelled and is_cancelled():
                return
            try:
                # 阻塞等待事件，0.5 秒超时以便定期检查取消状态
                queued = general_skill_events.get(timeout=0.5)
            except queue.Empty:
                continue
            # 收到哨兵 None：工作线程已结束，退出循环
            if queued is None:
                break
            event_name, payload = queued
            if event_name == "trace":
                # trace 事件：实时推送给前端
                if is_cancelled and is_cancelled():
                    return
                streamed_trace_count += 1
                yield self._stream_event(
                    "general_skill_trace",
                    chat_session,
                    self._turn_payload(payload, user_message_id),
                )
            elif event_name == "complete":
                # 完成事件：保存结果
                run_response = payload
            elif event_name == "error":
                # 错误事件：重新抛出异常
                raise payload

        # 工作线程结束后，校验是否有有效结果
        if run_response is None:
            raise LLMError("General skill stream ended without a result")
        if is_cancelled and is_cancelled():
            return
        # 记录通用技能运行事件；若已有 trace 实时推送过则不重复记录完整 trace
        self._record_general_skill_run_events(
            request.tenant_id,
            chat_session,
            run_response,
            user_message_id,
            include_trace=streamed_trace_count == 0,
        )
        if is_cancelled and is_cancelled():
            return
        # 将运行结果转换为标准 StepAgentResult 和 ToolResult
        step_result, tool_result = self._general_skill_agent_outputs(run_response)
        # 若无路由决策，构建默认的 answer_only 决策
        resolved_router_decision = router_decision or RouterDecision(
            decision="answer_only", user_intent="通用技能执行结果回复"
        )
        # 执行自动知识检索
        knowledge_stream_events: list[tuple[str, dict[str, object]]] = []
        knowledge_step = self._auto_knowledge_step_result(
            request,
            chat_session,
            model_config,
            resolved_router_decision,
            selection,
            stream_events=knowledge_stream_events,
        )
        # 合并知识检索结果到步骤结果
        self._merge_capability_knowledge(step_result, knowledge_step)
        # 推送知识检索事件
        for event_name, payload in knowledge_stream_events:
            yield self._stream_event(
                event_name,
                chat_session,
                self._turn_payload(payload, user_message_id),
            )
        yield self._stream_status(
            chat_session, "responding", "正在生成回复", user_message_id=user_message_id
        )
        active_skill = self._get_active_skill(
            request.tenant_id, chat_session.active_skill_id, chat_session.agent_id
        )
        # 流式生成回复文本，逐 token 推送
        reply = ""
        for chunk in self._generate_reply_stream_segment(
            request.message,
            chat_session,
            active_skill,
            resolved_router_decision,
            step_result,
            tool_result,
            model_config,
            persona_prompt
            if persona_prompt is not None
            else self._get_persona_prompt(request.tenant_id, chat_session.agent_id),
            memory_context or [],
            (
                conversation_context
                if conversation_context is not None
                else self._conversation_context(chat_session)
            ),
        ):
            reply += chunk
            yield self._stream_event(
                "stream_delta",
                chat_session,
                self._turn_payload({"content": chunk}, user_message_id),
            )
            self._pace_stream()
        # 若流式生成结果为空，回退到通用技能运行结果的回复文本
        if not reply.strip():
            reply = run_response.reply
            for chunk in self.response_generator.chunk_text(reply):
                yield self._stream_event(
                    "stream_delta",
                    chat_session,
                    self._turn_payload({"content": chunk}, user_message_id),
                )
                self._pace_stream()
        if is_cancelled and is_cancelled():
            return
        yield self._stream_event(
            "stream_end", chat_session, self._turn_payload({}, user_message_id)
        )
        if is_cancelled and is_cancelled():
            return
        self._finalize_turn(
            chat_session,
            request.tenant_id,
            reply,
            step_result,
            request.message,
            user_message_id=user_message_id,
        )
        self.db.commit()
        self.db.refresh(chat_session)
        self._enqueue_memory_capture(
            request,
            chat_session,
            step_result,
            tool_result,
            model_config,
        )
        result = ChatTurnResponse(
            reply=reply,
            session_id=chat_session.id,
            router_decision=router_decision,
            step_result=step_result,
            tool_result=tool_result,
            session_state=public_session(chat_session),
        )
        yield self._stream_event(
            "complete",
            chat_session,
            self._turn_payload(result.model_dump(mode="json"), user_message_id),
        )

    def _stream_continue_pending_after_completion(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        model_config: ModelConfig,
        skills: list[Skill],
        tools: list[Any],
        persona_prompt: str | None,
        memory_context: list[dict[str, object]],
        conversation_context: dict[str, object],
        completed_reply: str,
        completed_skill_ids_this_turn: set[str] | None = None,
        user_message_id: str | None = None,
        turn_task_frames: list[PendingTask] | None = None,
    ) -> Iterator[dict[str, object]]:
        """以流式方式在主任务完成后继续执行待续接的任务。

        这是流式版的多任务续接编排器。当一轮对话的主任务完成后，
        若仍存在待处理的 pending 任务或路由器指定的轮内任务帧
        （turn_task_frames），本方法会依次执行它们，并通过 yield
        实时推送中间状态。

        支持两种续接模式：
        - **轮内任务帧续接**：``turn_task_frames`` 非 None 时，
          按帧列表顺序依次执行。
        - **pending 任务续接**：``turn_task_frames`` 为 None 时，
          从会话的 ``pending_tasks_json`` 中取队首任务执行。

        每个任务的执行流程与单任务管道一致：路由→步骤推理→工具调用→
        反思→技能图推进→收尾。

        Args:
            request: 用户请求对象。
            chat_session: 当前会话。
            model_config: 模型配置。
            skills: 可用技能列表。
            tools: 可用工具列表。
            persona_prompt: 人设提示词。
            memory_context: 记忆上下文。
            conversation_context: 对话上下文。
            completed_reply: 主任务已生成的回复文本（用于拼接）。
            completed_skill_ids_this_turn: 本轮已完成的技能 ID 集合。
            user_message_id: 用户消息 ID。
            turn_task_frames: 路由器指定的轮内任务帧列表（None 表示走 pending 队列）。

        Yields:
            流式事件字典（status / router_decision / skill_state /
            step_result / tool_call / tool_result /
            reflection_decision / error 等）。

        Returns:
            ``None`` 表示没有待续接任务（无事件可 yield）。
        """
        remaining_turn_frames = list(turn_task_frames or [])
        # 是否使用轮内任务帧模式（而非 pending 队列模式）
        uses_turn_frames = turn_task_frames is not None
        if uses_turn_frames and not remaining_turn_frames:
            return None
        if not uses_turn_frames and not chat_session.pending_tasks_json:
            return None
        # 单轮最大动作数，防止无限续接
        max_actions = max(1, self._get_agent_loop_max_actions(request.tenant_id))
        executed_actions = 0
        replies: list[str] = []
        task_results: list[dict[str, object]] = []
        completed_skill_ids_this_turn = completed_skill_ids_this_turn or set()
        active_skill: Skill | None = None
        router_decision = RouterDecision(decision="answer_only", reason="No pending task selected")
        step_result = StepAgentResult()
        tool_result: ToolResult | None = None

        for queue_round in range(max_actions):
            if uses_turn_frames:
                if not remaining_turn_frames:
                    break
                turn_frame = remaining_turn_frames.pop(0)
                task_id = turn_frame.task_id or f"turn_task_{queue_round + 1}"
            else:
                if not chat_session.pending_tasks_json:
                    break
                task_id = self._next_pending_task_id(chat_session)
                if not task_id:
                    break
            yield self._stream_status(
                chat_session,
                "routing",
                "正在继续后续任务",
                {"queue_round": queue_round + 1},
                user_message_id=user_message_id,
            )
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "router_execution_order_advanced",
                {"task_id": task_id, "queue_round": queue_round + 1},
            )

            for task_id in [task_id]:
                if executed_actions >= max_actions:
                    break
                # —— 阶段 1：路由决策 —— 根据任务帧或 pending 队列构建路由决策
                router_decision = (
                    self._router_decision_from_turn_task_frame(turn_frame)
                    if uses_turn_frames
                    else self._router_decision_from_task_frame(
                        chat_session,
                        task_id,
                        "按 Router 已确定的任务顺序继续执行。",
                    )
                )
                if not router_decision:
                    continue
                yield self._stream_event(
                    "router_decision",
                    chat_session,
                    self._turn_payload(router_decision.model_dump(mode="json"), user_message_id),
                )

                # —— 阶段 2：应用决策与状态清理 ——
                before_skill = chat_session.active_skill_id
                before_step = chat_session.active_step_id
                self.runtime.apply_decision(chat_session, router_decision)
                state_pruned = self._drop_unavailable_skill_state(
                    request.tenant_id, chat_session, skills
                )
                if self._should_record_runtime_event_after_prune(
                    router_decision, chat_session, skills, state_pruned
                ):
                    self._record_runtime_event(
                        request.tenant_id, chat_session, before_skill, before_step, router_decision
                    )
                self.db.commit()
                self.db.refresh(chat_session)

                # —— 阶段 3：步骤推理 —— 若有活跃技能则运行 StepAgent
                active_skill = self._get_active_skill(
                    request.tenant_id, chat_session.active_skill_id, chat_session.agent_id
                )
                if not self._should_run_step_agent(router_decision, active_skill):
                    continue
                yield self._stream_event(
                    "skill_state",
                    chat_session,
                    self._skill_state_payload(
                        chat_session,
                        skills,
                        self._runtime_stream_context(
                            router_decision, before_skill, before_step, chat_session
                        ),
                        user_message_id=user_message_id,
                    ),
                )
                yield self._stream_status(
                    chat_session,
                    "stepping",
                    "正在思考",
                    {
                        "active_skill_id": chat_session.active_skill_id,
                        "active_step_id": chat_session.active_step_id,
                    },
                    user_message_id=user_message_id,
                )
                # 运行 StepAgent，支持上下文修复重试
                repair_stream_events: list[tuple[str, dict[str, object]]] = []
                step_result = self._run_step_agent_with_context_repair(
                    request,
                    chat_session,
                    active_skill,
                    tools,
                    model_config,
                    router_decision,
                    memory_context,
                    conversation_context,
                    repair_stream_events,
                )
                yield self._stream_event(
                    "step_result",
                    chat_session,
                    self._turn_payload(step_result.model_dump(mode="json"), user_message_id),
                )
                self.db.commit()
                self.db.refresh(chat_session)
                for event_name, payload in repair_stream_events:
                    yield self._stream_event(
                        event_name, chat_session, self._turn_payload(payload, user_message_id)
                    )

                # —— 阶段 4：工具调用 —— 若步骤结果含工具调用则执行工具循环
                tool_result = None
                if step_result.tool_call:
                    tool_stream_events: list[tuple[str, dict[str, object]]] = []
                    step_result, tool_result = self._execute_tool_action_cycle(
                        request,
                        chat_session,
                        active_skill,
                        tools,
                        model_config,
                        step_result,
                        tool_stream_events,
                        conversation_context=conversation_context,
                        memory_context=memory_context,
                    )
                    for event_name, payload in tool_stream_events:
                        yield self._stream_event(
                            event_name, chat_session, self._turn_payload(payload, user_message_id)
                        )

                # —— 阶段 5：反思重试 —— 评估步骤/工具结果质量并决定是否重试
                reflection_stream_events: list[tuple[str, dict[str, object]]] = []
                reflection_max_rounds = self._get_reflection_max_rounds(request.tenant_id)
                if reflection_max_rounds > 0 and self._should_try_reflection(
                    router_decision, step_result, tool_result
                ):
                    yield self._stream_status(
                        chat_session,
                        "reflecting",
                        "正在反思",
                        {"reflection_round": 1, "reflection_max_rounds": reflection_max_rounds},
                        user_message_id=user_message_id,
                    )
                (
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                ) = self._run_reflection_rounds(
                    request,
                    chat_session,
                    skills,
                    tools,
                    model_config,
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                    reflection_max_rounds,
                    conversation_context,
                    reflection_stream_events,
                    completed_skill_ids_this_turn,
                    memory_context=memory_context,
                )
                for event_name, payload in reflection_stream_events:
                    yield self._stream_event(event_name, chat_session, payload)

                # —— 阶段 6：技能图推进 —— 根据图编排自动推进到下一步骤
                graph_stream_events: list[tuple[str, dict[str, object]]] = []
                (
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                ) = self._auto_progress_skill_graph(
                    request,
                    chat_session,
                    skills,
                    tools,
                    model_config,
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                    memory_context,
                    conversation_context,
                    graph_stream_events,
                    completed_skill_ids_this_turn,
                )
                for event_name, payload in graph_stream_events:
                    yield self._stream_event(
                        event_name, chat_session, self._turn_payload(payload, user_message_id)
                    )

                # —— 阶段 7：收尾 —— 收集结果、判定完成状态
                task_results.append(
                    self._task_response_context(
                        chat_session,
                        active_skill,
                        router_decision,
                        step_result,
                        tool_result,
                    )
                )
                draft = self._task_response_draft(step_result)
                if draft:
                    replies, _ = self._merge_queued_reply_segment(replies, draft)
                executed_actions += 1
                # 判定当前任务收尾状态（继续/完成/转人工）
                finalize_state = self._finalize_execution_after_reply(
                    request.tenant_id,
                    chat_session,
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                )
                # 任务完成：记录已完成技能 ID
                if finalize_state == "completed" and active_skill:
                    completed_skill_ids_this_turn.add(active_skill.skill_id)
                # 转人工：立即返回续接结果，不再处理后续任务
                if finalize_state == "handoff":
                    return self._queued_continuation(
                        replies,
                        task_results,
                        active_skill,
                        router_decision,
                        step_result,
                        tool_result,
                    )
                # 任务仍继续（未完成）：判断是否还有后续任务可接
                if finalize_state == "continued":
                    # 轮内帧模式：还有剩余帧，挂起当前技能后继续下一个
                    if uses_turn_frames and remaining_turn_frames:
                        if chat_session.active_skill_id:
                            self.runtime.suspend_current_skill(chat_session, enqueue=True)
                        self.db.commit()
                        self.db.refresh(chat_session)
                        continue
                    # pending 模式：判断是否应继续尝试队列中的下一个任务
                    if self._should_attempt_queued_task_followup(
                        request,
                        chat_session,
                        skills,
                        "\n\n".join([completed_reply, *replies]).strip(),
                        queue_round + 1,
                    ):
                        if active_skill:
                            completed_skill_ids_this_turn.add(active_skill.skill_id)
                        continue
                    # 无法继续：返回已收集的续接结果
                    return self._queued_continuation(
                        replies,
                        task_results,
                        active_skill,
                        router_decision,
                        step_result,
                        tool_result,
                    )
                # 任务完成且无后续任务：记录 pending 等待事件
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "pending_tasks_waiting",
                    {
                        "pending_tasks": chat_session.pending_tasks_json or [],
                        "round": queue_round + 1,
                    },
                )
            # 达到最大动作数限制：退出循环
            if executed_actions >= max_actions:
                break

        return self._queued_continuation(
            replies,
            task_results,
            active_skill,
            router_decision,
            step_result,
            tool_result,
        )

    def handle_turn_stream(self, request: ChatTurnRequest) -> Iterator[dict[str, object]]:
        """处理一轮对话（流式入口）。

        这是 Agent 编排引擎的**流式主入口**。与 :meth:`handle_turn` 共享
        相同的编排逻辑，但通过 yield 逐步推送事件块，使前端能实时展示
        "正在路由""正在检索知识""正在思考""token 流"等中间状态。

        核心流程与 :meth:`handle_turn` 一致（准备→执行→回复→持久化），
        区别在于：
        - 通过 ``status_callback`` 将中间状态推入 ``stream_events`` 队列。
        - 回复文本通过 :meth:`_generate_reply_stream_segment` 逐 token 产出。
        - 支持取消检测（:func:`is_chat_turn_cancelled`），取消时持久化
          已生成的部分回复。
        - 异常时通过 :meth:`stream_failure_response` 推送错误事件块。

        内部定义了多个闭包（``record_current_turn_cancelled``、
        ``finalize_turn_once``、``recover_chat_session_after_exception`` 等）
        以处理流式场景下的取消、收尾和异常恢复。

        Yields:
            事件字典，类型包括 ``status``、``thinking``、``tool_call``、
            ``tool_result``、``knowledge_result``、``token``、
            ``reflection_decision``、``complete``、``error`` 等。
        """
        router_decision: RouterDecision | None = None
        step_result = StepAgentResult()
        tool_result: ToolResult | None = None
        chat_session: ChatSession | None = None
        reply = ""
        memory_model_config: ModelConfig | None = None
        turn_finalized = False
        user_message_id: str | None = None

        def record_current_turn_cancelled(client_turn_id: str | None = None) -> bool:
            """记录当前轮次已取消，持久化取消状态的 assistant 消息。

            在流式生成被用户取消时调用。该闭包会：
            1. 查找是否已存在同一轮次的取消事件（去重，避免重复持久化）。
            2. 若已存在则仅补做持久化；否则记录新的 ``stream_cancelled`` 事件。
            3. 持久化一条占位 assistant 消息（"已停止生成"）。
            4. 清除取消标记。

            使用 ``nonlocal turn_finalized`` 保证收尾操作只执行一次。

            Args:
                client_turn_id: 客户端轮次 ID（可选），用于跨服务端/客户端匹配取消事件。

            Returns:
                若成功记录取消则返回 ``True``；若会话未初始化或已收尾则返回 ``False``。
            """
            nonlocal turn_finalized
            # 前置检查：会话和用户消息 ID 必须就绪
            if not chat_session or not user_message_id:
                return False
            # 已收尾则不再重复处理
            if turn_finalized:
                return False
            normalized_client_turn_id = (client_turn_id or request.client_turn_id or "").strip()
            # 回滚未提交的变更，确保从干净状态读取取消事件
            self.db.rollback()
            # 查询已有的取消事件，判断是否已经记录过本轮取消
            existing_cancel = self.db.exec(
                select(AgentEvent)
                .where(
                    AgentEvent.tenant_id == request.tenant_id,
                    AgentEvent.session_id == chat_session.id,
                    AgentEvent.event_type == "stream_cancelled",
                )
                .order_by(AgentEvent.created_at.desc())
            ).all()
            for event in existing_cancel:
                payload = event.payload_json or {}
                # 收集事件中可能携带的各种轮次标识
                event_turn_ids = {
                    str(payload.get("turn_id") or "").strip(),
                    str(payload.get("user_message_id") or "").strip(),
                    str(payload.get("message_id") or "").strip(),
                    str(payload.get("client_turn_id") or "").strip(),
                }
                # 匹配服务端轮次 ID 或客户端轮次 ID
                matches_server_turn = user_message_id in event_turn_ids
                matches_client_turn = bool(
                    normalized_client_turn_id and normalized_client_turn_id in event_turn_ids
                )
                if matches_server_turn or matches_client_turn:
                    # 已有取消事件：仅补做消息持久化，不再重复记录事件
                    self._persist_cancelled_assistant_message(
                        request.tenant_id,
                        chat_session,
                        user_message_id,
                        normalized_client_turn_id,
                    )
                    clear_chat_turn_cancelled(chat_session.id, user_message_id)
                    if normalized_client_turn_id:
                        clear_chat_turn_cancelled(chat_session.id, normalized_client_turn_id)
                    turn_finalized = True
                    return True
            # 无已有取消事件：记录新的 stream_cancelled 事件
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "stream_cancelled",
                self._turn_payload(
                    {
                        "phase": "cancelled",
                        "text": "已停止生成",
                        "client_turn_id": normalized_client_turn_id or None,
                    },
                    user_message_id,
                ),
            )
            # 持久化取消时的占位 assistant 消息
            self._persist_cancelled_assistant_message(
                request.tenant_id,
                chat_session,
                user_message_id,
                normalized_client_turn_id,
            )
            self.db.commit()
            # 清除取消标记，避免后续误触发
            clear_chat_turn_cancelled(chat_session.id, user_message_id)
            if normalized_client_turn_id:
                clear_chat_turn_cancelled(chat_session.id, normalized_client_turn_id)
            turn_finalized = True
            return True

        def mark_current_turn_cancelled() -> bool:
            """检查取消标记并在已取消时调用 ``record_current_turn_cancelled``。"""
            if not chat_session or not user_message_id:
                return False
            client_turn_id = (request.client_turn_id or "").strip()
            server_cancelled = is_chat_turn_cancelled(chat_session.id, user_message_id)
            client_cancelled = bool(
                client_turn_id and is_chat_turn_cancelled(chat_session.id, client_turn_id)
            )
            if not server_cancelled and not client_cancelled:
                return False
            return record_current_turn_cancelled(client_turn_id)

        def finalize_turn_once(
            target_session: ChatSession,
            final_reply: str,
            final_step_result: StepAgentResult | None = None,
            final_source_message: str | None = None,
        ) -> None:
            """确保每轮的收尾操作只执行一次（幂等保护）。

            封装 :meth:`_finalize_turn`，通过 ``nonlocal turn_finalized``
            保证在流式管道的多个分支中（正常完成、取消、异常等），
            收尾持久化只执行一次。

            Args:
                target_session: 目标会话对象。
                final_reply: 最终回复文本。
                final_step_result: 步骤结果（可选）。
                final_source_message: 来源用户消息（可选）。
            """
            nonlocal turn_finalized
            # 幂等检查：已收尾则直接返回
            if turn_finalized:
                return
            self._finalize_turn(
                target_session,
                request.tenant_id,
                final_reply,
                final_step_result,
                final_source_message,
                user_message_id=user_message_id,
            )
            turn_finalized = True

        def recover_chat_session_after_exception() -> ChatSession:
            """在异常发生后恢复或重建聊天会话。

            异常可能导致会话状态不一致，该闭包会：
            1. 回滚当前数据库事务。
            2. 尝试按已有 session_id 重新加载会话。
            3. 若无法加载则创建新会话。
            4. 更新 ``nonlocal chat_session`` 指向恢复后的会话。

            Returns:
                恢复后的有效会话对象。
            """
            nonlocal chat_session
            # 获取已有 session_id（优先用当前引用，其次用请求中的）
            session_id = str(
                (chat_session.id if chat_session else request.session_id) or ""
            ).strip()
            # 回滚未提交的事务，确保数据库状态一致
            self.db.rollback()
            # 尝试从数据库重新加载会话
            recovered = self.db.get(ChatSession, session_id) if session_id else None
            if recovered is None:
                # 无法恢复时创建新会话
                recovered = self._get_or_create_session(request)
            chat_session = recovered
            return recovered

        def exception_payload(code: str, message: str) -> dict[str, object]:
            """构建异常事件的负载字典。

            将错误代码、消息、客户端轮次 ID 和截断后的 traceback 组装为
            事件负载，并补充轮次标识字段。

            Args:
                code: 机器可读的错误代码。
                message: 人类可读的错误描述。

            Returns:
                包含错误详情的事件负载字典（已附带 ``turn_id`` / ``user_message_id``）。
            """
            payload: dict[str, object] = {
                "code": code,
                "message": message,
                "client_turn_id": request.client_turn_id or None,
                # 截取最近 N 个字符的 traceback，避免超大日志
                "error_traceback": traceback.format_exc()[-ERROR_TRACEBACK_CHAR_LIMIT:],
            }
            return self._turn_payload(payload, user_message_id)

        def stream_failure_response(
            title: str,
            error: object,
            code: str,
            suggestion: str,
            message: str | None = None,
        ) -> Iterator[dict[str, object]]:
            """生成流式错误回复事件序列（恢复会话→记录错误→推送文本→收尾）。"""
            nonlocal reply
            target_session = recover_chat_session_after_exception()
            if mark_current_turn_cancelled():
                return
            error_message = message if message is not None else str(error)
            reply = format_runtime_failure_reply(title, error_message, code, suggestion)
            payload = exception_payload(code, error_message)
            self.events.record(request.tenant_id, target_session.id, "error_occurred", payload)
            self.db.commit()
            yield self._stream_status(
                target_session,
                "error",
                reply,
                {"code": code, "message": error_message},
                user_message_id=user_message_id,
            )
            yield self._stream_event("error_occurred", target_session, payload)
            for chunk in self.response_generator.chunk_text(reply):
                yield self._stream_event(
                    "stream_delta",
                    target_session,
                    self._turn_payload({"content": chunk}, user_message_id),
                )
                self._pace_stream()
            yield self._stream_event(
                "stream_end", target_session, self._turn_payload({}, user_message_id)
            )
            finalize_turn_once(target_session, reply, step_result, request.message)
            self.db.commit()
            self.db.refresh(target_session)

        try:
            # —— 阶段 A：会话初始化与用户消息持久化 ——
            chat_session = self._get_or_create_session(request)
            self._mark_session_running(chat_session)
            yield self._stream_event(
                "session_created",
                chat_session,
                {"newSessionId": chat_session.id, "sessionId": chat_session.id},
            )
            # 追加用户消息到数据库
            user_message = self._append_message(
                request.tenant_id,
                chat_session.id,
                "user",
                request.message,
                metadata=self._user_message_metadata(request),
            )
            user_message_id = user_message.id
            # 绑定事件轮次 ID（关联服务端消息 ID 与客户端轮次 ID）
            bind_event_turn = getattr(self.events, "bind_turn", None)
            if callable(bind_event_turn):
                bind_event_turn(user_message.id, request.client_turn_id)
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "user_message_received",
                {
                    "message_id": user_message.id,
                    "client_turn_id": request.client_turn_id,
                    "message": request.message,
                    "channel": request.channel,
                    "user_id": request.user_id,
                },
            )
            yield self._stream_event(
                "user_message_received",
                chat_session,
                self._turn_payload(
                    {
                        "message_id": user_message.id,
                        "client_turn_id": request.client_turn_id,
                        "message": request.message,
                        "channel": request.channel,
                        "user_id": request.user_id,
                    },
                    user_message.id,
                ),
            )
            self.db.commit()
            self.db.refresh(chat_session)
            self.db.refresh(user_message)
            model_config = self._get_request_model(request, chat_session.agent_id)
            if not model_config:
                raise AgentLoopPreconditionError("missing_model_config", "没有默认模型配置。")
            memory_model_config = model_config
            skills = self._list_published_skills(request.tenant_id, chat_session.agent_id)
            tools = self._tools_with_general_skills(
                request.tenant_id,
                self._list_enabled_tools(request.tenant_id, chat_session.agent_id),
                chat_session.agent_id,
            )
            persona_prompt = self._get_persona_prompt(request.tenant_id, chat_session.agent_id)
            self._drop_unavailable_skill_state(request.tenant_id, chat_session, skills)
            # 无场景技能降级分支：无可用 SOP 技能时，走通用技能 → 知识检索 → 直接回复
            if not skills:
                no_skill_context = self._conversation_context(
                    chat_session, model_config=model_config
                )
                # 上下文超限时触发压缩
                if self._context_compacted_now(no_skill_context):
                    yield self._stream_status(
                        chat_session,
                        "preparing",
                        "正在整理上下文",
                        user_message_id=user_message_id,
                    )
                yield self._stream_status(
                    chat_session, "routing", "正在判断用户意图", user_message_id=user_message_id
                )
                # 尝试选择通用技能
                capability = self._select_general_capability(
                    request.message,
                    model_config,
                    chat_session.agent_id,
                    no_skill_context,
                    [],
                )
                # 命中通用技能则直接流式执行并返回
                if capability[0] is not None:
                    yield from self._stream_general_skill_response(
                        request,
                        chat_session,
                        model_config,
                        (capability[0], capability[1]),
                        None,
                        [],
                        no_skill_context,
                        persona_prompt,
                        user_message.id,
                        mark_current_turn_cancelled,
                    )
                    return
                # 通用技能也未命中，构造 answer_only 决策并执行自动知识检索
                router_decision = RouterDecision(
                    decision="answer_only",
                    reason="No published scene skills are available; answer as chat.",
                )
                yield self._stream_event(
                    "router_decision",
                    chat_session,
                    self._turn_payload(router_decision.model_dump(mode="json"), user_message_id),
                )
                knowledge_stream_events: list[tuple[str, dict[str, object]]] = []
                step_result = self._auto_knowledge_step_result(
                    request,
                    chat_session,
                    model_config,
                    router_decision,
                    capability[1],
                    stream_events=knowledge_stream_events,
                )
                for event_name, payload in knowledge_stream_events:
                    yield self._stream_event(
                        event_name, chat_session, self._turn_payload(payload, user_message_id)
                    )
                yield self._stream_status(
                    chat_session, "responding", "正在生成回复", user_message_id=user_message_id
                )
                reply = ""
                for chunk in self._generate_reply_stream_segment(
                    request.message,
                    chat_session,
                    None,
                    router_decision,
                    step_result,
                    None,
                    model_config,
                    persona_prompt,
                    [],
                    no_skill_context,
                ):
                    reply += chunk
                    yield self._stream_event(
                        "stream_delta",
                        chat_session,
                        self._turn_payload({"content": chunk}, user_message_id),
                    )
                    self._pace_stream()
                if mark_current_turn_cancelled():
                    return
                yield self._stream_event(
                    "stream_end", chat_session, self._turn_payload({}, user_message_id)
                )
                if mark_current_turn_cancelled():
                    return
                finalize_turn_once(chat_session, reply, step_result, request.message)
                self.db.commit()
                self.db.refresh(chat_session)
                result = ChatTurnResponse(
                    reply=reply,
                    session_id=chat_session.id,
                    router_decision=router_decision,
                    step_result=step_result,
                    session_state=public_session(chat_session),
                )
                yield self._stream_event(
                    "complete",
                    chat_session,
                    self._turn_payload(result.model_dump(mode="json"), user_message_id),
                )
                return
            # 清理已过期的已完成技能状态，避免影响当前轮路由判断
            self._finish_stale_completed_skill(request.tenant_id, chat_session, skills)
            # 读取长期记忆上下文：召回与当前租户/用户/Agent 相关的记忆条目
            memory_context = [
                memory_read(row)
                for row in self.memory.context_memories(
                    request.tenant_id,
                    request.user_id,
                    agent_id=chat_session.agent_id,
                )
            ]
            # 记录记忆召回事件，供调试与审计追踪
            if memory_context:
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "memory_recalled",
                    {"memories": memory_context},
                )
            self.db.commit()
            self.db.refresh(chat_session)
            # 构建对话上下文（历史消息摘要），必要时触发上下文压缩
            conversation_context = self._conversation_context(chat_session, model_config=model_config)
            if self._context_compacted_now(conversation_context):
                yield self._stream_status(
                    chat_session,
                    "preparing",
                    "正在整理上下文",
                    user_message_id=user_message_id,
                )

            # 路由阶段：调用 Router 判定用户意图，决定走哪个技能/步骤
            yield self._stream_status(
                chat_session, "routing", "正在判断用户意图", user_message_id=user_message_id
            )
            router_decision = self.router.decide(
                request.message,
                chat_session,
                skills,
                model_config,
                conversation_context,
                memory_context,
            )
            # 从路由决策中提取本轮需要立即续接的后续任务帧（排除主任务本身）
            turn_followup_frames = self._turn_followup_task_frames(router_decision)
            # 水合路由决策中缺失的槽位：从历史上下文和记忆推断填充
            hydrated_slots = self._hydrate_router_decision_from_context(
                chat_session, router_decision, skills, memory_context
            )
            if hydrated_slots:
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "router_slots_hydrated",
                    hydrated_slots,
                )
            # 记录路由决策并推送流式事件
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "router_decision_created",
                self._turn_payload(router_decision.model_dump(), user_message_id),
            )
            yield self._stream_event(
                "router_decision",
                chat_session,
                self._turn_payload(router_decision.model_dump(mode="json"), user_message_id),
            )
            capability_selection: GeneralSkillSelection | None = None
            # 若路由判定推迟到通用技能（非场景 SOP），选择并尝试执行
            if self._scene_router_deferred_to_general(router_decision):
                capability = self._select_general_capability(
                    request.message,
                    model_config,
                    chat_session.agent_id,
                    conversation_context,
                    memory_context,
                )
                capability_selection = capability[1]
                # 若命中了通用技能，直接流式执行并返回，跳过 StepAgent 编排
                if capability[0] is not None:
                    yield from self._stream_general_skill_response(
                        request,
                        chat_session,
                        model_config,
                        (capability[0], capability[1]),
                        router_decision,
                        memory_context,
                        conversation_context,
                        persona_prompt,
                        user_message.id,
                        mark_current_turn_cancelled,
                    )
                    return

            # 应用路由决策到 SkillRuntime，记录决策前后的技能/步骤状态变化
            before_skill = chat_session.active_skill_id
            before_step = chat_session.active_step_id
            self.runtime.apply_decision(chat_session, router_decision)
            # 决策应用后可能引用了不可用技能，清理并按需记录事件
            state_pruned = self._drop_unavailable_skill_state(
                request.tenant_id, chat_session, skills
            )
            if self._should_record_runtime_event_after_prune(
                router_decision, chat_session, skills, state_pruned
            ):
                self._record_runtime_event(
                    request.tenant_id, chat_session, before_skill, before_step, router_decision
                )
            self.db.commit()
            self.db.refresh(chat_session)

            # 加载当前活跃技能对象
            active_skill = self._get_active_skill(
                request.tenant_id, chat_session.active_skill_id, chat_session.agent_id
            )
            # 分支：若不需要 StepAgent（如 answer_only），走自动知识检索 + 直接回复路径
            if not self._should_run_step_agent(router_decision, active_skill):
                knowledge_stream_events = []
                auto_step_result = self._auto_knowledge_step_result(
                    request,
                    chat_session,
                    model_config,
                    router_decision,
                    capability_selection,
                    stream_events=knowledge_stream_events,
                )
                # 若自动知识检索命中了查询，则用其结果替换空步骤结果
                if auto_step_result.knowledge_query:
                    step_result = auto_step_result
                for event_name, payload in knowledge_stream_events:
                    yield self._stream_event(
                        event_name, chat_session, self._turn_payload(payload, user_message_id)
                    )
                yield self._stream_status(
                    chat_session, "responding", "正在生成回复", user_message_id=user_message_id
                )
                for chunk in self._generate_reply_stream_segment(
                    request.message,
                    chat_session,
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                    model_config,
                    persona_prompt,
                    memory_context,
                    conversation_context,
                ):
                    reply += chunk
                    yield self._stream_event(
                        "stream_delta",
                        chat_session,
                        self._turn_payload({"content": chunk}, user_message_id),
                    )
                    self._pace_stream()
                yield self._stream_event(
                    "stream_end", chat_session, self._turn_payload({}, user_message_id)
                )
                finalize_turn_once(chat_session, reply, step_result, request.message)
                self.db.commit()
                self.db.refresh(chat_session)
                result = ChatTurnResponse(
                    reply=reply,
                    session_id=chat_session.id,
                    router_decision=router_decision,
                    step_result=step_result,
                    tool_result=tool_result,
                    session_state=public_session(chat_session),
                )
                yield self._stream_event(
                    "complete",
                    chat_session,
                    self._turn_payload(result.model_dump(mode="json"), user_message_id),
                )
                return
            # 推送技能状态变更事件（含决策前后对比），让前端同步展示
            yield self._stream_event(
                "skill_state",
                chat_session,
                self._skill_state_payload(
                    chat_session,
                    skills,
                    self._runtime_stream_context(
                        router_decision, before_skill, before_step, chat_session
                    ),
                    user_message_id=user_message_id,
                ),
            )
            # 推送"正在思考"状态，通知前端 StepAgent 已开始执行
            yield self._stream_status(
                chat_session,
                "stepping",
                "正在思考",
                {
                    "active_skill_id": chat_session.active_skill_id,
                    "active_step_id": chat_session.active_step_id,
                },
                user_message_id=user_message_id,
            )
            # 运行 StepAgent（含上下文修复重试），产出步骤结果并收集修复过程中的流式事件
            repair_stream_events: list[tuple[str, dict[str, object]]] = []
            step_result = self._run_step_agent_with_context_repair(
                request,
                chat_session,
                active_skill,
                tools,
                model_config,
                router_decision,
                memory_context,
                conversation_context,
                repair_stream_events,
            )
            # 推送步骤结果事件并回放修复过程中的流式事件
            yield self._stream_event(
                "step_result",
                chat_session,
                self._turn_payload(step_result.model_dump(mode="json"), user_message_id),
            )
            self.db.commit()
            self.db.refresh(chat_session)
            for event_name, payload in repair_stream_events:
                yield self._stream_event(
                    event_name, chat_session, self._turn_payload(payload, user_message_id)
                )

            # 若步骤结果包含知识查询，执行知识检索循环补充上下文
            if step_result.knowledge_query:
                knowledge_stream_events: list[tuple[str, dict[str, object]]] = []
                step_result = self._execute_knowledge_query_cycle(
                    request,
                    chat_session,
                    active_skill,
                    tools,
                    model_config,
                    step_result,
                    memory_context,
                    conversation_context,
                    knowledge_stream_events,
                )
                self.db.commit()
                self.db.refresh(chat_session)
                # 回放知识检索过程中收集的流式事件
                for event_name, payload in knowledge_stream_events:
                    yield self._stream_event(
                        event_name, chat_session, self._turn_payload(payload, user_message_id)
                    )
            # 若步骤结果包含工具调用，执行工具调用循环获取外部数据
            if step_result.tool_call:
                tool_stream_events: list[tuple[str, dict[str, object]]] = []
                step_result, tool_result = self._execute_tool_action_cycle(
                    request,
                    chat_session,
                    active_skill,
                    tools,
                    model_config,
                    step_result,
                    tool_stream_events,
                    conversation_context=conversation_context,
                    memory_context=memory_context,
                )
                # 回放工具执行过程中收集的流式事件
                for event_name, payload in tool_stream_events:
                    yield self._stream_event(
                        event_name, chat_session, self._turn_payload(payload, user_message_id)
                    )

            # 反思阶段：读取租户配置的最大反思轮数
            reflection_stream_events: list[tuple[str, dict[str, object]]] = []
            reflection_max_rounds = self._get_reflection_max_rounds(request.tenant_id)
            # 仅在配置允许反思且需要重试时，推送"正在反思"状态
            if reflection_max_rounds > 0 and self._should_try_reflection(
                router_decision, step_result, tool_result
            ):
                yield self._stream_status(
                    chat_session,
                    "reflecting",
                    "正在反思",
                    {"reflection_round": 1, "reflection_max_rounds": reflection_max_rounds},
                    user_message_id=user_message_id,
                )
            (
                active_skill,
                router_decision,
                step_result,
                tool_result,
            ) = self._run_reflection_rounds(
                request,
                chat_session,
                skills,
                tools,
                model_config,
                active_skill,
                router_decision,
                step_result,
                tool_result,
                reflection_max_rounds,
                conversation_context,
                reflection_stream_events,
                memory_context=memory_context,
            )
            # 回放反思过程中收集的流式事件
            for event_name, payload in reflection_stream_events:
                yield self._stream_event(
                    event_name, chat_session, self._turn_payload(payload, user_message_id)
                )

            # 自动推进技能图：在满足条件时连续推进技能步骤
            graph_stream_events: list[tuple[str, dict[str, object]]] = []
            (
                active_skill,
                router_decision,
                step_result,
                tool_result,
            ) = self._auto_progress_skill_graph(
                request,
                chat_session,
                skills,
                tools,
                model_config,
                active_skill,
                router_decision,
                step_result,
                tool_result,
                memory_context,
                conversation_context,
                graph_stream_events,
            )
            # 回放图推进过程中收集的流式事件
            for event_name, payload in graph_stream_events:
                yield self._stream_event(
                    event_name, chat_session, self._turn_payload(payload, user_message_id)
                )

            # 判定是否需要在生成回复前先收尾（有续接任务帧或定时任务模式）
            response_task_results: list[dict[str, object]] | None = None
            finalize_before_reply = bool(turn_followup_frames) or (
                request.interaction_mode == "scheduled_task"
            )
            if finalize_before_reply:
                # 先收集当前任务的结果上下文，再执行收尾判定（handoff/completed/continued）
                response_task_results = [
                    self._task_response_context(
                        chat_session,
                        active_skill,
                        router_decision,
                        step_result,
                        tool_result,
                    )
                ]
                finalize_state = self._finalize_execution_after_reply(
                    request.tenant_id,
                    chat_session,
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                )
            else:
                finalize_state = "continued"

            # 分支 A：有续接任务帧且未触发人工接管 —— 先保存主任务状态，再执行续接任务
            if turn_followup_frames and finalize_state != "handoff":
                # 快照主任务的执行状态，以便续接完成后恢复
                primary_active_skill = active_skill
                primary_router_decision = router_decision
                primary_step_result = step_result
                primary_tool_result = tool_result
                paused_primary = None
                # 若主任务仍在进行中，先挂起以便后续恢复
                if finalize_state == "continued" and active_skill:
                    paused_primary = self.runtime.suspend_current_skill(chat_session)
                continuation = None
                # 流式执行续接任务（按 turn_followup_frames 顺序逐个处理）
                if turn_followup_frames:
                    continuation = yield from self._stream_continue_pending_after_completion(
                        request,
                        chat_session,
                        model_config,
                        skills,
                        tools,
                        persona_prompt,
                        memory_context,
                        conversation_context,
                        "",
                        user_message_id=user_message_id,
                        turn_task_frames=turn_followup_frames,
                    )
                # 合并续接任务的结果到主任务结果列表
                if continuation and response_task_results is not None:
                    response_task_results.extend(continuation.task_results)
                # 恢复主任务状态：若主任务之前被挂起，则恢复并更新执行状态
                if paused_primary:
                    if chat_session.active_skill_id:
                        self.runtime.suspend_current_skill(chat_session, enqueue=True)
                    self.runtime.restore_task_frame(chat_session, paused_primary)
                    self.db.commit()
                    self.db.refresh(chat_session)
                    active_skill = primary_active_skill
                    router_decision = primary_router_decision
                    step_result = primary_step_result
                    tool_result = primary_tool_result
                # 若无主任务需恢复但有续接结果，则使用续接的最终状态
                elif continuation:
                    active_skill = continuation.active_skill
                    router_decision = continuation.router_decision
                    step_result = continuation.step_result
                    tool_result = continuation.tool_result
            # 分支 B：定时任务模式下技能已完成 —— 尝试续接下一个 pending 任务
            elif finalize_state == "completed" and request.interaction_mode == "scheduled_task":
                continuation = None
                # 检查是否还有待执行的 pending 任务
                if self._next_pending_task_id(chat_session):
                    continuation = yield from self._stream_continue_pending_after_completion(
                        request,
                        chat_session,
                        model_config,
                        skills,
                        tools,
                        persona_prompt,
                        memory_context,
                        conversation_context,
                        "",
                        user_message_id=user_message_id,
                    )
                if continuation:
                    if response_task_results is not None:
                        response_task_results.extend(continuation.task_results)
                    active_skill = continuation.active_skill
                    router_decision = continuation.router_decision
                    step_result = continuation.step_result
                    tool_result = continuation.tool_result
                elif chat_session.pending_tasks_json:
                    # 定时任务模式下无续接结果，但仍有待执行任务 → 记录等待事件
                    self.events.record(
                        request.tenant_id,
                        chat_session.id,
                        "pending_tasks_waiting",
                        {"pending_tasks": chat_session.pending_tasks_json or []},
                    )

            # 回复生成阶段：流式生成最终回复
            yield self._stream_status(
                chat_session, "responding", "正在生成回复", user_message_id=user_message_id
            )
            chunks: list[str] = []
            # 调用回复生成器流式产出回复片段，逐段推送 stream_delta
            for chunk in self._generate_reply_stream_segment(
                request.message,
                chat_session,
                active_skill,
                router_decision,
                step_result,
                tool_result,
                model_config,
                persona_prompt,
                memory_context,
                conversation_context,
                response_task_results,
            ):
                chunks.append(chunk)
                yield self._stream_event(
                    "stream_delta",
                    chat_session,
                    self._turn_payload({"content": chunk}, user_message_id),
                )
                self._pace_stream()
            # 拼接完整回复；若生成器未产出内容，使用兜底回复文本
            reply = "".join(chunks).strip() or FALLBACK_REPLY
            # 兜底回复也需要分片流式推送
            if not chunks:
                for chunk in self.response_generator.chunk_text(reply):
                    chunks.append(chunk)
                    yield self._stream_event(
                        "stream_delta",
                        chat_session,
                        self._turn_payload({"content": chunk}, user_message_id),
                    )
                    self._pace_stream()
            # 若之前未在回复前收尾（无续接任务），则在回复后执行收尾判定
            if not finalize_before_reply:
                self._finalize_execution_after_reply(
                    request.tenant_id,
                    chat_session,
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                )
            # 检查取消标志：若用户已取消本轮则直接退出
            if mark_current_turn_cancelled():
                return
            # 推送 stream_end 标志流式输出结束
            yield self._stream_event(
                "stream_end", chat_session, self._turn_payload({}, user_message_id)
            )
            if mark_current_turn_cancelled():
                return

        # 异常处理策略：
        # GeneratorExit —— 用户断开连接，回滚未提交的事务
        except GeneratorExit:
            if chat_session and user_message_id:
                try:
                    if not mark_current_turn_cancelled():
                        self.db.rollback()
                except Exception:
                    self.db.rollback()
            raise
        except AgentLoopPreconditionError as exc:
            yield from stream_failure_response(
                "系统配置错误",
                exc.message,
                exc.code,
                "请在管理端补齐配置后重试。",
                message=exc.message,
            )
            return
        except LLMError as exc:
            yield from stream_failure_response(
                "模型调用失败", exc, "LLM_ERROR", model_failure_suggestion(exc)
            )
            return
        except Exception as exc:
            turn_finalized = False
            yield from stream_failure_response(
                "Agent Loop 出错",
                exc,
                "AGENT_LOOP_ERROR",
                "请查看执行记录或服务日志定位具体原因。",
            )
            return

        turn_commit_completed = False
        try:
            if not chat_session:
                chat_session = self._get_or_create_session(request)
            if mark_current_turn_cancelled():
                return
            finalize_turn_once(chat_session, reply, step_result, request.message)
            self.db.commit()
            turn_commit_completed = True
            self.db.refresh(chat_session)
            if memory_model_config:
                self._enqueue_memory_capture(
                    request,
                    chat_session,
                    step_result,
                    tool_result,
                    memory_model_config,
                )
            result = ChatTurnResponse(
                reply=reply,
                session_id=chat_session.id,
                router_decision=router_decision,
                step_result=step_result,
                tool_result=tool_result,
                session_state=public_session(chat_session),
            )
            yield self._stream_event(
                "complete",
                chat_session,
                self._turn_payload(result.model_dump(mode="json"), user_message_id),
            )
        except Exception as exc:
            if not turn_commit_completed:
                turn_finalized = False
            yield from stream_failure_response(
                "Agent Loop 出错",
                exc,
                "AGENT_LOOP_ERROR",
                "请查看执行记录或服务日志定位具体原因。",
            )

    def _stream_status(
        self,
        chat_session: ChatSession,
        phase: str,
        text: str,
        extra: dict[str, object] | None = None,
        user_message_id: str | None = None,
    ) -> dict[str, object]:
        """构建并记录一个 ``status`` 类型的流式事件。"""
        payload: dict[str, object] = {"phase": phase, "text": text, **(extra or {})}
        if user_message_id:
            payload = self._turn_payload(payload, user_message_id)
            if phase != "received":
                self.events.record(
                    chat_session.tenant_id, chat_session.id, "stream_status", payload
                )
                self.db.commit()
        return self._stream_event(
            "status",
            chat_session,
            payload,
        )

    def _stream_event(
        self,
        kind: str,
        chat_session: ChatSession,
        payload: dict[str, object],
    ) -> dict[str, object]:
        """构建一个流式事件字典，并将持久化类型的事件落库。

        对于 ``persisted_stream_events`` 集合中的事件类型（如 stream_delta、
        tool_result、step_result 等），当 payload 携带 turn_id 或 user_message_id
        时，将事件记录到事件表中并提交事务，以便后续回放和审计。

        Args:
            kind: 事件类型（如 ``"stream_delta"``、``"step_result"``）。
            chat_session: 当前会话。
            payload: 事件负载数据。

        Returns:
            标准化的 SSE 事件字典 ``{"event": kind, "data": {...}}``。
        """
        persisted_stream_events = {
            "agent_loop_completed",
            "agent_loop_continued",
            "general_skill_run_finished",
            "general_skill_trace",
            "knowledge_result",
            "reflection_decision",
            "skill_state",
            "step_result",
            "stream_delta",
            "stream_replace",
            "stream_end",
            "tool_result",
        }
        if kind in persisted_stream_events and (
            payload.get("turn_id") or payload.get("user_message_id")
        ):
            self.events.record(chat_session.tenant_id, chat_session.id, kind, payload)
            self.db.commit()
        data = {
            "kind": kind,
            "sessionId": chat_session.id,
            "timestamp": utc_now().isoformat(),
            "provider": "skill",
            **payload,
        }
        return {"event": kind, "data": data}

    def _pace_stream(self) -> None:
        """在流式推送之间插入短暂延迟，避免前端事件洪泛。"""
        sleep(STREAM_CHUNK_INTERVAL_SECONDS)

    def _prepare_turn(
        self, request: ChatTurnRequest, status_callback: StatusCallback | None = None
    ) -> PreparedTurn:
        """准备阶段：完成路由、技能应用、步骤执行、知识查询、工具调用、反思。

        这是编排管道的**核心准备方法**，被 :meth:`handle_turn` 和
        :meth:`handle_turn_stream` 共同调用。完整执行以下子步骤：

        1. 获取/创建会话，标记为 running，追加用户消息并记录事件。
        2. 加载模型配置、可用技能列表、工具列表（含通用技能工具）。
        3. 若无可用技能：尝试通用技能 → 知识检索 → 直接回复。
        4. 清理过期技能状态，读取记忆上下文，构建对话上下文。
        5. 调用 :class:`Router` 进行意图路由，水合缺失槽位。
        6. 若路由推迟到通用技能：执行通用技能并直接返回。
        7. 应用技能决策 (:class:`SkillRuntime`)，推进到目标步骤。
        8. 若不需要 StepAgent：执行自动知识检索并返回。
        9. 运行 StepAgent（含上下文修复），产出步骤结果。
        10. 若步骤结果含知识查询：执行知识检索循环。
        11. 若步骤结果含工具调用：执行工具调用循环。
        12. 执行反思重试 (:meth:`_run_reflection_rounds`)。
        13. 自动推进技能图 (:meth:`_auto_progress_skill_graph`)。

        Args:
            request: 用户请求。
            status_callback: 可选的状态回调，用于流式推送中间状态。

        Returns:
            封装了所有中间状态的 :class:`PreparedTurn`。

        Raises:
            AgentLoopPreconditionError: 缺少模型配置等前置条件不满足时。
        """
        def status(phase: str, payload: dict[str, object] | None = None) -> None:
            if status_callback:
                status_callback(phase, payload or {})

        chat_session = self._get_or_create_session(request)
        self._mark_session_running(chat_session)
        status("received", {"session_id": chat_session.id})
        user_message = self._append_message(
            request.tenant_id,
            chat_session.id,
            "user",
            request.message,
            metadata=self._user_message_metadata(request),
        )
        bind_event_turn = getattr(self.events, "bind_turn", None)
        if callable(bind_event_turn):
            bind_event_turn(user_message.id, request.client_turn_id)
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "user_message_received",
            {
                "message_id": user_message.id,
                "client_turn_id": request.client_turn_id,
                "message": request.message,
                "channel": request.channel,
                "user_id": request.user_id,
            },
        )

        model_config = self._get_request_model(request, chat_session.agent_id)
        skills = self._list_published_skills(request.tenant_id, chat_session.agent_id)
        tools = self._tools_with_general_skills(
            request.tenant_id,
            self._list_enabled_tools(request.tenant_id, chat_session.agent_id),
            chat_session.agent_id,
        )
        if not model_config:
            raise AgentLoopPreconditionError("missing_model_config", "没有默认模型配置。")
        self._drop_unavailable_skill_state(request.tenant_id, chat_session, skills)
        if not skills:
            no_skill_context = self._conversation_context(
                chat_session, model_config=model_config
            )
            if self._context_compacted_now(no_skill_context):
                status("preparing", {"compacted_now": True})
            capability = self._select_general_capability(
                request.message,
                model_config,
                chat_session.agent_id,
                no_skill_context,
                [],
            )
            router_decision = RouterDecision(
                decision="answer_only",
                reason="No published scene skills are available; try general skills, then answer as chat.",
            )
            general_response = self._try_handle_general_skill_after_scene_router(
                request,
                chat_session,
                model_config,
                router_decision,
                [],
                no_skill_context,
                user_message.id,
                capability,
            )
            if general_response:
                return PreparedTurn(
                    chat_session=chat_session,
                    model_config=model_config,
                    active_skill=None,
                    router_decision=router_decision,
                    step_result=StepAgentResult(),
                    tool_result=None,
                    memory_context=[],
                    conversation_context=no_skill_context,
                    general_response=general_response,
                    user_message_id=user_message.id,
                )
            # 通用技能未命中，退而执行自动知识检索并封装结果返回
            step_result = self._auto_knowledge_step_result(
                request,
                chat_session,
                model_config,
                router_decision,
                capability[1],
                status_callback=status,
            )
            return PreparedTurn(
                chat_session=chat_session,
                model_config=model_config,
                active_skill=None,
                router_decision=router_decision,
                step_result=step_result,
                tool_result=None,
                memory_context=[],
                conversation_context=no_skill_context,
                user_message_id=user_message.id,
            )
        # 步骤 4：清理过期/已完成的技能状态，读取长期记忆上下文
        self._finish_stale_completed_skill(request.tenant_id, chat_session, skills)
        memory_context = [
            memory_read(row)
            for row in self.memory.context_memories(
                request.tenant_id,
                request.user_id,
                agent_id=chat_session.agent_id,
            )
        ]
        # 若召回了记忆上下文则记录事件，便于追踪与调试
        if memory_context:
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "memory_recalled",
                {"memories": memory_context},
            )
        self.db.commit()
        self.db.refresh(chat_session)
        # 步骤 5：构建对话上下文（含历史消息摘要），必要时触发上下文压缩
        conversation_context = self._conversation_context(chat_session, model_config=model_config)
        if self._context_compacted_now(conversation_context):
            status("preparing", {"compacted_now": True})

        # 步骤 6：调用 Router 进行意图路由，判定走哪个技能/步骤或直接回答
        status("routing")
        router_decision = self.router.decide(
            request.message,
            chat_session,
            skills,
            model_config,
            conversation_context,
            memory_context,
        )
        # 水合路由决策中缺失的槽位：从历史上下文和记忆中推断填充
        hydrated_slots = self._hydrate_router_decision_from_context(
            chat_session, router_decision, skills, memory_context
        )
        if hydrated_slots:
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "router_slots_hydrated",
                hydrated_slots,
            )
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "router_decision_created",
            self._turn_payload(router_decision.model_dump(), user_message.id),
        )
        # 步骤 7：若路由判定推迟到通用技能（非场景 SOP），尝试执行通用技能
        capability: tuple[GeneralSkill | None, GeneralSkillSelection] | None = None
        if self._scene_router_deferred_to_general(router_decision):
            capability = self._select_general_capability(
                request.message,
                model_config,
                chat_session.agent_id,
                conversation_context,
                memory_context,
            )
        general_response = self._try_handle_general_skill_after_scene_router(
            request,
            chat_session,
            model_config,
            router_decision,
            memory_context,
            conversation_context,
            user_message.id,
            capability,
        )
        if general_response:
            # 通用技能已处理完成，提前返回，跳过 StepAgent 编排
            return PreparedTurn(
                chat_session=chat_session,
                model_config=model_config,
                active_skill=None,
                router_decision=router_decision,
                step_result=StepAgentResult(),
                tool_result=None,
                memory_context=memory_context,
                conversation_context=conversation_context,
                general_response=general_response,
                user_message_id=user_message.id,
            )

        # 步骤 8：应用路由决策到 SkillRuntime，推进到目标技能/步骤
        before_skill = chat_session.active_skill_id
        before_step = chat_session.active_step_id
        self.runtime.apply_decision(chat_session, router_decision)
        # 决策应用后可能引用了不可用技能，再次清理并记录状态变化事件
        state_pruned = self._drop_unavailable_skill_state(request.tenant_id, chat_session, skills)
        if self._should_record_runtime_event_after_prune(
            router_decision, chat_session, skills, state_pruned
        ):
            self._record_runtime_event(
                request.tenant_id, chat_session, before_skill, before_step, router_decision
            )
        self.db.commit()
        self.db.refresh(chat_session)

        # 重新加载当前活跃技能对象（可能因决策而切换）
        active_skill = self._get_active_skill(
            request.tenant_id, chat_session.active_skill_id, chat_session.agent_id
        )
        # 步骤 9：若不需要运行 StepAgent（如 answer_only 场景），仅执行自动知识检索后返回
        if not self._should_run_step_agent(router_decision, active_skill):
            step_result = self._auto_knowledge_step_result(
                request,
                chat_session,
                model_config,
                router_decision,
                capability[1] if capability else None,
                status_callback=status,
            )
            return PreparedTurn(
                chat_session=chat_session,
                model_config=model_config,
                active_skill=active_skill,
                router_decision=router_decision,
                step_result=step_result,
                tool_result=None,
                memory_context=memory_context,
                conversation_context=conversation_context,
                user_message_id=user_message.id,
            )
        # 步骤 10：运行 StepAgent（含上下文自动修复），产出步骤结果
        status(
            "stepping",
            {
                "active_skill_id": chat_session.active_skill_id,
                "active_step_id": chat_session.active_step_id,
            },
        )
        step_result = self._run_step_agent_with_context_repair(
            request,
            chat_session,
            active_skill,
            tools,
            model_config,
            router_decision,
            memory_context,
            conversation_context,
        )

        tool_result: ToolResult | None = None
        self.db.commit()
        self.db.refresh(chat_session)
        # 步骤 11：若步骤结果包含知识查询，执行知识检索循环补充上下文
        if step_result.knowledge_query:
            step_result = self._execute_knowledge_query_cycle(
                request,
                chat_session,
                active_skill,
                tools,
                model_config,
                step_result,
                memory_context,
                conversation_context,
                status_callback=status,
            )
            self.db.commit()
            self.db.refresh(chat_session)
        # 步骤 12：若步骤结果包含工具调用，执行工具调用循环获取外部数据
        if step_result.tool_call:
            step_result, tool_result = self._execute_tool_action_cycle(
                request,
                chat_session,
                active_skill,
                tools,
                model_config,
                step_result,
                status_callback=status,
                conversation_context=conversation_context,
                memory_context=memory_context,
            )

        # 步骤 13：执行反思重试循环，评估步骤/工具质量并按需重试
        (
            active_skill,
            router_decision,
            step_result,
            tool_result,
        ) = self._run_reflection_rounds(
            request,
            chat_session,
            skills,
            tools,
            model_config,
            active_skill,
            router_decision,
            step_result,
            tool_result,
            self._get_reflection_max_rounds(request.tenant_id),
            conversation_context,
            memory_context=memory_context,
        )
        # 步骤 14：自动推进技能图：当当前步骤完成且有后续可执行步骤时继续推进
        (
            active_skill,
            router_decision,
            step_result,
            tool_result,
        ) = self._auto_progress_skill_graph(
            request,
            chat_session,
            skills,
            tools,
            model_config,
            active_skill,
            router_decision,
            step_result,
            tool_result,
            memory_context,
            conversation_context,
        )

        # 封装所有中间状态返回给调用方
        return PreparedTurn(
            chat_session=chat_session,
            model_config=model_config,
            active_skill=active_skill,
            router_decision=router_decision,
            step_result=step_result,
            tool_result=tool_result,
            memory_context=memory_context,
            conversation_context=conversation_context,
            user_message_id=user_message.id,
        )

    def _finalize_execution_after_reply(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> ExecutionFinalizeState:
        """在回复生成后判定执行状态的收尾分支。

        检查是否需要人工接管（``handoff_human``）或技能是否已完成，
        返回三种状态之一：

        - ``"handoff"`` —— 已创建人工接管请求
        - ``"completed"`` —— 技能已完成
        - ``"continued"`` —— 技能仍在进行中

        Args:
            tenant_id: 租户 ID。
            chat_session: 当前会话。
            active_skill: 活跃技能。
            router_decision: 路由决策。
            step_result: 步骤结果。
            tool_result: 工具结果。

        Returns:
            执行收尾状态字符串。
        """
        # 判断是否需要人工接管：路由决策或步骤结果中任一声明 handoff 即触发
        requested_handoff = router_decision.decision == "handoff_human" or step_result.handoff
        if requested_handoff:
            # 仅当当前步骤在技能图中声明了 handoff 动作时才真正创建接管请求
            if self._current_step_allows_human_handoff(active_skill, chat_session.active_step_id):
                self._create_human_handoff_request(
                    tenant_id, chat_session, active_skill, step_result
                )
                return "handoff"
            else:
                # 步骤未声明 handoff，记录忽略事件以便排查路由与步骤配置不一致的情况
                self.events.record(
                    tenant_id,
                    chat_session.id,
                    "human_handoff_ignored",
                    {
                        "reason": "current_step_does_not_declare_handoff",
                        "active_skill_id": chat_session.active_skill_id,
                        "active_step_id": chat_session.active_step_id,
                        "router_decision": router_decision.decision,
                        "step_handoff": step_result.handoff,
                    },
                )
        # 不需要接管时，检查技能是否已满足完成条件（步骤完成 + 工具成功 + 图终止等）
        if self._should_complete_skill(active_skill, chat_session, step_result, tool_result):
            self._complete_active_skill(tenant_id, chat_session, active_skill, "step_completed")
            return "completed"
        # 既不需要接管也未完成，技能仍在进行中
        return "continued"

    def _current_step_allows_human_handoff(
        self, skill: Skill | None, active_step_id: str | None
    ) -> bool:
        """判断当前技能步骤是否声明了人工接管（handoff）动作。

        Args:
            skill: 活跃技能，为 None 时直接返回 False。
            active_step_id: 当前步骤 ID。

        Returns:
            当前步骤的节点类型为 ``handoff`` 或其动作列表包含 ``handoff_human`` 时返回 True。
        """
        if not skill:
            return False
        current_step = self._current_skill_step(skill, active_step_id)
        if not current_step:
            return False
        return self._step_declares_human_handoff(current_step)

    def _step_declares_human_handoff(self, step: dict[str, Any]) -> bool:
        """检查步骤节点是否声明了人工接管。

        判定依据为节点类型为 ``handoff`` 或步骤动作列表中包含 ``handoff_human``。

        Args:
            step: 技能步骤字典。

        Returns:
            是否声明了人工接管。
        """
        node_type = str(step.get("type") or "").strip()
        return node_type == "handoff" or "handoff_human" in self._step_actions(step)

    def _create_human_handoff_request(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill | None,
        step_result: StepAgentResult,
    ) -> HumanHandoffRequest:
        """创建或复用人工接管请求。

        若当前会话已有 pending 状态的接管请求则直接复用；否则创建新的
        :class:`HumanHandoffRequest`，包含上下文摘要、待处理问题、恢复载荷等，
        并将会话状态设为 ``handoff``。

        Returns:
            人工接管请求对象。
        """
        existing = self.db.exec(
            select(HumanHandoffRequest)
            .where(HumanHandoffRequest.tenant_id == tenant_id)
            .where(HumanHandoffRequest.session_id == chat_session.id)
            .where(HumanHandoffRequest.status == "pending")
        ).first()
        if existing:
            # 复用已有的 pending 接管请求：更新会话状态并设置 awaiting_input
            chat_session.status = "handoff"
            chat_session.awaiting_input_json = {
                "type": "human_handoff",
                "handoff_id": existing.id,
                "pending_question": existing.pending_question,
            }
            chat_session.updated_at = utc_now()
            return existing

        # 获取当前步骤信息，用于构建接管的上下文
        current_step = (
            self._current_skill_step(active_skill, chat_session.active_step_id)
            if active_skill
            else None
        )
        # 创建新的接管请求，包含指派人、上下文摘要、待处理问题和恢复载荷
        handoff = HumanHandoffRequest(
            tenant_id=tenant_id,
            session_id=chat_session.id,
            agent_id=chat_session.agent_id,
            requester_user_id=chat_session.user_id,
            # 指派人优先级：Agent 元数据中的负责人 → 租户管理员 → 发起用户
            assignee_user_id=self._human_handoff_assignee_user_id(
                tenant_id, chat_session.agent_id, chat_session.user_id
            ),
            trigger_skill_id=chat_session.active_skill_id,
            trigger_step_id=chat_session.active_step_id,
            context_summary=self._human_handoff_context_summary(chat_session),
            pending_question=self._human_handoff_pending_question(current_step, step_result),
            # 恢复载荷保存当前技能/步骤/slots/pending，供人工处理后继续执行
            resume_payload_json={
                "active_skill_id": chat_session.active_skill_id,
                "active_step_id": chat_session.active_step_id,
                "slots": chat_session.slots_json or {},
                "pending_tasks": chat_session.pending_tasks_json or [],
            },
            metadata_json={
                "step": current_step or {},
                "step_reply": step_result.reply,
                "step_handoff": step_result.handoff,
            },
        )
        self.db.add(handoff)
        # 将会话状态切换为 handoff，并记录 awaiting_input 以阻塞自动执行
        chat_session.status = "handoff"
        chat_session.awaiting_input_json = {
            "type": "human_handoff",
            "handoff_id": handoff.id,
            "pending_question": handoff.pending_question,
        }
        chat_session.updated_at = utc_now()
        self.events.record(
            tenant_id,
            chat_session.id,
            "human_handoff_requested",
            {
                "handoff_id": handoff.id,
                "agent_id": handoff.agent_id,
                "assignee_user_id": handoff.assignee_user_id,
                "trigger_skill_id": handoff.trigger_skill_id,
                "trigger_step_id": handoff.trigger_step_id,
                "pending_question": handoff.pending_question,
            },
        )
        return handoff

    def _human_handoff_assignee_user_id(
        self, tenant_id: str, agent_id: str | None, fallback_user_id: str | None
    ) -> str | None:
        """确定人工接管请求的指派人用户 ID。

        按优先级依次尝试三种来源：
        1. Agent 元数据中的所有者/创建者字段（owner_user_id 等）。
        2. 租户管理员（role == "admin" 中最早的注册用户）。
        3. 传入的 fallback_user_id（通常为发起用户自身）。

        Args:
            tenant_id: 租户 ID。
            agent_id: 当前 Agent ID（用于查询元数据中的所有者）。
            fallback_user_id: 兜底用户 ID。

        Returns:
            指派人的用户 ID；若以上来源均无可用值则返回 None。
        """
        if agent_id:
            agent = self.db.exec(
                select(AgentProfile).where(
                    AgentProfile.tenant_id == tenant_id, AgentProfile.id == agent_id
                )
            ).first()
            metadata = agent.metadata_json if agent else {}
            if isinstance(metadata, dict):
                for key in (
                    "owner_user_id",
                    "created_by_user_id",
                    "creator_user_id",
                    "created_by",
                    "owner_id",
                ):
                    value = metadata.get(key)
                    if value:
                        return str(value)
        tenant_admin = self._human_handoff_tenant_admin_user_id(tenant_id)
        if tenant_admin:
            return tenant_admin
        return fallback_user_id

    def _human_handoff_tenant_admin_user_id(self, tenant_id: str) -> str | None:
        """查询租户管理员的用户 ID，作为人工接管的兜底指派人。"""
        row = self.db.exec(
            select(User)
            .where(User.tenant_id == tenant_id, User.role == "admin")
            .order_by(User.created_at)
        ).first()
        return row.id if row else None

    def _human_handoff_context_summary(self, chat_session: ChatSession) -> str:
        """构建人工接管的上下文摘要（最近 8 条消息的精简拼接）。

        Args:
            chat_session: 当前会话。

        Returns:
            以 "role: content" 格式拼接的对话摘要字符串。
        """
        rows = self.db.exec(
            select(Message)
            .where(Message.session_id == chat_session.id)
            .order_by(Message.created_at.desc())
            .limit(8)
        ).all()
        lines: list[str] = []
        for message in reversed(rows):
            content = re.sub(r"\s+", " ", message.content or "").strip()
            if not content:
                continue
            lines.append(f"{message.role}: {content[:240]}")
        return "\n".join(lines)

    def _human_handoff_pending_question(
        self, current_step: dict[str, Any] | None, step_result: StepAgentResult
    ) -> str:
        """提取人工接管时的待处理问题文本（优先步骤回复，其次步骤配置）。"""
        candidates: list[Any] = [
            step_result.reply,
            current_step.get("handoff_question") if current_step else None,
            current_step.get("question") if current_step else None,
            current_step.get("name") if current_step else None,
        ]
        for candidate in candidates:
            text = re.sub(r"\s+", " ", str(candidate or "")).strip()
            if text:
                return text[:600]
        return "当前 SOP 需要人工确认后继续执行。"

    def _generate_reply_segment(
        self,
        message: str,
        chat_session: ChatSession,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        model_config: ModelConfig,
        persona_prompt: str | None,
        memory_context: list[dict[str, object]],
        conversation_context: dict[str, object],
        task_results: list[dict[str, object]] | None = None,
    ) -> str:
        """同步生成完整回复文本（非流式）。

        委托给 :attr:`response_generator.generate`，将所有上下文信息整合后
        一次性返回完整回复字符串。

        Args:
            message: 用户原始消息。
            chat_session: 当前会话。
            active_skill: 活跃技能（可选）。
            router_decision: 路由决策。
            step_result: 步骤结果。
            tool_result: 工具结果（可选）。
            model_config: 模型配置。
            persona_prompt: 人格提示词（可选）。
            memory_context: 记忆上下文。
            conversation_context: 对话上下文。
            task_results: 多任务执行结果列表（可选）。

        Returns:
            完整回复文本字符串。
        """
        return self.response_generator.generate(
            message,
            chat_session,
            active_skill,
            router_decision,
            step_result,
            tool_result,
            model_config,
            persona_prompt,
            memory_context,
            conversation_context,
            task_results,
        )

    def _generate_reply_stream_segment(
        self,
        message: str,
        chat_session: ChatSession,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        model_config: ModelConfig,
        persona_prompt: str | None,
        memory_context: list[dict[str, object]],
        conversation_context: dict[str, object],
        task_results: list[dict[str, object]] | None = None,
    ) -> Iterator[str]:
        """流式生成回复文本片段（逐 chunk 产出）。

        委托给 :attr:`response_generator.generate_stream`，参数与
        :meth:`_generate_reply_segment` 相同，但以迭代器形式逐段产出回复，
        适用于 SSE 流式推送场景。

        Yields:
            回复文本的逐段片段字符串。
        """
        yield from self.response_generator.generate_stream(
            message,
            chat_session,
            active_skill,
            router_decision,
            step_result,
            tool_result,
            model_config,
            persona_prompt,
            memory_context,
            conversation_context,
            task_results,
        )

    def _task_response_context(
        self,
        chat_session: ChatSession,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> dict[str, object]:
        """构建任务回复的上下文摘要字典，供回复生成器引用。"""
        return {
            "task": router_decision.user_intent
            or (active_skill.name if active_skill else "当前任务"),
            "current_step_id": chat_session.active_step_id,
            "skill_content": dict(active_skill.content_json or {}) if active_skill else None,
            "slots": dict(chat_session.slots_json or {}),
            "step_result": step_result.model_dump(mode="json"),
            "tool_result": tool_result.model_dump(mode="json") if tool_result else None,
        }

    def _task_response_draft(self, step_result: StepAgentResult) -> str:
        """提取步骤结果中的回复草稿文本（去除首尾空白）。

        Args:
            step_result: 步骤结果对象。

        Returns:
            去除首尾空白后的回复文本；若为空则返回空字符串。
        """
        return str(step_result.reply or "").strip()

    def _try_continue_pending_after_completion(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        model_config: ModelConfig,
        skills: list[Skill],
        tools: list[Any],
        persona_prompt: str | None,
        memory_context: list[dict[str, object]],
        conversation_context: dict[str, object],
        completed_reply: str,
        completed_skill_ids_this_turn: set[str] | None = None,
        turn_task_frames: list[PendingTask] | None = None,
    ) -> QueuedTaskContinuation | None:
        """在当前技能完成后，尝试自动续接下一个排队中的待执行任务。

        该方法实现了"多任务串行执行"核心逻辑：当主任务完成后，如果还有
        pending 任务（或 Router 本轮分配的 task_frames），则按顺序逐个执行，
        每个任务都经历完整的"路由 → 步骤执行 → 工具调用 → 反思 → 图推进 →
        收尾"流程，直到达到最大动作数限制或没有更多待执行任务。

        Args:
            request: 对话轮次请求。
            chat_session: 当前会话。
            model_config: 模型配置。
            skills: 可见技能列表。
            tools: 可见工具列表。
            persona_prompt: 人格提示词。
            memory_context: 记忆上下文。
            conversation_context: 对话上下文。
            completed_reply: 已完成任务的回复文本（用于续接判断）。
            completed_skill_ids_this_turn: 本轮已完成的技能 ID 集合。
            turn_task_frames: Router 本轮分配的任务帧列表（若提供则优先使用）。

        Returns:
            续接结果封装对象，若无待执行任务或无回复则返回 None。
        """
        # 确定任务来源：优先使用 Router 本轮分配的 turn_task_frames，否则回退到会话 pending 队列
        remaining_turn_frames = list(turn_task_frames or [])
        uses_turn_frames = turn_task_frames is not None
        if uses_turn_frames and not remaining_turn_frames:
            return None
        if not uses_turn_frames and not chat_session.pending_tasks_json:
            return None
        # 获取租户配置的最大动作数（每轮最多执行多少个任务）
        max_actions = max(1, self._get_agent_loop_max_actions(request.tenant_id))
        executed_actions = 0
        replies: list[str] = []
        task_results: list[dict[str, object]] = []
        completed_skill_ids_this_turn = completed_skill_ids_this_turn or set()
        active_skill: Skill | None = None
        router_decision = RouterDecision(decision="answer_only", reason="No pending task selected")
        step_result = StepAgentResult()
        tool_result: ToolResult | None = None

        # 逐轮执行排队任务，直到达到 max_actions 或没有更多任务
        for queue_round in range(max_actions):
            if uses_turn_frames:
                # 从 turn_task_frames 中取出下一个任务帧
                if not remaining_turn_frames:
                    break
                turn_frame = remaining_turn_frames.pop(0)
                task_id = turn_frame.task_id or f"turn_task_{queue_round + 1}"
            else:
                # 从会话 pending 队列中取出下一个待执行任务 ID
                if not chat_session.pending_tasks_json:
                    break
                task_id = self._next_pending_task_id(chat_session)
                if not task_id:
                    break
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "router_execution_order_advanced",
                {"task_id": task_id, "queue_round": queue_round + 1},
            )

            for task_id in [task_id]:
                if executed_actions >= max_actions:
                    break
                # 根据任务来源构建路由决策：turn_frame 或 pending 队列帧
                router_decision = (
                    self._router_decision_from_turn_task_frame(turn_frame)
                    if uses_turn_frames
                    else self._router_decision_from_task_frame(
                        chat_session,
                        task_id,
                        "按 Router 已确定的任务顺序继续执行。",
                    )
                )
                if not router_decision:
                    continue

                # 记录路由前的技能/步骤快照，用于事件记录
                before_skill = chat_session.active_skill_id
                before_step = chat_session.active_step_id
                # 应用路由决策，切换到目标任务对应的技能/步骤
                self.runtime.apply_decision(chat_session, router_decision)
                # 清理引用了不可用技能的状态
                state_pruned = self._drop_unavailable_skill_state(
                    request.tenant_id, chat_session, skills
                )
                if self._should_record_runtime_event_after_prune(
                    router_decision, chat_session, skills, state_pruned
                ):
                    self._record_runtime_event(
                        request.tenant_id, chat_session, before_skill, before_step, router_decision
                    )
                self.db.commit()
                self.db.refresh(chat_session)

                # 获取路由后激活的技能对象
                active_skill = self._get_active_skill(
                    request.tenant_id, chat_session.active_skill_id, chat_session.agent_id
                )
                # 判断是否需要运行 StepAgent（仅当决策需要执行步骤时）
                if not self._should_run_step_agent(router_decision, active_skill):
                    continue
                # 运行步骤 Agent（含上下文修复重试机制）
                step_result = self._run_step_agent_with_context_repair(
                    request,
                    chat_session,
                    active_skill,
                    tools,
                    model_config,
                    router_decision,
                    memory_context,
                    conversation_context,
                )
                self.db.commit()
                self.db.refresh(chat_session)
                tool_result = None
                # 若步骤结果包含工具调用，执行工具动作循环（含权限校验、幂等重放等）
                if step_result.tool_call:
                    step_result, tool_result = self._execute_tool_action_cycle(
                        request,
                        chat_session,
                        active_skill,
                        tools,
                        model_config,
                        step_result,
                        conversation_context=conversation_context,
                        memory_context=memory_context,
                    )

                # 反思重试循环：评估步骤/工具结果质量，必要时切换技能或重试工具
                (
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                ) = self._run_reflection_rounds(
                    request,
                    chat_session,
                    skills,
                    tools,
                    model_config,
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                    self._get_reflection_max_rounds(request.tenant_id),
                    conversation_context,
                    completed_skill_ids_this_turn=completed_skill_ids_this_turn,
                    memory_context=memory_context,
                )
                # 自动推进技能图：在满足条件时连续推进步骤
                (
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                ) = self._auto_progress_skill_graph(
                    request,
                    chat_session,
                    skills,
                    tools,
                    model_config,
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                    memory_context,
                    conversation_context,
                    completed_skill_ids_this_turn=completed_skill_ids_this_turn,
                )
                # 收集任务结果上下文（用于回复生成）
                task_results.append(
                    self._task_response_context(
                        chat_session,
                        active_skill,
                        router_decision,
                        step_result,
                        tool_result,
                    )
                )
                # 提取步骤回复草稿并合并到回复列表
                draft = self._task_response_draft(step_result)
                if draft:
                    replies, _ = self._merge_queued_reply_segment(replies, draft)
                executed_actions += 1
                # 收尾判定：判断该任务是已完成、需人工接管还是仍进行中
                finalize_state = self._finalize_execution_after_reply(
                    request.tenant_id,
                    chat_session,
                    active_skill,
                    router_decision,
                    step_result,
                    tool_result,
                )
                if finalize_state == "completed" and active_skill:
                    # 技能已完成，加入本轮已完成集合以避免反思重复触发
                    completed_skill_ids_this_turn.add(active_skill.skill_id)
                if finalize_state == "handoff":
                    # 触发了人工接管，立即返回当前已收集的续接结果
                    return self._queued_continuation(
                        replies,
                        task_results,
                        active_skill,
                        router_decision,
                        step_result,
                        tool_result,
                    )
                if finalize_state == "continued":
                    if uses_turn_frames and remaining_turn_frames:
                        # turn_frame 模式：挂起当前技能以执行下一个任务帧
                        if chat_session.active_skill_id:
                            self.runtime.suspend_current_skill(chat_session, enqueue=True)
                        self.db.commit()
                        self.db.refresh(chat_session)
                        continue
                    # 非 turn_frame 模式：判断是否应继续尝试后续排队任务（仅定时任务模式）
                    if self._should_attempt_queued_task_followup(
                        request,
                        chat_session,
                        skills,
                        "\n\n".join([completed_reply, *replies]).strip(),
                        queue_round + 1,
                    ):
                        if active_skill:
                            completed_skill_ids_this_turn.add(active_skill.skill_id)
                        continue
                    return self._queued_continuation(
                        replies,
                        task_results,
                        active_skill,
                        router_decision,
                        step_result,
                        tool_result,
                    )
                # 技能处于等待用户输入状态，记录 pending_tasks_waiting 事件
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "pending_tasks_waiting",
                    {
                        "pending_tasks": chat_session.pending_tasks_json or [],
                        "round": queue_round + 1,
                    },
                )
            if executed_actions >= max_actions:
                break

        # 所有任务执行完毕或达到上限，返回最终续接结果
        return self._queued_continuation(
            replies,
            task_results,
            active_skill,
            router_decision,
            step_result,
            tool_result,
        )

    def _queued_continuation(
        self,
        replies: list[str],
        task_results: list[dict[str, object]],
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> QueuedTaskContinuation | None:
        """将排队任务续接的多轮执行结果封装为统一返回对象。

        当所有排队任务执行完毕或中途终止（如触发 handoff）时调用，
        将收集到的回复片段、任务结果和最终执行状态打包。

        Args:
            replies: 各任务产生的回复片段列表。
            task_results: 各任务的结果上下文列表。
            active_skill: 最后一个执行的任务对应的活跃技能。
            router_decision: 最后一次路由决策。
            step_result: 最后一次步骤结果。
            tool_result: 最后一次工具结果。

        Returns:
            续接结果封装对象；若无回复且无任务结果则返回 None。
        """
        if not replies and not task_results:
            return None
        return QueuedTaskContinuation(
            reply="\n\n".join(replies).strip(),
            task_results=task_results,
            active_skill=active_skill,
            router_decision=router_decision,
            step_result=step_result,
            tool_result=tool_result,
        )

    def _should_attempt_queued_task_followup(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        skills: list[Skill],
        completed_reply: str,
        schedule_round: int,
    ) -> bool:
        """判断是否应该尝试排队任务续接（定时任务模式下且有 pending 任务）。"""
        if request.interaction_mode != "scheduled_task":
            return False
        if chat_session.awaiting_input_json:
            return False
        if not chat_session.pending_tasks_json:
            return False

        self._finish_stale_completed_skill(request.tenant_id, chat_session, skills)
        self._drop_unavailable_skill_state(request.tenant_id, chat_session, skills)
        self.db.commit()
        self.db.refresh(chat_session)

        if chat_session.awaiting_input_json or chat_session.active_skill_id:
            return False
        if not chat_session.pending_tasks_json:
            return False

        self.events.record(
            request.tenant_id,
            chat_session.id,
            "scheduled_task_followup_requested",
            {
                "round": schedule_round,
                "pending_tasks": chat_session.pending_tasks_json or [],
                "completed_reply": completed_reply[:500],
                "reason": "scheduled_task_mode_attempts_to_finish_pending_work",
            },
        )
        return True

    def _merge_queued_reply_segment(
        self, replies: list[str], segment: str
    ) -> tuple[list[str], bool]:
        """将一段回复文本合并到排队任务回复列表中。

        Args:
            replies: 已收集的回复片段列表。
            segment: 待合并的新回复片段。

        Returns:
            (更新后的回复列表, 是否发生了合并去重) 二元组。
            第二个元素当前始终为 False（预留接口）。
        """
        clean_segment = str(segment or "").strip()
        if not clean_segment:
            return replies, False
        return [*replies, clean_segment], False

    def _router_decision_from_task_frame(
        self,
        chat_session: ChatSession,
        task_id: str,
        order_reason: str | None = None,
    ) -> RouterDecision | None:
        """根据会话 pending 队列中的任务帧构建路由决策。

        从 ``pending_tasks_json`` 中查找指定 ``task_id`` 对应的任务帧，
        提取技能 ID、步骤 ID、slots 提示等信息，构建一个
        ``switch_to_pending`` 类型的路由决策。

        Args:
            chat_session: 当前会话。
            task_id: 待执行任务的 ID。
            order_reason: 覆盖任务帧中默认 reason 的排序原因文本（可选）。

        Returns:
            路由决策对象；若任务帧不存在或缺少技能 ID 则返回 None。
        """
        frame = self._find_task_frame(chat_session, task_id)
        if not frame:
            return None
        # 提取目标技能 ID（兼容 skill_id 和 target_skill_id 两种字段名）
        skill_id = frame.get("skill_id") or frame.get("target_skill_id")
        if not skill_id:
            return None
        # 提取 slot 提示（兼容 slots 和 slot_hints 两种字段名）
        slot_hints = {}
        if isinstance(frame.get("slots"), dict):
            slot_hints = dict(frame["slots"])
        elif isinstance(frame.get("slot_hints"), dict):
            slot_hints = dict(frame["slot_hints"])
        return RouterDecision(
            decision="switch_to_pending",
            selected_task_id=str(task_id),
            target_skill_id=str(skill_id),
            target_step_id=frame.get("step_id") or frame.get("target_step_id"),
            confidence=float(frame.get("confidence") or 0.0),
            user_intent=frame.get("intent_summary") or frame.get("user_intent"),
            reason=order_reason or frame.get("reason"),
            source_message=frame.get("source_message"),
            slot_hints=slot_hints,
        )

    def _find_task_frame(self, chat_session: ChatSession, task_id: str) -> dict[str, Any] | None:
        """在会话 pending 队列中按 task_id 查找任务帧。

        Args:
            chat_session: 当前会话。
            task_id: 任务 ID。

        Returns:
            匹配的任务帧字典；若未找到则返回 None。
        """
        for frame in chat_session.pending_tasks_json or []:
            if isinstance(frame, dict) and str(frame.get("task_id") or "") == str(task_id):
                return frame
        return None

    def _turn_followup_task_frames(
        self, router_decision: RouterDecision
    ) -> list[PendingTask]:
        """从路由决策中提取需要在本轮立即续接的任务帧（排除与主任务相同的首帧）。"""
        frames = list(router_decision.task_frames or [])
        if not frames:
            return []
        first = frames[0]
        if first.target_skill_id == router_decision.target_skill_id:
            return frames[1:]
        return frames

    def _router_decision_from_turn_task_frame(
        self, frame: PendingTask
    ) -> RouterDecision:
        """从 Router 本轮分配的任务帧构建 ``start_new_task`` 路由决策。

        与 :meth:`_router_decision_from_task_frame` 不同，此方法处理的是
        Router 在当前轮次中即时分配的 :class:`PendingTask` 对象（而非
        会话中持久化的 pending 队列帧），决策类型为 ``start_new_task``。

        Args:
            frame: Router 分配的任务帧对象。

        Returns:
            对应的路由决策对象。
        """
        return RouterDecision(
            decision="start_new_task",
            target_skill_id=frame.target_skill_id,
            target_step_id=frame.target_step_id,
            confidence=frame.confidence,
            user_intent=frame.user_intent,
            reason=frame.reason or "按 Router 本轮 task_frames 顺序继续执行。",
            source_message=frame.source_message,
            slot_hints=dict(frame.slot_hints or {}),
            task_frames=[frame],
        )

    def _next_pending_task_id(self, chat_session: ChatSession) -> str | None:
        """获取会话 pending 队列中下一个可执行任务的 ID。

        遍历 ``pending_tasks_json``，返回第一个状态为 ``pending`` 且
        关联了技能 ID 的任务的 ``task_id``。

        Args:
            chat_session: 当前会话。

        Returns:
            下一个待执行任务的 ID；若无符合条件的任务则返回 None。
        """
        for frame in chat_session.pending_tasks_json or []:
            if not isinstance(frame, dict):
                continue
            if str(frame.get("status") or "pending") != "pending":
                continue
            if not (frame.get("skill_id") or frame.get("target_skill_id")):
                continue
            task_id = str(frame.get("task_id") or "").strip()
            if task_id:
                return task_id
        return None

    def _run_reflection_rounds(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        skills: list[Skill],
        tools: list[Tool],
        model_config: ModelConfig,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        max_rounds: int,
        conversation_context: dict[str, object] | None = None,
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
        completed_skill_ids_this_turn: set[str] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> tuple[Skill | None, RouterDecision, StepAgentResult, ToolResult | None]:
        """反思重试循环：评估步骤/工具结果质量并按需重试。

        在准备阶段末尾执行。根据租户配置的最大反思轮数（上限 5 轮），
        循环调用 :meth:`_should_try_reflection` 判断是否需要反思，
        若需要则调用 :meth:`_reflect_and_retry` 执行反思与重试。

        当反思轮数配置为 0 时，跳过反思但记录 ``reflection_skipped`` 事件。

        Args:
            max_rounds: 租户配置的反思最大轮数。
            stream_events: 流式事件收集列表（可选）。
            completed_skill_ids_this_turn: 本轮已完成的技能 ID 集合。
            memory_context: 记忆上下文。

        Returns:
            更新后的 (active_skill, router_decision, step_result, tool_result) 四元组。
        """
        if conversation_context is None:
            conversation_context = self._conversation_context(chat_session)
        completed_skill_ids_this_turn = completed_skill_ids_this_turn or set()
        # 将租户配置的反思轮数限制在全局上限 REFLECTION_MAX_ROUNDS_LIMIT（当前为 5）以内
        rounds = max(0, min(max_rounds, REFLECTION_MAX_ROUNDS_LIMIT))
        # 分支 A：反思轮数配置为 0 —— 跳过反思但记录事件，便于运营排查
        if rounds <= 0:
            if self._should_try_reflection(router_decision, step_result, tool_result):
                payload = {
                    "needs_retry": False,
                    "reason": "企业端反思轮数配置为 0，已跳过反思。",
                    "target_skill_id": None,
                    "target_step_id": None,
                    "target_tool_name": None,
                    "skipped": True,
                    "skip_reason": "reflection_disabled",
                }
                events = getattr(self, "events", None)
                if events is not None:
                    events.record(request.tenant_id, chat_session.id, "reflection_skipped", payload)
                if stream_events is not None:
                    stream_events.append(("reflection_decision", payload))
            return active_skill, router_decision, step_result, tool_result
        # 分支 B：反思轮数 > 0 —— 逐轮执行反思与重试
        # 循环终止条件：达到最大轮数 / 不再需要反思(_should_try_reflection 返回 False) / 反思未触发重试(retried=False)
        for round_index in range(rounds):
            # 每轮开始先判断是否仍需要反思，不需要则提前退出循环
            if not self._should_try_reflection(router_decision, step_result, tool_result):
                break
            # 第二轮起推送 reflecting 状态事件，通知前端当前处于反思阶段
            if stream_events is not None and round_index > 0:
                stream_events.append(
                    (
                        "status",
                        {
                            "phase": "reflecting",
                            "text": "正在反思",
                            "reflection_round": round_index + 1,
                            "reflection_max_rounds": rounds,
                        },
                    )
                )
            # 调用单轮反思+重试，retried 标记是否实际执行了重试动作
            (
                active_skill,
                router_decision,
                step_result,
                tool_result,
                retried,
            ) = self._reflect_and_retry(
                request,
                chat_session,
                skills,
                tools,
                model_config,
                active_skill,
                router_decision,
                step_result,
                tool_result,
                conversation_context,
                stream_events,
                completed_skill_ids_this_turn,
                memory_context,
            )
            # 若反思未触发重试（例如反思认为结果已合格），终止循环
            if not retried:
                break
        return active_skill, router_decision, step_result, tool_result

    def _auto_progress_skill_graph(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        skills: list[Skill],
        tools: list[Tool],
        model_config: ModelConfig,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
        completed_skill_ids_this_turn: set[str] | None = None,
    ) -> tuple[Skill | None, RouterDecision, StepAgentResult, ToolResult | None]:
        """自动推进技能图：在满足条件时连续推进技能步骤。

        当当前步骤已满足完成条件且技能图中有后续可执行步骤时，
        自动激活下一个步骤并重新运行 StepAgent，最多循环
        ``max_actions`` 次。

        自动推进算法的每轮循环：
        1. 前置守卫检查：当前步骤是否已完成、是否无工具/交接阻塞、
           图中是否有未完成工作、期望信息是否已满足。
        2. 快照推进前状态（active_step_id + pending_steps），用于
           检测是否陷入"无变化"死循环。
        3. 构造 ``continue_active`` 路由决策，重新运行 StepAgent 推进。
        4. 若 StepAgent 产出知识查询 → 执行知识检索循环。
        5. 若 StepAgent 产出工具调用 → 执行工具执行循环。
        6. 执行反思重试循环，对本轮结果进行质量评估。
        7. 比较前后状态，若无变化且无新动作 → 跳出循环避免死循环。

        Returns:
            更新后的 (active_skill, router_decision, step_result, tool_result) 四元组。
        """
        if conversation_context is None:
            conversation_context = self._conversation_context(chat_session)
        completed_skill_ids_this_turn = completed_skill_ids_this_turn or set()
        max_actions = max(1, self._get_agent_loop_max_actions(request.tenant_id))
        for iteration in range(max_actions):
            # 每轮重新获取最新的技能对象（可能因状态变更而切换）
            active_skill = self._get_active_skill(
                request.tenant_id, chat_session.active_skill_id, chat_session.agent_id
            )
            # ── 前置守卫条件：不满足任一条件则终止自动推进 ──
            if not active_skill or not step_result.is_step_completed:
                break  # 无激活技能或当前步骤未完成
            if step_result.tool_call or step_result.handoff:
                break  # 有待处理工具调用或交接，交给上层处理
            if not self._graph_flow_has_unfinished_work(active_skill, chat_session, step_result):
                break  # 图中已无未完成的后续工作
            if not self._current_step_expected_info_satisfied(active_skill, chat_session):
                break  # 当前步骤的期望用户信息尚未满足

            # 推送"继续推进 SOP 分支"状态事件到流式队列
            payload = {
                "phase": "skill",
                "text": "继续推进 SOP 分支",
                "active_skill_id": chat_session.active_skill_id,
                "active_step_id": chat_session.active_step_id,
                "pending_step_ids": self._graph_pending_steps(chat_session),
                "iteration": iteration + 1,
                "max_iterations": max_actions,
            }
            self.events.record(
                request.tenant_id, chat_session.id, "graph_auto_progress_started", payload
            )
            if stream_events is not None:
                stream_events.append(("status", payload))

            # 快照推进前的状态，用于循环结束时检测是否陷入无变化死循环
            before_state = (
                chat_session.active_step_id,
                tuple(self._graph_pending_steps(chat_session)),
            )
            # 构造"继续当前技能"的路由决策，驱动 StepAgent 推进到下一节点
            router_decision = RouterDecision(
                decision="continue_active",
                target_skill_id=active_skill.skill_id,
                target_step_id=chat_session.active_step_id,
                confidence=max(router_decision.confidence, 0.7),
                user_intent=router_decision.user_intent or "继续执行 SOP 图",
                reason="SOP 图还有可自动执行的后续节点。",
                source_message=router_decision.source_message or request.message,
                slot_hints={},
            )
            # 运行 StepAgent（含上下文修复重试），产出新的步骤结果
            repair_events: list[tuple[str, dict[str, object]]] | None = (
                [] if stream_events is not None else None
            )
            step_result = self._run_step_agent_with_context_repair(
                request,
                chat_session,
                active_skill,
                tools,
                model_config,
                router_decision,
                memory_context,
                conversation_context,
                repair_events,
            )
            self.db.commit()
            self.db.refresh(chat_session)
            if repair_events:
                stream_events.extend(repair_events)

            # 若 StepAgent 产出知识查询请求 → 执行知识检索循环
            if step_result.knowledge_query:
                knowledge_events: list[tuple[str, dict[str, object]]] | None = (
                    [] if stream_events is not None else None
                )
                step_result = self._execute_knowledge_query_cycle(
                    request,
                    chat_session,
                    active_skill,
                    tools,
                    model_config,
                    step_result,
                    memory_context,
                    conversation_context,
                    knowledge_events,
                )
                self.db.commit()
                self.db.refresh(chat_session)
                if knowledge_events:
                    stream_events.extend(knowledge_events)

            # 若 StepAgent 产出工具调用 → 执行工具执行循环
            if step_result.tool_call:
                tool_events: list[tuple[str, dict[str, object]]] | None = (
                    [] if stream_events is not None else None
                )
                step_result, tool_result = self._execute_tool_action_cycle(
                    request,
                    chat_session,
                    active_skill,
                    tools,
                    model_config,
                    step_result,
                    tool_events,
                    conversation_context=conversation_context,
                    memory_context=memory_context,
                )
                self.db.commit()
                self.db.refresh(chat_session)
                if tool_events:
                    stream_events.extend(tool_events)

            # 执行反思重试循环：对本轮步骤/工具结果进行质量评估和按需重试
            reflection_events: list[tuple[str, dict[str, object]]] | None = (
                [] if stream_events is not None else None
            )
            active_skill, router_decision, step_result, tool_result = self._run_reflection_rounds(
                request,
                chat_session,
                skills,
                tools,
                model_config,
                active_skill,
                router_decision,
                step_result,
                tool_result,
                self._get_reflection_max_rounds(request.tenant_id),
                conversation_context,
                reflection_events,
                completed_skill_ids_this_turn,
                memory_context,
            )
            if reflection_events:
                stream_events.extend(reflection_events)

            # 比较推进前后的状态快照，检测是否陷入无变化死循环
            after_state = (
                chat_session.active_step_id,
                tuple(self._graph_pending_steps(chat_session)),
            )
            # 若状态无变化且无新的工具调用/知识查询 → 跳出循环，避免无限推进
            if (
                after_state == before_state
                and not step_result.tool_call
                and not step_result.knowledge_query
            ):
                break
        return active_skill, router_decision, step_result, tool_result

    def _reflect_and_retry(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        skills: list[Skill],
        tools: list[Tool],
        model_config: ModelConfig,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        conversation_context: dict[str, object] | None = None,
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
        completed_skill_ids_this_turn: set[str] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> tuple[Skill | None, RouterDecision, StepAgentResult, ToolResult | None, bool]:
        """执行单轮反思与重试。

        调用 :class:`ReflectionAgent` 审查当前步骤/工具结果，根据反思决策
        选择重试路径：
        1. 若反思建议重试工具调用且目标为当前技能 → :meth:`_retry_with_reflection_tool_call`
        2. 若反思建议重试路由决策 → :meth:`_retry_with_router_decision`
        3. 若反思建议重试工具调用（其他目标） → :meth:`_retry_with_reflection_tool_call`

        Args:
            stream_events: 流式事件收集列表。
            completed_skill_ids_this_turn: 本轮已完成的技能 ID 集合。

        Returns:
            (active_skill, router_decision, step_result, tool_result, retried) 五元组，
            ``retried`` 表示是否实际执行了重试。
        """
        if conversation_context is None:
            conversation_context = self._conversation_context(chat_session)
        completed_skill_ids_this_turn = completed_skill_ids_this_turn or set()
        if not self._should_try_reflection(router_decision, step_result, tool_result):
            return active_skill, router_decision, step_result, tool_result, False

        try:
            reflection = self.reflection_agent.review(
                request.message,
                chat_session,
                active_skill,
                router_decision,
                step_result,
                tool_result,
                skills,
                tools,
                model_config,
                conversation_context,
                memory_context,
            )
        except LLMError as exc:
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "reflection_error",
                {"message": str(exc)},
            )
            if stream_events is not None:
                stream_events.append(
                    (
                        "reflection_decision",
                        {
                            "needs_retry": False,
                            "reason": f"反思失败：{exc}",
                            "target_skill_id": None,
                            "target_step_id": None,
                            "target_tool_name": None,
                        },
                    )
                )
            return active_skill, router_decision, step_result, tool_result, False

        self.events.record(
            request.tenant_id,
            chat_session.id,
            "reflection_decision_created",
            reflection.model_dump(),
        )
        if stream_events is not None:
            stream_events.append(("reflection_decision", reflection.model_dump(mode="json")))
        if not reflection.needs_retry:
            return active_skill, router_decision, step_result, tool_result, False

        retry_tool_call = self._tool_call_from_reflection(
            reflection,
            chat_session,
            tools,
            request.message,
        )
        if retry_tool_call and self._reflection_tool_retry_targets_current_skill(
            reflection, chat_session
        ):
            retry_result = self._retry_with_reflection_tool_call(
                request,
                chat_session,
                active_skill,
                router_decision,
                retry_tool_call,
                reflection.reason,
                stream_events,
                tools,
                model_config,
                conversation_context,
                memory_context,
            )
            return (*retry_result, True)

        retry_router_decision = self._router_decision_from_reflection(
            reflection,
            chat_session,
            skills,
            router_decision,
            completed_skill_ids_this_turn,
        )
        if retry_router_decision:
            retry_result = self._retry_with_router_decision(
                request,
                chat_session,
                skills,
                tools,
                retry_router_decision,
                model_config,
                conversation_context,
                stream_events,
                memory_context,
            )
            return (*retry_result, True)

        if retry_tool_call:
            retry_result = self._retry_with_reflection_tool_call(
                request,
                chat_session,
                active_skill,
                router_decision,
                retry_tool_call,
                reflection.reason,
                stream_events,
                tools,
                model_config,
                conversation_context,
                memory_context,
            )
            return (*retry_result, True)

        self.events.record(
            request.tenant_id,
            chat_session.id,
            "reflection_retry_skipped",
            {
                "reason": reflection.reason,
                "target_skill_id": reflection.target_skill_id,
                "target_tool_name": reflection.target_tool_name,
            },
        )
        return active_skill, router_decision, step_result, tool_result, False

    def _retry_with_reflection_tool_call(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        retry_tool_call: ToolCall,
        retry_reason: str | None,
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
        tools: list[Tool] | None = None,
        model_config: ModelConfig | None = None,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> tuple[Skill | None, RouterDecision, StepAgentResult, ToolResult | None]:
        """基于反思决策重试工具调用。

        构造一个包含重试工具调用的 StepAgentResult，然后通过
        :meth:`_execute_tool_action_cycle` 执行完整的工具循环。

        Args:
            retry_tool_call: 反思建议重试的工具调用。
            retry_reason: 反思给出的重试原因说明。
            stream_events: 流式事件收集列表。
            tools: 可用工具列表。
            model_config: 模型配置。
            conversation_context: 对话上下文。
            memory_context: 记忆上下文。

        Returns:
            (active_skill, router_decision, retry_step_result, retry_tool_result) 四元组。
        """
        # 构造重试步骤结果：标记当前步骤已完成，携带重试工具调用
        retry_step_result = StepAgentResult(
            tool_call=retry_tool_call,
            next_step_id=chat_session.active_step_id,
            is_step_completed=True,
        )
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "reflection_retry_started",
            {
                "mode": "tool",
                "reason": retry_reason,
                "target_tool_name": retry_tool_call.name,
            },
        )
        retry_step_result, retry_tool_result = self._execute_tool_action_cycle(
            request,
            chat_session,
            active_skill,
            tools or [],
            model_config,
            retry_step_result,
            stream_events,
            conversation_context=conversation_context,
            memory_context=memory_context,
        )
        return active_skill, router_decision, retry_step_result, retry_tool_result

    def _reflection_tool_retry_targets_current_skill(
        self, reflection: ReflectionDecision, chat_session: ChatSession
    ) -> bool:
        """判断反思建议的工具重试是否指向当前激活的技能。

        当反思决策指定了 target_tool_name，且 target_skill_id 为空或等于
        当前技能时，认为重试目标为当前技能。

        Args:
            reflection: 反思决策对象。
            chat_session: 当前会话。

        Returns:
            是否目标当前技能。
        """
        return bool(
            reflection.target_tool_name
            and (
                not reflection.target_skill_id
                or reflection.target_skill_id == chat_session.active_skill_id
            )
        )

    def _retry_with_router_decision(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        skills: list[Skill],
        tools: list[Tool],
        router_decision: RouterDecision,
        model_config: ModelConfig,
        conversation_context: dict[str, object],
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> tuple[Skill | None, RouterDecision, StepAgentResult, ToolResult | None]:
        """基于反思决策重试路由：切换技能/步骤后重新执行 StepAgent。

        应用新的路由决策，重新运行步骤推理、知识查询、工具调用等完整流程。

        Returns:
            (active_skill, router_decision, step_result, tool_result) 四元组。
        """
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "reflection_retry_started",
            {
                "mode": "skill",
                "target_skill_id": router_decision.target_skill_id,
                "target_step_id": router_decision.target_step_id,
                "reason": router_decision.reason,
            },
        )
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "router_decision_created",
            router_decision.model_dump(),
        )

        before_skill = chat_session.active_skill_id
        before_step = chat_session.active_step_id
        self.runtime.apply_decision(chat_session, router_decision)
        state_pruned = self._drop_unavailable_skill_state(request.tenant_id, chat_session, skills)
        if self._should_record_runtime_event_after_prune(
            router_decision, chat_session, skills, state_pruned
        ):
            self._record_runtime_event(
                request.tenant_id, chat_session, before_skill, before_step, router_decision
            )
        self.db.commit()
        self.db.refresh(chat_session)

        active_skill = self._get_active_skill(
            request.tenant_id, chat_session.active_skill_id, chat_session.agent_id
        )
        if stream_events is not None:
            stream_events.append(
                (
                    "skill_state",
                    self._skill_state_payload(
                        chat_session,
                        skills,
                        self._runtime_stream_context(
                            router_decision, before_skill, before_step, chat_session
                        ),
                    ),
                )
            )
            stream_events.append(
                (
                    "status",
                    {
                        "phase": "stepping",
                        "text": "正在思考",
                        "active_skill_id": chat_session.active_skill_id,
                        "active_step_id": chat_session.active_step_id,
                    },
                )
            )

        step_result = self._run_step_agent_with_context_repair(
            request,
            chat_session,
            active_skill,
            tools,
            model_config,
            router_decision,
            memory_context=memory_context,
            conversation_context=conversation_context,
            stream_events=stream_events,
        )
        self.db.commit()
        self.db.refresh(chat_session)

        tool_result: ToolResult | None = None
        if step_result.tool_call:
            step_result, tool_result = self._execute_tool_action_cycle(
                request,
                chat_session,
                active_skill,
                tools,
                model_config,
                step_result,
                stream_events,
                conversation_context=conversation_context,
                memory_context=memory_context,
            )
        return active_skill, router_decision, step_result, tool_result

    def _tool_loop_decision_payload(
        self,
        iteration: int,
        mode: str,
        tool_call: ToolCall | None = None,
    ) -> dict[str, object]:
        """构建工具循环决策事件的 payload。

        Args:
            iteration: 当前迭代轮次（1-based）。
            mode: 决策模式（``model_tool_call``/``respond``/``respond_after_duplicate``）。
            tool_call: 相关的工具调用（可选）。

        Returns:
            事件 payload 字典。
        """
        payload: dict[str, object] = {"mode": mode, "iteration": iteration}
        if tool_call:
            payload["tool_call"] = tool_call.model_dump(mode="json")
            payload["target_tool_name"] = tool_call.name
        return payload

    def _execute_tool_action_cycle(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        active_skill: Skill | None,
        tools: list[Tool],
        model_config: ModelConfig | None,
        step_result: StepAgentResult,
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
        status_callback: StatusCallback | None = None,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> tuple[StepAgentResult, ToolResult | None]:
        """工具执行循环：迭代执行工具调用直到模型不再请求工具或达到上限。

        这是编排管道的**核心执行方法之一**。当 StepAgent 产出的步骤结果
        包含工具调用时，进入此循环：

        循环逻辑（最多 ``max_actions`` 次）：
        1. 提取步骤结果中的工具调用，计算去重签名。
        2. 若签名已见过（重复调用）：
           - 若上次成功且有回复 → 直接回复（``respond_after_duplicate``）。
           - 否则 → 停止循环（``duplicate_tool_call``）。
        3. 执行工具调用 (:meth:`_execute_tool_call`)，记录结果到槽位。
        4. 若工具失败：
           - 通用技能工具失败时 → 运行 StepAgent 续接决策。
           - 其他工具失败 → 停止循环。
        5. 若工具成功且无模型配置 → 推进技能步骤后停止。
        6. 若工具成功 → 运行 StepAgent 续接决策：
           - 若续接结果含新工具调用 → 继续循环（``model_tool_call``）。
           - 否则 → 推进技能步骤后停止（``respond``）。

        Args:
            stream_events: 流式事件收集列表。
            status_callback: 状态回调。
            conversation_context: 对话上下文。
            memory_context: 记忆上下文。

        Returns:
            (最终步骤结果, 最终工具结果) 元组。
        """
        tool_result: ToolResult | None = None
        current_knowledge = list(step_result.knowledge_results or [])
        seen_calls: set[str] = set()
        max_actions = self._get_agent_loop_max_actions(request.tenant_id)
        for iteration in range(max_actions):
            tool_call = step_result.tool_call
            if not tool_call:
                break
            tool_call_id = new_id("toolcall")
            # 计算工具调用签名用于去重检测
            signature = self._tool_call_signature(tool_call)
            if signature in seen_calls:
                # 重复调用：若上次成功且有回复则直接回复，否则停止
                if tool_result and tool_result.success and step_result.reply:
                    step_result = step_result.model_copy(
                        update={"tool_call": None, "is_step_completed": True}
                    )
                    payload = self._tool_loop_decision_payload(
                        iteration + 1, "respond_after_duplicate"
                    )
                    self.events.record(
                        request.tenant_id, chat_session.id, "agent_loop_completed", payload
                    )
                    if stream_events is not None:
                        stream_events.append(("agent_loop_completed", payload))
                    break
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "agent_loop_stopped",
                    {"reason": "duplicate_tool_call", "tool_call": tool_call.model_dump()},
                )
                break
            seen_calls.add(signature)
            self._emit_tool_status(tool_call, tool_call_id, stream_events, status_callback)
            tool_result = self._execute_tool_call(
                request,
                chat_session,
                tool_call,
                tool_call_id,
                stream_events=stream_events,
                conversation_context=conversation_context,
                memory_context=memory_context,
            )
            self._record_tool_result_in_slots(chat_session, tool_call, tool_result)
            if stream_events is not None:
                stream_events.append(
                    (
                        "tool_result",
                        self._tool_activity_payload(
                            request.tenant_id,
                            tool_call.name,
                            tool_result,
                            tool_call,
                            tool_call_id,
                        ),
                    )
                )
            self.db.commit()
            self.db.refresh(chat_session)
            if not tool_result.success:
                if (
                    model_config
                    and tool_call.name.startswith(GENERAL_SKILL_TOOL_PREFIX)
                    and active_skill is not None
                ):
                    self._emit_thinking_status(
                        chat_session, iteration + 1, stream_events, status_callback
                    )
                    continuation_result = self._run_step_agent_once(
                        request,
                        chat_session,
                        active_skill,
                        tools,
                        model_config,
                        repair_reason="tool_continuation",
                        repair_context=self._tool_continuation_context(
                            request.tenant_id,
                            tool_call,
                            tool_result,
                            chat_session,
                            iteration + 1,
                        ),
                        memory_context=memory_context,
                        conversation_context=conversation_context,
                        current_knowledge=current_knowledge,
                        allow_general_skill_selection=False,
                    )
                    self._apply_step_result(
                        request.tenant_id,
                        chat_session,
                        continuation_result,
                        active_skill,
                    )
                    self.db.commit()
                    self.db.refresh(chat_session)
                    step_result = continuation_result
                break

            if not model_config:
                self._advance_after_successful_tool(
                    request.tenant_id, chat_session, active_skill, step_result, tool_result
                )
                self.db.commit()
                self.db.refresh(chat_session)
                break

            self._emit_thinking_status(chat_session, iteration + 1, stream_events, status_callback)
            continuation_result = self._run_step_agent_once(
                request,
                chat_session,
                active_skill,
                tools,
                model_config,
                repair_reason="tool_continuation",
                repair_context=self._tool_continuation_context(
                    request.tenant_id,
                    tool_call,
                    tool_result,
                    chat_session,
                    iteration + 1,
                ),
                memory_context=memory_context,
                conversation_context=conversation_context,
                current_knowledge=current_knowledge,
                allow_general_skill_selection=False,
            )
            if current_knowledge and not continuation_result.knowledge_results:
                continuation_result.knowledge_results = current_knowledge
            self._apply_step_result(
                request.tenant_id, chat_session, continuation_result, active_skill
            )
            self.db.commit()
            self.db.refresh(chat_session)
            step_result = continuation_result
            if step_result.tool_call:
                payload = self._tool_loop_decision_payload(
                    iteration + 1,
                    "model_tool_call",
                    step_result.tool_call,
                )
                self.events.record(
                    request.tenant_id, chat_session.id, "agent_loop_continued", payload
                )
                if stream_events is not None:
                    stream_events.append(("agent_loop_continued", payload))
                continue

            payload = self._tool_loop_decision_payload(iteration + 1, "respond")
            self.events.record(request.tenant_id, chat_session.id, "agent_loop_completed", payload)
            if stream_events is not None:
                stream_events.append(("agent_loop_completed", payload))
            self._advance_after_successful_tool(
                request.tenant_id,
                chat_session,
                active_skill,
                StepAgentResult(
                    tool_call=tool_call,
                    next_step_id=step_result.next_step_id,
                    is_step_completed=True,
                ),
                tool_result,
            )
            self.db.commit()
            self.db.refresh(chat_session)
            break
        return step_result, tool_result

    def _execute_knowledge_query_cycle(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        active_skill: Skill | None,
        tools: list[Tool],
        model_config: ModelConfig,
        step_result: StepAgentResult,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
        status_callback: StatusCallback | None = None,
    ) -> StepAgentResult:
        """知识检索循环：执行知识库查询并将结果反馈给 StepAgent。

        这是编排管道的**核心执行方法之一**。当 StepAgent 产出的步骤结果
        包含知识查询请求时，进入此流程：

        1. 从步骤结果提取知识查询请求。
        2. 构建 :class:`KnowledgeSearchRequest`，调用 :class:`KnowledgeService`
           执行向量检索/全文检索。
        3. 将检索结果（chunks、buckets、trace 等）封装为知识条目。
        4. 运行 StepAgent 续接决策，让模型基于知识结果判断下一步动作。
        5. 将知识结果固定到续接结果中并应用。

        Args:
            stream_events: 流式事件收集列表。
            status_callback: 状态回调。

        Returns:
            包含知识结果的更新后步骤结果。
        """
        query = step_result.knowledge_query
        if not query or not query.query.strip():
            return step_result
        payload = {
            "phase": "knowledge",
            "text": "正在检索知识",
            "query": query.model_dump(mode="json"),
            "active_skill_id": chat_session.active_skill_id,
            "active_step_id": chat_session.active_step_id,
        }
        self.events.record(request.tenant_id, chat_session.id, "knowledge_query_started", payload)
        if stream_events is not None:
            stream_events.append(("status", payload))
        if status_callback is not None:
            status_callback("knowledge", payload)

        knowledge_base_ids = self._agent_visible_knowledge_base_ids(
            request.tenant_id,
            chat_session.agent_id,
        )
        if (
            self._agent_requires_resource_filter(request.tenant_id, chat_session.agent_id)
            and not knowledge_base_ids
        ):
            search_response = KnowledgeSearchResponse(
                selected_buckets=[],
                chunks=[],
                trace=[],
                route_trace=[],
                selected_documents=[],
                expanded_sections=[],
                evidence_pack=[],
            )
        else:
            search_query = query.query.strip()
            original_message = request.message.strip()
            if original_message and original_message not in search_query:
                search_query = f"{search_query}\n{original_message}"
            search_response = KnowledgeService(self.db).search(
                KnowledgeSearchRequest(
                    tenant_id=request.tenant_id,
                    agent_id=chat_session.agent_id,
                    query=search_query,
                    mode="chat",
                    knowledge_base_ids=knowledge_base_ids,
                    max_chunks=max(1, min(query.max_chunks, 12)),
                    max_buckets=4,
                    max_depth=max(1, min(query.max_depth, 4)),
                    need_evidence_pack=True,
                ),
                model_config,
            )
        knowledge_items = {
            "query": query.model_dump(mode="json"),
            "source_message": request.message,
            "selected_buckets": [
                item.model_dump(mode="json") for item in search_response.selected_buckets
            ],
            "chunks": [item.model_dump(mode="json") for item in search_response.chunks],
            "trace": search_response.route_trace or search_response.trace,
            "selected_documents": search_response.selected_documents,
            "selected_concepts": search_response.selected_concepts,
            "expanded_sections": search_response.expanded_sections,
            "okf_citations": search_response.okf_citations,
            "evidence_pack": search_response.evidence_pack,
        }
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "knowledge_query_finished",
            knowledge_items,
        )
        if stream_events is not None:
            for trace in search_response.route_trace or search_response.trace:
                stream_events.append(("status", {"phase": "knowledge", **trace}))
            stream_events.append(("knowledge_result", knowledge_items))

        continuation_result = self._run_step_agent_once(
            request,
            chat_session,
            active_skill,
            tools,
            model_config,
            repair_reason="knowledge_continuation",
            repair_context={
                "reason": "knowledge_continuation",
                "knowledge_results": knowledge_items,
                "instruction": "基于知识结果继续判断下一步动作；如果知识足够，推进、调用工具或回复；如果不足，由模型决定是否继续追问或停止。",
            },
            memory_context=memory_context,
            conversation_context=conversation_context,
            current_knowledge=[knowledge_items],
            allow_general_skill_selection=False,
        )
        continuation_result.knowledge_results = [knowledge_items]
        self._apply_step_result(request.tenant_id, chat_session, continuation_result, active_skill)
        return continuation_result

    def _auto_knowledge_step_result(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        model_config: ModelConfig,
        router_decision: RouterDecision,
        selection: GeneralSkillSelection | None,
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
        status_callback: StatusCallback | None = None,
    ) -> StepAgentResult:
        """为第二轮通用能力选择自动执行知识检索并生成步骤结果。

        当通用技能选择器判断需要企业知识支撑时，自动构建知识查询请求
        并执行检索，将结果封装为 StepAgentResult 返回。

        Args:
            selection: 通用技能选择结果；若为空或不需要知识则返回空结果。
            stream_events: 流式事件收集列表。
            status_callback: 状态回调。

        Returns:
            包含知识查询和检索结果的步骤结果；若无知识需求则返回空结果。
        """
        del router_decision
        if selection is None or not selection.use_knowledge:
            return StepAgentResult()

        query_text = (selection.knowledge_query or request.message).strip()
        query = KnowledgeQuery(
            query=query_text,
            reason=selection.reason or "第二轮能力选择判断需要企业知识",
            max_chunks=8,
            max_depth=3,
        )
        payload = {
            "phase": "knowledge",
            "text": "正在检索业务资料",
            "query": query.model_dump(mode="json"),
            "auto": True,
        }
        self.events.record(request.tenant_id, chat_session.id, "knowledge_query_started", payload)
        if stream_events is not None:
            stream_events.append(("status", payload))
        if status_callback is not None:
            status_callback("knowledge", payload)

        knowledge_items = self._knowledge_items_for_message(
            request.tenant_id,
            chat_session.agent_id,
            request.message,
            query,
            model_config,
        )
        finished_payload = knowledge_items or {
            "query": query.model_dump(mode="json"),
            "source_message": request.message,
            "selected_buckets": [],
            "chunks": [],
            "trace": [],
            "selected_documents": [],
            "selected_concepts": [],
            "expanded_sections": [],
            "okf_citations": [],
            "evidence_pack": [],
        }
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "knowledge_query_finished",
            {**finished_payload, "auto": True},
        )
        if stream_events is not None:
            for trace in finished_payload.get("trace") or []:
                stream_events.append(("status", {"phase": "knowledge", **trace}))
            stream_events.append(("knowledge_result", finished_payload))
        return StepAgentResult(
            knowledge_query=query,
            knowledge_results=[knowledge_items] if knowledge_items else [],
        )

    def _knowledge_items_for_message(
        self,
        tenant_id: str,
        agent_id: str,
        message: str,
        query: KnowledgeQuery | None = None,
        model_config: ModelConfig | None = None,
    ) -> dict[str, Any] | None:
        """为指定消息执行知识检索并返回结构化的知识条目字典。

        Returns:
            知识条目字典（含 chunks、buckets、trace 等）；若无可用知识库
            或检索结果为空则返回 None。
        """
        knowledge_base_ids = self._agent_visible_knowledge_base_ids(tenant_id, agent_id)
        if self._agent_requires_resource_filter(tenant_id, agent_id) and not knowledge_base_ids:
            return None
        knowledge_query = query or KnowledgeQuery(
            query=message,
            reason="用户要求基于业务资料或规则回答",
            max_chunks=8,
            max_depth=3,
        )
        search_response = KnowledgeService(self.db).search(
            KnowledgeSearchRequest(
                tenant_id=tenant_id,
                agent_id=agent_id,
                query=knowledge_query.query.strip() or message,
                mode="chat",
                knowledge_base_ids=knowledge_base_ids,
                max_chunks=8,
                max_buckets=4,
                max_depth=3,
                need_evidence_pack=True,
            ),
            model_config,
        )
        if not (
            search_response.selected_concepts
            or search_response.okf_citations
            or search_response.evidence_pack
            or search_response.chunks
        ):
            return None
        return {
            "query": knowledge_query.model_dump(mode="json"),
            "source_message": message,
            "selected_buckets": [
                item.model_dump(mode="json") for item in search_response.selected_buckets
            ],
            "chunks": [item.model_dump(mode="json") for item in search_response.chunks],
            "trace": search_response.route_trace or search_response.trace,
            "selected_documents": search_response.selected_documents,
            "selected_concepts": search_response.selected_concepts,
            "expanded_sections": search_response.expanded_sections,
            "okf_citations": search_response.okf_citations,
            "evidence_pack": search_response.evidence_pack,
        }

    def _tool_continuation_context(
        self,
        tenant_id: str,
        tool_call: ToolCall,
        tool_result: ToolResult,
        chat_session: ChatSession,
        completed_actions: int,
    ) -> dict[str, object]:
        """构建工具续接上下文，供 StepAgent 在工具执行后判断下一步。

        收集上一次工具调用及其结果、已积累的工具结果历史、工具调用历史、
        以及当前迭代进度，供模型决定是否继续调用工具或直接回复。

        Args:
            tenant_id: 租户 ID（用于查询最大工具动作数配置）。
            tool_call: 刚刚执行的工具调用。
            tool_result: 工具执行结果。
            chat_session: 当前会话（用于读取槽位中的历史数据）。
            completed_actions: 本轮已完成的工具动作数。

        Returns:
            续接上下文字典，包含 reason、previous_tool_call/result、
            accumulated_tool_results、tool_call_history、进度信息和模型指令。
        """
        slots = chat_session.slots_json or {}
        max_actions = self._get_agent_loop_max_actions(tenant_id)
        return {
            "reason": "tool_continuation",
            "previous_tool_call": tool_call.model_dump(mode="json"),
            "previous_tool_result": tool_result.model_dump(mode="json"),
            "accumulated_tool_results": slots.get(TOOL_RESULTS_SLOT, []),
            "tool_call_history": slots.get(TOOL_CALL_HISTORY_SLOT, []),
            "completed_tool_actions_this_turn": completed_actions,
            "max_tool_actions_per_turn": max_actions,
            "instruction": (
                "基于工具结果、slots、当前技能步骤和用户目标判断是否已经完成。"
                "如果还需要工具调用，由模型输出下一次 tool_call；"
                "如果已经足够回复，输出无 tool_call 的结果并推进到可回复步骤。"
                "不要重复调用 tool_call_history 中相同 name + arguments 的工具。"
            ),
        }

    def _emit_tool_status(
        self,
        tool_call: ToolCall,
        tool_call_id: str,
        stream_events: list[tuple[str, dict[str, object]]] | None,
        status_callback: StatusCallback | None,
    ) -> None:
        """推送工具调用状态事件到流式队列和回调。

        Args:
            tool_call: 即将执行的工具调用。
            tool_call_id: 工具调用唯一标识。
            stream_events: 流式事件收集列表。
            status_callback: 状态回调函数。
        """
        payload = {
            "phase": "tool",
            "text": f"正在调用工具 {tool_call.name}",
            "tool_name": tool_call.name,
            "tool_call_id": tool_call_id,
            "tool_call": tool_call.model_dump(mode="json"),
        }
        if stream_events is not None:
            stream_events.append(("status", payload))
        if status_callback is not None:
            status_callback("tool", {"tool_name": tool_call.name})

    def _emit_thinking_status(
        self,
        chat_session: ChatSession,
        iteration: int,
        stream_events: list[tuple[str, dict[str, object]]] | None,
        status_callback: StatusCallback | None,
    ) -> None:
        """推送"正在思考"状态事件到流式队列和回调。"""
        payload = {
            "phase": "stepping",
            "text": "正在思考",
            "active_skill_id": chat_session.active_skill_id,
            "active_step_id": chat_session.active_step_id,
            "repair_reason": "tool_continuation",
            "iteration": iteration,
        }
        if stream_events is not None:
            stream_events.append(("status", payload))
        if status_callback is not None:
            status_callback(
                "stepping",
                {
                    "active_skill_id": chat_session.active_skill_id,
                    "active_step_id": chat_session.active_step_id,
                    "repair_reason": "tool_continuation",
                },
            )

    def _run_step_agent_with_context_repair(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        active_skill: Skill | None,
        tools: list[Tool],
        model_config: ModelConfig,
        router_decision: RouterDecision,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
    ) -> StepAgentResult:
        """运行 StepAgent 并在需要时进行上下文修复重试。

        首先尝试预选通用技能；若未命中，则调用 :meth:`_run_step_agent_once`
        执行步骤推理。若首次结果不理想（如缺少必要槽位），会通过
        :meth:`_retry_slot_validation_if_needed` 进行槽位修复重试。

        Returns:
            最终步骤结果。
        """
        if conversation_context is None:
            conversation_context = self._conversation_context(chat_session)
        selected_general_result = self._preselect_general_skill_for_scene(
            request,
            chat_session,
            active_skill,
            tools,
            model_config,
            router_decision,
            memory_context,
            conversation_context,
            stream_events,
        )
        if selected_general_result is not None:
            return selected_general_result
        step_result = self._run_step_agent_once(
            request,
            chat_session,
            active_skill,
            tools,
            model_config,
            router_decision,
            memory_context=memory_context,
            conversation_context=conversation_context,
            allow_general_skill_selection=False,
        )
        self._apply_step_result(request.tenant_id, chat_session, step_result, active_skill)
        step_result = self._retry_slot_validation_if_needed(
            request,
            chat_session,
            active_skill,
            tools,
            model_config,
            router_decision,
            step_result,
            memory_context,
            conversation_context,
        )

        return step_result

    def _preselect_general_skill_for_scene(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        active_skill: Skill | None,
        tools: list[Tool],
        model_config: ModelConfig,
        router_decision: RouterDecision,
        memory_context: list[dict[str, object]] | None,
        conversation_context: dict[str, object] | None,
        stream_events: list[tuple[str, dict[str, object]]] | None,
    ) -> StepAgentResult | None:
        """在场景技能流程中预选通用技能作为工具调用。

        当路由决策的 ``general_intent`` 匹配到通用技能且对应工具可用时，
        直接生成工具调用结果，跳过标准 StepAgent 推理。

        Returns:
            预选的步骤结果；若未命中则返回 None。
        """
        if active_skill is None:
            return None
        general_query = str(router_decision.general_intent or "").strip()
        if not general_query:
            return None
        skill, selection = self._select_general_capability(
            general_query,
            model_config,
            chat_session.agent_id,
            conversation_context,
            memory_context,
        )
        if skill is None:
            return None
        tool_name = f"{GENERAL_SKILL_TOOL_PREFIX}{skill.slug}"
        if not any(
            getattr(tool, "enabled", False)
            and str(getattr(tool, "name", "") or "") == tool_name
            for tool in tools
        ):
            return None

        query = general_query
        self._validated_general_skill_calls.add(
            self._general_skill_call_key(chat_session.id, tool_name, query)
        )
        selection_payload = {
            "skill_slug": skill.slug,
            "skill_name": skill.name,
            "confidence": selection.confidence,
            "reason": selection.reason,
            "scene_router_decision": router_decision.model_dump(mode="json"),
            "execution_mode": "scene_and_general",
        }
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "general_skill_intent_checked",
            selection_payload,
        )
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "general_skill_selected",
            selection_payload,
        )
        if stream_events is not None:
            stream_events.extend(
                [
                    ("general_skill_intent_checked", selection_payload),
                    ("general_skill_selected", selection_payload),
                ]
            )

        result = StepAgentResult(
            action="call_tool",
            tool_call=ToolCall(name=tool_name, arguments={"query": query}),
        )
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "step_agent_result_created",
            {
                **result.model_dump(mode="json"),
                "execution_source": "general_skill_preselection",
            },
        )
        return result

    def _retry_slot_validation_if_needed(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        active_skill: Skill | None,
        tools: list[Tool],
        model_config: ModelConfig,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
    ) -> StepAgentResult:
        """在当前步骤缺少期望用户信息时执行槽位验证修复重试。

        当步骤结果缺少必要的槽位信息时，以 ``slot_validation`` 为修复原因
        重新运行 StepAgent，尝试补全缺失信息。仅当重试结果产生了进展
        （新槽位更新/工具调用/回复修复）时才采用重试结果。

        Args:
            step_result: 原始步骤结果。
            memory_context: 记忆上下文。
            conversation_context: 对话上下文。

        Returns:
            修复后的步骤结果（若重试有进展），否则返回原始结果。
        """
        # 检查当前步骤缺少哪些期望的用户信息字段
        missing_fields = self._missing_expected_fields(active_skill, chat_session)
        # 前置条件检查：无缺失字段、或已有工具/交接、或不允许修复、或修复无意义 → 跳过
        if (
            not missing_fields
            or step_result.tool_call
            or step_result.handoff
            or not self._router_allows_schema_tool_repair(router_decision, chat_session)
            or not self._slot_validation_retry_is_worthwhile(router_decision, step_result)
        ):
            return step_result

        # 以 slot_validation 为修复原因重新运行 StepAgent
        validation_result = self._run_step_agent_once(
            request,
            chat_session,
            active_skill,
            tools,
            model_config,
            router_decision,
            repair_reason="slot_validation",
            repair_context={
                "reason": "slot_validation",
                "missing_expected_user_info": missing_fields,
                "previous_step_result": step_result.model_dump(mode="json"),
            },
            memory_context=memory_context,
            conversation_context=conversation_context,
            allow_general_skill_selection=False,
        )
        # 若重试结果既无进展也无回复修复 → 保留原始结果
        if not self._step_result_has_progress(
            validation_result
        ) and not self._step_result_has_reply_repair(
            step_result,
            validation_result,
        ):
            return step_result
        # 若重试结果缺少回复但原始结果有回复 → 继承原始回复
        if not validation_result.reply and step_result.reply:
            validation_result.reply = step_result.reply
        self._apply_step_result(request.tenant_id, chat_session, validation_result, active_skill)
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "step_agent_result_repaired",
            {
                "mode": "slot_validation",
                "active_skill_id": chat_session.active_skill_id,
                "active_step_id": chat_session.active_step_id,
                "missing_expected_user_info": missing_fields,
                "slot_updates": validation_result.slot_updates,
                "tool_call": validation_result.tool_call.model_dump()
                if validation_result.tool_call
                else None,
            },
        )
        return validation_result

    def _slot_validation_retry_is_worthwhile(
        self, router_decision: RouterDecision, step_result: StepAgentResult
    ) -> bool:
        """判断槽位验证修复重试是否值得执行。

        当步骤结果已含槽位更新，或路由决策为新建/继续任务时才值得重试。

        Returns:
            是否值得执行修复重试。
        """
        if step_result.slot_updates:
            return True
        return router_decision.decision in {
            "start_new_task",
            "continue_active",
        }

    def _step_result_has_progress(self, step_result: StepAgentResult) -> bool:
        """判断步骤结果是否包含实质性进展（槽位更新/工具调用/知识查询/交接）。"""
        return bool(
            step_result.slot_updates
            or step_result.tool_call
            or step_result.knowledge_query
            or step_result.handoff
        )

    def _step_result_has_reply_repair(
        self, previous_result: StepAgentResult, validation_result: StepAgentResult
    ) -> bool:
        """判断修复后的回复是否相比原始回复有变化。

        Args:
            previous_result: 原始步骤结果。
            validation_result: 修复后的步骤结果。

        Returns:
            修复回复非空且与原始回复不同时返回 True。
        """
        previous_reply = (previous_result.reply or "").strip()
        repaired_reply = (validation_result.reply or "").strip()
        return bool(repaired_reply and repaired_reply != previous_reply)

    def _missing_expected_fields(self, skill: Skill | None, chat_session: ChatSession) -> list[str]:
        """检查当前步骤缺少哪些期望的用户信息字段。"""
        if not skill:
            return []
        step = self._current_skill_step(skill, chat_session.active_step_id)
        if not step:
            return []
        slots = chat_session.slots_json or {}
        return [
            str(field)
            for field in step.get("expected_user_info", [])
            if not self._skill_slot_satisfied(slots, str(field))
        ]

    def _router_allows_schema_tool_repair(
        self, router_decision: RouterDecision, chat_session: ChatSession
    ) -> bool:
        """判断路由决策是否允许进行 Schema 工具修复重试。"""
        if router_decision.decision not in {
            "start_new_task",
            "continue_active",
        }:
            return False
        if (
            router_decision.target_skill_id
            and chat_session.active_skill_id
            and router_decision.target_skill_id != chat_session.active_skill_id
        ):
            return False
        return True

    def _run_step_agent_once(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        active_skill: Skill | None,
        tools: list[Tool],
        model_config: ModelConfig,
        router_decision: RouterDecision | None = None,
        repair_reason: str | None = None,
        repair_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        current_knowledge: list[dict[str, object]] | None = None,
        allow_general_skill_selection: bool = True,
    ) -> StepAgentResult:
        """执行单次 StepAgent 步骤推理。

        这是编排管道的**核心推理入口**。根据当前技能、工具、对话上下文等
        调用 :class:`StepAgent` 进行推理，产出步骤结果（含工具调用、知识查询、
        槽位更新、回复等）。可选传入修复原因和修复上下文，用于工具续接、
        知识续接、槽位验证等修复场景。

        Args:
            router_decision: 路由决策（可选，用于指导推理方向）。
            repair_reason: 修复原因标识（如 ``tool_continuation``、``slot_validation``）。
            repair_context: 修复上下文（包含历史工具结果、缺失字段等）。
            memory_context: 记忆上下文。
            conversation_context: 对话上下文。
            current_knowledge: 当前已积累的知识检索结果。
            allow_general_skill_selection: 是否允许 StepAgent 自主选择通用技能工具。

        Returns:
            StepAgent 产出的步骤结果。
        """
        if conversation_context is None:
            conversation_context = self._conversation_context(chat_session)
        # 从对话上下文中提取最近的 user/assistant 消息，供 StepAgent 参考
        recent_messages = [
            message
            for message in conversation_context.get("messages", [])
            if isinstance(message, dict) and message.get("role") in {"user", "assistant"}
        ]
        # 调用 StepAgent 执行推理，传入过滤后的工具列表和全部上下文
        step_result = self.step_agent.run(
            message=request.message,
            session=chat_session,
            skill=active_skill,
            tools=self._step_agent_tools(
                active_skill,
                tools,
                request.message,
                model_config,
                chat_session.agent_id,
                conversation_context,
                memory_context,
                active_step_id=chat_session.active_step_id,
                slots=chat_session.slots_json,
                allow_general_skill_selection=allow_general_skill_selection,
            ),
            model_config=model_config,
            router_decision=router_decision,
            repair_context=repair_context,
            recent_messages=recent_messages,
            memory_context=memory_context,
            conversation_context=conversation_context,
            current_knowledge=current_knowledge,
        )
        # 记录步骤结果事件，附带修复原因（若有）
        payload = step_result.model_dump()
        if repair_reason:
            payload["repair_reason"] = repair_reason
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "step_agent_result_created",
            payload,
        )
        return step_result

    def _step_agent_tools(
        self,
        active_skill: Skill | None,
        tools: list[Tool],
        user_message: str | None = None,
        model_config: ModelConfig | None = None,
        agent_id: str | None = None,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
        *,
        active_step_id: str | None = None,
        slots: dict[str, object] | None = None,
        allow_general_skill_selection: bool = True,
    ) -> list[Tool]:
        """根据当前技能步骤的 ``allowed_actions`` 过滤出可用的工具列表。

        解析步骤的 ``allowed_actions``，筛选出显式声明的工具和通用技能工具。
        若步骤声明了 ``call_tool``（无特定工具名），则允许所有工具。

        过滤规则：
        1. 仅保留已启用（enabled）的工具。
        2. 通用技能工具（前缀匹配）单独收集，后续通过意图匹配择一加入。
        3. 普通工具：若步骤允许任意工具（``call_tool``）或在显式工具名列表中，
           且工具的 ``allowed_skills`` 限制包含当前技能 → 加入结果。
        4. 通用技能工具择一后追加到结果列表末尾。

        Args:
            active_step_id: 当前步骤 ID。
            slots: 会话槽位。
            allow_general_skill_selection: 是否允许加入通用技能工具。

        Returns:
            过滤后的可用工具列表。
        """
        if active_skill is None:
            return []
        current_step = self._current_skill_step(active_skill, active_step_id)
        if not current_step:
            return []
        # 解析步骤的 allowed_actions，构建动作集合
        actions = {
            str(action).strip()
            for action in current_step.get("allowed_actions") or []
            if str(action).strip()
        }
        # 提取显式声明的工具名（格式为 call_tool:<tool_name>）
        explicit_tool_names = {
            action.split(":", 1)[1]
            for action in actions
            if action.startswith("call_tool:") and ":" in action
        }
        # 若步骤声明了无参 call_tool，则允许使用任意工具
        allow_any_tool = "call_tool" in actions
        active_skill_id = active_skill.skill_id
        scoped_tools: list[Tool] = []
        general_skill_tools: list[Tool] = []
        for tool in tools:
            # 跳过未启用的工具
            if not getattr(tool, "enabled", False):
                continue
            tool_name = str(getattr(tool, "name", "") or "")
            # 通用技能工具单独收集，不直接加入 scoped_tools
            if tool_name.startswith(GENERAL_SKILL_TOOL_PREFIX):
                if allow_general_skill_selection:
                    general_skill_tools.append(tool)
                continue
            # 普通工具：需满足"允许任意工具"或"在显式工具名列表中"
            if not allow_any_tool and tool_name not in explicit_tool_names:
                continue
            # 检查工具的技能限制（allowed_skills），非空则需包含当前技能
            allowed_skills = [
                str(skill_id)
                for skill_id in (getattr(tool, "allowed_skills_json", None) or [])
                if str(skill_id).strip()
            ]
            if allowed_skills and active_skill_id not in allowed_skills:
                continue
            scoped_tools.append(tool)
        # 从候选通用技能工具中择一匹配用户意图
        selected_general_tool = self._selected_general_skill_tool_name(
            user_message,
            model_config,
            agent_id,
            general_skill_tools,
            conversation_context,
            memory_context,
        )
        if selected_general_tool:
            scoped_tools.extend(
                tool
                for tool in general_skill_tools
                if str(getattr(tool, "name", "") or "") == selected_general_tool
            )
        return scoped_tools

    def _selected_general_skill_tool_name(
        self,
        user_message: str | None,
        model_config: ModelConfig | None,
        agent_id: str | None,
        general_skill_tools: list[Tool],
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> str | None:
        """根据用户消息从通用技能工具列表中选择最匹配的工具名。

        通过通用技能选择器（GeneralSkillSelector）对候选通用技能进行意图匹配，
        返回匹配度最高的通用技能工具名。

        Args:
            user_message: 用户消息文本。
            model_config: 模型配置。
            agent_id: Agent ID。
            general_skill_tools: 候选通用技能工具列表。
            conversation_context: 对话上下文。
            memory_context: 记忆上下文。

        Returns:
            匹配的通用技能工具名（如 ``general__xxx``）；若无匹配则返回 None。
        """
        message = str(user_message or "").strip()
        # 缺少必要输入则无法匹配
        if not message or not model_config or not general_skill_tools:
            return None
        # 从工具名中提取技能 slug（去掉通用技能前缀）
        allowed_slugs = {
            str(getattr(tool, "name", "") or "").removeprefix(GENERAL_SKILL_TOOL_PREFIX)
            for tool in general_skill_tools
            if str(getattr(tool, "name", "") or "").startswith(GENERAL_SKILL_TOOL_PREFIX)
        }
        allowed_slugs = {slug for slug in allowed_slugs if slug}
        if not allowed_slugs:
            return None
        # 从已发布的通用技能中筛选出 slug 匹配的候选技能
        candidates = [
            skill
            for skill in self._list_published_general_skills(model_config.tenant_id, agent_id)
            if skill.slug in allowed_slugs
        ]
        if not candidates:
            return None
        # 调用通用技能选择器进行意图匹配
        try:
            selection = self.general_skill_selector.decide(
                message,
                candidates,
                model_config,
                conversation_context,
                memory_context,
            )
        except LLMError:
            return None
        # 选择器判断不使用通用技能或未选中 → 返回 None
        if not selection.use_general_skill or not selection.selected_slug:
            return None
        # 选中的 slug 不在允许列表中 → 返回 None
        if selection.selected_slug not in allowed_slugs:
            return None
        return f"{GENERAL_SKILL_TOOL_PREFIX}{selection.selected_slug}"

    def _apply_step_result(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        step_result: StepAgentResult,
        active_skill: Skill | None = None,
    ) -> None:
        """将 StepAgent 结果应用到会话状态（槽位更新、步骤推进等）。

        处理槽位更新、``next_step_id`` 推进、图并行步骤合并等逻辑，
        确保会话状态与步骤结果保持一致。

        图并行步骤合并算法核心逻辑：
        - 当存在 pending_steps（待处理并行分支）时，模型给出的 next_step_id
          可能与队列中的某个步骤重合，此时执行**合并**（从队列移除并直接激活）。
        - 若 next_step_id 不在队列中，则将其追加到队列，再激活队列头部步骤。
        - 若无 pending_steps，则将 next_step_id 的兄弟步骤（同一父节点、
          相同条件的其他分支）排入队列，供后续自动推进消费。

        Args:
            tenant_id: 租户 ID。
            chat_session: 当前会话。
            step_result: StepAgent 产出的步骤结果。
            active_skill: 当前激活的技能对象。
        """
        # ── 1. 应用槽位更新：将 step_result 中的 slot_updates 合并到会话 slots ──
        if step_result.slot_updates:
            chat_session.slots_json = {
                **(chat_session.slots_json or {}),
                **step_result.slot_updates,
            }
            self.events.record(
                tenant_id,
                chat_session.id,
                "slot_updated",
                {"slot_updates": step_result.slot_updates, "slots": chat_session.slots_json},
            )

        # 若无激活技能，则无需推进步骤，直接返回
        if not chat_session.active_skill_id:
            return

        # ── 2. 处理 next_step_id 推进与图并行合并 ──
        active_skill_matches = bool(
            active_skill and active_skill.skill_id == chat_session.active_skill_id
        )
        if active_skill_matches and step_result.next_step_id:
            next_step_id = str(step_result.next_step_id).strip()
            # 校验 next_step_id 是否是技能图中真实存在的节点，不存在则忽略
            if not self._skill_has_step(active_skill, next_step_id):
                self.events.record(
                    tenant_id,
                    chat_session.id,
                    "step_agent_result_repaired",
                    {
                        "mode": "invalid_next_step_ignored",
                        "active_skill_id": chat_session.active_skill_id,
                        "active_step_id": chat_session.active_step_id,
                        "invalid_next_step_id": step_result.next_step_id,
                    },
                )
                step_result.next_step_id = None
                return

            source_step_id = chat_session.active_step_id
            pending_steps = self._graph_pending_steps(chat_session)
            # ── 2a. 有待处理并行步骤：执行合并或排队逻辑 ──
            if pending_steps:
                # 情况一：next_step_id 已在待处理队列 → 分支汇合（merge）
                #   从队列中移除该步骤并直接激活，表示两个并行分支在此节点合并
                if next_step_id in pending_steps:
                    pending_steps = [item for item in pending_steps if item != next_step_id]
                    self._store_graph_pending_steps(tenant_id, chat_session, pending_steps)
                    self._change_active_step(
                        tenant_id,
                        chat_session,
                        next_step_id,
                        reason="graph_merge_step",
                    )
                    return

                # 情况二：next_step_id 不在队列 → 将其加入队列尾部，
                #   然后激活队列头部步骤（即另一个并行分支），实现交替推进
                if next_step_id not in pending_steps:
                    pending_steps.append(next_step_id)
                    self._store_graph_pending_steps(tenant_id, chat_session, pending_steps)
                if self._activate_next_pending_graph_step(
                    tenant_id,
                    chat_session,
                    active_skill,
                    reason="graph_sibling_step",
                ):
                    # 将激活后的 active_step_id 回写到 step_result，供上层感知
                    step_result.next_step_id = chat_session.active_step_id
                return

            # ── 2b. 无待处理步骤：将 next_step_id 的兄弟步骤排入队列 ──
            #   兄弟步骤 = 同一父节点(source_step_id)出发、相同条件的其他分支
            self._queue_graph_sibling_steps(
                tenant_id,
                chat_session,
                active_skill,
                source_step_id,
                next_step_id,
            )

        # ── 3. 执行步骤切换（普通线性推进场景）──
        if step_result.next_step_id:
            self._change_active_step(tenant_id, chat_session, str(step_result.next_step_id).strip())
            return

        # ── 4. 步骤已完成但无 next_step：尝试激活待处理队列中的步骤 ──
        if active_skill_matches and step_result.is_step_completed:
            if self._activate_next_pending_graph_step(
                tenant_id,
                chat_session,
                active_skill,
                reason="graph_pending_step",
            ):
                step_result.next_step_id = chat_session.active_step_id

    def _change_active_step(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        next_step_id: str,
        *,
        reason: str | None = None,
    ) -> None:
        """切换会话的激活步骤，并记录 ``skill_step_changed`` 事件。

        仅当目标步骤与当前步骤不同时才记录事件，避免无意义的状态变更日志。

        Args:
            tenant_id: 租户 ID。
            chat_session: 当前会话。
            next_step_id: 要切换到的目标步骤 ID。
            reason: 切换原因标识（如 ``graph_merge_step``、``graph_sibling_step``），
                用于事件追溯。
        """
        previous_step = chat_session.active_step_id
        chat_session.active_step_id = next_step_id
        # 若新旧步骤相同，无需记录事件（避免无意义的冗余日志）
        if previous_step == next_step_id:
            return
        # 构建步骤切换事件 payload，包含来源/目标技能和步骤信息
        payload: dict[str, Any] = {
            "from_skill_id": chat_session.active_skill_id,
            "to_skill_id": chat_session.active_skill_id,
            "from_step_id": previous_step,
            "to_step_id": next_step_id,
        }
        if reason:
            payload["reason"] = reason
        self.events.record(tenant_id, chat_session.id, "skill_step_changed", payload)

    def _graph_pending_steps(self, chat_session: ChatSession) -> list[str]:
        """从会话槽位中读取图并行待处理步骤 ID 列表。

        从 ``slots_json`` 的 ``GRAPH_PENDING_STEPS_SLOT`` 键读取已排队的
        并行分支步骤 ID，自动过滤空值和重复项。

        Returns:
            去重后的待处理步骤 ID 列表（保持插入顺序）。
        """
        value = (chat_session.slots_json or {}).get(GRAPH_PENDING_STEPS_SLOT)
        # 槽位中可能存放的是非列表类型（如旧数据残留），统一返回空列表
        if not isinstance(value, list):
            return []
        pending: list[str] = []
        for item in value:
            step_id = str(item or "").strip()
            # 跳过空值和已存在的重复步骤，保持列表唯一性
            if step_id and step_id not in pending:
                pending.append(step_id)
        return pending

    def _store_graph_pending_steps(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        pending_steps: list[str],
    ) -> None:
        """将图并行待处理步骤 ID 列表持久化到会话槽位。

        对传入列表进行规范化（去空、去重），若结果为空则从槽位中移除该键，
        避免残留空列表。每次更新都记录 ``graph_pending_steps_updated`` 事件。

        Args:
            tenant_id: 租户 ID。
            chat_session: 当前会话。
            pending_steps: 待持久化的步骤 ID 列表（可能包含空值/重复项）。
        """
        slots = dict(chat_session.slots_json or {})
        # 规范化：去除空白、过滤空值、保持插入顺序去重
        normalized = []
        for item in pending_steps:
            step_id = str(item or "").strip()
            if step_id and step_id not in normalized:
                normalized.append(step_id)
        # 列表为空时移除槽位键，避免存储无意义的空列表
        if normalized:
            slots[GRAPH_PENDING_STEPS_SLOT] = normalized
        else:
            slots.pop(GRAPH_PENDING_STEPS_SLOT, None)
        chat_session.slots_json = slots
        self.events.record(
            tenant_id,
            chat_session.id,
            "graph_pending_steps_updated",
            {"pending_step_ids": normalized},
        )

    def _queue_graph_sibling_steps(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill,
        source_step_id: str | None,
        selected_step_id: str,
    ) -> None:
        """将选定步骤的兄弟步骤排入待处理队列。

        兄弟步骤排队逻辑（图并行分支发现算法）：
        1. 从 source_step_id 的所有出边中，找出指向 selected_step_id 的边，
           收集这些边的条件（condition）。
        2. 从同一组出边中，筛选出**条件相同但目标不同**的其他步骤，
           这些即为 selected_step_id 的"兄弟"并行分支。
        3. 将这些兄弟步骤追加到 pending_steps 队列，供后续自动推进消费。

        这样设计使得当模型选择某一条分支时，其他同等条件的并行分支
        也被记录，以便在适当时机交替执行。

        Args:
            tenant_id: 租户 ID。
            chat_session: 当前会话。
            active_skill: 当前激活的技能（用于查询图结构）。
            source_step_id: 分支起点的步骤 ID（即当前步骤）。
            selected_step_id: 模型选定的目标步骤 ID。
        """
        if not source_step_id:
            return
        # 获取 source_step_id 的所有出边（即该步骤可到达的后续节点列表）
        outgoing = self._graph_outgoing_edges(active_skill).get(source_step_id) or []
        # 收集指向 selected_step_id 的所有边的触发条件
        selected_conditions = {
            self._edge_condition(edge)
            for edge in outgoing
            if str(edge.get("next_node_id") or "").strip() == selected_step_id
        }
        # 筛选兄弟步骤：同一出边组中，条件相同但目标不是 selected_step_id 的步骤
        sibling_steps = [
            str(edge.get("next_node_id") or "").strip()
            for edge in outgoing
            if str(edge.get("next_node_id") or "").strip()
            and str(edge.get("next_node_id") or "").strip() != selected_step_id
            and self._edge_condition(edge) in selected_conditions
        ]
        if not sibling_steps:
            return
        # 将兄弟步骤追加到待处理队列（跳过已存在的）
        pending_steps = self._graph_pending_steps(chat_session)
        for step_id in sibling_steps:
            if step_id not in pending_steps:
                pending_steps.append(step_id)
        self._store_graph_pending_steps(tenant_id, chat_session, pending_steps)

    def _edge_condition(self, edge: dict[str, Any]) -> str:
        """提取图边的条件标识，统一为小写字符串用于条件匹配比较。"""
        return str(edge.get("condition") or "").strip().lower()

    def _activate_next_pending_graph_step(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill,
        *,
        reason: str,
    ) -> bool:
        """从待处理队列中激活下一个有效的图并行步骤。

        遍历 pending_steps 队列（FIFO），跳过已失效的步骤（不在技能图中），
        找到第一个有效步骤后激活并返回。若队列全部失效则清空队列。

        Args:
            tenant_id: 租户 ID。
            chat_session: 当前会话。
            active_skill: 当前激活的技能。
            reason: 激活原因标识。

        Returns:
            是否成功激活了一个步骤。``False`` 表示队列为空或全部步骤已失效。
        """
        pending_steps = self._graph_pending_steps(chat_session)
        while pending_steps:
            # 从队列头部取出下一个候选步骤（FIFO 先进先出）
            next_step_id = pending_steps.pop(0)
            # 跳过已不在技能图中的失效步骤（可能因技能编辑导致节点被删除）
            if not self._skill_has_step(active_skill, next_step_id):
                continue
            # 持久化剩余队列并切换到该步骤
            self._store_graph_pending_steps(tenant_id, chat_session, pending_steps)
            self._change_active_step(tenant_id, chat_session, next_step_id, reason=reason)
            return True
        # 队列全部失效，清空并返回
        self._store_graph_pending_steps(tenant_id, chat_session, [])
        return False

    def _skill_has_step(self, skill: Skill, step_id: str | None) -> bool:
        """判断技能图中是否存在指定步骤节点。"""
        if not step_id:
            return False
        return any(node.get("node_id") == step_id for node in self._skill_nodes(skill))

    def _step_actions(self, step: dict[str, Any]) -> list[str]:
        """提取步骤的 ``allowed_actions`` 列表，过滤掉无效/空值动作。"""
        return [
            action
            for action in (_normalize_action(item) for item in step.get("allowed_actions", []))
            if action
        ]

    def _record_tool_result_in_slots(
        self,
        chat_session: ChatSession,
        tool_call: ToolCall,
        tool_result: ToolResult,
    ) -> None:
        """将工具调用和结果记录到会话槽位的 ``_tool_call_history`` 和 ``_tool_results`` 中。

        维护两个槽位：
        - ``_tool_call_history``: 去重的工具调用历史（按签名去重），供 StepAgent
          避免重复调用相同工具。
        - ``_tool_results``: 所有工具执行结果列表（不去重），供上下文积累。

        Args:
            chat_session: 当前会话。
            tool_call: 工具调用请求。
            tool_result: 工具执行结果。
        """
        slots = dict(chat_session.slots_json or {})
        history = self._tool_call_history(slots)
        # 计算当前调用的签名，仅在历史中不存在相同签名时追加（去重）
        signature = self._tool_call_signature(tool_call)
        if signature not in {self._tool_history_signature(item) for item in history}:
            history.append({"tool_name": tool_call.name, "arguments": tool_call.arguments})

        # 工具结果列表：每次执行都追加（不去重），用于积累完整执行上下文
        results = slots.get(TOOL_RESULTS_SLOT)
        result_items = list(results) if isinstance(results, list) else []
        result_items.append(
            {
                "tool_name": tool_call.name,
                "arguments": tool_call.arguments,
                "success": tool_result.success,
                "data": tool_result.data,
                "error": tool_result.error.model_dump() if tool_result.error else None,
            }
        )
        slots[TOOL_CALL_HISTORY_SLOT] = history
        slots[TOOL_RESULTS_SLOT] = result_items
        chat_session.slots_json = slots

    def _tool_call_history(self, slots: dict[str, Any]) -> list[dict[str, Any]]:
        """从会话槽位中读取工具调用历史列表。

        Args:
            slots: 会话槽位字典。

        Returns:
            工具调用历史列表（每项含 tool_name 和 arguments）；无有效数据时返回空列表。
        """
        history = slots.get(TOOL_CALL_HISTORY_SLOT)
        if not isinstance(history, list):
            return []
        return [item for item in history if isinstance(item, dict)]

    def _tool_history_signature(self, item: dict[str, Any]) -> str:
        """为历史工具调用记录计算去重签名。

        Args:
            item: 历史记录项（含 tool_name 和 arguments）。

        Returns:
            工具调用的 JSON 签名字符串。
        """
        return self._tool_signature(
            str(item.get("tool_name") or ""),
            item.get("arguments") if isinstance(item.get("arguments"), dict) else {},
        )

    def _tool_call_signature(self, tool_call: ToolCall) -> str:
        """计算工具调用的去重签名。"""
        return self._tool_signature(tool_call.name, tool_call.arguments)

    def _tool_signature(self, tool_name: str, arguments: dict[str, Any]) -> str:
        """计算工具调用的 JSON 去重签名（用于重复调用检测和幂等匹配）。

        将工具名和参数序列化为排序后的 JSON 字符串，确保相同工具+参数
        始终生成相同签名。

        Args:
            tool_name: 工具名称。
            arguments: 工具参数字典。

        Returns:
            JSON 格式的签名字符串。
        """
        return json.dumps(
            {"tool_name": tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    def _tool_idempotency_config(self, tool: Tool) -> tuple[bool | None, list[str] | None]:
        """解析工具的幂等性配置。

        从工具的 ``config_json`` 和 ``input_schema`` 中提取幂等性策略，
        支持多种配置键名（``idempotency``、``x-idempotency`` 等）。
        若显式配置了 ``requires_confirmation``，则视为启用幂等重放。

        Args:
            tool: 工具对象。

        Returns:
            (enabled, key_fields) 二元组：
            - ``enabled``: 幂等是否启用（None 表示未配置）。
            - ``key_fields``: 幂等匹配的关键参数字段列表（None 表示用全部参数匹配）。
        """
        raw_config = tool.config_json if isinstance(tool.config_json, dict) else {}
        raw_schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
        # 从 config_json 或 input_schema 中查找幂等配置
        config = raw_config.get("idempotency", raw_config.get("idempotency_policy"))
        if config is None:
            config = raw_schema.get("x-idempotency", raw_schema.get("x_idempotency"))
        enabled: bool | None = None
        key_fields: list[str] | None = None
        if isinstance(config, dict):
            # 配置为字典：提取 enabled 开关和 key_fields
            enabled = self._idempotency_enabled_value(
                config.get("enabled", config.get("mode", config.get("scope")))
            )
            fields = (
                config.get("key_fields") or config.get("fields") or config.get("argument_fields")
            )
            if isinstance(fields, list):
                key_fields = [str(item).strip() for item in fields if str(item).strip()]
        else:
            # 配置为标量：直接解析为布尔值
            enabled = self._idempotency_enabled_value(config)

        # 若未显式配置幂等但配置了 requires_confirmation → 启用幂等重放
        requires_confirmation = raw_config.get(
            "requires_confirmation", raw_schema.get("requires_confirmation")
        )
        confirmation_enabled = self._idempotency_enabled_value(requires_confirmation)
        if enabled is None and confirmation_enabled is True:
            enabled = True
        return enabled, key_fields

    def _idempotency_enabled_value(self, value: object) -> bool | None:
        """将任意类型的幂等开关值解析为布尔或 None（无法判断时返回 None）。"""
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {
            "1",
            "true",
            "yes",
            "on",
            "enabled",
            "enable",
            "replay",
            "session",
            "session_arguments",
        }:
            return True
        if normalized in {
            "0",
            "false",
            "no",
            "off",
            "disabled",
            "disable",
            "none",
            "read_only",
            "readonly",
        }:
            return False
        return None

    def _tool_requires_idempotent_replay(
        self, tenant_id: str, tool_call: ToolCall
    ) -> tuple[bool, list[str] | None]:
        """判断工具调用是否需要幂等重放（即跳过重复执行直接返回历史结果）。

        判断优先级：
        1. 通用技能工具 → 永不重放。
        2. 查询数据库获取工具配置 → 若显式配置了幂等策略则按配置返回。
        3. 未配置时，若工具 HTTP 方法属于幂等写方法集合 → 默认启用重放。

        Args:
            tenant_id: 租户 ID。
            tool_call: 工具调用请求。

        Returns:
            (requires_replay, key_fields) 二元组。
        """
        # 通用技能工具不做幂等重放
        if tool_call.name.startswith(GENERAL_SKILL_TOOL_PREFIX):
            return False, None
        if not hasattr(self.db, "exec"):
            return False, None
        # 查询工具配置
        statement = select(Tool).where(Tool.tenant_id == tenant_id, Tool.name == tool_call.name)
        no_autoflush = getattr(self.db, "no_autoflush", None)
        if no_autoflush is None:
            tool = self.db.exec(statement).first()
        else:
            with no_autoflush:
                tool = self.db.exec(statement).first()
        if not tool:
            return False, None
        # 若工具显式配置了幂等策略 → 按配置返回
        configured, key_fields = self._tool_idempotency_config(tool)
        if configured is not None:
            return configured, key_fields
        # 未配置时，根据 HTTP 方法推断：幂等写方法默认启用重放
        method = str(tool.method or "").upper()
        if method not in IDEMPOTENT_WRITE_METHODS:
            return False, None
        return True, key_fields

    def _idempotency_arguments(
        self, arguments: dict[str, Any], key_fields: list[str] | None
    ) -> dict[str, Any]:
        """根据幂等 key_fields 从参数中提取用于签名匹配的子集。

        若未指定 key_fields，则使用全部参数进行匹配。

        Args:
            arguments: 工具调用的完整参数。
            key_fields: 幂等匹配的关键字段列表（None 表示用全部参数）。

        Returns:
            用于幂等签名匹配的参数子集字典。
        """
        if not key_fields:
            return arguments
        return {field: arguments.get(field) for field in key_fields if field in arguments}

    def _previous_successful_side_effect_tool_result(
        self,
        tenant_id: str,
        session_id: str,
        tool_call: ToolCall,
    ) -> tuple[ToolResult, str] | None:
        """查找本会话中相同工具调用的历史成功结果，用于幂等重放。

        从事件库中检索最近的 ``tool_call_finished`` 事件，按 key_fields 匹配
        签名，找到第一个成功结果后构造重放 ToolResult 返回。

        Args:
            tenant_id: 租户 ID。
            session_id: 会话 ID。
            tool_call: 当前工具调用请求。

        Returns:
            (重放结果, 来源事件 ID) 二元组；无匹配则返回 None。
        """
        # 先检查该工具是否启用了幂等重放
        replay_enabled, key_fields = self._tool_requires_idempotent_replay(tenant_id, tool_call)
        if not replay_enabled:
            return None
        # 计算当前调用的目标签名（仅使用 key_fields 子集）
        target_signature = self._tool_signature(
            tool_call.name,
            self._idempotency_arguments(tool_call.arguments, key_fields),
        )
        # 检索本会话最近的工具完成事件（最多 200 条）
        rows = self.db.exec(
            select(AgentEvent)
            .where(
                AgentEvent.tenant_id == tenant_id,
                AgentEvent.session_id == session_id,
                AgentEvent.event_type == "tool_call_finished",
            )
            .order_by(AgentEvent.created_at.desc())
            .limit(200)
        ).all()
        for event in rows:
            payload = event.payload_json or {}
            # 跳过失败的工具调用
            if payload.get("success") is not True:
                continue
            # 工具名不匹配则跳过
            payload_tool_name = str(payload.get("tool_name") or "").strip()
            if payload_tool_name != tool_call.name:
                continue
            # 从事件 payload 中提取历史调用的参数
            payload_tool_call = (
                payload.get("tool_call") if isinstance(payload.get("tool_call"), dict) else {}
            )
            payload_arguments = payload_tool_call.get("arguments")
            if not isinstance(payload_arguments, dict):
                payload_arguments = (
                    payload.get("arguments") if isinstance(payload.get("arguments"), dict) else {}
                )
            # 使用相同的 key_fields 子集计算签名并比较
            replay_arguments = self._idempotency_arguments(payload_arguments, key_fields)
            if self._tool_signature(payload_tool_name, replay_arguments) != target_signature:
                continue
            # 签名匹配成功：构造重放结果，标记幂等重放来源
            replay_data = payload.get("data")
            if isinstance(replay_data, dict):
                replay_data = {
                    **replay_data,
                    "idempotent_replay": True,
                    "replayed_from_event_id": event.id,
                }
            else:
                replay_data = {
                    "result": replay_data,
                    "idempotent_replay": True,
                    "replayed_from_event_id": event.id,
                }
            return ToolResult(tool_name=tool_call.name, success=True, data=replay_data), event.id
        return None

    def _execute_tool_call(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        tool_call: ToolCall,
        tool_call_id: str | None = None,
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> ToolResult:
        """执行单次工具调用。

        执行前进行权限校验（Agent 是否启用了该工具）；若为通用技能工具则
        走 :meth:`_execute_general_skill_tool_call`；若为幂等写操作且已有
        成功历史则直接重放结果；否则调用 :class:`ToolExecutor` 执行。

        Args:
            tool_call: 工具调用请求。
            tool_call_id: 工具调用唯一标识。
            stream_events: 流式事件收集列表。

        Returns:
            工具执行结果。
        """
        # ── 阶段一：权限校验 ── 非通用技能工具需检查 Agent 是否已启用
        if (
            not tool_call.name.startswith(GENERAL_SKILL_TOOL_PREFIX)
            and chat_session.agent_id
            and tool_call.name
            not in {
                row.name
                for row in self._list_enabled_tools(request.tenant_id, chat_session.agent_id)
            }
        ):
            # 权限不足：直接返回失败结果并记录事件
            tool_result = ToolResult(
                tool_name=tool_call.name,
                success=False,
                data=None,
                error=ToolError(code="NOT_ALLOWED", message="当前员工未启用该工具。"),
            )
            started_payload = tool_call.model_dump(mode="json")
            if tool_call_id:
                started_payload["tool_call_id"] = tool_call_id
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "tool_call_started",
                started_payload,
            )
            finished_payload = tool_result.model_dump(mode="json")
            if tool_call_id:
                finished_payload["tool_call_id"] = tool_call_id
            finished_payload["tool_call"] = tool_call.model_dump(mode="json")
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "tool_call_finished",
                finished_payload,
            )
            self.db.commit()
            self.db.refresh(chat_session)
            return tool_result

        # ── 阶段二：幂等重放检查 ── 若为幂等写操作且有历史成功结果则直接重放
        replayed = self._previous_successful_side_effect_tool_result(
            request.tenant_id,
            chat_session.id,
            tool_call,
        )
        if replayed:
            tool_result, replayed_event_id = replayed
            replay_payload = {
                "tool_name": tool_call.name,
                "tool_call": tool_call.model_dump(mode="json"),
                "replayed_from_event_id": replayed_event_id,
            }
            if tool_call_id:
                replay_payload["tool_call_id"] = tool_call_id
            self.events.record(
                request.tenant_id, chat_session.id, "tool_call_reused", replay_payload
            )
            finished_payload = tool_result.model_dump(mode="json")
            if tool_call_id:
                finished_payload["tool_call_id"] = tool_call_id
            finished_payload["tool_call"] = tool_call.model_dump(mode="json")
            finished_payload["idempotent_replay"] = True
            finished_payload["replayed_from_event_id"] = replayed_event_id
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "tool_call_finished",
                finished_payload,
            )
            self.db.commit()
            self.db.refresh(chat_session)
            return tool_result

        # ── 阶段三：实际执行 ── 记录开始事件后执行工具
        started_payload = tool_call.model_dump(mode="json")
        if tool_call_id:
            started_payload["tool_call_id"] = tool_call_id
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "tool_call_started",
            started_payload,
        )
        self.db.commit()
        self.db.refresh(chat_session)
        # 根据工具类型分流：通用技能工具 vs 普通工具
        if tool_call.name.startswith(GENERAL_SKILL_TOOL_PREFIX):
            # 通用技能工具要求当前有激活的场景技能
            if not chat_session.active_skill_id:
                tool_result = ToolResult(
                    tool_name=tool_call.name,
                    success=False,
                    data=None,
                    error=ToolError(
                        code="GENERAL_SKILL_REQUIRES_SCENE_SKILL",
                        message="通用技能只能作为当前场景技能的辅助工具调用。",
                    ),
                )
                finished_payload = tool_result.model_dump(mode="json")
                if tool_call_id:
                    finished_payload["tool_call_id"] = tool_call_id
                finished_payload["tool_call"] = tool_call.model_dump(mode="json")
                self.events.record(
                    request.tenant_id,
                    chat_session.id,
                    "tool_call_finished",
                    finished_payload,
                )
                self.db.commit()
                self.db.refresh(chat_session)
                return tool_result
            tool_result = self._execute_general_skill_tool_call(
                request,
                chat_session,
                tool_call,
                chat_session.agent_id,
                stream_events=stream_events,
                conversation_context=conversation_context,
                memory_context=memory_context,
            )
        else:
            tool_result = self.tool_executor.execute(
                request.tenant_id,
                tool_call,
                chat_session.active_skill_id,
                chat_session.agent_id,
            )
        finished_payload = tool_result.model_dump(mode="json")
        if tool_call_id:
            finished_payload["tool_call_id"] = tool_call_id
        finished_payload["tool_call"] = tool_call.model_dump(mode="json")
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "tool_call_finished",
            finished_payload,
        )
        self.db.commit()
        self.db.refresh(chat_session)
        return tool_result

    def _execute_general_skill_tool_call(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        tool_call: ToolCall,
        agent_id: str | None,
        stream_events: list[tuple[str, dict[str, object]]] | None = None,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> ToolResult:
        """执行通用技能工具调用。

        从工具名解析技能 slug，校验匹配后调用 :class:`GeneralSkillRunner`
        执行技能，并将 trace 事件推送到流式队列。
        """
        # 步骤 1：从工具名中解析技能 slug（移除通用技能前缀）
        slug = tool_call.name.removeprefix(GENERAL_SKILL_TOOL_PREFIX).strip()
        if not slug:
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                data=None,
                error=ToolError(code="INVALID_GENERAL_SKILL", message="通用技能名称为空。"),
            )
        # 步骤 2：从已发布通用技能列表中查找匹配 slug 的技能
        skill = next(
            (
                item
                for item in self._list_published_general_skills(request.tenant_id, agent_id)
                if item.slug == slug
            ),
            None,
        )
        if not skill:
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                data=None,
                error=ToolError(code="GENERAL_SKILL_NOT_FOUND", message="通用技能不存在或未发布。"),
            )
        # 步骤 3：获取模型配置（通用技能执行需要模型来生成代码和推理）
        try:
            model_config = self._get_request_model(request, agent_id)
        except AgentLoopPreconditionError as exc:
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                data=None,
                error=ToolError(code=exc.code.upper(), message=exc.message),
            )
        if not model_config:
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                data=None,
                error=ToolError(code="MISSING_MODEL_CONFIG", message="没有默认模型配置。"),
            )
        # 步骤 4：提取查询文本（优先使用工具参数中的 query，回退到用户原始消息）
        query = str(tool_call.arguments.get("query") or request.message).strip()
        # 步骤 5：调用守卫校验 —— 确保该通用技能与当前子任务匹配
        guard_result = self._validate_general_skill_tool_match(
            request,
            chat_session,
            tool_call,
            skill,
            query,
            model_config,
            agent_id,
            conversation_context,
            memory_context,
        )
        if guard_result is not None:
            # 守卫拦截：技能不匹配，直接返回拒绝结果
            return guard_result
        # 步骤 6：设置 trace 事件去重机制（避免流式推送和最终回放产生重复）
        emitted_trace_keys: set[str] = set()

        def trace_key(trace_item: dict[str, Any]) -> str:
            return json.dumps(trace_item, ensure_ascii=False, sort_keys=True, default=str)

        def emit_general_skill_trace(trace_item: dict[str, Any]) -> None:
            emitted_trace_keys.add(trace_key(trace_item))
            payload: dict[str, object] = {
                "skill_slug": skill.slug,
                "skill_name": skill.name,
                **trace_item,
            }
            self.events.record(request.tenant_id, chat_session.id, "general_skill_trace", payload)
            if stream_events is not None:
                stream_events.append(("general_skill_trace", payload))

        def trace_sink(trace_item: dict[str, Any]) -> None:
            """通用技能 trace 事件接收器（委托给 emit_general_skill_trace）。"""
            emit_general_skill_trace(trace_item)

        # 步骤 7：执行通用技能（含代码生成、沙箱执行、结果结构化）
        try:
            response = self.general_skill_runner.run(
                skill,
                query,
                model_config,
                request.user_id,
                event_sink=trace_sink,
                conversation_context=conversation_context,
                memory_context=memory_context,
            )
        except Exception as exc:
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                data=None,
                error=ToolError(code="GENERAL_SKILL_EXECUTION_ERROR", message=str(exc)),
            )
        # 发射尚未推送过的 trace 事件（补发 runner 返回但未实时推送的 trace）
        for trace_item in response.execution_trace:
            if trace_key(trace_item) not in emitted_trace_keys:
                emit_general_skill_trace(trace_item)
        # 从结构化结果中提取成功状态（未明确声明时默认成功）
        structured = (
            response.structured_result if isinstance(response.structured_result, dict) else {}
        )
        success = structured.get("success")
        is_success = True if success is None else bool(success)
        # 构建 finished 事件 payload 并推送
        finished_payload: dict[str, object] = {
            "skill_slug": response.skill_slug,
            "success": is_success,
            "stdout_preview": response.stdout[:600],
            "stderr_preview": response.stderr[:600],
            "structured_result": response.structured_result,
            "tool_call": tool_call.model_dump(mode="json"),
        }
        self.events.record(
            request.tenant_id, chat_session.id, "general_skill_run_finished", finished_payload
        )
        if stream_events is not None:
            stream_events.append(("general_skill_run_finished", finished_payload))
        data = {
            "skill_slug": response.skill_slug,
            "reply": response.reply,
            "structured_result": response.structured_result,
            "stdout": response.stdout,
            "stderr": response.stderr,
            "generated_code": response.generated_code,
            "execution_trace": response.execution_trace,
        }
        if is_success:
            return ToolResult(tool_name=tool_call.name, success=True, data=data, error=None)
        return ToolResult(
            tool_name=tool_call.name,
            success=False,
            data=data,
            error=ToolError(
                code=str(structured.get("error") or "GENERAL_SKILL_FAILED"),
                message=str(structured.get("message") or response.reply or "通用技能执行失败。"),
            ),
        )

    def _validate_general_skill_tool_match(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        tool_call: ToolCall,
        requested_skill: GeneralSkill,
        query: str,
        model_config: ModelConfig,
        agent_id: str | None,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> ToolResult | None:
        """校验通用技能工具调用是否与用户意图匹配。

        通过通用技能选择器对查询重新匹配，若选择结果与请求的技能 slug
        不一致则拒绝调用（返回 mismatch ToolResult）。若调用已在预选阶段
        被验证过（存在于 ``_validated_general_skill_calls`` 集合），则直接放行。

        Args:
            requested_skill: 被请求的通用技能。
            query: 查询文本。
            model_config: 模型配置。

        Returns:
            ``None`` 表示校验通过；``ToolResult`` 表示校验失败（含拒绝原因）。
        """
        # 检查该调用是否已在预选阶段验证过，若是则直接放行
        call_key = self._general_skill_call_key(
            chat_session.id,
            tool_call.name,
            query,
        )
        if call_key in self._validated_general_skill_calls:
            self._validated_general_skill_calls.discard(call_key)
            return None
        # 查询为空 → 拒绝
        if not query:
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                data={
                    "requested_slug": requested_skill.slug,
                    "selected_slug": None,
                    "reason": "通用技能调用缺少自然语言任务。",
                },
                error=ToolError(
                    code="GENERAL_SKILL_MISMATCH", message="通用技能调用缺少自然语言任务。"
                ),
            )
        candidates = self._list_published_general_skills(request.tenant_id, agent_id)
        if not candidates:
            return ToolResult(
                tool_name=tool_call.name,
                success=False,
                data={
                    "requested_slug": requested_skill.slug,
                    "selected_slug": None,
                    "reason": "当前员工没有可用通用技能。",
                },
                error=ToolError(
                    code="GENERAL_SKILL_NOT_FOUND", message="当前员工没有可用通用技能。"
                ),
            )
        try:
            selection = self.general_skill_selector.decide(
                query,
                candidates,
                model_config,
                conversation_context,
                memory_context,
            )
        except LLMError:
            # 选择器 LLM 调用失败时宽容放行（避免阻塞正常流程）
            return None
        # 比对选择器选中的技能与实际请求的技能是否一致
        selected_slug = selection.selected_slug if selection.use_general_skill else None
        if selected_slug == requested_skill.slug:
            # 匹配一致，校验通过
            return None
        # 不匹配：记录拦截事件并返回 mismatch 结果
        payload = {
            "requested_slug": requested_skill.slug,
            "selected_slug": selected_slug,
            "reason": selection.reason,
            "query": query,
            "tool_call": tool_call.model_dump(mode="json"),
        }
        self.events.record(
            request.tenant_id, chat_session.id, "general_skill_guard_rejected", payload
        )
        return ToolResult(
            tool_name=tool_call.name,
            success=False,
            data=payload,
            error=ToolError(
                code="GENERAL_SKILL_MISMATCH",
                message="通用技能与当前子任务不匹配，已取消调用。",
            ),
        )

    def _general_skill_call_key(
        self,
        session_id: str,
        tool_name: str,
        query: str,
    ) -> tuple[str, str, str]:
        """构建通用技能调用的去重键（会话 ID + 工具名 + 归一化查询）。"""
        return session_id, tool_name.strip(), " ".join(query.split())

    def _advance_after_successful_tool(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        active_skill: Skill | None,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> bool:
        """工具成功执行后自动推进到下一个合适的技能步骤。

        仅在以下条件全满足时推进：
        - 有激活技能且步骤结果包含非通用技能的工具调用。
        - 步骤未指定 next_step_id（或与当前步骤相同）。
        - 工具执行成功。
        - 图中存在满足推进条件的下一步骤。

        Args:
            active_skill: 当前技能。
            step_result: 步骤结果。
            tool_result: 工具执行结果。

        Returns:
            是否成功推进了步骤。
        """
        # 前置条件检查：不满足任一则不推进
        if (
            not active_skill
            or not step_result.tool_call
            or step_result.tool_call.name.startswith(GENERAL_SKILL_TOOL_PREFIX)
            or (
                step_result.next_step_id and step_result.next_step_id != chat_session.active_step_id
            )
            or not tool_result
            or not tool_result.success
        ):
            return False

        # 查找工具成功后可推进的下一步骤
        next_step_id = self._next_step_after_successful_tool(
            active_skill, chat_session.active_step_id, chat_session.slots_json or {}
        )
        if not next_step_id:
            return False

        # 切换到下一步并记录事件
        previous_step = chat_session.active_step_id
        chat_session.active_step_id = next_step_id
        step_result.next_step_id = next_step_id
        self.events.record(
            tenant_id,
            chat_session.id,
            "skill_step_changed",
            {
                "from_skill_id": chat_session.active_skill_id,
                "to_skill_id": chat_session.active_skill_id,
                "from_step_id": previous_step,
                "to_step_id": next_step_id,
                "reason": "tool_completed",
            },
        )
        return True

    def _next_step_after_successful_tool(
        self, skill: Skill, active_step_id: str | None, slots: dict[str, Any]
    ) -> str | None:
        """查找工具成功后可推进的下一个步骤。

        遍历当前步骤的后续节点，优先选择满足以下条件之一的步骤：
        - 缺少期望用户信息（需用户继续提供信息）。
        - 动作允许最终回复或回复动作。

        Args:
            skill: 当前技能。
            active_step_id: 当前步骤 ID。
            slots: 会话槽位。

        Returns:
            可推进的下一步骤 ID；无合适步骤则返回 None。
        """
        if not active_step_id:
            return None
        for step in self._next_steps_from_graph(skill, active_step_id):
            step_id = str(step.get("step_id") or "")
            if not step_id:
                continue
            # 检查该步骤是否缺少期望的用户信息字段
            expected = [str(field) for field in step.get("expected_user_info", [])]
            if any(not self._skill_slot_satisfied(slots, field) for field in expected):
                return step_id
            # 检查该步骤是否允许最终回复
            actions = self._step_actions(step)
            if self._actions_allow_final_reply(actions) or any(
                action.startswith("call_tool:") for action in actions
            ):
                return step_id
        return None

    def _router_decision_from_reflection(
        self,
        reflection: ReflectionDecision,
        chat_session: ChatSession,
        skills: list[Skill],
        previous_decision: RouterDecision,
        completed_skill_ids_this_turn: set[str],
    ) -> RouterDecision | None:
        """根据反思决策构建用于重试的路由决策。

        若反思建议切换到其他技能/步骤，则构建对应的路由决策；
        已完成的技能会被排除。
        """
        if not reflection.target_skill_id:
            return None
        completed_skill_ids_this_turn = completed_skill_ids_this_turn or set()
        if (
            reflection.target_skill_id in completed_skill_ids_this_turn
            and chat_session.active_skill_id != reflection.target_skill_id
        ):
            self.events.record(
                chat_session.tenant_id,
                chat_session.id,
                "reflection_retry_skipped_completed_task",
                {
                    "reason": reflection.reason,
                    "target_skill_id": reflection.target_skill_id,
                    "active_skill_id": chat_session.active_skill_id,
                },
            )
            return None
        target_skill = next(
            (skill for skill in skills if skill.skill_id == reflection.target_skill_id),
            None,
        )
        if not target_skill:
            return None
        decision = (
            "continue_active"
            if chat_session.active_skill_id == target_skill.skill_id
            else "start_new_task"
        )
        return RouterDecision(
            decision=decision,
            target_skill_id=target_skill.skill_id,
            target_step_id=reflection.target_step_id or self._first_step_id(target_skill),
            confidence=0.7,
            user_intent=previous_decision.user_intent,
            reason=f"反思重试：{reflection.reason or '当前技能或工具可能不匹配用户诉求'}",
        )

    def _tool_call_from_reflection(
        self,
        reflection: ReflectionDecision,
        chat_session: ChatSession,
        tools: list[Tool],
        user_message: str | None = None,
    ) -> ToolCall | None:
        """根据反思决策构建工具调用请求。

        若反思建议重试某个工具，则从可用工具列表中查找该工具并构建
        :class:`ToolCall`。对通用技能工具使用用户消息作为 query；
        对普通工具从 slots 构建参数，并校验必填字段。

        Args:
            reflection: 反思决策对象。
            chat_session: 当前会话。
            tools: 可用工具列表。
            user_message: 用户消息原文（通用技能工具需要）。

        Returns:
            工具调用对象；若工具不存在、权限不符或必填参数缺失则返回 None。
        """
        if not reflection.target_tool_name:
            return None
        tool = next(
            (item for item in tools if item.enabled and item.name == reflection.target_tool_name),
            None,
        )
        if not tool:
            return None
        if str(getattr(tool, "name", "") or "").startswith(GENERAL_SKILL_TOOL_PREFIX):
            query = str(user_message or "").strip()
            if not query:
                return None
            return ToolCall(name=tool.name, arguments={"query": query})
        if (
            chat_session.active_skill_id
            and tool.allowed_skills_json
            and chat_session.active_skill_id not in tool.allowed_skills_json
        ):
            return None
        arguments = self._build_tool_arguments_from_slots(tool, chat_session.slots_json or {})
        required = [str(field) for field in (tool.input_schema or {}).get("required", [])]
        if any(not self._slot_has_value(arguments, field) for field in required):
            return None
        return ToolCall(name=tool.name, arguments=arguments)

    def _build_tool_arguments_from_slots(self, tool: Tool, slots: dict[str, Any]) -> dict[str, Any]:
        schema = tool.input_schema or {}
        properties = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        fields = [str(field) for field in properties]
        for field in schema.get("required", []):
            if str(field) not in fields:
                fields.append(str(field))

        arguments: dict[str, Any] = {}
        for field in fields:
            if self._slot_has_value(slots, field):
                arguments[field] = slots[field]
        used_signatures = {
            self._tool_history_signature(item) for item in self._tool_call_history(slots)
        }
        if self._tool_signature(tool.name, arguments) in used_signatures:
            return {}
        return arguments

    def _should_try_reflection(
        self,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> bool:
        """判断当前步骤/工具结果是否需要触发反思。

        委托给 :func:`action_needs_reflection` 判断，该函数综合考虑
        路由决策类型、步骤完成状态和工具执行结果来决定是否需要反思。

        Args:
            router_decision: 路由决策。
            step_result: 步骤结果。
            tool_result: 工具结果（可为 None）。

        Returns:
            是否需要反思。
        """
        return action_needs_reflection(router_decision, step_result, tool_result)

    def _first_step_id(self, skill: Skill) -> str | None:
        content = skill.content_json or {}
        start_node_id = str(content.get("start_node_id") or "").strip()
        if start_node_id and self._skill_has_step(skill, start_node_id):
            return start_node_id
        steps = self._skill_steps(skill)
        first_step = steps[0] if steps and isinstance(steps[0], dict) else None
        return first_step.get("step_id") if first_step else None

    def _skill_steps(self, skill: Skill) -> list[dict[str, Any]]:
        return [_node_as_step(node) for node in self._ordered_skill_nodes(skill)]

    def _skill_nodes(self, skill: Skill) -> list[dict[str, Any]]:
        """获取技能定义中的所有节点列表。"""
        content = skill.content_json or {}
        return [node for node in content.get("nodes", []) if isinstance(node, dict)]

    def _ordered_skill_nodes(self, skill: Skill) -> list[dict[str, Any]]:
        """对技能节点进行拓扑排序，返回从起始节点开始的有序列表。"""
        nodes = self._skill_nodes(skill)
        if not nodes:
            return []
        content = skill.content_json or {}
        nodes_by_id = {
            str(node.get("node_id") or ""): node for node in nodes if node.get("node_id")
        }
        start_node_id = str(content.get("start_node_id") or "").strip()
        if not start_node_id or start_node_id not in nodes_by_id:
            start_node_id = str(nodes[0].get("node_id") or "")
        outgoing = self._graph_outgoing_edges(skill)
        ordered: list[dict[str, Any]] = []
        visited: set[str] = set()

        def visit(node_id: str) -> None:
            if not node_id or node_id in visited:
                return
            node = nodes_by_id.get(node_id)
            if not node:
                return
            visited.add(node_id)
            ordered.append(node)
            for edge in outgoing.get(node_id, []):
                visit(str(edge.get("next_node_id") or ""))

        visit(start_node_id)
        for node in nodes:
            node_id = str(node.get("node_id") or "")
            if node_id not in visited:
                ordered.append(node)
        return ordered

    def _graph_outgoing_edges(self, skill: Skill) -> dict[str, list[dict[str, Any]]]:
        """从技能定义中提取图的出边邻接表（按 source_node_id 分组，按 priority 排序）。"""
        content = skill.content_json or {}
        edges = [edge for edge in content.get("edges", []) if isinstance(edge, dict)]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for edge in edges:
            source = str(edge.get("source_node_id") or "")
            target = str(edge.get("next_node_id") or "")
            if not source or not target:
                continue
            grouped.setdefault(source, []).append(edge)
        for source, items in grouped.items():
            grouped[source] = sorted(items, key=lambda item: int(item.get("priority") or 0))
        return grouped

    def _next_steps_from_graph(
        self, skill: Skill, active_step_id: str | None
    ) -> list[dict[str, Any]]:
        if not active_step_id:
            return []
        nodes_by_id = {
            str(node.get("node_id") or ""): _node_as_step(node) for node in self._skill_nodes(skill)
        }
        outgoing = self._graph_outgoing_edges(skill).get(active_step_id, [])
        return [
            nodes_by_id[target_id]
            for target_id in (str(edge.get("next_node_id") or "") for edge in outgoing)
            if target_id in nodes_by_id
        ]

    def _default_next_step(self, skill: Skill, active_step_id: str | None) -> dict[str, Any] | None:
        if not active_step_id:
            return None
        nodes_by_id = {
            str(node.get("node_id") or ""): _node_as_step(node) for node in self._skill_nodes(skill)
        }
        outgoing = self._graph_outgoing_edges(skill).get(active_step_id, [])
        if not outgoing:
            return None
        if len(outgoing) == 1:
            return nodes_by_id.get(str(outgoing[0].get("next_node_id") or ""))
        unconditional = []
        for edge in outgoing:
            condition = str(edge.get("condition") or "").strip().lower()
            if condition in {"", "default", "else"}:
                target = nodes_by_id.get(str(edge.get("next_node_id") or ""))
                if target:
                    unconditional.append(target)
        if len(unconditional) == 1:
            return unconditional[0]
        return None

    def _get_or_create_session(self, request: ChatTurnRequest) -> ChatSession:
        """获取或创建会话，若不存在则按请求参数新建并 flush。"""
        session_id = request.session_id or new_id("session")
        chat_session = self.db.get(ChatSession, session_id)
        if not chat_session:
            chat_session = ChatSession(
                id=session_id,
                tenant_id=request.tenant_id,
                user_id=request.user_id,
                agent_id=request.agent_id,
            )
            self.db.add(chat_session)
            self.db.flush()
        elif not chat_session.agent_id and request.agent_id:
            chat_session.agent_id = request.agent_id
        return chat_session

    def _finish_stale_completed_skill(
        self, tenant_id: str, chat_session: ChatSession, skills: list[Skill]
    ) -> None:
        if chat_session.skill_stack_json or chat_session.resume_after_answer_json:
            chat_session.skill_stack_json = []
            chat_session.resume_after_answer_json = None
            chat_session.updated_at = utc_now()
        active_skill = next(
            (skill for skill in skills if skill.skill_id == chat_session.active_skill_id), None
        )
        if active_skill and self._is_terminal_skill_state(active_skill, chat_session):
            self._complete_active_skill(
                tenant_id, chat_session, active_skill, "stale_terminal_state"
            )

    def _should_complete_skill(
        self,
        skill: Skill | None,
        chat_session: ChatSession,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> bool:
        if not skill or not step_result.is_step_completed:
            return False
        if tool_result and not tool_result.success:
            return False
        if (
            tool_result
            and tool_result.success
            and self._current_step_can_finish_after_tool(skill, chat_session)
        ):
            return True
        if self._graph_pending_steps(chat_session):
            return False
        if self._is_answer_ready_skill_state(skill, chat_session):
            return True
        if self._is_terminal_skill_state(skill, chat_session):
            return True
        if not step_result.next_step_id and not step_result.tool_call:
            return True
        if self._graph_flow_has_unfinished_work(skill, chat_session, step_result):
            return False
        return self._is_terminal_skill_state(skill, chat_session)

    def _is_terminal_skill_state(self, skill: Skill, chat_session: ChatSession) -> bool:
        return self._is_terminal_skill_position(
            skill, chat_session.active_step_id, chat_session.slots_json or {}
        )

    def _is_answer_ready_skill_state(self, skill: Skill, chat_session: ChatSession) -> bool:
        """判断当前技能是否处于"答案就绪"状态（所有必填信息已收集完毕）。"""
        step = self._current_skill_step(skill, chat_session.active_step_id)
        if not step:
            return False
        actions = self._step_actions(step)
        if not self._actions_allow_final_reply(actions):
            return False
        required = [str(field) for field in (skill.content_json or {}).get("required_info", [])]
        return all(
            self._skill_slot_satisfied(chat_session.slots_json or {}, field) for field in required
        )

    def _current_step_expected_info_satisfied(
        self, skill: Skill, chat_session: ChatSession
    ) -> bool:
        step = self._current_skill_step(skill, chat_session.active_step_id)
        if not step:
            return False
        expected = [str(field) for field in step.get("expected_user_info", [])]
        return all(
            self._skill_slot_satisfied(chat_session.slots_json or {}, field) for field in expected
        )

    def _graph_flow_has_unfinished_work(
        self,
        skill: Skill | None,
        chat_session: ChatSession,
        step_result: StepAgentResult | None = None,
    ) -> bool:
        if not skill or chat_session.active_skill_id != skill.skill_id:
            return False
        if self._graph_pending_steps(chat_session):
            return True
        if (
            step_result
            and step_result.next_step_id
            and str(step_result.next_step_id) == str(chat_session.active_step_id)
        ):
            return True
        if not chat_session.active_step_id:
            return False
        return bool(self._graph_outgoing_edges(skill).get(chat_session.active_step_id))

    def _is_terminal_skill_frame(self, skill: Skill, frame: dict[str, Any]) -> bool:
        """判断指定任务帧是否处于技能终止位置。"""
        return self._is_terminal_skill_position(
            skill,
            str(frame.get("step_id") or ""),
            frame.get("slots") if isinstance(frame.get("slots"), dict) else {},
        )

    def _is_terminal_skill_position(
        self, skill: Skill, active_step_id: str | None, slots: dict[str, Any]
    ) -> bool:
        """判断指定步骤位置（step_id + slots）是否为技能的终止位置。

        终止位置需同时满足：步骤在 terminal_node_ids 中、步骤期望信息已收集、
        技能必填信息已收集、步骤动作均为终止类动作（answer_user、handoff_human
        等）或工具调用。

        Args:
            skill: 技能对象。
            active_step_id: 待检查的步骤 ID。
            slots: 当前 slots 字典。

        Returns:
            是否为终止位置。
        """
        if not active_step_id:
            return False
        content = skill.content_json or {}
        # 检查 1：步骤是否在技能定义的终止节点列表中
        terminal_node_ids = {str(node_id) for node_id in content.get("terminal_node_ids", [])}
        if active_step_id not in terminal_node_ids:
            return False
        current_step = self._current_skill_step(skill, active_step_id)
        if not current_step:
            return False

        # 检查 2：步骤期望收集的用户信息是否已全部满足
        expected = [str(field) for field in current_step.get("expected_user_info", [])]
        if any(not self._skill_slot_satisfied(slots, field) for field in expected):
            return False

        # 检查 3：技能级别的必填信息是否已全部满足
        required = [str(field) for field in content.get("required_info", [])]
        if any(not self._skill_slot_satisfied(slots, field) for field in required):
            return False

        # 检查 4：步骤动作是否均为终止类动作或工具调用
        actions = self._step_actions(current_step)
        if not actions:
            # 无动作的终止节点直接判定为终止
            return True
        terminal_actions = {
            "answer_user",
            "handoff_human",
            "continue_flow",
            "ask_user",
            "ask_clarification",
        }
        return all(
            action in terminal_actions or action.startswith("call_tool:") for action in actions
        )

    def _current_skill_step(
        self, skill: Skill, active_step_id: str | None
    ) -> dict[str, Any] | None:
        if not active_step_id:
            return None
        for step in self._skill_steps(skill):
            if not isinstance(step, dict):
                continue
            step_ids = {
                str(step.get("step_id") or ""),
                str(step.get("node_id") or ""),
                str(step.get("id") or ""),
            }
            if active_step_id in step_ids:
                return step
        return None

    def _current_step_can_finish_after_tool(self, skill: Skill, chat_session: ChatSession) -> bool:
        """判断当前步骤在工具执行后是否可以直接完成并回复。"""
        step = self._current_skill_step(skill, chat_session.active_step_id)
        if not step:
            return False
        actions = self._step_actions(step)
        if not self._actions_allow_final_reply(actions):
            return False
        expected = [str(field) for field in step.get("expected_user_info", [])]
        return all(
            self._skill_slot_satisfied(chat_session.slots_json or {}, field) for field in expected
        )

    def _actions_allow_final_reply(self, actions: list[str]) -> bool:
        """判断步骤动作列表是否包含 ``answer_user``（允许直接回复用户）。"""
        actions = [_normalize_action(action) for action in actions]
        return "answer_user" in actions

    def _skill_slot_satisfied(self, slots: dict[str, Any], field: str) -> bool:
        """判断指定字段在 slots 中是否已有有效值。"""
        normalized = field.strip()
        if not normalized:
            return True
        if self._slot_has_value(slots, normalized):
            return True
        return False

    def _slot_has_value(self, slots: dict[str, Any], field: str) -> bool:
        """检查 slots 中指定字段的值是否非 None 且非空字符串。"""
        value = slots.get(field)
        return value is not None and value != ""

    def _complete_active_skill(
        self, tenant_id: str, chat_session: ChatSession, skill: Skill, reason: str
    ) -> None:
        """完成当前活跃技能，清理状态并记录 ``skill_completed`` 事件。"""
        before_skill = chat_session.active_skill_id
        before_step = chat_session.active_step_id
        self.runtime.complete_current_skill(chat_session)
        self.events.record(
            tenant_id,
            chat_session.id,
            "skill_completed",
            {
                "skill_id": before_skill or skill.skill_id,
                "step_id": before_step,
                "reason": reason,
                "resumed_skill_id": chat_session.active_skill_id,
                "resumed_step_id": chat_session.active_step_id,
            },
        )

    def _get_request_model(
        self,
        request: ChatTurnRequest,
        agent_id: str | None = None,
        role: str = "default",
    ) -> ModelConfig | None:
        if request.model_config_id:
            row = self.db.get(ModelConfig, request.model_config_id)
            if not row or row.tenant_id != request.tenant_id:
                raise AgentLoopPreconditionError("invalid_model_config", "选中的模型配置不存在。")
            if not row.enabled:
                raise AgentLoopPreconditionError("disabled_model_config", "选中的模型配置已停用。")
            return resolve_model_config_for_runtime(self.db, request.tenant_id, row.id)
        return self._get_default_model(request.tenant_id, agent_id, role)

    def _get_default_model(
        self, tenant_id: str, agent_id: str | None = None, role: str = "default"
    ) -> ModelConfig | None:
        return model_for_agent(self.db, tenant_id, agent_id, role)

    def _get_persona_prompt(self, tenant_id: str, agent_id: str | None = None) -> str | None:
        """获取 Agent 或租户的 persona 系统提示词。"""
        agent = self._get_agent_profile(tenant_id, agent_id)
        if agent and not agent.is_overall:
            return _agent_identity_prompt(agent)
        if agent and agent.is_overall and agent.persona_prompt:
            return agent.persona_prompt
        row = self.db.get(PersonaConfig, tenant_id)
        return row.system_prompt if row else None

    def _get_reflection_max_rounds(self, tenant_id: str) -> int:
        """获取租户配置的反思最大轮数（限制在 [0, REFLECTION_MAX_ROUNDS_LIMIT] 范围内）。"""
        row = self.db.get(UIConfig, tenant_id)
        value = row.reflection_max_rounds if row else DEFAULT_REFLECTION_MAX_ROUNDS
        return max(0, min(int(value), REFLECTION_MAX_ROUNDS_LIMIT))

    def _get_agent_loop_max_actions(self, tenant_id: str) -> int:
        """获取租户配置的每轮最大动作数（限制在 [1, 20] 范围内）。"""
        if not hasattr(self.db, "get"):
            return MAX_TOOL_ACTIONS_PER_TURN
        row = self.db.get(UIConfig, tenant_id)
        value = row.agent_loop_max_actions if row else MAX_TOOL_ACTIONS_PER_TURN
        return max(1, min(int(value), 20))

    def _list_published_skills(self, tenant_id: str, agent_id: str | None = None) -> list[Skill]:
        return visible_published_skills(self.db, tenant_id, agent_id)

    def _list_published_general_skills(
        self, tenant_id: str, agent_id: str | None = None
    ) -> list[GeneralSkill]:
        """列出 Agent 可见的已发布通用技能列表（含开放画廊和绑定资源）。"""
        agent = self._get_agent_profile(tenant_id, agent_id)
        if not agent or agent.is_overall:
            rows = self.db.exec(
                select(GeneralSkill).where(
                    GeneralSkill.tenant_id == tenant_id,
                    GeneralSkill.status == "published",
                )
            ).all()
            return [
                row
                for row in rows
                if is_open_gallery_resource(self.db, tenant_id, "general_skill", row)
            ]

        bindings = self.db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == tenant_id,
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "general_skill",
                AgentResourceBinding.status == "active",
            )
        ).all()
        visible: list[GeneralSkill] = []
        for binding in bindings:
            row = self.db.get(GeneralSkill, binding.resource_id)
            if not row or row.tenant_id != tenant_id or row.status != "published":
                continue
            if is_bound_resource_visible_for_agent(
                self.db,
                tenant_id,
                "general_skill",
                row,
                binding,
            ):
                visible.append(row)
        return visible

    def _select_general_skill(
        self,
        message: str,
        model_config: ModelConfig,
        agent_id: str | None = None,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> tuple[GeneralSkill, GeneralSkillSelection] | None:
        skill, selection = self._select_general_capability(
            message,
            model_config,
            agent_id,
            conversation_context,
            memory_context,
        )
        if skill is None:
            return None
        return skill, selection

    def _select_general_capability(
        self,
        message: str,
        model_config: ModelConfig,
        agent_id: str | None = None,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> tuple[GeneralSkill | None, GeneralSkillSelection]:
        general_skills = self._list_published_general_skills(model_config.tenant_id, agent_id)
        try:
            selection = self.general_skill_selector.decide(
                message,
                general_skills,
                model_config,
                conversation_context,
                memory_context,
            )
        except LLMError as exc:
            return None, GeneralSkillSelection(reason=f"Capability selection failed: {exc}")
        if not selection.use_general_skill or not selection.selected_slug:
            return None, selection
        skill = next(
            (item for item in general_skills if item.slug == selection.selected_slug), None
        )
        if not skill:
            return None, selection.model_copy(
                update={"use_general_skill": False, "selected_slug": None}
            )
        return skill, selection

    def _list_enabled_tools(self, tenant_id: str, agent_id: str | None = None) -> list[Tool]:
        """列出 Agent 可见的已启用工具列表。"""
        return visible_tool_rows(self.db, tenant_id, agent_id, include_inactive=False)

    def _tools_with_general_skills(
        self, tenant_id: str, tools: list[Tool], agent_id: str | None = None
    ) -> list[Any]:
        combined: list[Any] = list(tools)
        for skill in self._list_published_general_skills(tenant_id, agent_id):
            combined.append(
                SimpleNamespace(
                    enabled=True,
                    name=f"{GENERAL_SKILL_TOOL_PREFIX}{skill.slug}",
                    display_name=skill.name,
                    description=(
                        f"通用技能：{skill.description or skill.name}。"
                        "仅当当前子任务与该名称、描述和能力边界直接匹配时才能调用；"
                        "不得把它作为场景工具、已有工具结果、知识查询或追问用户的兜底替代。"
                    ),
                    input_schema={
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "传给通用技能的自然语言任务或问题。",
                            }
                        },
                        "required": ["query"],
                    },
                    allowed_skills_json=[],
                )
            )
        return combined

    def _get_agent_profile(self, tenant_id: str, agent_id: str | None) -> AgentProfile | None:
        """获取 Agent 配置信息，校验归属租户和启用状态。

        Args:
            tenant_id: 租户 ID。
            agent_id: Agent ID。

        Returns:
            Agent 配置对象；若 ID 为空、不存在、归属不符或未激活则返回 None。
        """
        if not agent_id:
            return None
        row = self.db.get(AgentProfile, agent_id)
        if not row or row.tenant_id != tenant_id or row.status != "active":
            return None
        return row

    def _agent_requires_resource_filter(self, tenant_id: str, agent_id: str | None) -> bool:
        agent = self._get_agent_profile(tenant_id, agent_id)
        return bool(agent and not agent.is_overall)

    def _agent_visible_knowledge_base_ids(self, tenant_id: str, agent_id: str | None) -> list[str]:
        """获取 Agent 可见的知识库 ID 列表。"""
        return visible_knowledge_base_ids(self.db, tenant_id, agent_id)

    def _get_active_skill(
        self, tenant_id: str, skill_id: str | None, agent_id: str | None = None
    ) -> Skill | None:
        if not skill_id:
            return None
        return visible_skill(self.db, tenant_id, skill_id, agent_id)

    def _drop_unavailable_skill_state(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        skills: list[Skill],
    ) -> bool:
        """清理会话中引用了已不可用技能的状态（活跃技能、pending 任务等）。

        Returns:
            是否清理了任何状态。
        """
        available_skill_ids = {skill.skill_id for skill in skills}
        changed = False
        removed_skill_ids: set[str] = set()

        # 清理技能栈和恢复标记（这些是旧版技能挂起/恢复机制遗留的状态）
        if chat_session.skill_stack_json or chat_session.resume_after_answer_json:
            chat_session.skill_stack_json = []
            chat_session.resume_after_answer_json = None
            changed = True

        # 辅助函数：从任务帧中提取技能 ID（兼容 target_skill_id 和 skill_id）
        def frame_skill_id(frame: object) -> str:
            if not isinstance(frame, dict):
                return ""
            return str(frame.get("target_skill_id") or frame.get("skill_id") or "").strip()

        # 辅助函数：判断任务帧是否应保留（引用的技能仍可用则保留）
        def keep_frame(frame: object) -> bool:
            skill_id = frame_skill_id(frame)
            if not skill_id:
                return True
            if skill_id in available_skill_ids:
                return True
            removed_skill_ids.add(skill_id)
            return False

        # 检查活跃技能是否已不可用，若不可用则清除相关状态
        active_skill_id = str(chat_session.active_skill_id or "").strip()
        if active_skill_id and active_skill_id not in available_skill_ids:
            removed_skill_ids.add(active_skill_id)
            chat_session.active_skill_id = None
            chat_session.active_step_id = None
            chat_session.slots_json = {}
            chat_session.awaiting_input_json = None
            chat_session.resume_after_answer_json = None
            changed = True

        # 过滤 pending_tasks_json 中引用了不可用技能的任务帧
        for attr in ("pending_tasks_json",):
            value = getattr(chat_session, attr) or []
            if not isinstance(value, list):
                continue
            kept = [frame for frame in value if keep_frame(frame)]
            if len(kept) != len(value):
                setattr(chat_session, attr, kept)
                changed = True

        # 检查 awaiting_input 状态中引用的技能是否可用
        awaiting = chat_session.awaiting_input_json
        if isinstance(awaiting, dict):
            awaiting_skill_id = str(awaiting.get("skill_id") or "").strip()
            if awaiting_skill_id and awaiting_skill_id not in available_skill_ids:
                removed_skill_ids.add(awaiting_skill_id)
                chat_session.awaiting_input_json = None
                changed = True

        # 若有任何状态变更，更新时间戳并记录 skill_state_pruned 事件
        if changed:
            chat_session.updated_at = utc_now()
            if hasattr(self, "events"):
                self.events.record(
                    tenant_id,
                    chat_session.id,
                    "skill_state_pruned",
                    {"removed_skill_ids": sorted(removed_skill_ids)},
                )
        return changed

    def _should_record_runtime_event_after_prune(
        self,
        router_decision: RouterDecision,
        chat_session: ChatSession,
        skills: list[Skill],
        state_pruned: bool,
    ) -> bool:
        """判断在状态清理后是否仍应记录运行时事件。

        当状态清理移除了活跃技能后，若路由决策的目标技能同样不可用，
        则跳过事件记录以避免产生误导性的技能切换事件。

        Args:
            router_decision: 当前路由决策。
            chat_session: 当前会话。
            skills: 可用技能列表。
            state_pruned: 是否执行了状态清理。

        Returns:
            是否应记录运行时事件。
        """
        if not state_pruned:
            # 未执行清理，正常记录
            return True
        if chat_session.active_skill_id:
            # 清理后仍有活跃技能，正常记录
            return True
        # 清理后无活跃技能，检查路由目标技能是否可用
        target_skill_id = str(router_decision.target_skill_id or "").strip()
        if not target_skill_id:
            # 路由无目标技能（如 answer_only），正常记录
            return True
        return target_skill_id in {skill.skill_id for skill in skills}

    def _recent_messages(self, chat_session: ChatSession, limit: int = 8) -> list[dict[str, Any]]:
        if not hasattr(self, "db"):
            return []
        rows = list(
            self.db.exec(
                select(Message)
                .where(
                    Message.tenant_id == chat_session.tenant_id,
                    Message.session_id == chat_session.id,
                )
                .order_by(Message.created_at.desc())
                .limit(limit)
            ).all()
        )
        rows.reverse()
        return [self._message_context_entry(row) for row in rows]

    def _conversation_context(
        self,
        chat_session: ChatSession,
        model_config: ModelConfig | None = None,
    ) -> dict[str, object]:
        if not hasattr(self, "db") or not hasattr(self.db, "exec"):
            return build_conversation_context([])
        rows = list(
            self.db.exec(
                select(Message)
                .where(
                    Message.tenant_id == chat_session.tenant_id,
                    Message.session_id == chat_session.id,
                )
                .order_by(Message.created_at.asc())
            ).all()
        )
        context = build_conversation_context(
            [self._message_context_entry(row) for row in rows],
            context_state=chat_session.context_state_json,
            summary_builder=self._context_summary_builder(model_config)
            if model_config
            else None,
        )
        next_state = context.get("context_state")
        if isinstance(next_state, dict) and next_state != (chat_session.context_state_json or {}):
            chat_session.context_state_json = next_state
            self.db.add(chat_session)
        return context

    @staticmethod
    def _context_compacted_now(context: dict[str, object] | None) -> bool:
        """检查上下文是否在本轮触发了压缩（metadata.compacted_now）。"""
        if not isinstance(context, dict):
            return False
        metadata = context.get("metadata")
        return isinstance(metadata, dict) and metadata.get("compacted_now") is True

    def _context_summary_builder(
        self, model_config: ModelConfig
    ) -> Callable[[str, str, int], str]:
        def summarize(label: str, source: str, token_budget: int) -> str:
            payload = stage_payload(
                phase="Context Compression",
                user_message=f"请压缩{label}",
                conversation_context={},
                memory_context=None,
                instructions=(
                    "把输入的历史对话压缩成一段可供后续对话继续使用的中文事实摘要。"
                    "保留用户身份与偏好、已确认事实、未完成任务、关键约束、工具或知识结论；"
                    "删除寒暄、重复内容、内部 ID、时间戳和推理过程，不新增原文没有的信息。"
                ),
                stage_data={"history_to_compress": source},
                output_contract=(
                    f"只输出一段纯文本摘要，控制在约 {token_budget} tokens 以内。"
                ),
            )
            with llm_operation("context.compact"):
                return LLMClient(model_config).generate_text(
                    unified_system_prompt(), payload
                ).strip()

        return summarize

    def _message_context_entry(self, row: Message) -> dict[str, Any]:
        """将消息数据库行转换为对话上下文条目字典。"""
        entry: dict[str, Any] = {
            "id": row.id,
            "role": row.role,
            "content": message_content_with_attachment_context(row.content, row.metadata_json),
            "created_at": row.created_at,
        }
        images = message_images_from_metadata(row.metadata_json)
        if images and row.role == "user":
            entry["images"] = images
        return entry

    def _assistant_message_metadata(
        self,
        step_result: StepAgentResult | None,
        chat_session: ChatSession,
        source_message: str | None = None,
    ) -> dict[str, Any]:
        knowledge_results = list(step_result.knowledge_results or []) if step_result else []
        citations = self._dedupe_knowledge_citations(
            knowledge_citations_from_results(knowledge_results)
        )
        if not citations:
            return {}
        first_query = next(
            (
                item.get("query")
                for item in knowledge_results
                if isinstance(item.get("query"), dict)
            ),
            None,
        )
        return {
            "knowledge_citations": citations,
            "knowledge_query": first_query or {},
        }

    def _dedupe_knowledge_citations(self, citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """对知识引用列表去重（基于文档 ID 和 chunk 位置）。"""
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for citation in citations:
            if not isinstance(citation, dict):
                continue
            identity = str(
                citation.get("title")
                or citation.get("section_path")
                or citation.get("summary")
                or citation.get("excerpt")
                or citation.get("source_path")
                or citation.get("concept_id")
                or citation.get("id")
                or ""
            )
            key = re.sub(r"\s+", " ", identity).strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            result.append({**citation, "label": f"[{len(result) + 1}]"})
            if len(result) >= 4:
                break
        return result

    def _append_message(
        self,
        tenant_id: str,
        session_id: str,
        role: str,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> Message:
        """创建并持久化一条消息记录。

        Args:
            tenant_id: 租户 ID。
            session_id: 会话 ID。
            role: 消息角色（user/assistant/system 等）。
            content: 消息内容。
            metadata: 消息元数据（可选）。

        Returns:
            新创建的消息对象（尚未 commit）。
        """
        message = Message(
            tenant_id=tenant_id,
            session_id=session_id,
            role=role,
            content=content,
            metadata_json=metadata or {},
        )
        self.db.add(message)
        return message

    def _persist_cancelled_assistant_message(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        user_message_id: str,
        client_turn_id: str | None = None,
    ) -> Message | None:
        if not user_message_id:
            return None
        user_message = self.db.get(Message, user_message_id)
        if (
            not user_message
            or user_message.tenant_id != tenant_id
            or user_message.session_id != chat_session.id
            or user_message.role != "user"
        ):
            return None

        normalized_client_turn_id = (client_turn_id or "").strip()
        turn_ids = {user_message_id}
        if normalized_client_turn_id:
            turn_ids.add(normalized_client_turn_id)
        existing_messages = self.db.exec(
            select(Message)
            .where(
                Message.tenant_id == tenant_id,
                Message.session_id == chat_session.id,
                Message.role == "assistant",
            )
            .order_by(Message.created_at)
        ).all()
        for row in existing_messages:
            metadata = row.metadata_json or {}
            row_turn_ids = {
                str(metadata.get("turn_id") or "").strip(),
                str(metadata.get("user_message_id") or "").strip(),
                str(metadata.get("client_turn_id") or "").strip(),
            }
            if turn_ids & row_turn_ids:
                return None

        chat_session.updated_at = utc_now()
        chat_session.status = "active"
        chat_session.summary = f"最近回复：{CANCELLED_ASSISTANT_REPLY}"
        assistant_message = self._append_message(
            tenant_id,
            chat_session.id,
            "assistant",
            CANCELLED_ASSISTANT_REPLY,
            metadata={
                "turn_id": user_message_id,
                "user_message_id": user_message_id,
                "client_turn_id": normalized_client_turn_id or None,
                "status": "cancelled",
            },
        )
        self.events.record(
            tenant_id,
            chat_session.id,
            "assistant_message_created",
            {
                "message_id": assistant_message.id,
                "assistant_message_id": assistant_message.id,
                "user_message_id": user_message_id,
                "turn_id": user_message_id,
                "client_turn_id": normalized_client_turn_id or None,
                "reply": CANCELLED_ASSISTANT_REPLY,
                "status": "cancelled",
            },
        )
        self.events.record(
            tenant_id,
            chat_session.id,
            "session_state_changed",
            public_session(chat_session).model_dump(),
        )
        return assistant_message

    def _user_message_metadata(self, request: ChatTurnRequest) -> dict[str, Any]:
        """构建用户消息的元数据（客户端轮次 ID、附件、交互模式等）。"""
        metadata: dict[str, Any] = {}
        if request.client_turn_id:
            metadata["client_turn_id"] = request.client_turn_id
        if request.interaction_mode == "scheduled_task":
            metadata["interaction_mode"] = "scheduled_task"
        if request.model_config_id:
            metadata["model_config_id"] = request.model_config_id
        if request.attachments:
            metadata["attachments"] = [item.model_dump(mode="json") for item in request.attachments]
        return metadata

    def _record_runtime_event(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        before_skill: str | None,
        before_step: str | None,
        decision: RouterDecision,
    ) -> None:
        """根据路由决策类型记录对应的运行时技能事件。

        将路由决策映射为前端可识别的事件类型：

        - ``start_new_task`` → ``skill_started``
        - ``switch_to_pending`` → ``skill_resumed``
        - ``complete_task`` → ``skill_exited``
        - ``handoff_human`` → ``handoff_triggered``
        - 其他 → ``skill_step_changed``

        若事件为步骤变更但技能和步骤均未变化，则跳过记录以避免冗余事件。

        Args:
            tenant_id: 租户 ID。
            chat_session: 当前会话（已应用路由决策后的状态）。
            before_skill: 路由前的活跃技能 ID。
            before_step: 路由前的活跃步骤 ID。
            decision: 路由决策对象。
        """
        # 根据决策类型映射事件类型
        event_type = "skill_step_changed"
        if decision.decision == "start_new_task":
            event_type = "skill_started"
        elif decision.decision == "switch_to_pending":
            event_type = "skill_resumed"
        elif decision.decision == "complete_task":
            event_type = "skill_exited"
        elif decision.decision == "handoff_human":
            event_type = "handoff_triggered"

        # 若为步骤变更事件但技能和步骤都未变化，跳过以避免冗余
        if (
            event_type == "skill_step_changed"
            and before_skill == chat_session.active_skill_id
            and before_step == chat_session.active_step_id
        ):
            return

        # 构建事件 payload，包含前后技能/步骤对比和版本信息
        payload = {
            "decision": decision.decision,
            "from_skill_id": before_skill,
            "to_skill_id": chat_session.active_skill_id,
            "from_skill_version": self._skill_version(tenant_id, before_skill),
            "to_skill_version": self._skill_version(tenant_id, chat_session.active_skill_id),
            "from_step_id": before_step,
            "to_step_id": chat_session.active_step_id,
        }
        self.events.record(tenant_id, chat_session.id, event_type, payload)

    def _skill_version(self, tenant_id: str, skill_id: str | None) -> str | None:
        """查询指定技能的版本号。

        Args:
            tenant_id: 租户 ID。
            skill_id: 技能 ID。

        Returns:
            技能版本字符串；若技能不存在则返回 None。
        """
        if not skill_id:
            return None
        row = self.db.exec(
            select(Skill.version).where(Skill.tenant_id == tenant_id, Skill.skill_id == skill_id)
        ).first()
        return str(row) if row else None

    def _runtime_stream_context(
        self,
        decision: RouterDecision,
        before_skill: str | None,
        before_step: str | None,
        chat_session: ChatSession,
    ) -> dict[str, object]:
        return {
            "runtimeDecision": decision.decision,
            "fromSkillId": before_skill,
            "fromStepId": before_step,
            "toSkillId": chat_session.active_skill_id,
            "toStepId": chat_session.active_step_id,
        }

    def _skill_state_payload(
        self,
        chat_session: ChatSession,
        skills: list[Skill],
        runtime_context: dict[str, object] | None = None,
        *,
        user_message_id: str | None = None,
    ) -> dict[str, object]:
        skill_names = {skill.skill_id: skill.name for skill in skills}
        visible_skill_ids = set(skill_names)
        current_skills: list[dict[str, object]] = []
        active_skill_id = (
            chat_session.active_skill_id
            if chat_session.active_skill_id in visible_skill_ids
            else None
        )
        if active_skill_id:
            current_skills.append(
                {
                    "skillId": active_skill_id,
                    "name": skill_names.get(active_skill_id, active_skill_id),
                    "stepId": chat_session.active_step_id,
                    "state": "active",
                }
            )
        for task in chat_session.pending_tasks_json or []:
            if not isinstance(task, dict):
                continue
            # 从任务帧中提取技能 ID（兼容 target_skill_id 和 skill_id）
            skill_id = str(task.get("target_skill_id") or task.get("skill_id") or "").strip()
            # 跳过不可见的技能（已被删除或 Agent 无权访问）
            if not skill_id or skill_id not in visible_skill_ids:
                continue
            current_skills.append(
                {
                    "skillId": skill_id,
                    "name": skill_names.get(skill_id, skill_id),
                    "stepId": task.get("target_step_id") or task.get("step_id"),
                    "state": task.get("status") or "pending",
                }
            )
        # 合并活跃技能、排队任务技能和运行时上下文，生成最终 payload
        payload = {
            "activeSkillId": active_skill_id,
            "activeStepId": chat_session.active_step_id if active_skill_id else None,
            "currentSkills": current_skills,
            **(runtime_context or {}),
        }
        return self._turn_payload(payload, user_message_id)

    def _tool_activity_payload(
        self,
        tenant_id: str,
        tool_name: str,
        tool_result: ToolResult,
        tool_call: ToolCall | None = None,
        tool_call_id: str | None = None,
    ) -> dict[str, object]:
        """构建工具活动 payload（含工具信息、调用参数、执行结果）。"""
        tool = self.db.exec(
            select(Tool).where(Tool.tenant_id == tenant_id, Tool.name == tool_name)
        ).first()
        payload: dict[str, object] = {
            "toolId": tool_name,
            "toolName": tool.display_name or tool.name if tool else tool_name,
            "rawToolName": tool_name,
            "content": tool_result.model_dump(mode="json"),
            "isError": not tool_result.success,
            "success": tool_result.success,
        }
        if tool_call:
            payload["toolCall"] = tool_call.model_dump(mode="json")
            payload["arguments"] = tool_call.arguments
        if tool_call_id:
            payload["toolCallId"] = tool_call_id
        return payload

    def _record_general_skill_run_events(
        self,
        tenant_id: str,
        chat_session: ChatSession,
        run_response: GeneralSkillRunResponse,
        user_message_id: str | None = None,
        include_trace: bool = True,
    ) -> None:
        if include_trace:
            for item in run_response.execution_trace:
                self.events.record(
                    tenant_id,
                    chat_session.id,
                    "general_skill_trace",
                    self._turn_payload(
                        {
                            "skill_slug": run_response.skill_slug,
                            **item,
                        },
                        user_message_id,
                    ),
                )
        self.events.record(
            tenant_id,
            chat_session.id,
            "general_skill_run_finished",
            self._turn_payload(
                {
                    "skill_slug": run_response.skill_slug,
                    "success": bool(run_response.structured_result.get("success", True)),
                    "stdout_preview": run_response.stdout[:600],
                    "stderr_preview": run_response.stderr[:600],
                    "structured_result": run_response.structured_result,
                },
                user_message_id,
            ),
        )

    def _enqueue_memory_capture(
        self,
        request: ChatTurnRequest,
        chat_session: ChatSession,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        model_config: ModelConfig,
    ) -> list[dict[str, object]]:
        """将记忆提取任务异步入队。

        在对话轮次完成后，将本轮的步骤结果和工具结果交给后台记忆提取流水线
        （通过 :func:`enqueue_memory_capture` 提交异步 Job），由后台 Worker
        从对话内容中提取并持久化用户偏好、事实记忆等。

        若入队失败（如任务队列不可用），则记录 ``memory_error`` 事件并返回空列表，
        不影响主流程。

        Args:
            request: 对话轮次请求。
            chat_session: 当前会话。
            step_result: 本轮步骤结果。
            tool_result: 本轮工具结果（可为 None）。
            model_config: 模型配置（记忆提取需要使用模型）。

        Returns:
            包含 Job 信息的字典列表 ``[{"job_id": ..., "job_name": ...}]``；
            入队失败时返回空列表。
        """
        try:
            # 提交异步记忆提取任务到后台队列
            job = enqueue_memory_capture(
                request,
                chat_session.id,
                step_result,
                tool_result,
                model_config.id,
            )
        except Exception as exc:
            # 入队失败：记录错误事件但不中断主流程
            self.events.record(
                request.tenant_id,
                chat_session.id,
                "memory_error",
                {"message": str(exc)},
            )
            return []
        # 记录异步任务入队事件，便于前端展示"正在记忆"状态
        self.events.record(
            request.tenant_id,
            chat_session.id,
            "async_job_enqueued",
            {"job_id": job.id, "job_name": job.name, "feature": "memory"},
        )
        self.db.commit()
        return [{"job_id": job.id, "job_name": job.name}]

    def _finish_with_error(
        self, chat_session: ChatSession, code: str, message: str
    ) -> ChatTurnResponse:
        """以前置条件错误终止本轮对话，生成错误回复并持久化。"""
        reply = format_runtime_failure_reply(
            "系统配置错误",
            message,
            code,
            "请在管理端补齐配置后重试。",
        )
        self.events.record(
            chat_session.tenant_id,
            chat_session.id,
            "error_occurred",
            {"code": code, "message": message},
        )
        self._finalize_turn(chat_session, chat_session.tenant_id, reply)
        self.db.commit()
        self.db.refresh(chat_session)
        return ChatTurnResponse(
            reply=reply,
            session_id=chat_session.id,
            session_state=public_session(chat_session),
        )

    def _finalize_turn(
        self,
        chat_session: ChatSession,
        tenant_id: str,
        reply: str,
        step_result: StepAgentResult | None = None,
        source_message: str | None = None,
        user_message_id: str | None = None,
    ) -> None:
        """收尾持久化：保存 assistant 消息、规范化引用、记录事件。

        这是编排管道的**最终持久化方法**，在 :meth:`handle_turn` 和
        :meth:`handle_turn_stream` 末尾调用。执行以下操作：

        1. 更新会话时间戳和状态（非 handoff 时设为 active）。
        2. 构建 assistant 消息元数据（知识引用等）。
        3. 规范化回复中的引用标签（重新编号、去重）。
        4. 移除回复末尾的引用摘要段落。
        5. 若会话无标题，从用户消息生成兜底标题。
        6. 更新会话摘要。
        7. 追加 assistant 消息到数据库。
        8. 记录 ``assistant_message_created`` 和 ``session_state_changed`` 事件。

        Args:
            chat_session: 当前会话。
            tenant_id: 租户 ID。
            reply: 最终回复文本。
            step_result: 步骤结果（用于提取知识引用）。
            source_message: 用户消息原文（用于生成标题）。
            user_message_id: 用户消息 ID（关联轮次）。
        """
        # 步骤 1：更新会话时间戳和状态（若非 handoff 状态则设为 active）
        chat_session.updated_at = utc_now()
        if chat_session.status != "handoff":
            chat_session.status = "active"
        # 步骤 2：构建 assistant 消息元数据（提取知识引用 citations）
        metadata = self._assistant_message_metadata(step_result, chat_session, source_message)
        # 步骤 3：规范化回复中的引用标签序号（确保 [n] 不超出引用列表范围）
        reply = self._normalize_reply_citation_labels(reply, metadata.get("knowledge_citations"))
        # 步骤 4：移除回复末尾自动生成的"参考资料：[1][2]..."段落
        reply = self._strip_trailing_citation_summary(reply)
        # 步骤 4b：压缩引用标签（合并连续引用、去重）
        reply, compacted_citations = compact_knowledge_citation_labels(
            reply,
            metadata.get("knowledge_citations"),
        )
        # 根据压缩结果更新元数据中的引用信息
        metadata = dict(metadata)
        if compacted_citations:
            metadata["knowledge_citations"] = compacted_citations
        else:
            # 无引用时清理多余的引用字段
            metadata.pop("knowledge_citations", None)
            metadata.pop("knowledge_query", None)
        # 步骤 5：若会话尚无标题，从用户消息生成兜底标题
        if not chat_session.title and source_message:
            fallback_title = self._fallback_session_title_from_message(source_message)
            if fallback_title:
                chat_session.title = fallback_title
        # 步骤 6：更新会话摘要（截取回复前 120 字符）
        chat_session.summary = f"最近回复：{reply[:120]}"
        # 步骤 7：构建 assistant 消息元数据并追加到数据库
        assistant_metadata = dict(metadata or {})
        if user_message_id:
            assistant_metadata.setdefault("user_message_id", user_message_id)
            assistant_metadata.setdefault("turn_id", user_message_id)
        assistant_message = self._append_message(
            tenant_id,
            chat_session.id,
            "assistant",
            reply,
            metadata=assistant_metadata,
        )
        # 步骤 8：构建并记录 assistant_message_created 事件
        event_payload: dict[str, Any] = {
            "message_id": assistant_message.id,
            "assistant_message_id": assistant_message.id,
            "reply": reply,
        }
        if user_message_id:
            event_payload["user_message_id"] = user_message_id
            event_payload["turn_id"] = user_message_id
        if assistant_metadata.get("knowledge_citations"):
            event_payload["knowledge_citations"] = assistant_metadata["knowledge_citations"]
        self.events.record(
            tenant_id,
            chat_session.id,
            "assistant_message_created",
            event_payload,
        )
        # 记录会话状态变更事件，供前端同步会话面板
        self.events.record(
            tenant_id,
            chat_session.id,
            "session_state_changed",
            public_session(chat_session).model_dump(),
        )

    def _mark_session_running(self, chat_session: ChatSession) -> None:
        """将非 handoff 状态的会话标记为 running（表示正在处理中）。"""
        if chat_session.status == "handoff":
            return
        chat_session.status = "running"
        chat_session.updated_at = utc_now()
        self.db.add(chat_session)

    @staticmethod
    def _fallback_session_title_from_message(message: str) -> str:
        title = re.sub(r"\s+", " ", message).strip().strip("。！？!?")
        if not title:
            return ""
        return title[:28]

    def _normalize_reply_citation_labels(self, reply: str, citations: object) -> str:
        """规范化回复文本中的引用标签序号，确保不超出引用列表范围。"""
        if not isinstance(citations, list) or not citations:
            return reply
        max_label = len(citations)

        def replace(match: re.Match[str]) -> str:
            try:
                value = int(match.group(1))
            except ValueError:
                return match.group(0)
            if 1 <= value <= max_label:
                return match.group(0)
            return f"[{max_label if max_label > 1 else 1}]"

        return re.sub(r"\[(\d+)\]", replace, reply)

    def _strip_trailing_citation_summary(self, reply: str) -> str:
        return re.sub(
            r"(?:\n|\s){0,3}(?:参考资料|引用来源|资料来源)\s*[:：]\s*(?:\[\d+\]\s*)+$",
            "",
            reply.rstrip(),
        ).rstrip()
