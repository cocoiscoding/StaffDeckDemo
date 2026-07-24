"""对话上下文构建与压缩（compaction）模块。

本模块负责将完整的对话消息历史转换为可送入 LLM 的上下文窗口，核心解决
"消息量超过 token 预算时如何保留关键信息" 的问题。

核心概念
--------
- **token 预算（token_budget）**：上下文窗口允许的最大 token 数。模块会保证最终
  产出的 ``messages`` 不超过该预算。
- **压缩触发阈值（compaction_trigger）**：当未摘要消息的估算 token 数达到预算的
  ``COMPACTION_TRIGGER_RATIO``（默认 70%）时，触发一次压缩操作。
- **双层摘要策略**：
  - **long_term_summary（长期摘要）**：累积的历史长期信息摘要，每次压缩时将
    旧的长期摘要 + 被移除的更早期消息合并后重新摘要。
  - **medium_term_summary（近期摘要）**：最近一次压缩时被移除的那批消息的摘要。
- **summarized_through_message_id（摘要游标）**：记录已经被摘要处理到的消息 ID，
  下次构建时从该游标之后的消息开始处理，避免重复摘要。
- **RECENT_ROUND_LIMIT**：压缩时始终保留的最近对话轮数（以 user 消息计）。

数据流转
--------
1. 输入原始 ``messages`` 列表 + 上一次的 ``context_state``。
2. ``_normalize_messages`` 过滤非法角色、空内容，补全内部字段（_message_id 等）。
3. ``_messages_after_cursor`` 根据游标切分出"尚未被摘要"的消息子集。
4. ``_project_messages`` 将长期/近期摘要拼装为前置 user 消息，后接近期消息。
5. 若估算 token 超阈值，执行压缩：保留最近 N 轮，其余生成摘要，推进游标。
6. ``_fit_projected_messages`` 做最终裁剪，确保严格不超预算。

与其他模块的关系
----------------
- 被 :mod:`app.core.context_projection` 中的 ``compact_conversation_context`` 调用，
  作为控制上下文压缩的底层实现。
- ``summary_builder`` 回调允许上层注入真实的 LLM 摘要能力；若未提供则退化为
  纯文本截断（``_compact_transcript``）。
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any


# 默认上下文 token 预算（约 32K tokens）
DEFAULT_CONTEXT_TOKEN_BUDGET = 32_000
# 触发压缩的阈值比例：未摘要消息 token 数 >= 预算的 70% 时触发压缩
COMPACTION_TRIGGER_RATIO = 0.70
# 压缩时保留的最近对话轮数（以 user 消息条数计）
RECENT_ROUND_LIMIT = 6
# 长期摘要的 token 预算
LONG_SUMMARY_TOKEN_BUDGET = 4_000
# 近期摘要的 token 预算
MEDIUM_SUMMARY_TOKEN_BUDGET = 4_000
# 允许进入上下文的消息角色白名单（仅保留 user 和 assistant）
ALLOWED_CONTEXT_ROLES = {"user", "assistant"}
# 长期摘要消息的前缀文本，用于在上下文中标识该消息为长期历史摘要
LONG_SUMMARY_PREFIX = "历史的信息可以被总结为："
# 近期摘要消息的前缀文本，用于在上下文中标识该消息为近期历史摘要
MEDIUM_SUMMARY_PREFIX = "近期的历史信息总结为："

# 摘要构建器回调类型：(标签, 待摘要文本, token预算) -> 摘要文本
SummaryBuilder = Callable[[str, str, int], str]


def build_conversation_context(
    messages: list[dict[str, Any]],
    token_budget: int = DEFAULT_CONTEXT_TOKEN_BUDGET,
    *,
    context_state: dict[str, Any] | None = None,
    summary_builder: SummaryBuilder | None = None,
) -> dict[str, object]:
    """根据消息历史和 token 预算构建 LLM 可用的对话上下文。

    该函数是模块的主入口。它会规范化消息、根据游标切分未摘要部分、按需执行
    压缩（生成摘要并推进游标），最终裁剪到预算内并返回完整的上下文结构。

    Args:
        messages: 原始对话消息列表，每条消息应包含 ``role``、``content``，
            可选 ``id``、``created_at``、``images``（仅 user）。
        token_budget: 上下文 token 预算上限，最终产出的消息不会超过此值。
        context_state: 上一次构建返回的 ``context_state``，用于延续摘要游标。
            若为 ``None`` 则视为首次构建。传入后**会被原地修改**。
        summary_builder: 可选的摘要构建回调。若提供则使用其生成摘要文本；
            若不提供则退化为纯文本截断。

    Returns:
        包含以下键的字典：
        - ``messages``: 最终送入 LLM 的消息列表（已裁剪至预算内）。
        - ``compacted_summary``: 长期+近期摘要拼接的纯文本（可能为空）。
        - ``context_state``: 更新后的上下文状态（含游标、摘要等），应持久化供下次使用。
        - ``metadata``: 构建过程的统计元数据（token 估算、消息计数、压缩标记等）。
    """
    # 第一步：规范化消息，过滤非法角色和空内容
    normalized = _normalize_messages(messages)
    # 第二步：规范化上下文状态，补全缺失字段
    state = _normalize_state(context_state)
    # 第三步：根据摘要游标，取出尚未被摘要处理的消息子集
    unsummarized, summarized_count = _messages_after_cursor(normalized, state)
    # 第四步：将已有摘要拼装为前置消息 + 近期消息，形成初始投影
    projected = _project_messages(state, unsummarized)
    # 计算触发压缩的 token 阈值（预算 * 比例，至少为 1）
    trigger_tokens = max(1, math.floor(token_budget * COMPACTION_TRIGGER_RATIO))
    compacted_now = False

    # 当投影后的消息 token 估算达到触发阈值时，执行压缩
    if _messages_tokens(projected) >= trigger_tokens:
        # 取出最近 N 轮对话（以 user 消息为分割），这些将被原样保留
        recent = _recent_rounds(unsummarized, RECENT_ROUND_LIMIT)
        older_count = len(unsummarized) - len(recent)
        older = unsummarized[:older_count]
        if older:
            # 获取已有的历史摘要文本（长期+近期拼接），作为新长期摘要的输入之一
            previous_history = _joined_existing_history(state)
            # 将旧长期摘要 + 更早的消息合并，生成新的长期摘要
            state["long_term_summary"] = _summarize(
                "长期历史信息",
                previous_history,
                LONG_SUMMARY_TOKEN_BUDGET,
                summary_builder,
            )
            # 将本次被移除的那批消息生成近期摘要
            state["medium_term_summary"] = _summarize(
                "近期历史信息",
                _transcript(older),
                MEDIUM_SUMMARY_TOKEN_BUDGET,
                summary_builder,
            )
            # 推进游标到最后一条被摘要的消息 ID
            state["summarized_through_message_id"] = older[-1]["_message_id"]
            # 压缩计数 +1
            state["compaction_count"] = int(state.get("compaction_count") or 0) + 1
            # 未摘要消息缩减为仅保留的最近部分
            unsummarized = recent
            summarized_count += len(older)
            compacted_now = True
            # 用更新后的状态重新投影
            projected = _project_messages(state, unsummarized)

    # 第五步：最终裁剪，确保消息列表严格不超过 token 预算
    projected = _fit_projected_messages(projected, token_budget)
    summary = _joined_existing_history(state)
    return {
        "messages": projected,
        "compacted_summary": summary,
        "context_state": state,
        "metadata": {
            "token_budget": token_budget,
            "compaction_trigger_ratio": COMPACTION_TRIGGER_RATIO,
            "compaction_trigger_tokens": trigger_tokens,
            "estimated_tokens": _messages_tokens(projected),
            "total_messages": len(normalized),
            "included_messages": len(projected),
            "omitted_messages": summarized_count,
            "compacted": bool(summary),
            "compacted_now": compacted_now,
            "long_term_summary": bool(state.get("long_term_summary")),
            "medium_term_summary": bool(state.get("medium_term_summary")),
            "recent_round_limit": RECENT_ROUND_LIMIT,
            "current_turn_time": normalized[-1].get("_created_at") if normalized else None,
        },
    }


def _normalize_state(value: dict[str, Any] | None) -> dict[str, Any]:
    """规范化上下文状态字典，确保所有必需字段存在且类型正确。

    Args:
        value: 原始状态字典，可能为 ``None`` 或缺少某些字段。

    Returns:
        包含完整字段的干净状态字典：
        - ``long_term_summary``: 长期摘要文本（str，可能为空）。
        - ``medium_term_summary``: 近期摘要文本（str，可能为空）。
        - ``summarized_through_message_id``: 摘要游标消息 ID（str，可能为空）。
        - ``compaction_count``: 压缩次数（int，>= 0）。
    """
    source = value if isinstance(value, dict) else {}
    return {
        "long_term_summary": str(source.get("long_term_summary") or "").strip(),
        "medium_term_summary": str(source.get("medium_term_summary") or "").strip(),
        "summarized_through_message_id": str(
            source.get("summarized_through_message_id") or ""
        ).strip(),
        "compaction_count": max(0, int(source.get("compaction_count") or 0)),
    }


def _normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """规范化原始消息列表，过滤无效消息并补全内部追踪字段。

    处理规则：
    - 仅保留角色在 ``ALLOWED_CONTEXT_ROLES``（user/assistant）中的消息。
    - 丢弃内容为空的消息。
    - 为每条消息补全 ``_message_id``（优先使用原始 id，否则按序号生成）和
      ``_created_at``（统一转为字符串）。
    - 仅 user 角色保留 ``images`` 字段（若存在且非空）。

    Args:
        messages: 原始消息列表。

    Returns:
        规范化后的消息列表，每条消息含 ``role``、``content``、``_message_id``、
        ``_created_at``，user 消息可能额外含 ``images``。
    """
    normalized: list[dict[str, Any]] = []
    for index, message in enumerate(messages):
        role = str(message.get("role") or "").strip()
        content = str(message.get("content") or "").strip()
        # 跳过角色不在白名单内或内容为空的消息
        if role not in ALLOWED_CONTEXT_ROLES or not content:
            continue
        normalized_message: dict[str, Any] = {
            "role": role,
            "content": content,
            "_message_id": str(message.get("id") or f"context_message_{index}"),
            "_created_at": _string_time(message.get("created_at")),
        }
        images = message.get("images")
        # 仅 user 消息携带图片附件
        if role == "user" and isinstance(images, list) and images:
            normalized_message["images"] = images
        normalized.append(normalized_message)
    return normalized


def _messages_after_cursor(
    messages: list[dict[str, Any]], state: dict[str, Any]
) -> tuple[list[dict[str, Any]], int]:
    """根据摘要游标将消息列表切分为"已摘要"和"未摘要"两部分。

    通过 ``summarized_through_message_id`` 定位游标位置，返回游标之后的消息。
    若游标消息在当前列表中找不到（可能历史被替换/截断），则重置整个状态
    以避免摘要与实际消息不一致。

    Args:
        messages: 规范化后的完整消息列表。
        state: 上下文状态（会被原地修改以重置游标）。

    Returns:
        二元组 ``(unsummarized_messages, summarized_count)``：
        - ``unsummarized_messages``: 游标之后的消息列表（需要进入上下文的部分）。
        - ``summarized_count``: 已被摘要的消息数量。
    """
    cursor = str(state.get("summarized_through_message_id") or "")
    # 无游标表示从未压缩过，全部消息都未摘要
    if not cursor:
        return messages, 0
    for index, message in enumerate(messages):
        if message["_message_id"] == cursor:
            # 返回游标消息之后的所有消息
            return messages[index + 1 :], index + 1
    # 游标消息不存在于当前列表，说明底层历史已被替换，安全地重置状态后重建
    state["long_term_summary"] = ""
    state["medium_term_summary"] = ""
    state["summarized_through_message_id"] = ""
    state["compaction_count"] = 0
    return messages, 0


def _project_messages(
    state: dict[str, Any], recent: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """将摘要状态 + 近期消息投影为最终送入 LLM 的消息列表。

    若存在任何摘要（长期或近期），则在消息列表头部插入两条 user 角色的摘要消息，
    随后追加近期原始消息。摘要消息以特定前缀标识，便于 LLM 理解其性质。

    Args:
        state: 上下文状态，用于读取长期/近期摘要。
        recent: 近期未摘要的消息列表。

    Returns:
        投影后的消息列表，可能包含 0~2 条摘要前置消息 + 近期消息。
    """
    projected: list[dict[str, Any]] = []
    long_summary = str(state.get("long_term_summary") or "").strip()
    medium_summary = str(state.get("medium_term_summary") or "").strip()
    # 长期摘要前置消息：只要有任何摘要就插入（缺失时用占位文本）
    if long_summary or medium_summary:
        projected.append(
            {
                "role": "user",
                "content": f"{LONG_SUMMARY_PREFIX}\n{long_summary or '暂无长期历史摘要。'}",
            }
        )
    # 近期摘要前置消息：同样只要有任何摘要就插入
    if long_summary or medium_summary:
        projected.append(
            {
                "role": "user",
                "content": f"{MEDIUM_SUMMARY_PREFIX}\n{medium_summary or '暂无近期历史摘要。'}",
            }
        )
    # 追加近期原始消息（仅保留对外字段）
    projected.extend(_public_message(message) for message in recent)
    return projected


def _public_message(message: dict[str, Any]) -> dict[str, Any]:
    """提取消息中对 LLM 可见的公共字段，剥离内部追踪字段。

    Args:
        message: 含内部字段（``_message_id`` 等）的规范化消息。

    Returns:
        仅包含 ``role``、``content``、``images``（如有）的干净消息字典。
    """
    return {
        key: value
        for key, value in message.items()
        if key in {"role", "content", "images"}
    }


def _recent_rounds(
    messages: list[dict[str, Any]], round_limit: int
) -> list[dict[str, Any]]:
    """从消息列表尾部截取最近指定轮数的对话。

    "一轮"以一条 user 消息为标志。例如 round_limit=6 表示保留最后 6 个 user
    消息及其后的所有 assistant 回复。

    Args:
        messages: 消息列表。
        round_limit: 要保留的最近 user 消息轮数。

    Returns:
        截取后的消息子列表。若 user 消息总数不超过 round_limit 则原样返回。
    """
    # 收集所有 user 消息的索引位置
    user_indexes = [
        index for index, message in enumerate(messages) if message.get("role") == "user"
    ]
    # user 消息数不足限制值，无需截取
    if len(user_indexes) <= round_limit:
        return messages
    # 从倒数第 round_limit 个 user 消息处开始截取
    return messages[user_indexes[-round_limit] :]


def _joined_existing_history(state: dict[str, Any]) -> str:
    """将长期摘要和近期摘要拼接为单一文本。

    用于生成 ``compacted_summary`` 返回值，以及作为新一轮长期摘要的输入文本。

    Args:
        state: 上下文状态。

    Returns:
        长期摘要与近期摘要以换行拼接的文本；两者皆空时返回空字符串。
    """
    return "\n".join(
        value
        for value in (
            str(state.get("long_term_summary") or "").strip(),
            str(state.get("medium_term_summary") or "").strip(),
        )
        if value
    )


def _summarize(
    label: str,
    source: str,
    token_budget: int,
    summary_builder: SummaryBuilder | None,
) -> str:
    """生成摘要文本，优先使用 LLM 回调，失败时退化为文本截断。

    Args:
        label: 摘要类别标签（如"长期历史信息"），传给 summary_builder。
        source: 待摘要的原始文本。
        token_budget: 摘要的 token 预算上限。
        summary_builder: 可选的摘要回调。若为 ``None`` 或调用异常，
            则退化为 :func:`_compact_transcript` 纯文本压缩。

    Returns:
        摘要文本。若 source 为空则返回空字符串。
    """
    if not source.strip():
        return ""
    # 优先尝试使用外部提供的摘要构建器（通常是 LLM 摘要）
    if summary_builder:
        try:
            summary = str(summary_builder(label, source, token_budget) or "").strip()
            if summary:
                # 确保摘要不超过预算
                return _trim_text_to_tokens(summary, token_budget)
        except Exception:
            # 摘要构建器异常时静默降级，不阻断主流程
            pass
    # 降级路径：纯文本截断式压缩
    return _compact_transcript(source, token_budget)


def _transcript(messages: list[dict[str, Any]]) -> str:
    """将消息列表转换为纯文本对话记录格式。

    每条消息格式为 ``role: content``，content 内部的多余空白被压缩为单个空格。

    Args:
        messages: 消息列表。

    Returns:
        换行分隔的对话文本。
    """
    return "\n".join(
        f"{message['role']}: {' '.join(str(message['content']).split())}"
        for message in messages
    )


def _compact_transcript(source: str, token_budget: int) -> str:
    """通过逐行累积的方式将文本压缩到 token 预算内。

    这是一种降级的纯文本压缩策略：逐行加入文本，一旦累积 token 估算超过预算
    就停止，保证不超限。

    Args:
        source: 原始文本。
        token_budget: token 预算上限。

    Returns:
        压缩后的文本，末尾可能带 ``...`` 省略标记。
    """
    # 规范化每行：压缩多余空白，丢弃空行
    lines = [" ".join(line.split()) for line in source.splitlines() if line.strip()]
    selected: list[str] = []
    for line in lines:
        candidate = "\n".join([*selected, line])
        # 已有内容且加入新行后超预算，则停止累积
        if selected and _estimate_tokens(candidate) > token_budget:
            break
        selected.append(line)
    result = "\n".join(selected)
    # 兜底：若 selected 为空（首行就超限），则对原始文本做硬截断
    return _trim_text_to_tokens(result or source, token_budget)


def _fit_projected_messages(
    messages: list[dict[str, Any]], token_budget: int
) -> list[dict[str, Any]]:
    """将投影后的消息列表裁剪到 token 预算内。

    策略：
    1. 优先从前面移除近期消息（保留摘要前置消息和最后一条消息）。
    2. 若仅剩摘要 + 最后一条仍超限，则对最后一条消息做内容截断。

    Args:
        messages: 投影后的消息列表。
        token_budget: token 预算上限。

    Returns:
        裁剪后的消息列表。
    """
    projected = list(messages)
    # 统计头部摘要消息的数量（最多 2 条），这些消息在裁剪时被保留
    summary_count = sum(
        1
        for message in projected[:2]
        if str(message.get("content") or "").startswith(
            (LONG_SUMMARY_PREFIX, MEDIUM_SUMMARY_PREFIX)
        )
    )
    # 循环移除摘要消息之后、最后一条消息之前的近期消息，直到不超限或无可移除
    while len(projected) > summary_count + 1 and _messages_tokens(projected) > token_budget:
        projected.pop(summary_count)
    # 若仅剩摘要 + 最后一条仍超限，则截断最后一条消息的内容
    if projected and _messages_tokens(projected) > token_budget:
        last = projected[-1]
        remaining = max(1, token_budget - _messages_tokens(projected[:-1]))
        projected[-1] = _trim_message(last, remaining)
    return projected


def _trim_message(message: dict[str, Any], token_budget: int) -> dict[str, Any]:
    """截断单条消息的 content 以满足 token 预算。

    Args:
        message: 原始消息字典。
        token_budget: 该消息允许的最大 token 数。

    Returns:
        新的消息字典，content 被截断（可能带 ``...``）。
    """
    # 为 role 标签和 JSON 结构开销预留 token（约 6 个）
    content_budget = max(1, token_budget - _estimate_tokens(str(message["role"])) - 6)
    return {**message, "content": _trim_text_to_tokens(str(message["content"]), content_budget)}


def _trim_text_to_tokens(text: str, token_budget: int) -> str:
    """将文本截断到 token 预算内（基于 UTF-8 字节估算）。

    使用 4 字节 ≈ 1 token 的粗略估算。截断后追加 ``...`` 省略标记。

    Args:
        text: 原始文本。
        token_budget: token 预算上限。

    Returns:
        截断后的文本。若未超限则原样返回。
    """
    if _estimate_tokens(text) <= token_budget:
        return text
    encoded = text.encode("utf-8")
    # 1 token ≈ 4 字节，据此计算字节截断量
    byte_budget = max(4, token_budget * 4)
    # 截断后用 ignore 模式解码，避免 UTF-8 多字节字符被截断一半导致解码错误
    trimmed = encoded[:byte_budget].decode("utf-8", errors="ignore").rstrip()
    return f"{trimmed}..."


def _messages_tokens(messages: list[dict[str, Any]]) -> int:
    """计算消息列表的总 token 估算值。

    Args:
        messages: 消息列表。

    Returns:
        所有消息 token 估算值之和。
    """
    return sum(_message_tokens(message) for message in messages)


def _message_tokens(message: dict[str, Any]) -> int:
    """估算单条消息的 token 数。

    token = role 文本 token + content 文本 token + 结构开销（6）。

    Args:
        message: 消息字典。

    Returns:
        token 估算值（至少为 1）。
    """
    return _estimate_tokens(str(message["role"])) + _estimate_tokens(
        str(message["content"])
    ) + 6


def _estimate_tokens(text: str) -> int:
    """基于 UTF-8 字节数粗略估算文本的 token 数。

    采用 4 字节 ≈ 1 token 的经验比例，适用于中英文混合文本的快速估算。
    精度足以用于预算控制决策，无需依赖 tokenizer。

    Args:
        text: 待估算的文本。

    Returns:
        token 估算值，至少为 1。
    """
    return max(1, math.ceil(len(text.encode("utf-8")) / 4))


def _string_time(value: object) -> str | None:
    """将时间值统一转换为字符串表示。

    若输入是 datetime 等具有 ``isoformat`` 方法的对象，则调用其 ``isoformat()``；
    否则转为字符串。空值返回 ``None``。

    Args:
        value: 原始时间值（可能是 datetime、字符串、None 等）。

    Returns:
        ISO 格式时间字符串，或 ``None``。
    """
    if value is None:
        return None
    # 优先使用 datetime 对象的 isoformat 方法
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    text = str(value).strip()
    return text or None
