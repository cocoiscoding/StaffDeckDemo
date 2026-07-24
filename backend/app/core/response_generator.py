"""回复生成模块（ResponseGenerator）。

本模块是多智能体对话流水线的最终输出层，负责将上游各阶段（Router、
StepAgent、Reflection、工具执行等）的结果转化为面向用户的自然语言回复。

核心功能
--------
- **智能回复路由**：对于简单的 ask_user / clarify 场景，直接使用
  StepAgent 的回复，无需额外 LLM 调用；对于复杂场景，调用 LLM
  重新生成更自然的回复。
- **流式输出**：支持流式生成回复（``generate_stream``），逐块返回文本。
- **错误兜底**：当工具调用失败或 LLM 调用异常时，生成友好的错误提示。
- **任务结果汇总**：支持多任务场景下汇总各任务结果生成综合回复。
- **引用提示**：集成知识检索的引用提示信息，增强回复可信度。

与其他模块的关系
----------------
- 在流水线末端执行，接收 Router、StepAgent、工具执行等阶段的产出。
- 依赖 :mod:`app.llm` 进行回复文本生成。
- 使用 :mod:`app.core.context_projection` 压缩上下文。
- 集成 :mod:`app.knowledge.citations` 处理知识引用。
"""

from __future__ import annotations

import re
from collections.abc import Iterator

from app import paths
from app.core.context_projection import (
    compact_citation_hints,
    compact_current_step,
    compact_knowledge_context,
    compact_response_step_result,
)
from app.db.models import ChatSession, ModelConfig, Skill
from app.knowledge.citations import knowledge_citations_from_results
from app.llm import LLMClient
from app.llm.stage_protocol import stage_payload, unified_system_prompt
from app.observability.spans import llm_operation
from app.session.session_schema import RouterDecision, StepAgentResult
from app.tools.tool_schema import ToolResult


# 回复生成提示词文件路径
PROMPT_PATH = paths.resource_dir() / "app" / "llm" / "prompts" / "response_generator_prompt.md"
# 兜底回复文本，当所有候选回复均为空时使用
FALLBACK_REPLY = "抱歉，我暂时无法处理这个问题。您可以换个说法，或者我可以帮您转人工。"
# 模型调用失败时的用户提示建议
MODEL_FAILURE_SUGGESTION = "请检查模型配置、API Key、网络或模型服务状态后重试。"
# 工具调用失败时的用户提示建议
TOOL_FAILURE_SUGGESTION = "请检查工具配置、调用参数或外部服务状态后重试。"


def public_error_detail(value: object, fallback: str = "未知原因") -> str:
    """将错误信息转换为面向用户的安全文本。

    执行以下处理：
      - 合并多余空白字符。
      - 脱敏 API Key（``sk-xxx`` / ``pt-xxx`` 替换为 ``sk-***`` / ``pt-***``）。
      - 空值使用 fallback 文本。
      - 截断超长错误信息（最长 500 字符）。

    Args:
        value: 原始错误值（任意类型，会转为字符串）。
        fallback: 当错误信息为空时使用的默认文本。

    Returns:
        脱敏、截断后的安全错误文本。
    """
    detail = re.sub(r"\s+", " ", str(value or "")).strip()
    # 脱敏 OpenAI / 其他 API Key
    detail = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-***", detail)
    detail = re.sub(r"\bpt-[A-Za-z0-9_-]{8,}\b", "pt-***", detail)
    if not detail:
        detail = fallback
    return detail[:500]


def format_runtime_failure_reply(
    title: str,
    detail: object,
    code: str | None = None,
    suggestion: str | None = None,
) -> str:
    """格式化运行时失败的面向用户回复文本。

    将标题、错误代码、错误详情、修复建议组装为统一的错误回复格式：
    ``{title}（{code}）：{detail}。{suggestion}``

    Args:
        title: 错误标题（如"工具调用失败"）。
        detail: 原始错误详情（会经过脱敏处理）。
        code: 错误代码（可选，会经过脱敏处理）。
        suggestion: 修复建议文本（可选，为空时使用默认建议）。

    Returns:
        格式化后的错误回复字符串。
    """
    normalized_detail = public_error_detail(detail)
    normalized_code = public_error_detail(code, "").strip()
    code_part = f"（{normalized_code}）" if normalized_code else ""
    # 移除详情末尾的句号等标点，避免与格式化模板中的句号重复
    normalized_detail = normalized_detail.rstrip("。.!！")
    suffix = (suggestion or "请稍后重试，或联系管理员查看执行记录。").strip()
    return f"{title}{code_part}：{normalized_detail}。{suffix}"


def model_failure_suggestion(detail: object) -> str:
    """返回模型调用失败时的统一修复建议。

    Args:
        detail: 模型错误详情（当前实现未使用，保留参数以备扩展）。

    Returns:
        模型故障修复建议文本。
    """
    return MODEL_FAILURE_SUGGESTION


def tool_failure_reply(tool_result: ToolResult) -> str:
    """根据工具调用结果生成失败回复文本。

    Args:
        tool_result: 失败的工具调用结果对象。

    Returns:
        格式化的工具调用失败回复字符串。
    """
    error = tool_result.error
    code = error.code if error else None
    detail = error.message if error else "工具未返回可用结果"
    return format_runtime_failure_reply(
        f"工具调用失败：{tool_result.tool_name}",
        detail,
        code,
        TOOL_FAILURE_SUGGESTION,
    )


class ResponseGenerator:
    """回复生成器，将流水线各阶段结果转化为面向用户的自然语言回复。

    设计意图：作为流水线的最终输出层，综合路由决策、步骤结果、工具结果、
    知识检索等信息，调用 LLM 生成连贯、准确且符合人设的回复。内置
    多层兜底机制，确保在任何异常情况下都能返回有意义的用户提示。
    """

    def generate(
        self,
        message: str,
        session: ChatSession,
        skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        model_config: ModelConfig,
        persona_prompt: str | None = None,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        task_results: list[dict[str, object]] | None = None,
    ) -> str:
        """生成完整的面向用户回复文本。

        处理流程：
          1. 如果是简单的 ask_user / clarify 场景，直接使用 StepAgent 回复。
          2. 如果工具调用失败且无任务结果，返回工具失败提示。
          3. 否则调用 LLM 生成回复，并经过多层兜底校验确保有可见文本。

        Args:
            message: 用户发送的原始消息文本。
            session: 当前会话状态对象。
            skill: 当前活跃技能对象（可能为 None）。
            router_decision: 上游路由决策。
            step_result: StepAgent 的步骤执行结果。
            tool_result: 工具调用结果（如果有）。
            model_config: LLM 模型配置。
            persona_prompt: 坐席人设提示词（可选），注入到 payload 中
                控制回复的语气和身份。
            memory_context: 长期记忆上下文条目列表（可选）。
            conversation_context: 压缩后的对话历史上下文（可选）。
            task_results: 多任务场景下的各任务结果列表（可选）。

        Returns:
            面向用户的回复文本字符串。
        """
        # 简单回复场景：直接使用 StepAgent 的回复，跳过 LLM 调用
        if self._can_use_step_reply_directly(step_result, tool_result, task_results):
            return step_result.reply.strip()
        # 组装 payload 并构建阶段数据
        raw_payload = self._payload(
            message,
            session,
            skill,
            router_decision,
            step_result,
            tool_result,
            memory_context,
            conversation_context,
            task_results,
        )
        payload = self._stage_payload(raw_payload, persona_prompt)
        try:
            # 工具调用失败且无任务结果时，直接返回工具失败提示
            if tool_result and not tool_result.success and not task_results:
                return tool_failure_reply(tool_result)
            # 调用 LLM 生成回复文本
            with llm_operation("response.generate"):
                text = LLMClient(model_config).generate_text(
                    unified_system_prompt(), payload
                )
            # 从 LLM 输出、StepAgent 回复、最小兜底中选择首个非空回复
            reply = text.strip() or step_result.reply or self._minimal_fallback(router_decision)
            # 多层兜底校验，确保最终返回有可见文本
            return self._visible_reply_or_fallback(
                reply, session, router_decision, step_result, tool_result, skill
            )
        except Exception as exc:
            # LLM 调用异常时返回格式化的错误提示
            return format_runtime_failure_reply("模型调用失败", exc, "LLM_ERROR", model_failure_suggestion(exc))

    def generate_stream(
        self,
        message: str,
        session: ChatSession,
        skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        model_config: ModelConfig,
        persona_prompt: str | None = None,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        task_results: list[dict[str, object]] | None = None,
    ) -> Iterator[str]:
        """流式生成面向用户的回复文本，逐块 yield 输出。

        与 :meth:`generate` 功能一致，但以流式方式返回文本块，适用于
        需要实时展示打字效果的场景。

        处理流程：
          1. 简单场景直接分块输出 StepAgent 回复。
          2. 工具失败时分块输出失败提示。
          3. clarify 场景且有 StepAgent 回复时直接分块输出。
          4. 调用 LLM 流式接口逐块输出，首个非空块之前不 yield（避免
            输出空白内容）。流式完成后若有内容则直接返回，否则回退
            到非流式生成。

        Args:
            message: 用户发送的原始消息文本。
            session: 当前会话状态对象。
            skill: 当前活跃技能对象（可能为 None）。
            router_decision: 上游路由决策。
            step_result: StepAgent 的步骤执行结果。
            tool_result: 工具调用结果（如果有）。
            model_config: LLM 模型配置。
            persona_prompt: 坐席人设提示词（可选）。
            memory_context: 长期记忆上下文条目列表（可选）。
            conversation_context: 压缩后的对话历史上下文（可选）。
            task_results: 多任务场景下的各任务结果列表（可选）。

        Yields:
            回复文本的逐个分块字符串。
        """
        # 简单回复场景：直接分块输出 StepAgent 的回复
        if self._can_use_step_reply_directly(step_result, tool_result, task_results):
            yield from self.chunk_text(step_result.reply or "")
            return
        raw_payload = self._payload(
            message,
            session,
            skill,
            router_decision,
            step_result,
            tool_result,
            memory_context,
            conversation_context,
            task_results,
        )
        payload = self._stage_payload(raw_payload, persona_prompt)
        try:
            # 工具调用失败且无任务结果时，分块输出失败提示
            if tool_result and not tool_result.success and not task_results:
                yield from self.chunk_text(tool_failure_reply(tool_result))
                return
            # clarify 场景且有 StepAgent 回复时直接分块输出（跳过 LLM）
            if router_decision.decision == "clarify" and step_result.reply:
                yield from self.chunk_text(step_result.reply)
                return
            # 调用 LLM 流式接口逐块输出
            with llm_operation("response.generate_stream"):
                stream = LLMClient(model_config).generate_text_stream(
                    unified_system_prompt(), payload
                )
                reply_parts: list[str] = []
                has_streamed = False  # 标记是否已向用户输出过内容
                for chunk in stream:
                    if not chunk:
                        continue
                    reply_parts.append(chunk)
                    # 首次输出前检查拼接内容是否为非空（跳过前导空白）
                    if not has_streamed:
                        preview = "".join(reply_parts).strip()
                        if not preview:
                            continue
                        has_streamed = True
                    yield chunk
            # 如果流式已输出内容，则直接结束
            if has_streamed:
                return
            # 流式未输出有效内容时，回退到非流式兜底生成
            reply = self._visible_reply_or_fallback(
                "".join(reply_parts).strip() or step_result.reply or self._minimal_fallback(router_decision),
                session,
                router_decision,
                step_result,
                tool_result,
                skill,
            )
            yield from self.chunk_text(reply)
            return
        except Exception as exc:
            # LLM 调用异常时分块输出格式化的错误提示
            yield from self.chunk_text(
                format_runtime_failure_reply("模型调用失败", exc, "LLM_ERROR", model_failure_suggestion(exc))
            )

    def chunk_text(self, text: str, chunk_size: int = 8) -> Iterator[str]:
        """将文本按固定大小切分为块，用于模拟流式输出效果。

        Args:
            text: 待切分的文本。
            chunk_size: 每块的字符数，默认为 8。

        Yields:
            文本的逐个分块字符串。
        """
        stripped = text.strip()
        if not stripped:
            return
        for index in range(0, len(stripped), chunk_size):
            yield stripped[index : index + chunk_size]

    def _can_use_step_reply_directly(
        self,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        task_results: list[dict[str, object]] | None = None,
    ) -> bool:
        """判断是否可以直接使用 StepAgent 的回复，跳过 LLM 调用。

        当满足以下全部条件时返回 True：
          - 无多任务结果
          - StepAgent 有非空回复
          - 动作为 ask_user 或 clarify
          - 无工具调用和工具结果
          - 无知识查询和知识结果

        Args:
            step_result: StepAgent 的步骤执行结果。
            tool_result: 工具调用结果（可能为 None）。
            task_results: 多任务结果列表（可能为 None）。

        Returns:
            True 表示可直接使用 StepAgent 回复，False 表示需要 LLM 重新生成。
        """
        return bool(
            not task_results
            and str(step_result.reply or "").strip()
            and step_result.action in {"ask_user", "clarify"}
            and tool_result is None
            and step_result.tool_call is None
            and step_result.knowledge_query is None
            and not step_result.knowledge_results
        )

    def _payload(
        self,
        message: str,
        session: ChatSession,
        skill: Skill | None,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        memory_context: list[dict[str, object]] | None = None,
        conversation_context: dict[str, object] | None = None,
        task_results: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        """组装回复生成所需的 payload 字典。

        根据是否有任务结果，走两条不同的组装路径：
          - **多任务路径**：仅包含用户消息、对话上下文和投影后的任务结果。
          - **单任务路径**：包含完整的上下文信息（当前步骤、进度、槽位、
            步骤摘要、工具结果、知识检索、引用提示、响应规则等）。

        Args:
            message: 用户发送的原始消息文本。
            session: 当前会话状态对象。
            skill: 当前活跃技能对象（可能为 None）。
            router_decision: 上游路由决策。
            step_result: StepAgent 的步骤执行结果。
            tool_result: 工具调用结果（可能为 None）。
            memory_context: 长期记忆上下文条目列表（可选）。
            conversation_context: 压缩后的对话历史上下文（可选）。
            task_results: 多任务场景下的各任务结果列表（可选）。

        Returns:
            组装好的 payload 字典。
        """
        # 投影任务结果，判断是否走多任务路径
        projected_task_results = self._project_task_results(task_results)
        if projected_task_results:
            # 多任务路径：精简 payload，仅包含任务汇总信息
            return {
                "user_message": message,
                "conversation_context": (
                    conversation_context if isinstance(conversation_context, dict) else {}
                ),
                "task_results": projected_task_results,
            }
        # 单任务路径：构建完整的上下文 payload
        knowledge_context = self._current_knowledge_context(message, session, step_result)
        compact_knowledge = compact_knowledge_context(knowledge_context)
        return {
            "user_message": message,
            "conversation_context": (
                conversation_context if isinstance(conversation_context, dict) else {}
            ),
            "current_step": compact_current_step(
                skill.content_json if skill else None, session.active_step_id
            ),
            "progress": self._progress_payload(session, skill, step_result, tool_result),
            "slots": session.slots_json or {},
            "step_summary": compact_response_step_result(
                step_result.model_dump(mode="json")
            ),
            "tool_result": tool_result.model_dump() if tool_result else None,
            "retrieved_knowledge": compact_knowledge,
            "knowledge_citation_hints": compact_citation_hints(
                knowledge_citations_from_results(knowledge_context)
            ),
            "response_rules": skill.content_json.get("response_rules", []) if skill else [],
        }

    def _project_task_results(
        self, task_results: list[dict[str, object]] | None
    ) -> list[dict[str, object]]:
        """将原始多任务结果投影为 LLM 可理解的结构化列表。

        对每个任务项提取并压缩以下信息：任务名称、当前步骤、槽位、
        步骤摘要、工具结果、知识检索、引用提示、响应规则等。

        Args:
            task_results: 原始的多任务结果列表（每项为字典）。

        Returns:
            投影后的任务结果 payload 列表，输入无效时返回空列表。
        """
        if not isinstance(task_results, list):
            return []
        projected: list[dict[str, object]] = []
        for item in task_results:
            if not isinstance(item, dict):
                continue
            # 提取技能内容，用于获取响应规则和步骤定义
            skill_content = item.get("skill_content")
            content = skill_content if isinstance(skill_content, dict) else {}
            # 提取步骤结果，用于获取知识和工具信息
            raw_step_result = item.get("step_result")
            step_result = raw_step_result if isinstance(raw_step_result, dict) else {}
            # 提取知识检索结果
            knowledge_context = (
                step_result.get("knowledge_results")
                if isinstance(step_result.get("knowledge_results"), list)
                else []
            )
            compact_knowledge = compact_knowledge_context(knowledge_context)
            projected.append(
                {
                    "task": item.get("task") or "当前任务",
                    "current_step": compact_current_step(
                        content, str(item.get("current_step_id") or "") or None
                    ),
                    "slots": item.get("slots") if isinstance(item.get("slots"), dict) else {},
                    "step_summary": compact_response_step_result(step_result),
                    "tool_result": item.get("tool_result"),
                    "retrieved_knowledge": compact_knowledge,
                    "knowledge_citation_hints": compact_citation_hints(
                        knowledge_citations_from_results(knowledge_context)
                    ),
                    "response_rules": content.get("response_rules", []),
                }
            )
        return projected

    def _current_knowledge_context(
        self,
        message: str,
        session: ChatSession,
        step_result: StepAgentResult,
    ) -> list[dict[str, object]]:
        """获取当前步骤的知识检索结果列表。

        Args:
            message: 用户消息文本（当前实现未直接使用）。
            session: 当前会话状态（当前实现未直接使用）。
            step_result: 步骤执行结果，从中提取 knowledge_results。

        Returns:
            知识检索结果列表，无结果时返回空列表。
        """
        if step_result.knowledge_results:
            return list(step_result.knowledge_results)
        return []

    def _visible_reply_or_fallback(
        self,
        reply: str,
        session: ChatSession,
        router_decision: RouterDecision,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        skill: Skill | None = None,
    ) -> str:
        """从多个候选回复中选择首个有可见内容的回复，确保不返回空文本。

        根据当前上下文状态构建候选回复优先级列表（通过
        :meth:`_reply_candidates`），依次检查每个候选是否有非空内容，
        返回首个有效的候选。如果所有候选均为空，返回最终兜底回复。

        Args:
            reply: LLM 生成的回复文本。
            session: 当前会话状态对象。
            router_decision: 上游路由决策。
            step_result: StepAgent 的步骤执行结果。
            tool_result: 工具调用结果（可能为 None）。
            skill: 当前活跃技能对象（可选）。

        Returns:
            首个有可见内容的回复字符串，或最终兜底回复。
        """
        # 检查技能是否已收集完整信息、可以完成
        completion_ready = self._skill_completion_ready(session, skill, step_result, tool_result)
        completion_fallback = self._completion_fallback() if completion_ready else ""
        # clarify 场景优先使用 StepAgent 回复
        prefer_step_reply = bool(step_result.reply and router_decision.decision == "clarify")
        # 构建候选回复优先级列表
        candidates = self._reply_candidates(
            reply,
            step_result.reply or "",
            completion_fallback,
            self._minimal_fallback_for_session(session),
            tool_result,
            completion_ready,
            prefer_step_reply,
        )
        # 遍历候选，返回首个有可见内容的回复
        for candidate in candidates:
            stripped = candidate.strip()
            if not stripped:
                continue
            return stripped
        # 所有候选均为空时返回最终兜底
        return FALLBACK_REPLY

    def _reply_candidates(
        self,
        model_reply: str,
        step_reply: str,
        completion_fallback: str,
        session_fallback: str,
        tool_result: ToolResult | None,
        completion_ready: bool,
        prefer_step_reply: bool,
    ) -> tuple[str, ...]:
        """根据上下文状态构建候选回复的优先级元组。

        不同场景下的候选优先级不同：
          - **优先 StepAgent 回复**（prefer_step_reply）：clarify 场景，
            step_reply 优先于 model_reply。
          - **技能完成**（completion_ready）：completion_fallback 优先。
          - **有工具结果**：model_reply 优先于 step_reply。
          - **默认**：model_reply 优先于 step_reply。

        所有场景的最后一个候选始终是 :data:`FALLBACK_REPLY`。

        Args:
            model_reply: LLM 生成的回复文本。
            step_reply: StepAgent 生成的回复文本。
            completion_fallback: 技能完成时的兜底回复。
            session_fallback: 会话级别的兜底回复。
            tool_result: 工具调用结果（可能为 None）。
            completion_ready: 技能是否已完成。
            prefer_step_reply: 是否优先使用 StepAgent 回复。

        Returns:
            按优先级排列的候选回复元组。
        """
        if prefer_step_reply:
            return (
                step_reply,
                model_reply,
                completion_fallback,
                session_fallback,
                FALLBACK_REPLY,
            )
        if completion_ready:
            return (
                model_reply,
                completion_fallback,
                step_reply,
                session_fallback,
                FALLBACK_REPLY,
            )
        if tool_result is not None:
            return (
                model_reply,
                step_reply,
                completion_fallback,
                session_fallback,
                FALLBACK_REPLY,
            )
        return (
            model_reply,
            step_reply,
            completion_fallback,
            session_fallback,
            FALLBACK_REPLY,
        )

    def _progress_payload(
        self,
        session: ChatSession,
        skill: Skill | None,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> dict[str, object]:
        """构建技能进度信息 payload，供 LLM 了解当前完成状态。

        Args:
            session: 当前会话状态对象。
            skill: 当前活跃技能对象（可能为 None）。
            step_result: StepAgent 的步骤执行结果。
            tool_result: 工具调用结果（可能为 None）。

        Returns:
            包含缺失信息列表、技能完成状态等字段的字典。
        """
        # 无技能时返回空进度结构
        if not skill:
            return {
                "missing_current_step_info": [],
                "missing_required_info": [],
                "skill_completion_ready": False,
            }
        return {
            "missing_current_step_info": self._missing_current_step_info(session, skill),
            "missing_required_info": self._missing_required_info(session, skill),
            "skill_completion_ready": self._skill_completion_ready(session, skill, step_result, tool_result),
            "step_completed": step_result.is_step_completed,
        }

    def _skill_completion_ready(
        self,
        session: ChatSession,
        skill: Skill | None,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
    ) -> bool:
        """判断技能是否已收集完整信息、可以视为完成。

        完成条件（全部满足）：
          - 存在活跃技能
          - 当前步骤已完成 (is_step_completed)
          - 工具调用成功（如果有工具调用）
          - 当前步骤无缺失的期望信息
          - 技能无缺失的必填信息

        Args:
            session: 当前会话状态对象。
            skill: 当前活跃技能对象（可能为 None）。
            step_result: StepAgent 的步骤执行结果。
            tool_result: 工具调用结果（可能为 None）。

        Returns:
            True 表示技能已完成，False 表示未完成。
        """
        if not skill or not step_result.is_step_completed:
            return False
        # 工具调用失败则技能不能视为完成
        if tool_result and not tool_result.success:
            return False
        # 当前步骤和全局必填信息均无缺失时才视为完成
        return not self._missing_current_step_info(session, skill) and not self._missing_required_info(session, skill)

    def _missing_current_step_info(self, session: ChatSession, skill: Skill) -> list[str]:
        """获取当前步骤中尚未收集的期望用户信息字段列表。

        Args:
            session: 当前会话状态对象。
            skill: 当前活跃技能对象。

        Returns:
            缺失的期望信息字段名列表；当前步骤不存在时返回空列表。
        """
        step = self._current_step(session, skill)
        if not step:
            return []
        return [
            str(field)
            for field in step.get("expected_user_info", [])
            if not self._slot_has_value(session.slots_json or {}, str(field))
        ]

    def _missing_required_info(self, session: ChatSession, skill: Skill) -> list[str]:
        """获取技能全局尚未收集的必填信息字段列表。

        Args:
            session: 当前会话状态对象。
            skill: 当前活跃技能对象。

        Returns:
            缺失的必填信息字段名列表。
        """
        return [
            str(field)
            for field in (skill.content_json or {}).get("required_info", [])
            if not self._slot_has_value(session.slots_json or {}, str(field))
        ]

    def _current_step(self, session: ChatSession, skill: Skill) -> dict | None:
        """从技能流程图中查找当前活跃步骤的节点定义。

        Args:
            session: 当前会话状态对象，提供 active_step_id。
            skill: 当前活跃技能对象，提供流程图节点列表。

        Returns:
            当前步骤节点的结构化字典，包含 step_id、name、instruction、
            expected_user_info、allowed_actions 等字段；未找到时返回 None。
        """
        for node in (skill.content_json or {}).get("nodes", []):
            if isinstance(node, dict) and node.get("node_id") == session.active_step_id:
                return {
                    "step_id": node.get("node_id"),
                    "node_id": node.get("node_id"),
                    "name": node.get("name"),
                    "instruction": node.get("instruction"),
                    "expected_user_info": node.get("expected_user_info", []),
                    "allowed_actions": node.get("allowed_actions", []),
                }
        return None

    def _slot_has_value(self, slots: dict, field: str) -> bool:
        """检查指定槽位是否已填充有效值（非 None 且非空字符串）。

        Args:
            slots: 槽位字典。
            field: 待检查的槽位键名。

        Returns:
            True 表示槽位有有效值，False 表示无值。
        """
        value = slots.get(field)
        return value is not None and value != ""

    def _completion_fallback(self) -> str:
        """返回技能完成时的标准兜底回复文本。

        Returns:
            技能完成的兜底回复字符串。
        """
        return "已记录完整信息。请问还有其他需要帮助的吗？"

    def _minimal_fallback_for_session(self, session: ChatSession) -> str:
        """返回会话级别的最小兜底回复文本。

        Args:
            session: 当前会话状态对象（当前实现未使用参数，保留以备扩展）。

        Returns:
            会话级别的兜底回复字符串。
        """
        return "请您再补充一下具体诉求，我会继续帮您处理。"

    def _minimal_fallback(self, router_decision: RouterDecision) -> str:
        """根据路由决策返回最小兜底回复文本。

        clarify 场景使用路由决策中的澄清问题；其他场景使用最终兜底回复。

        Args:
            router_decision: 上游路由决策。

        Returns:
            最小兜底回复字符串。
        """
        if router_decision.decision == "clarify" and router_decision.clarification_question:
            return router_decision.clarification_question
        return FALLBACK_REPLY

    def _system_prompt(self, persona_prompt: str | None) -> str:
        """返回统一的系统提示词。

        当前实现直接返回全局统一系统提示词，persona_prompt 参数保留
        以备未来扩展（当前人设通过 payload 注入而非系统提示词）。

        Args:
            persona_prompt: 坐席人设提示词（当前未使用）。

        Returns:
            统一系统提示词字符串。
        """
        return unified_system_prompt()

    def _stage_payload(
        self, payload: dict[str, object], persona_prompt: str | None
    ) -> dict[str, object]:
        """将业务 payload 包装为统一的阶段 payload 结构。

        将 payload 中的非消息字段提取为 stage_data，可选手动注入人设
        提示词（employee_identity），然后通过 :func:`stage_payload`
        构建包含阶段标识、用户消息、对话上下文、提示词指令和输出约束
        的完整 payload。

        Args:
            payload: 原始业务 payload 字典。
            persona_prompt: 坐席人设提示词（可选），注入到 stage_data 头部。

        Returns:
            符合阶段协议的完整 payload 字典。
        """
        # 从 payload 中分离 stage_data（排除 user_message 和 conversation_context）
        stage_data = {
            key: value
            for key, value in payload.items()
            if key not in {"user_message", "conversation_context"}
        }
        # 注入人设提示词到 stage_data 头部
        if persona_prompt:
            stage_data = {"employee_identity": persona_prompt.strip(), **stage_data}
        return stage_payload(
            phase="Response Generator",
            user_message=str(payload.get("user_message") or ""),
            conversation_context=payload.get("conversation_context")
            if isinstance(payload.get("conversation_context"), dict)
            else {},
            memory_context=None,
            instructions=PROMPT_PATH.read_text(encoding="utf-8"),
            stage_data=stage_data,
            output_contract="只输出最终用户可见的纯文本，不输出 JSON、Markdown 代码围栏、分析过程或内部状态。",
        )
