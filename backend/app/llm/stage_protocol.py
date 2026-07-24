"""智能体多阶段对话协议：阶段输入的构造与渲染。

本模块负责把智能体每一轮推理所需的阶段上下文组装成结构化负载，并渲染成最终
发送给 LLM 的用户消息文本。智能体执行采用“多阶段”范式，典型阶段包括：

- **Router（路由）**：判断本轮用户意图，决定继续当前任务、切换/创建/更新任务，
  还是直接回答/澄清/转人工；
- **Step（技能步骤）**：在当前技能节点上决定动作（提问、推进、调用工具、检索
  知识、转人工等）；
- **Reflection（反思）**：评估上一动作结果，决定放行、重试或修正。

模块提供：
- 三种阶段的输出契约 schema（``ROUTER_OUTPUT_SCHEMA`` 等），作为对模型输出的
  约束；
- ``stage_payload``：组装一个阶段的原始结构化负载；
- ``render_stage_user_message``：把负载渲染为紧凑的多段文本，作为 LLM 的
  user message；
- ``unified_system_prompt``：读取统一的系统提示词文件。

本模块被 ``client.py`` 与智能体编排层调用，是“提示词工程”与“调用层”之间的
桥梁。
"""

from __future__ import annotations

import copy
import json
from typing import Any

from app import paths


# 统一系统提示词文件路径（描述智能体整体行为规范）。
UNIFIED_PROMPT_PATH = (
    paths.resource_dir() / "app" / "llm" / "prompts" / "unified_agent_prompt.md"
)
# 阶段负载中标记阶段元数据的键名。
STAGE_PROTOCOL_KEY = "_agent_stage"
TURN_STAGE_MESSAGES_KEY = "_agent_turn_messages"


# Router 阶段输出契约：描述路由决策的结构（decision、目标技能/步骤、任务帧与更新）。
ROUTER_OUTPUT_SCHEMA: dict[str, Any] = {
    "decision": "continue_active | switch_to_pending | create_pending | update_pending | complete_task | start_new_task | answer_only | handoff_human | clarify",
    "selected_task_id": "string?",
    "target_skill_id": "string?",
    "target_step_id": "string?",
    "confidence": "number",
    "user_intent": "string?",
    "general_intent": "string?",
    "reason": "string?",
    "clarification_question": "string?",
    "slot_hints": "object?",
    "task_frames": [
        {
            "task_id": "string?",
            "status": "pending?",
            "decision": "start_new_task | continue_active?",
            "target_skill_id": "string",
            "target_step_id": "string?",
            "user_intent": "string?",
            "slot_hints": "object?",
        }
    ],
    "pending_tasks": [
        {
            "task_id": "string?",
            "status": "pending?",
            "decision": "start_new_task | continue_active?",
            "target_skill_id": "string?",
            "target_step_id": "string?",
            "confidence": "number?",
            "user_intent": "string?",
            "reason": "string?",
            "slot_hints": "object?",
        }
    ],
    "task_updates": [
        {
            "task_id": "string",
            "status": "string?",
            "target_skill_id": "string?",
            "target_step_id": "string?",
            "user_intent": "string?",
            "reason": "string?",
            "slot_hints": "object?",
            "remove": "boolean?",
        }
    ],
}

# Step 阶段输出契约：描述单步动作（提问/回复/推进/工具调用/知识检索/转人工）。
STEP_AGENT_OUTPUT_SCHEMA: dict[str, Any] = {
    "action": "ask_user | clarify | reply | advance | call_tool | query_knowledge | handoff",
    "reply": "string?",
    "slot_updates": "object",
    "tool_call": {"name": "string", "arguments": "object"},
    "knowledge_query": {
        "query": "string",
        "reason": "string?",
        "scope": "object?",
        "max_chunks": "integer?",
        "query_type": "answer | policy_check | tool_discovery | skill_discovery?",
    },
    "next_step_id": "string?",
    "is_step_completed": "boolean",
    "handoff": "boolean?",
}

# Reflection 阶段输出契约：描述对上一动作的反思结论（放行/重试/修正/停止）。
REFLECTION_OUTPUT_SCHEMA: dict[str, Any] = {
    "action": "pass | retry_tool | try_other_tool | ask_user | revise_step | stop",
    "needs_retry": "boolean",
    "reason": "string?",
    "target_skill_id": "string?",
    "target_step_id": "string?",
    "target_tool_name": "string?",
}


def unified_system_prompt() -> str:
    """读取并返回统一系统提示词文本。

    Returns:
        str: 统一系统提示词（去除首尾空白）。
    """
    return UNIFIED_PROMPT_PATH.read_text(encoding="utf-8").strip()


def stage_payload(
    *,
    phase: str,
    user_message: str,
    conversation_context: dict[str, object] | None,
    memory_context: list[dict[str, object]] | str | None,
    instructions: str,
    stage_data: dict[str, Any],
    output_contract: dict[str, Any] | str,
) -> dict[str, Any]:
    """组装单个阶段的原始结构化负载。

    把阶段元数据（phase/instructions/output_contract/memory/turn_time）、用户
    消息、对话上下文与阶段独有数据合并为一个字典。其中阶段元数据统一收拢到
    ``STAGE_PROTOCOL_KEY`` 下；Router 阶段会额外把记忆渲染为文本。

    Args:
        phase: 阶段名（Router / Step / Reflection 等）。
        user_message: 本轮用户原始输入。
        conversation_context: 对话上下文（含 metadata，如当前轮时间）。
        memory_context: 记忆上下文（列表或文本，仅 Router 渲染）。
        instructions: 本阶段的规则/指令文本。
        stage_data: 本阶段独有的附加数据（会展开合并进负载）。
        output_contract: 本阶段的输出契约（schema dict 或字符串）。

    Returns:
        dict[str, Any]: 组装好的阶段负载字典。
    """
    metadata = (
        conversation_context.get("metadata", {})
        if isinstance(conversation_context, dict)
        else {}
    )
    turn_time = metadata.get("current_turn_time") if isinstance(metadata, dict) else None
    memory_text = ""
    # 仅 Router 阶段需要把记忆渲染为文本注入；其他阶段不渲染记忆。
    if phase == "Router":
        memory_text = (
            _memory_text(memory_context)
            if isinstance(memory_context, list)
            else str(memory_context or "").strip()
        )
    return {
        STAGE_PROTOCOL_KEY: {
            "phase": phase,
            "instructions": instructions.strip(),
            "output_contract": output_contract,
            "memory": memory_text,
            "turn_time": str(turn_time or "未提供"),
        },
        "user_message": user_message,
        "conversation_context": (
            conversation_context if isinstance(conversation_context, dict) else {}
        ),
        **stage_data,
    }


def render_stage_user_message(
    user_payload: dict[str, Any], *, include_turn_header: bool = True
) -> str:
    """把阶段负载渲染为发送给 LLM 的用户消息文本。

    渲染策略：剥离 conversation_context，将阶段元数据与阶段独有内容组织为多个
    带标题的分段（用户记忆/本轮时间/本轮输入/当前阶段/思考要求/阶段规则/独有
    内容/输出约束），用空行分隔拼接。输出契约若是 dict 会序列化为紧凑 JSON。

    Args:
        user_payload: 由 ``stage_payload`` 产出的阶段负载。
        include_turn_header: 是否包含用户记忆/时间/输入的头部段落。

    Returns:
        str: 渲染后的多段文本，作为 LLM 的 user message。
    """
    payload = copy.deepcopy(
        {
            key: value
            for key, value in user_payload.items()
            if key != "conversation_context"
        }
    )
    stage = payload.pop(STAGE_PROTOCOL_KEY, {})
    user_message = str(payload.pop("user_message", "") or "").strip()
    # 投射掉空值，避免把无意义的空字段塞进提示词。
    projected = _drop_empty_values(payload)
    output_contract = stage.get("output_contract") if isinstance(stage, dict) else None
    if not isinstance(output_contract, str):
        output_contract = json.dumps(
            output_contract or {}, ensure_ascii=False, separators=(",", ":")
        )
    sections = []
    if include_turn_header:
        sections.extend(
            [
                f"用户记忆：\n{stage.get('memory') or '无'}",
                f"本轮时间：\n{stage.get('turn_time') or '未提供'}",
                f"本轮用户输入：\n{user_message or '（空）'}",
            ]
        )
    sections.extend(
        [
            f"当前阶段：\n{stage.get('phase') or '未指定'}",
            (
                "思考要求：\n保留完成当前阶段所需的简短思考；不要复述上下文、逐字段展开检查、"
                "罗列无关备选方案或反复验证已明确的信息。得到可靠结论后立即按输出约束作答。"
            ),
            f"阶段规则：\n{str(stage.get('instructions') or '').strip()}",
            "当前阶段独有内容：\n"
            + json.dumps(projected, ensure_ascii=False, separators=(",", ":")),
            f"输出约束：\n{output_contract}",
        ]
    )
    return "\n\n".join(sections)


def _memory_text(items: list[dict[str, object]]) -> str:
    """把记忆条目列表渲染为无序列表文本（去重、折叠空白）。

    Args:
        items: 记忆条目列表，每项含 content 字段。

    Returns:
        str: 形如 ``- 条目1\n- 条目2`` 的文本；无内容时返回空串。
    """
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = " ".join(str(item.get("content") or "").split())
        if not content or content in lines:
            continue
        lines.append(content)
    return "\n".join(f"- {line}" for line in lines)


def _drop_empty_values(value: Any) -> Any:
    """递归剔除容器中的空值（None/空串/空列表/空字典）。

    Args:
        value: 任意值（dict/list/标量）。

    Returns:
        Any: 剔除空值后的结构；标量原样返回。
    """
    if isinstance(value, dict):
        projected = {key: _drop_empty_values(item) for key, item in value.items()}
        return {
            key: item
            for key, item in projected.items()
            if item not in (None, "", [], {})
        }
    if isinstance(value, list):
        return [
            projected
            for item in value
            if (projected := _drop_empty_values(item)) not in (None, "", [], {})
        ]
    return value
