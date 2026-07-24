"""路由决策模块（Router）。

本模块是多智能体对话流水线的入口决策层，负责理解用户意图并决定对话的
下一步走向。Router 通过调用 LLM 分析用户消息、当前会话状态和可用技能，
输出一个 :class:`RouterDecision`，指示后续编排器应当采取的动作：

  - ``start_new_task``  —— 启动新任务
  - ``continue_active`` —— 继续当前活跃任务
  - ``switch_to_pending`` —— 切换到某个待处理任务
  - ``create_pending`` —— 创建并暂存待处理任务
  - ``clarify`` —— 向用户澄清意图

核心概念
--------
- **RouterDecision**: 路由决策的结构化结果，包含决策类型、目标技能、
  目标步骤、槽位提示、任务帧等字段。
- **技能 (Skill)**: 可执行的业务流程图，由若干节点 (node) 组成，每个节点
  代表一个交互步骤。
- **会话状态 (ChatSession)**: 当前对话上下文快照，包括活跃技能、已收集
  槽位、待处理任务列表等。

与其他模块的关系
----------------
- 被主编排器调用，产出的决策结果传递给 :mod:`app.core.step_agent` 执行。
- 依赖 :mod:`app.llm` 提供的 LLM 客户端进行推理。
- 依赖 :mod:`app.session.slot_policy` 进行槽位策略清洗。
- 使用 :mod:`app.core.context_projection` 压缩上下文以减少 token 消耗。
"""

from __future__ import annotations

from typing import Any

from app import paths
from app.core.context_projection import (
    compact_awaiting_input,
    compact_conversation_context,
    compact_pending_tasks,
)
from app.db.models import ChatSession, ModelConfig, Skill
from app.llm import LLMClient, LLMError
from app.llm.stage_protocol import (
    ROUTER_OUTPUT_SCHEMA,
    stage_payload,
    unified_system_prompt,
)
from app.observability.spans import llm_operation
from app.session.session_schema import PendingTask, RouterDecision
from app.session.slot_policy import strip_router_generated_message_slots


# Router 提示词文件路径，LLM 据此理解如何进行路由决策
PROMPT_PATH = paths.resource_dir() / "app" / "llm" / "prompts" / "router_prompt.md"


class Router:
    """路由决策器，负责分析用户消息并输出路由决策。

    设计意图：将"理解用户意图 → 选择技能 → 决定动作"这一核心逻辑封装
    为单一职责的类。Router 内部调用 LLM 进行推理，随后对 LLM 返回的
    原始决策进行多层校验与归一化，确保下游接收到的是合法、一致的决策。

    主要流程：
        1. 组装提示词 payload（会话状态 + 可用技能 + 对话上下文）。
        2. 调用 LLM 生成结构化 JSON 并解析为 :class:`RouterDecision`。
        3. 对决策进行归一化（校验技能/步骤存在性、合并任务帧等）。
    """

    def decide(
        self,
        message: str,
        session: ChatSession,
        available_skills: list[Skill],
        model_config: ModelConfig,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> RouterDecision:
        """分析用户消息并输出路由决策。

        将用户消息、会话状态、可用技能等组装为 LLM 输入 payload，调用
        LLM 进行意图理解，随后对返回的原始决策进行归一化校验。

        Args:
            message: 用户发送的原始消息文本。
            session: 当前会话状态对象，包含活跃技能、槽位、待处理任务等。
            available_skills: 当前可用的技能列表，供 LLM 选择目标技能。
            model_config: LLM 模型配置（API Key、模型名称等）。
            conversation_context: 压缩后的对话历史上下文，用于多轮理解。
            memory_context: 长期记忆上下文条目列表（可选）。

        Returns:
            经过归一化校验的 :class:`RouterDecision` 对象。

        Raises:
            LLMError: 当 LLM 调用失败或返回的 JSON 不符合 Router 输出
                schema 时抛出。
        """
        # 组装阶段 payload：包含阶段标识、用户消息、压缩后的上下文、
        # 提示词指令、会话状态快照、可用技能列表，以及输出格式约束。
        payload = stage_payload(
            phase="Router",
            user_message=message,
            conversation_context=compact_conversation_context(conversation_context),
            memory_context=memory_context,
            instructions=PROMPT_PATH.read_text(encoding="utf-8"),
            stage_data={
                "current_session": _router_session_payload(session),
                "available_skills": _available_skill_payloads(available_skills),
            },
            output_contract=ROUTER_OUTPUT_SCHEMA,
        )
        try:
            # 在可观测性 span 中记录本次 LLM 调用
            with llm_operation("router.scene"):
                raw = LLMClient(model_config).generate_json(
                    unified_system_prompt(), payload
                )
            # 将 LLM 返回的原始 JSON 解析为 RouterDecision 模型
            decision = RouterDecision.model_validate(raw)
        except Exception as exc:
            # LLM 调用本身的错误直接向上传播
            if isinstance(exc, LLMError):
                raise
            # schema 解析失败包装为 LLMError
            raise LLMError(f"Router returned invalid JSON schema: {exc}") from exc
        # 对原始决策进行多层归一化校验后返回
        return self._normalize_decision(decision, session, available_skills)

    def _normalize_decision(
        self, decision: RouterDecision, session: ChatSession, available_skills: list[Skill]
    ) -> RouterDecision:
        """对 LLM 返回的原始路由决策进行多层归一化校验。

        该方法是 Router 的核心校验逻辑，确保下游接收到的决策在语义和
        数据引用上都是合法、一致的。主要执行以下步骤：

        1. 清洗槽位提示（移除不应由 Router 生成的消息槽位）。
        2. 规范化 general_intent 文本（合并空白字符）。
        3. 校验目标技能/步骤是否存在于可用技能集合中。
        4. 针对 ``start_new_task``、``switch_to_pending``、``create_pending``
           三种决策类型执行特定的合法性检查与降级处理。
        5. 如果目标技能存在但缺少步骤，则自动推断首节点。
        6. 归一化各任务列表（pending_tasks、task_frames 等）。

        Args:
            decision: LLM 返回并解析后的原始 :class:`RouterDecision`。
            session: 当前会话状态，用于查询待处理任务等。
            available_skills: 可用技能列表，用于校验技能引用合法性。

        Returns:
            归一化后的 :class:`RouterDecision`（原地修改后返回）。
        """
        # 步骤 1：清洗槽位提示，移除 Router 不应生成的消息类槽位
        self._strip_generated_message_slots(decision)
        # 步骤 2：规范化 general_intent，合并多余空白字符，空则置 None
        decision.general_intent = " ".join(
            str(decision.general_intent or "").split()
        ) or None
        # 构建技能 ID 到技能对象的映射，用于后续校验
        skills = {skill.skill_id: skill for skill in available_skills}
        # 步骤 3a：如果目标技能不在可用集合中，则清除目标和步骤
        if decision.target_skill_id and decision.target_skill_id not in skills:
            decision.target_skill_id = None
            decision.target_step_id = None
        # 步骤 3b：如果 awaiting_input 引用了不存在的技能，则清除
        if decision.awaiting_input and decision.awaiting_input.skill_id not in {None, *skills.keys()}:
            decision.awaiting_input = None
        # 步骤 4a：start_new_task 必须有合法目标技能，否则降级为 clarify
        if decision.decision == "start_new_task":
            if not decision.target_skill_id or decision.target_skill_id not in skills:
                decision.decision = "clarify"
                decision.target_skill_id = None
                decision.target_step_id = None
                decision.clarification_question = "请问您想办理哪类业务？"
                return decision
        # 步骤 4b：switch_to_pending 必须选中合法的待处理任务 ID，否则降级
        if decision.decision == "switch_to_pending":
            # 从会话中提取所有待处理任务的 ID 集合
            pending_ids = {
                str(task.get("task_id"))
                for task in (session.pending_tasks_json or [])
                if isinstance(task, dict) and task.get("task_id")
            }
            if not decision.selected_task_id or decision.selected_task_id not in pending_ids:
                decision.decision = "clarify"
                decision.clarification_question = "请问您想继续哪一项待处理任务？"
                return decision
        # 步骤 4c：create_pending 尝试将首个任务帧提升为实际任务
        if decision.decision == "create_pending":
            # 按优先级合并所有任务来源：task_frames > pending_tasks > created_tasks
            ordered_tasks = [
                *decision.task_frames,
                *decision.pending_tasks,
                *decision.created_tasks,
            ]
            if ordered_tasks:
                primary = ordered_tasks[0]
                # 将 create_pending 转换为 start_new_task，以首个任务为主任务
                decision.decision = "start_new_task"
                decision.selected_task_id = primary.task_id
                decision.target_skill_id = primary.target_skill_id
                decision.target_step_id = primary.target_step_id
                decision.user_intent = primary.user_intent or decision.user_intent
                # 合并槽位提示：主任务槽位优先，全局槽位补充
                decision.slot_hints = {
                    **dict(primary.slot_hints or {}),
                    **dict(decision.slot_hints or {}),
                }
                decision.task_frames = ordered_tasks
                decision.pending_tasks = []
                decision.created_tasks = []
                # 再次校验提升后的目标技能是否合法，不合法则降级为 clarify
                if not decision.target_skill_id or decision.target_skill_id not in skills:
                    decision.decision = "clarify"
                    decision.selected_task_id = None
                    decision.target_skill_id = None
                    decision.target_step_id = None
                    decision.clarification_question = "请问您想办理哪类业务？"
                    return decision
        # 步骤 5a：如果没有明确目标技能，但有活跃技能，则继承当前活跃技能
        if not decision.target_skill_id and session.active_skill_id:
            decision.target_skill_id = session.active_skill_id
        # 步骤 5b：如果目标技能存在但缺少步骤 ID，则自动推断技能的首节点
        if decision.target_skill_id and not decision.target_step_id:
            target_skill = skills.get(decision.target_skill_id)
            if target_skill:
                decision.target_step_id = _first_node_id(target_skill)
        # 步骤 6：归一化各任务列表，过滤无效技能引用并自动补全步骤 ID
        normalized_tasks = self._normalize_tasks(decision.pending_tasks, skills)
        decision.pending_tasks = normalized_tasks
        legacy_created_tasks = self._normalize_tasks(decision.created_tasks, skills)
        # 将当前轮次的任务帧统一归一化，合并主决策信息与任务帧列表
        decision.task_frames = self._normalize_turn_task_frames(
            decision,
            self._normalize_tasks(decision.task_frames, skills),
            legacy_created_tasks,
        )
        # created_tasks 已合并到 task_frames，清空旧字段
        decision.created_tasks = []
        return decision

    def _strip_generated_message_slots(self, decision: RouterDecision) -> None:
        """从决策及其关联任务的槽位提示中移除消息类槽位。

        Router 不应自行生成用户消息内容（如回复文本），因此需要通过
        slot_policy 清洗函数剥离这类无效槽位，保持槽位的语义纯净。

        Args:
            decision: 待清洗的路由决策对象（原地修改）。
        """
        decision.slot_hints = strip_router_generated_message_slots(decision.slot_hints)
        for task in [*decision.task_frames, *decision.pending_tasks, *decision.created_tasks]:
            task.slot_hints = strip_router_generated_message_slots(task.slot_hints)
        for update in decision.task_updates:
            update.slot_hints = strip_router_generated_message_slots(update.slot_hints)

    def _normalize_tasks(self, tasks, skills: dict[str, Skill]):
        """归一化任务列表，过滤无效技能引用并自动补全步骤 ID。

        遍历任务列表，丢弃引用了不存在技能的任务。对于缺少步骤 ID 的
        合法任务，自动从技能内容中推断首节点 ID。

        Args:
            tasks: 待归一化的任务对象列表（PendingTask）。
            skills: 技能 ID 到技能对象的映射字典。

        Returns:
            过滤并补全后的任务列表。
        """
        normalized_tasks = []
        for task in tasks:
            # 跳过引用了不存在技能的任务
            if not task.target_skill_id or task.target_skill_id not in skills:
                continue
            # 自动补全缺失的步骤 ID
            if not task.target_step_id:
                target_skill = skills.get(task.target_skill_id)
                if target_skill:
                    task.target_step_id = _first_node_id(target_skill)
            normalized_tasks.append(task)
        return normalized_tasks

    def _normalize_turn_task_frames(
        self,
        decision: RouterDecision,
        task_frames: list[PendingTask],
        legacy_created_tasks: list[PendingTask],
    ) -> list[PendingTask]:
        """归一化当前轮次的任务帧列表，确保主决策信息被正确合并。

        根据决策类型决定是否需要保留任务帧。对于 ``continue_active``、
        ``start_new_task``、``switch_to_pending`` 三种决策，会构建一个
        主任务帧并合并到列表头部；其他决策类型返回空列表。

        合并逻辑：
          - 如果列表为空或首个任务帧的目标技能与主决策不一致，则在
            头部插入主任务帧。
          - 如果首个任务帧的目标技能与主决策一致，则用主决策信息
            补全其缺失字段（task_id、step_id、slot_hints 等）。

        Args:
            decision: 当前轮次的路由决策。
            task_frames: 已归一化的任务帧列表。
            legacy_created_tasks: 旧版 created_tasks 归一化后的任务列表。

        Returns:
            合并后的任务帧列表。
        """
        # 非执行类决策不需要任务帧
        if decision.decision not in {"continue_active", "start_new_task", "switch_to_pending"}:
            return []
        # 从主决策构建主任务帧
        primary = PendingTask(
            task_id=decision.selected_task_id,
            decision=decision.decision,
            target_skill_id=decision.target_skill_id,
            target_step_id=decision.target_step_id,
            confidence=decision.confidence,
            user_intent=decision.user_intent,
            reason=decision.reason,
            slot_hints=dict(decision.slot_hints or {}),
        )
        # 合并任务帧和旧版 created_tasks
        ordered = [*task_frames, *legacy_created_tasks]
        first = ordered[0] if ordered else None
        # 首个任务帧不存在或技能不匹配 → 插入主任务帧
        if not first or first.target_skill_id != primary.target_skill_id:
            ordered.insert(0, primary)
        else:
            # 首个任务帧技能匹配 → 用主决策信息补全缺失字段
            first.decision = decision.decision
            first.task_id = first.task_id or decision.selected_task_id
            first.target_step_id = first.target_step_id or decision.target_step_id
            # 合并槽位：全局槽位优先，任务帧原有槽位补充
            first.slot_hints = {
                **dict(decision.slot_hints or {}),
                **dict(first.slot_hints or {}),
            }
        return ordered


def _first_node_id(skill: Skill) -> str | None:
    """从技能内容中推断流程图的起始节点 ID。

    优先使用技能内容中的 ``start_node_id`` 字段；若不存在，则回退到
    ``nodes`` 列表中的第一个有效节点。

    Args:
        skill: 技能对象，包含 ``content_json`` 流程图数据。

    Returns:
        起始节点 ID 字符串，若无法推断则返回 None。
    """
    content = skill.content_json or {}
    # 优先使用显式声明的 start_node_id
    start_node_id = content.get("start_node_id")
    if isinstance(start_node_id, str) and start_node_id.strip():
        return start_node_id
    # 回退：取 nodes 列表中第一个拥有 node_id 的节点
    nodes = content.get("nodes")
    if isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, dict) and node.get("node_id"):
                return str(node["node_id"])
    return None


def _available_skill_payloads(available_skills: list[Skill]) -> list[dict[str, Any]]:
    """将可用技能列表转换为 LLM 可理解的 payload 格式。

    Args:
        available_skills: 可用技能对象列表。

    Returns:
        每个技能的精简 payload 字典列表。
    """
    return [_skill_payload(skill) for skill in available_skills]


def _skill_payload(skill: Skill) -> dict[str, Any]:
    """将单个技能转换为 LLM payload 字典。

    提取技能 ID、名称、描述和触发意图，移除空值字段以减少 token。

    Args:
        skill: 技能对象。

    Returns:
        精简后的技能 payload 字典。
    """
    content = skill.content_json or {}
    return _without_empty(
        {
            "skill_id": skill.skill_id,
            "name": skill.name,
            "description": skill.description,
            "trigger_intents": content.get("trigger_intents", []),
        }
    )


def _router_session_payload(session: ChatSession) -> dict[str, Any]:
    """将会话状态转换为 Router LLM payload 字典。

    提取活跃技能、活跃步骤、槽位、待处理任务、等待输入状态等关键信息，
    压缩为精简格式供 LLM 理解当前对话上下文。

    Args:
        session: 当前会话状态对象。

    Returns:
        精简后的会话 payload 字典。
    """
    return _without_empty(
        {
            "active_skill_id": session.active_skill_id,
            "active_step_id": session.active_step_id,
            "slots": session.slots_json or {},
            "pending_tasks": compact_pending_tasks(session.pending_tasks_json),
            "awaiting_input": compact_awaiting_input(session.awaiting_input_json),
            "status": session.status,
        }
    )


def _without_empty(value: dict[str, Any]) -> dict[str, Any]:
    """从字典中移除值为 None、空字符串、空列表或空字典的键。

    用于精简 LLM payload，避免发送无意义的空值字段。

    Args:
        value: 原始字典。

    Returns:
        仅包含非空值的字典。
    """
    return {
        key: item
        for key, item in value.items()
        if item is not None and item != "" and item != [] and item != {}
    }
