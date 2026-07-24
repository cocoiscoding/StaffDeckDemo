"""反思审查智能体模块（ReflectionAgent）。

本模块是多智能体对话流水线的质量保障层。在 StepAgent 产出步骤结果后，
ReflectionAgent 对结果进行审查，判断是否需要修复或重试。

审查策略：
  - **轻量预判**：对于不涉及外部操作（工具调用、知识检索、转人工等）
    的简单回复，直接跳过 LLM 审查以节省开销。
  - **深度审查**：对于涉及外部操作的结果，调用 LLM 进行质量评估，
    判断是否需要重试、是否应切换目标技能/步骤/工具等。

ReflectionAgent 的核心输出是 :class:`ReflectionDecision`，包含：
  - ``action``: 决策动作（``pass`` 表示通过，``retry`` 表示需要重试）。
  - ``needs_retry``: 是否需要重试。
  - ``reason``: 决策原因。
  - ``target_skill_id`` / ``target_step_id`` / ``target_tool_name``:
    修复时建议调整的目标。

与其他模块的关系
----------------
- 紧随 :mod:`app.core.step_agent` 之后执行。
- 如果判定需要重试，会以 ``repair_context`` 回调 StepAgent。
- 依赖 :mod:`app.llm` 进行推理，使用 :mod:`app.core.context_projection`
  压缩上下文。
"""

from __future__ import annotations

from pydantic import BaseModel

from app import paths
from app.core.context_projection import (
    compact_conversation_context,
    compact_current_step,
    compact_router_decision,
    compact_step_result,
)
from app.db.models import ChatSession, ModelConfig, Skill, Tool
from app.llm import LLMClient, LLMError
from app.llm.stage_protocol import (
    REFLECTION_OUTPUT_SCHEMA,
    stage_payload,
    unified_system_prompt,
)
from app.observability.spans import llm_operation
from app.session.session_schema import RouterDecision, StepAgentResult
from app.tools.tool_schema import ToolResult


# Reflection 提示词文件路径，LLM 据此理解如何审查步骤结果
PROMPT_PATH = paths.resource_dir() / "app" / "llm" / "prompts" / "reflection_prompt.md"


class ReflectionDecision(BaseModel):
    """反思审查决策模型，描述对步骤结果的审查结论。

    Attributes:
        action: 决策动作，``pass``（默认）表示审查通过，``retry`` 表示
            需要修复重试。
        needs_retry: 是否需要重试 StepAgent。
        reason: 决策原因说明（供日志和修复上下文使用）。
        target_skill_id: 修复时建议切换的目标技能 ID。
        target_step_id: 修复时建议切换的目标步骤 ID。
        target_tool_name: 修复时建议使用的目标工具名称。
    """

    action: str = "pass"
    needs_retry: bool = False
    reason: str | None = None
    target_skill_id: str | None = None
    target_step_id: str | None = None
    target_tool_name: str | None = None


class ReflectionAgent:
    """反思审查智能体，评估 StepAgent 结果的质量并决定是否重试。

    设计意图：作为流水线的质量门控，在步骤执行后自动识别潜在问题
    （如工具调用失败、知识不足、流程异常等），必要时触发修复流程。
    通过轻量预判减少不必要的 LLM 调用，优化性能。
    """

    def review(
        self,
        message: str,
        session: ChatSession,
        active_skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        available_skills: list[Skill],
        available_tools: list[Tool],
        model_config: ModelConfig,
        conversation_context: dict[str, object] | None = None,
        memory_context: list[dict[str, object]] | None = None,
    ) -> ReflectionDecision:
        """审查步骤执行结果的质量，决定是否需要修复重试。

        首先进行轻量预判（:func:`action_needs_reflection`），对于不涉及
        外部操作的简单结果直接返回 ``pass`` 决策，跳过 LLM 调用。
        对于需要审查的结果，组装包含当前步骤、槽位、路由决策、步骤结果、
        工具结果等上下文的 payload，调用 LLM 进行深度评估。

        Args:
            message: 用户发送的原始消息文本。
            session: 当前会话状态对象。
            active_skill: 当前活跃技能对象（可能为 None）。
            router_decision: 上游 Router 的路由决策。
            step_result: StepAgent 的步骤执行结果。
            tool_result: 工具调用结果（如果有），None 表示未调用工具。
            available_skills: 可用技能列表（供修复时参考切换目标）。
            available_tools: 可用工具列表（供修复时参考切换目标）。
            model_config: LLM 模型配置。
            conversation_context: 压缩后的对话历史上下文（可选）。
            memory_context: 长期记忆上下文条目列表（可选）。

        Returns:
            :class:`ReflectionDecision` 对象，描述审查结论和修复建议。

        Raises:
            LLMError: 当 LLM 调用失败或返回的 JSON 不符合 Reflection
                输出 schema 时抛出。
        """
        # 轻量预判：不需要审查的操作直接返回 pass 决策
        if not action_needs_reflection(router_decision, step_result, tool_result):
            return ReflectionDecision()

        # 组装审查所需的阶段数据
        stage_data = {
            "current_step": compact_current_step(
                active_skill.content_json if active_skill else None,
                session.active_step_id,
            ),
            "rules": {
                "response_rules": active_skill.content_json.get("response_rules", [])
                if active_skill
                else [],
            },
            "slots": session.slots_json or {},
            "router_decision": compact_router_decision(
                router_decision.model_dump(mode="json")
            ),
            "step_result": compact_step_result(step_result.model_dump(mode="json")),
            "tool_result": tool_result.model_dump() if tool_result else None,
        }
        payload = stage_payload(
            phase="Reflection",
            user_message=message,
            conversation_context=compact_conversation_context(conversation_context),
            memory_context=memory_context,
            instructions=PROMPT_PATH.read_text(encoding="utf-8"),
            stage_data=stage_data,
            output_contract=REFLECTION_OUTPUT_SCHEMA,
        )
        try:
            # 在可观测性 span 中记录本次审查 LLM 调用
            with llm_operation("reflection.review"):
                raw = LLMClient(model_config).generate_json(
                    unified_system_prompt(), payload
                )
            # 解析 LLM 返回的 JSON 为 ReflectionDecision
            return ReflectionDecision.model_validate(raw)
        except Exception as exc:
            # LLM 调用本身的错误直接向上传播
            if isinstance(exc, LLMError):
                raise
            # schema 解析失败包装为 LLMError
            raise LLMError(f"Reflection agent returned invalid JSON schema: {exc}") from exc

def action_needs_reflection(
    router_decision: RouterDecision,
    step_result: StepAgentResult,
    tool_result: ToolResult | None,
) -> bool:
    """判断当前步骤结果是否需要经过 ReflectionAgent 审查。

    审查策略（返回 True 表示需要 LLM 深度审查）：
      - 路由决策为 clarify/answer_only 时：仅当存在工具调用或知识检索
        时才需要审查（这些是外部操作，可能有质量问题）。
      - 其他决策时：任何外部操作（工具调用、知识检索/结果、转人工）
        都需要审查。
      - 步骤推进 (next_step_id) 属于正常的技能流程进度，不需要审查。
      - 步骤完成 (is_step_completed) 需要审查，确认完成状态正确。

    Args:
        router_decision: 上游路由决策。
        step_result: 步骤执行结果。
        tool_result: 工具调用结果（可能为 None）。

    Returns:
        True 表示需要审查，False 表示可跳过。
    """
    # clarify/answer_only 场景：仅在有外部操作时才审查
    if router_decision.decision in {"clarify", "answer_only"}:
        return bool(tool_result or step_result.tool_call or step_result.knowledge_query)
    # 任何外部操作都需要审查
    if (
        tool_result
        or step_result.tool_call
        or step_result.knowledge_query
        or step_result.knowledge_results
        or step_result.handoff
    ):
        return True
    # 推进到已决定的下一节点是正常的技能图进度。
    # 反思仅保留给外部操作或整体技能完成。
    if step_result.next_step_id:
        return False
    # 步骤完成需要审查
    return bool(step_result.is_step_completed)


def tool_result_needs_reflection(tool_result: ToolResult | None) -> bool:
    """判断工具调用结果是否失败、需要触发反思审查。

    Args:
        tool_result: 工具调用结果对象，None 表示未调用工具。

    Returns:
        True 表示工具调用失败、需要审查，False 表示无需审查。
    """
    if tool_result is None:
        return False
    return not tool_result.success


def _data_indicates_unexpected_result(value: object) -> bool:
    """启发式判断数据是否暗示了意外/异常结果。

    检查常见的数据异常信号，包括：
      - None 或空列表
      - 字典中 found=False 或 success=False
      - 包含 miss_reason / not_found / empty / error / error_code 等错误键
      - 嵌套的 results / items / data 列表为空

    Args:
        value: 待检查的数据值。

    Returns:
        True 表示数据暗示了意外结果，False 表示数据正常。
    """
    if value is None:
        return True
    if isinstance(value, list):
        return len(value) == 0
    if not isinstance(value, dict):
        return False

    # 检查显式的失败标志
    if value.get("found") is False or value.get("success") is False:
        return True
    # 检查错误相关字段是否存在且非空
    for key in ("miss_reason", "not_found", "empty", "error", "error_code"):
        if value.get(key):
            return True
    # 检查嵌套数据列表是否为空
    for key in ("results", "items", "data"):
        nested = value.get(key)
        if isinstance(nested, list) and len(nested) == 0:
            return True
    return False
