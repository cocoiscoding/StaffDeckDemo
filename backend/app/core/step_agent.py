"""步骤执行智能体模块（StepAgent）。

本模块是多智能体对话流水线的执行层，负责在路由决策确定后，针对当前
技能流程图的具体节点执行推理。StepAgent 接收用户消息、会话状态、
路由决策等上下文，调用 LLM 生成一个 :class:`StepAgentResult`，包含：

  - ``reply``: 对用户的回复文本
  - ``action``: 执行动作（ask_user / clarify / call_tool / query_knowledge /
    advance / handoff / reply）
  - ``tool_call``: 工具调用请求
  - ``knowledge_query``: 知识检索请求
  - ``next_step_id`` / ``is_step_completed``: 流程推进信息
  - ``slot_updates``: 槽位更新

StepAgent 支持多种运行模式：
  - 正常执行 (run)
  - 修复模式 (repair_context)，当 ReflectionAgent 判定需要重试时触发。
  - 工具续行 (tool_continuation)、知识续行 (knowledge_continuation) 等
    特殊场景通过不同的规则提示词注入实现。

与其他模块的关系
----------------
- 被主编排器调用，紧随 :mod:`app.core.router` 的决策之后执行。
- 产出结果传递给 :mod:`app.core.reflection_agent` 进行质量审查。
- 最终回复由 :mod:`app.core.response_generator` 生成。
- 依赖 :mod:`app.llm` 进行推理，使用 :mod:`app.core.context_projection`
  压缩上下文。
"""

from __future__ import annotations

from app import paths
from app.core.context_projection import (
    compact_awaiting_input,
    compact_conversation_context,
    compact_deferred_intents,
    compact_knowledge_context,
    compact_step_router_decision,
    compact_step_skill_context,
)
from app.db.models import ChatSession, ModelConfig, Skill, Tool
from app.llm import LLMClient, LLMError
from app.llm.stage_protocol import (
    STEP_AGENT_OUTPUT_SCHEMA,
    stage_payload,
    unified_system_prompt,
)
from app.observability.spans import llm_operation
from app.session.session_schema import RouterDecision, StepAgentResult


# StepAgent 主提示词文件路径
PROMPT_PATH = paths.resource_dir() / "app" / "llm" / "prompts" / "step_agent_prompt.md"
# 不同场景下注入的补充规则提示词文件路径映射
RULE_PATHS = {
    "repair": paths.resource_dir() / "app" / "llm" / "prompts" / "step_agent_repair_rules.md",
    "tool_continuation": paths.resource_dir()
    / "app"
    / "llm"
    / "prompts"
    / "step_agent_tool_continuation_rules.md",
    "knowledge": paths.resource_dir()
    / "app"
    / "llm"
    / "prompts"
    / "step_agent_knowledge_rules.md",
    "awaiting_input": paths.resource_dir()
    / "app"
    / "llm"
    / "prompts"
    / "step_agent_awaiting_input_rules.md",
    "general_skill": paths.resource_dir()
    / "app"
    / "llm"
    / "prompts"
    / "step_agent_general_skill_rules.md",
    "tools": paths.resource_dir() / "app" / "llm" / "prompts" / "step_agent_tool_rules.md",
}
# 内部调度器使用的槽位键集合，这些键不应对 LLM 可见
INTERNAL_SCHEDULER_SLOT_KEYS = {"_graph_pending_steps"}
# 通用技能工具的名称前缀，以此前缀开头的工具属于跨技能通用工具
GENERAL_SKILL_TOOL_PREFIX = "general_skill."


class StepAgent:
    """步骤执行智能体，在技能流程图的特定节点上进行推理决策。

    设计意图：在路由决策确定目标技能和步骤后，StepAgent 负责执行
    该步骤的核心推理。它会根据当前步骤的上下文（期望信息、允许的
    动作、可用工具等）动态组装提示词，调用 LLM 生成结构化结果。

    支持修复模式 (repair_context)：当 ReflectionAgent 判定上一步结果
    需要修正时，通过注入修复规则提示词来引导 LLM 重新生成更优结果。
    """

    def run(
        self,
        message: str,
        session: ChatSession,
        skill: Skill | None,
        tools: list[Tool],
        model_config: ModelConfig,
        router_decision: RouterDecision | None = None,
        repair_context: dict[str, object] | None = None,
        recent_messages: list[dict[str, str]] | None = None,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        current_knowledge: list[dict[str, object]] | None = None,
    ) -> StepAgentResult:
        """执行当前技能步骤的推理，生成步骤执行结果。

        组装包含活跃技能上下文、槽位、工具、知识检索、修复上下文等
        信息的 payload，调用 LLM 生成结构化结果。如果 LLM 未返回明确的
        action 字段，则根据结果中的其他字段（tool_call、knowledge_query
        等）自动推断。

        Args:
            message: 用户发送的原始消息文本。
            session: 当前会话状态对象。
            skill: 当前活跃技能对象，None 表示无技能上下文。
            tools: 所有已注册的工具列表，会根据步骤权限进行过滤。
            model_config: LLM 模型配置。
            router_decision: 上游 Router 的路由决策（可选）。
            repair_context: 修复上下文，用于 ReflectionAgent 触发的重试
                场景，包含修复原因等信息。为 None 表示正常执行模式。
            recent_messages: 最近的对话消息列表（可选，用于上下文参考）。
            memory_context: 长期记忆上下文条目列表（可选）。
            conversation_context: 压缩后的对话历史上下文（可选）。
            current_knowledge: 当前已检索到的知识结果列表（可选）。

        Returns:
            :class:`StepAgentResult` 对象，包含回复文本、执行动作、
            工具调用、流程推进等结果字段。

        Raises:
            LLMError: 当 LLM 调用失败或返回的 JSON 不符合 StepAgent
                输出 schema 时抛出。
        """
        # 压缩知识检索结果，减少 token 消耗
        compact_knowledge = compact_knowledge_context(current_knowledge)
        # 压缩修复上下文，移除冗余字段
        compact_repair = _compact_repair_context(repair_context)
        # 压缩活跃技能上下文，提取当前步骤和后续步骤的关键信息
        active_skill = (
            compact_step_skill_context(
                skill.content_json,
                session.active_step_id,
                skill_id=skill.skill_id,
                name=skill.name,
                description=skill.description,
            )
            if skill
            else None
        )
        # 根据当前步骤的权限配置过滤可用工具
        available_tools = _available_tools_for_step(
            active_skill,
            session.slots_json,
            tools,
        )
        # 提取延迟意图（来自待处理任务列表），供 StepAgent 参考
        deferred_intents = compact_deferred_intents(
            session.pending_tasks_json,
            selected_task_id=router_decision.selected_task_id if router_decision else None,
        )
        # 组装阶段数据，包含所有 StepAgent 推理所需的上下文
        stage_data = {
            "active_skill": active_skill,
            "retrieved_knowledge": compact_knowledge,
            "router_decision": compact_step_router_decision(
                router_decision.model_dump(mode="json") if router_decision else None
            ),
            "slots": _step_agent_slots(session.slots_json),
            "awaiting_input": compact_awaiting_input(session.awaiting_input_json),
            "deferred_intents": deferred_intents,
            "repair_context": compact_repair,
            "available_tools": available_tools,
        }
        # 构建最终 payload，包含动态组装的提示词指令
        payload = stage_payload(
            phase="Step Agent",
            user_message=message,
            conversation_context=compact_conversation_context(conversation_context),
            memory_context=None,
            instructions=_step_instructions(
                repair_context=compact_repair,
                retrieved_knowledge=compact_knowledge,
                awaiting_input=stage_data["awaiting_input"],
                available_tools=available_tools,
            ),
            stage_data=stage_data,
            output_contract=STEP_AGENT_OUTPUT_SCHEMA,
        )
        try:
            # 根据是否有修复上下文选择不同的可观测性 span 名称
            operation = "step_agent.repair" if repair_context else "step_agent.run"
            repair_reason = str((repair_context or {}).get("reason") or "") or None
            with llm_operation(operation, repair_reason=repair_reason):
                raw = LLMClient(model_config).generate_json(
                    unified_system_prompt(), payload
                )
            # 解析 LLM 返回的 JSON 为 StepAgentResult
            result = StepAgentResult.model_validate(raw)
            # 如果 LLM 未返回明确的 action，则根据其他字段自动推断
            if not result.action:
                result.action = _infer_action(result, router_decision)
            return result
        except Exception as exc:
            # LLM 调用本身的错误直接向上传播
            if isinstance(exc, LLMError):
                raise
            # schema 解析失败包装为 LLMError
            raise LLMError(f"Step agent returned invalid JSON schema: {exc}") from exc


def _step_agent_slots(slots: dict[str, object] | None) -> dict[str, object]:
    """过滤掉内部调度器槽位，只返回对 LLM 可见的业务槽位。

    Args:
        slots: 原始槽位字典，可能包含 ``_graph_pending_steps`` 等内部键。

    Returns:
        仅包含业务槽位的字典。
    """
    if not isinstance(slots, dict):
        return {}
    return {
        key: value
        for key, value in slots.items()
        if str(key) not in INTERNAL_SCHEDULER_SLOT_KEYS
    }


def _compact_repair_context(
    repair_context: dict[str, object] | None,
) -> dict[str, object] | None:
    """压缩修复上下文，根据修复原因移除冗余字段。

    对于 ``knowledge_continuation``（知识续行）场景，知识结果已通过
    ``retrieved_knowledge`` 字段传入，因此需要从修复上下文中移除
    重复的 ``knowledge_results``，替换为指向信息。

    Args:
        repair_context: 原始修复上下文字典，可能为 None。

    Returns:
        压缩后的修复上下文字典，输入为 None 时返回 None。
    """
    if not isinstance(repair_context, dict):
        return None
    projected = dict(repair_context)
    # 知识续行场景：知识结果已在 retrieved_knowledge 中提供，移除冗余副本
    if projected.get("reason") == "knowledge_continuation":
        projected.pop("knowledge_results", None)
        projected["knowledge_results_available_in"] = "retrieved_knowledge"
    return projected


def _step_instructions(
    *,
    repair_context: dict[str, object] | None,
    retrieved_knowledge: list[dict[str, object]],
    awaiting_input: dict[str, object] | None,
    available_tools: list[dict[str, object]],
) -> str:
    """动态组装 StepAgent 的提示词指令。

    以主提示词为基础，根据当前运行场景的条件，按需拼接不同的
    补充规则提示词（修复规则、工具续行规则、知识规则、等待输入规则、
    工具规则、通用技能规则等）。

    Args:
        repair_context: 修复上下文，决定是否注入修复/续行规则。
        retrieved_knowledge: 已检索的知识结果，决定是否注入知识规则。
        awaiting_input: 等待输入状态，决定是否注入等待输入规则。
        available_tools: 可用工具列表，决定是否注入工具规则。

    Returns:
        拼接后的完整提示词指令字符串。
    """
    # 主提示词始终作为第一段
    sections = [PROMPT_PATH.read_text(encoding="utf-8").strip()]
    repair_reason = str((repair_context or {}).get("reason") or "")
    # 修复模式（非工具/知识续行场景）注入通用修复规则
    if repair_context and repair_reason not in {"tool_continuation", "knowledge_continuation"}:
        sections.append(RULE_PATHS["repair"].read_text(encoding="utf-8").strip())
    # 工具续行场景注入专用规则
    if repair_reason == "tool_continuation":
        sections.append(
            RULE_PATHS["tool_continuation"].read_text(encoding="utf-8").strip()
        )
    # 有知识结果或处于知识续行场景时注入知识处理规则
    if retrieved_knowledge or repair_reason == "knowledge_continuation":
        sections.append(RULE_PATHS["knowledge"].read_text(encoding="utf-8").strip())
    # 存在等待输入状态时注入相关规则
    if awaiting_input:
        sections.append(RULE_PATHS["awaiting_input"].read_text(encoding="utf-8").strip())
    # 有可用工具时注入工具调用规则
    if available_tools:
        sections.append(RULE_PATHS["tools"].read_text(encoding="utf-8").strip())
    # 可用工具中包含通用技能工具时注入通用技能规则
    if any(
        str(tool.get("name") or "").startswith(GENERAL_SKILL_TOOL_PREFIX)
        for tool in available_tools
    ):
        sections.append(RULE_PATHS["general_skill"].read_text(encoding="utf-8").strip())
    return "\n\n".join(section for section in sections if section)


def _available_tools_for_step(
    active_skill: dict[str, object] | None,
    slots: dict[str, object] | None,
    tools: list[Tool],
) -> list[dict[str, object]]:
    """根据当前技能步骤的权限配置，过滤出该步骤可使用的工具列表。

    过滤逻辑基于步骤的 ``allowed_actions`` 字段：
      - ``call_tool`` 表示允许调用任何工具。
      - ``call_tool:<name>`` 表示仅允许调用指定名称的工具。
      - 通用技能工具（以 ``general_skill.`` 为前缀）始终允许。

    此外，如果当前步骤仍有未填的期望信息槽位，则仅考虑当前步骤
    的允许动作；否则同时考虑后续步骤的允许动作（扩大可用范围）。

    Args:
        active_skill: 压缩后的活跃技能上下文，包含 current_step 和 next_steps。
        slots: 当前会话的槽位字典。
        tools: 所有已注册的工具对象列表。

    Returns:
        过滤后的工具 payload 列表，每项包含 name、description、input_schema。
    """
    if not isinstance(active_skill, dict):
        return []
    current_step = active_skill.get("current_step")
    if not isinstance(current_step, dict):
        return []
    # 提取当前步骤期望收集的用户信息字段列表
    current_expected = [
        str(field)
        for field in current_step.get("expected_user_info") or []
        if str(field).strip()
    ]
    slot_values = slots if isinstance(slots, dict) else {}
    # 如果当前步骤仍有缺失的期望信息，则仅考虑当前步骤的工具权限
    # 否则同时纳入后续步骤的权限（允许提前调用下一步骤的工具）
    if any(not _slot_has_value(slot_values, field) for field in current_expected):
        candidate_steps = [current_step]
    else:
        candidate_steps = [
            current_step,
            *[
                step
                for step in active_skill.get("next_steps") or []
                if isinstance(step, dict)
            ],
        ]
    # 汇总所有候选步骤允许的动作集合
    actions = {
        str(action).strip()
        for step in candidate_steps
        for action in step.get("allowed_actions") or []
        if str(action).strip()
    }
    # 从 "call_tool:<name>" 格式中提取显式允许的工具名称
    explicit_names = {
        action.split(":", 1)[1]
        for action in actions
        if action.startswith("call_tool:") and ":" in action
    }
    # "call_tool" 表示允许调用任意工具
    allow_any = "call_tool" in actions
    projected: list[dict[str, object]] = []
    for tool in tools:
        # 跳过未启用的工具
        if not getattr(tool, "enabled", False):
            continue
        name = str(getattr(tool, "name", "") or "").strip()
        if not name:
            continue
        # 非通用技能工具需要检查是否在显式允许列表或 allow_any 中
        if not name.startswith(GENERAL_SKILL_TOOL_PREFIX) and (
            not allow_any and name not in explicit_names
        ):
            continue
        projected.append(
            {
                "name": name,
                "description": str(getattr(tool, "description", "") or "").strip(),
                "input_schema": getattr(tool, "input_schema", None) or {},
            }
        )
    return projected


def _slot_has_value(slots: dict[str, object], field: str) -> bool:
    """检查指定槽位是否已填充有效值。

    有效值定义为非 None、非空字符串、非空列表、非空字典。

    Args:
        slots: 槽位字典。
        field: 待检查的槽位键名。

    Returns:
        槽位是否有有效值。
    """
    value = slots.get(field)
    return value is not None and value != "" and value != [] and value != {}


def _infer_action(
    result: StepAgentResult,
    router_decision: RouterDecision | None,
) -> str:
    """当 LLM 未返回明确的 action 字段时，根据结果内容推断执行动作。

    推断优先级（从高到低）：
      1. ``call_tool`` —— 结果包含工具调用请求
      2. ``query_knowledge`` —— 结果包含知识检索请求
      3. ``handoff`` —— 结果包含转人工请求
      4. ``advance`` —— 结果包含步骤推进（next_step_id 或 is_step_completed）
      5. ``clarify`` / ``ask_user`` —— 结果包含回复文本，根据路由决策区分
      6. ``reply`` —— 默认兜底

    Args:
        result: StepAgent 的执行结果（action 字段为空）。
        router_decision: 上游路由决策，用于区分 clarify 和 ask_user。

    Returns:
        推断出的 action 字符串。
    """
    if result.tool_call:
        return "call_tool"
    if result.knowledge_query:
        return "query_knowledge"
    if result.handoff:
        return "handoff"
    if result.next_step_id or result.is_step_completed:
        return "advance"
    if result.reply:
        # 路由决策为 clarify 时推断为 clarify，否则为 ask_user
        return "clarify" if router_decision and router_decision.decision == "clarify" else "ask_user"
    return "reply"
