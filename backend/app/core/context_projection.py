"""上下文投影与压缩（projection）模块。

本模块是"控制上下文（control context）"的核心预处理层。在将各类运行时数据
（知识检索结果、技能编排图、路由决策、任务队列、记忆条目等）送入 LLM 之前，
需要对其进行**投影（projection）**——即只提取 LLM 决策所需的关键字段、
裁剪冗长文本、合并去重，从而控制 token 消耗并提升 LLM 聚焦度。

核心概念
--------
- **投影（projection）**：从原始数据结构中提取子集字段，丢弃内部/冗余字段。
  所有 ``compact_*`` 函数本质上都是投影操作。
- **``_without_empty``**：通用工具函数，移除值为 ``None``/空字符串/空列表/空字典
  的字段，保持投影结果的简洁。
- **``_short_text``**：通用文本截断函数，将长文本限制到指定字符数并追加省略号。
- **token 预算**：对话消息部分通过 :func:`build_conversation_context` 进行
  token 级压缩；其他结构化数据通过条目数限制（如 ``KNOWLEDGE_EVIDENCE_LIMIT``）
  控制体量。

主要数据流
----------
1. **知识检索结果**：``compact_step_result`` → ``compact_knowledge_context`` →
   ``_compact_knowledge_result`` → ``_compact_retrieved_knowledge``（多源合并去重）。
2. **对话上下文**：``compact_conversation_context`` 委托给
   :func:`build_conversation_context` 做消息级压缩。
3. **技能编排图**：``compact_current_step`` / ``compact_step_skill_context``
   从技能 DAG 中投影当前步骤及可达的后继步骤。
4. **路由决策**：``compact_router_decision`` / ``compact_step_router_decision``
   投影路由器的决策字段。
5. **任务/记忆**：``compact_pending_tasks`` / ``compact_memory_context`` /
   ``compact_deferred_intents`` 投影任务队列和记忆。

与其他模块的关系
----------------
- 依赖 :mod:`app.core.conversation_context` 的 ``build_conversation_context``
  实现消息级压缩。
- 依赖 :mod:`app.llm.stage_protocol` 中的 ``TURN_STAGE_MESSAGES_KEY`` 常量，
  用于在上下文中保留阶段消息。
- 被上层编排/会话管理模块调用，在每次 LLM 调用前做上下文准备。
"""

from __future__ import annotations

from typing import Any

from app.core.conversation_context import build_conversation_context
from app.llm.stage_protocol import TURN_STAGE_MESSAGES_KEY


# 控制上下文（含消息）的 token 预算上限
CONTROL_CONTEXT_TOKEN_BUDGET = 32_000
# 知识检索历史保留的最大条目数（每轮检索结果算一条）
KNOWLEDGE_HISTORY_LIMIT = 1
# 检索证据（evidence/chunks）的最大保留条目数
KNOWLEDGE_EVIDENCE_LIMIT = 6
# 检索概念（concepts）的最大保留条目数
KNOWLEDGE_CONCEPT_LIMIT = 8
# 检索文档（documents）的最大保留条目数
KNOWLEDGE_DOCUMENT_LIMIT = 5
# 最终合并去重后保留的检索知识条目上限
RETRIEVED_KNOWLEDGE_LIMIT = 4


def compact_knowledge_context(
    items: list[dict[str, Any]] | None,
    *,
    max_items: int = KNOWLEDGE_HISTORY_LIMIT,
) -> list[dict[str, Any]]:
    """压缩知识检索历史条目列表。

    从历史检索结果中取最后 ``max_items`` 条，并对每条做投影压缩（提取 query
    和检索到的知识）。

    Args:
        items: 知识检索历史条目列表，可能为 ``None``。
        max_items: 保留的最大条目数，默认为 1（仅保留最近一次检索）。

    Returns:
        压缩后的知识检索条目列表。
    """
    if not isinstance(items, list):
        return []
    # 过滤出字典类型的条目，取最后 max_items 条
    selected = [item for item in items if isinstance(item, dict)][-max(1, max_items) :]
    return [_compact_knowledge_result(item) for item in selected]


def compact_step_result(payload: dict[str, Any]) -> dict[str, Any]:
    """压缩单步执行结果，将原始的 ``knowledge_results`` 转为精简的 ``retrieved_knowledge``。

    原始执行结果可能包含大量知识检索详情（``knowledge_results``），直接传入 LLM
    会浪费 token。此函数移除该字段，替换为经过 :func:`compact_knowledge_context`
    压缩后的 ``retrieved_knowledge``。

    Args:
        payload: 原始步骤执行结果字典。

    Returns:
        投影后的步骤结果字典，不含 ``knowledge_results``，含精简的 ``retrieved_knowledge``。
    """
    projected = dict(payload)
    # 移除体积庞大的原始知识检索结果
    projected.pop("knowledge_results", None)
    # 用压缩后的检索知识替代
    projected["retrieved_knowledge"] = compact_knowledge_context(
        payload.get("knowledge_results") if isinstance(payload.get("knowledge_results"), list) else []
    )
    return projected


def compact_conversation_context(
    context: dict[str, object] | None,
    *,
    token_budget: int = CONTROL_CONTEXT_TOKEN_BUDGET,
) -> dict[str, object]:
    """压缩对话上下文，确保消息部分不超过 token 预算。

    若上下文不存在则构建空上下文；若消息列表无效则初始化为空；若已有元数据
    表明 token 估算在预算内则直接返回（跳过压缩以节省开销）；否则委托给
    :func:`build_conversation_context` 执行消息级压缩，并保留阶段消息。

    Args:
        context: 原始对话上下文字典，可能为 ``None``。
        token_budget: 消息 token 预算上限。

    Returns:
        压缩后的对话上下文字典。
    """
    # 上下文不存在，返回空的基础上下文
    if not isinstance(context, dict):
        return build_conversation_context([], token_budget)
    # 保留阶段消息（turn stage messages），压缩后需要回填
    turn_messages = context.setdefault(TURN_STAGE_MESSAGES_KEY, [])
    messages = context.get("messages")
    # 消息列表类型不合法，初始化为空并直接返回
    if not isinstance(messages, list):
        context["messages"] = []
        return context
    metadata = context.get("metadata")
    # 若已有元数据且 token 估算在预算内，无需重新压缩
    if (
        isinstance(metadata, dict)
        and int(metadata.get("estimated_tokens") or 0) <= token_budget
    ):
        return context
    # 执行消息级压缩
    compacted = build_conversation_context(
        [message for message in messages if isinstance(message, dict)], token_budget
    )
    # 回填阶段消息
    compacted[TURN_STAGE_MESSAGES_KEY] = turn_messages
    return compacted


def compact_current_step(
    content: dict[str, Any] | None,
    step_id: str | None,
) -> dict[str, Any] | None:
    """从技能编排内容中投影出当前步骤的精简信息。

    在技能 DAG（有向无环图）中根据 ``step_id`` 定位当前节点，提取其关键字段
    （类型、名称、指令、预期用户信息等）。

    Args:
        content: 技能编排内容字典（含 nodes/steps 和 edges）。
        step_id: 当前步骤 ID。若为 ``None`` 则尝试从 ``start_node_id`` 推断。

    Returns:
        当前步骤的投影字典；若未找到匹配节点则返回 ``None``。
    """
    if not isinstance(content, dict):
        return None
    # 解析步骤 ID，优先使用传入参数，否则回退到 start_node_id
    resolved_step_id = step_id or _optional_text(content.get("start_node_id"))
    # 在技能节点列表中查找匹配的节点
    node = next(
        (
            item
            for item in _skill_nodes(content)
            if isinstance(item, dict)
            and _optional_text(item.get("node_id") or item.get("step_id")) == resolved_step_id
        ),
        None,
    )
    return _project_node(node) if node else None


def compact_step_skill_context(
    content: dict[str, Any] | None,
    step_id: str | None,
    *,
    skill_id: str | None = None,
    name: str | None = None,
    description: str | None = None,
) -> dict[str, Any] | None:
    """投影当前技能步骤的完整上下文，包括当前步骤和可达的后继步骤。

    从技能 DAG 中提取当前步骤节点，然后遍历所有从当前步骤出发的边（edges），
    找到目标后继节点并投影其信息（含转移条件 transition）。

    Args:
        content: 技能编排内容字典。
        step_id: 当前步骤 ID。
        skill_id: 可选的技能 ID（当前未在投影中使用，预留给上层传递）。
        name: 可选的技能名称（当前未在投影中使用）。
        description: 可选的技能描述（当前未在投影中使用）。

    Returns:
        包含 ``current_step`` 和 ``next_steps`` 的字典；若 content 无效则返回 ``None``。
    """
    if not isinstance(content, dict):
        return None
    # 投影当前步骤
    current_step = compact_current_step(content, step_id)
    current_step_id = _optional_text((current_step or {}).get("node_id"))
    # 构建 node_id -> node 的索引，便于快速查找后继节点
    nodes_by_id = {
        _optional_text(node.get("node_id") or node.get("step_id")): node
        for node in _skill_nodes(content)
        if isinstance(node, dict)
        and _optional_text(node.get("node_id") or node.get("step_id"))
    }
    edges = content.get("edges")
    next_steps: list[dict[str, Any]] = []
    # 遍历所有边，找出从当前步骤出发的转移
    for edge in edges if isinstance(edges, list) else []:
        if not isinstance(edge, dict):
            continue
        # 只关注 source 为当前步骤的边
        if _optional_text(edge.get("source_node_id")) != current_step_id:
            continue
        # 解析目标节点 ID（兼容多种字段命名）
        target_id = _optional_text(edge.get("next_node_id") or edge.get("target_node_id"))
        target_node = nodes_by_id.get(target_id)
        if not target_node:
            continue
        # 投影目标节点
        projected_step = _project_step_agent_node(target_node)
        # 投影转移条件
        transition = _project_transition(edge)
        if transition:
            projected_step["transition"] = transition
        next_steps.append(projected_step)
    return _without_empty(
        {
            "current_step": _project_step_agent_node(current_step or {}),
            "next_steps": next_steps,
        }
    )


def compact_router_decision(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """投影顶层路由决策的关键字段。

    从路由器的完整决策 payload 中提取 LLM 后续推理所需的决策字段，包括
    决策类型、目标任务/技能/步骤、置信度、用户意图、澄清问题等。

    Args:
        payload: 路由决策 payload，可能为 ``None``。

    Returns:
        仅含关键字段的投影字典；若 payload 无效或投影后为空则返回 ``None``。
    """
    if not isinstance(payload, dict):
        return None
    projected = _without_empty(
        {
            key: payload.get(key)
            for key in (
                "decision",
                "selected_task_id",
                "target_skill_id",
                "target_step_id",
                "confidence",
                "user_intent",
                "reason",
                "clarification_question",
                "slot_hints",
            )
        }
    )
    return projected or None


def compact_step_router_decision(
    payload: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """投影步骤级路由决策（精简版，仅保留决策类型和用户意图）。

    相比 :func:`compact_router_decision`，此函数用于步骤内路由，只提取
    最核心的决策类型和用户意图（截断到 300 字符）。

    Args:
        payload: 步骤路由决策 payload。

    Returns:
        含 ``decision`` 和 ``user_intent`` 的投影字典；若无效则为 ``None``。
    """
    if not isinstance(payload, dict):
        return None
    projected = _without_empty(
        {
            "decision": payload.get("decision"),
            "user_intent": _short_text(payload.get("user_intent"), 300),
        }
    )
    return projected or None


def compact_response_step_result(payload: dict[str, Any]) -> dict[str, Any] | None:
    """投影响应步骤的执行结果，仅保留回复和后续指引字段。

    Args:
        payload: 响应步骤的执行结果字典。

    Returns:
        含 ``reply``、``next_step_id``、``is_step_completed``、``handoff``
        的投影字典；若投影后为空则返回 ``None``。
    """
    projected = _without_empty(
        {
            key: payload.get(key)
            for key in ("reply", "next_step_id", "is_step_completed", "handoff")
        }
    )
    return projected or None


def compact_citation_hints(citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """投影引用提示列表，每条仅保留标签、类型、标题、路径等元信息。

    Args:
        citations: 引用提示列表。

    Returns:
        投影后的引用提示列表，每条仅含非空字段。
    """
    return [
        {
            key: item.get(key)
            for key in (
                "label",
                "kind",
                "title",
                "source_path",
                "section_path",
            )
            if item.get(key) not in (None, "")
        }
        for item in citations
        if isinstance(item, dict)
    ]


def compact_memory_context(items: list[dict[str, Any]] | None) -> str:
    """将记忆条目列表压缩为换行分隔的纯文本摘要。

    每条记忆提取 ``content`` 字段（截断到 1000 字符），去重后以
    ``- 内容`` 的列表格式拼接。

    Args:
        items: 记忆条目列表。

    Returns:
        换行分隔的记忆摘要文本；若无有效条目则返回空字符串。
    """
    if not isinstance(items, list):
        return ""
    lines: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = _short_text(item.get("content"), 1_000)
        # 去重：相同内容不重复加入
        if content and content not in lines:
            lines.append(content)
    return "\n".join(f"- {line}" for line in lines)


def compact_pending_tasks(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """投影待办任务列表，提取 LLM 理解任务所需的关键字段。

    每条任务提取任务 ID、状态、技能/步骤、槽位、意图摘要、来源消息、恢复策略等，
    并对长文本字段做截断。

    Args:
        items: 待办任务列表。

    Returns:
        投影后的任务列表（空值字段被移除）。
    """
    if not isinstance(items, list):
        return []
    tasks: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        task = _without_empty(
            {
                "task_id": item.get("task_id"),
                "status": item.get("status"),
                # 兼容两种字段命名
                "skill_id": item.get("skill_id") or item.get("target_skill_id"),
                "step_id": item.get("step_id") or item.get("target_step_id"),
                "slots": item.get("slots") or item.get("slot_hints"),
                "intent_summary": _short_text(
                    item.get("intent_summary") or item.get("user_intent"), 300
                ),
                "source_message": _short_text(item.get("source_message"), 500),
                "resume_policy": item.get("resume_policy"),
            }
        )
        if task:
            tasks.append(task)
    return tasks


def compact_deferred_intents(
    items: list[dict[str, Any]] | None,
    *,
    selected_task_id: str | None = None,
) -> list[str]:
    """提取被延迟处理的用户意图文本列表。

    从待办任务列表中提取所有处于 pending 状态、且不是当前选中任务的意图摘要，
    用于告知 LLM "还有哪些用户意图尚未处理"。

    Args:
        items: 待办任务列表。
        selected_task_id: 当前已选中的任务 ID，该任务的意图会被排除。

    Returns:
        去重后的意图文本列表（每条截断到 300 字符）。
    """
    if not isinstance(items, list):
        return []
    intents: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        # 跳过当前选中的任务
        if selected_task_id and str(item.get("task_id") or "") == selected_task_id:
            continue
        # 只保留 pending 状态的任务
        if str(item.get("status") or "pending") != "pending":
            continue
        intent = _short_text(
            item.get("intent_summary")
            or item.get("user_intent")
            or item.get("source_message"),
            300,
        )
        # 去重
        if intent and intent not in intents:
            intents.append(intent)
    return intents


def compact_awaiting_input(value: dict[str, Any] | None) -> dict[str, Any] | None:
    """投影"等待用户输入"状态，提取技能/步骤/期望字段/问题摘要。

    Args:
        value: 等待输入状态字典。

    Returns:
        投影后的字典；若无效或投影后为空则返回 ``None``。
    """
    if not isinstance(value, dict):
        return None
    projected = _without_empty(
        {
            "skill_id": value.get("skill_id"),
            "step_id": value.get("step_id"),
            "expected_fields": value.get("expected_fields"),
            "question_summary": _short_text(value.get("question_summary"), 500),
        }
    )
    return projected or None


def _compact_knowledge_result(item: dict[str, Any]) -> dict[str, Any]:
    """压缩单条知识检索结果，提取查询语句和检索到的知识条目。

    Args:
        item: 知识检索结果字典，含 ``query`` 和各类检索产物。

    Returns:
        含 ``query``（截断到 500 字符）和 ``retrieved_knowledge`` 的投影字典。
    """
    query = item.get("query")
    # query 可能是嵌套结构，尝试提取内层 query 字段
    if isinstance(query, dict):
        query = query.get("query")
    return _without_empty(
        {
            "query": _short_text(query, 500),
            "retrieved_knowledge": _compact_retrieved_knowledge(item),
        }
    )


def _compact_retrieved_knowledge(item: dict[str, Any]) -> list[dict[str, Any]]:
    """从知识检索结果中合并多源检索产物，去重后返回统一格式的知识条目列表。

    知识检索可能返回多种类型的产物：证据片段（evidence/chunks）、概念
    （concepts）、文档（documents）、桶（buckets）、OKF 引用（okf_citations）。
    此函数将它们统一投影为 ``{title, source, summary, content}`` 格式，
    然后按 ``source|title|content|summary`` 拼接的指纹去重，最多保留
    ``RETRIEVED_KNOWLEDGE_LIMIT`` 条。

    Args:
        item: 知识检索结果字典。

    Returns:
        去重后的统一格式知识条目列表（每条带 ``label`` 序号标签）。
    """
    candidates: list[dict[str, Any]] = []
    # 优先使用 evidence_pack，回退到 chunks
    evidence = _dict_items(item.get("evidence_pack"), KNOWLEDGE_EVIDENCE_LIMIT)
    if not evidence:
        evidence = _dict_items(item.get("chunks"), KNOWLEDGE_EVIDENCE_LIMIT)
    # 投影证据片段
    for value in evidence:
        candidates.append(
            {
                "title": _short_text(value.get("title") or value.get("label"), 180),
                "source": _short_text(
                    value.get("section_path")
                    or value.get("source_path")
                    or value.get("source_ref"),
                    300,
                ),
                "summary": _short_text(value.get("summary"), 300),
                "content": _short_text(value.get("content") or value.get("excerpt"), 800),
            }
        )
    # 投影概念
    for value in _dict_items(item.get("selected_concepts"), KNOWLEDGE_CONCEPT_LIMIT):
        candidates.append(
            {
                "title": _short_text(value.get("title") or value.get("name"), 180),
                "source": _short_text(value.get("source_path") or value.get("concept_id"), 300),
                "summary": _short_text(value.get("summary"), 300),
                "content": _short_text(value.get("content") or value.get("content_md"), 600),
            }
        )
    # 投影文档
    for value in _dict_items(item.get("selected_documents"), KNOWLEDGE_DOCUMENT_LIMIT):
        candidates.append(
            {
                "title": _short_text(value.get("title") or value.get("filename"), 180),
                "source": _short_text(value.get("filename"), 180),
                "summary": _short_text(value.get("summary"), 600),
            }
        )
    # 投影桶（buckets）
    for value in _dict_items(item.get("selected_buckets"), KNOWLEDGE_DOCUMENT_LIMIT):
        candidates.append(
            {
                "title": _short_text(value.get("title"), 180),
                "summary": _short_text(value.get("summary"), 600),
            }
        )
    # 投影 OKF 引用
    for value in _dict_items(item.get("okf_citations"), KNOWLEDGE_EVIDENCE_LIMIT):
        candidates.append(
            {
                "title": _short_text(value.get("title") or value.get("label"), 180),
                "source": _short_text(
                    value.get("source_path") or value.get("path") or value.get("uri"),
                    300,
                ),
            }
        )

    # 基于指纹去重并限制最终条目数
    compacted: list[dict[str, Any]] = []
    seen: set[str] = set()
    for candidate in candidates:
        projected = _without_empty(candidate)
        # 构建去重指纹：source + title + content + summary 拼接
        identity = "|".join(
            str(projected.get(key) or "")
            for key in ("source", "title", "content", "summary")
        ).strip("|")
        if not identity or identity in seen:
            continue
        seen.add(identity)
        compacted.append(
            {"label": f"检索到的知识 {len(compacted) + 1}", **projected}
        )
        # 达到上限即停止
        if len(compacted) >= RETRIEVED_KNOWLEDGE_LIMIT:
            break
    return compacted


def _dict_items(value: object, limit: int) -> list[dict[str, Any]]:
    """从可能为列表的值中提取前 ``limit`` 个字典元素。

    Args:
        value: 原始值，预期为字典列表。
        limit: 最大提取数量。

    Returns:
        字典列表（截取前 limit 个）；若输入非列表则返回空列表。
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)][:limit]


def _skill_nodes(content: dict[str, Any]) -> list[dict[str, Any]]:
    """从技能编排内容中提取节点列表，兼容 ``nodes`` 和 ``steps`` 两种字段名。

    Args:
        content: 技能编排内容字典。

    Returns:
        节点字典列表；若无有效节点则返回空列表。
    """
    value = content.get("nodes")
    # 回退到 steps 字段（旧版命名）
    if not isinstance(value, list):
        value = content.get("steps")
    return [item for item in value or [] if isinstance(item, dict)]


def _project_node(node: dict[str, Any]) -> dict[str, Any]:
    """投影技能节点，提取节点的完整定义字段。

    适用于需要查看节点全部配置的场景（如 ``compact_current_step``）。

    Args:
        node: 原始节点字典。

    Returns:
        含 ``node_id`` 及类型、名称、指令、条件等字段的投影字典。
    """
    projected = {"node_id": node.get("node_id") or node.get("step_id")}
    projected.update(
        {
            key: node.get(key)
            for key in (
                "type",
                "name",
                "instruction",
                "optional",
                "condition",
                "expected_user_info",
                "allowed_actions",
                "knowledge_scope",
                "retry_policy",
            )
        }
    )
    return _without_empty(projected)


def _project_step_agent_node(node: dict[str, Any]) -> dict[str, Any]:
    """投影步骤代理节点，仅提取步骤执行所需的核心字段。

    相比 :func:`_project_node`，此函数提取的字段更少，专注于步骤执行
    所需的指令和约束。

    Args:
        node: 原始节点字典。

    Returns:
        含 ``node_id``、``type``、``instruction`` 等核心字段的投影字典。
    """
    if not isinstance(node, dict):
        return {}
    return _without_empty(
        {
            "node_id": node.get("node_id") or node.get("step_id"),
            "type": node.get("type"),
            "instruction": node.get("instruction"),
            "expected_user_info": node.get("expected_user_info"),
            "allowed_actions": node.get("allowed_actions"),
            "knowledge_scope": node.get("knowledge_scope"),
        }
    )


def _project_transition(edge: dict[str, Any]) -> dict[str, Any]:
    """投影边的转移信息，提取转移条件和标签。

    Args:
        edge: 技能 DAG 中的边字典。

    Returns:
        含 ``condition`` 和 ``label`` 的投影字典（空值字段被移除）。
    """
    return _without_empty(
        {
            key: edge.get(key)
            for key in (
                "condition",
                "label",
            )
        }
    )


def _short_text(value: object, limit: int) -> str:
    """将值转为文本并截断到指定字符数，压缩多余空白。

    先将值转为字符串并压缩所有连续空白为单个空格，若超过 ``limit`` 字符则
    截断并追加 ``...``。

    Args:
        value: 原始值（会被转为字符串）。
        limit: 最大字符数。

    Returns:
        截断后的文本字符串。
    """
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _optional_text(value: object) -> str | None:
    """将值转为去除首尾空白的字符串，空值返回 ``None``。

    Args:
        value: 原始值。

    Returns:
        去除空白的字符串；若为空则返回 ``None``。
    """
    text = str(value or "").strip()
    return text or None


def _without_empty(value: dict[str, Any]) -> dict[str, Any]:
    """移除字典中值为 ``None``、空字符串、空列表、空字典的字段。

    Args:
        value: 原始字典。

    Returns:
        仅含非空值字段的新字典。
    """
    return {
        key: item
        for key, item in value.items()
        if item is not None and item != "" and item != [] and item != {}
    }
