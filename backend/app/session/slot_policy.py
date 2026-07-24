"""会话槽位策略模块。

定义 Router（路由决策器）生成的用户消息类槽位过滤规则。
Router 在分析用户输入时可能产生对消息的改写/归一化结果，
但这些中间产物不应作为技能（Skill）槽位被持久化到会话状态中，
否则会污染后续技能步骤的槽位上下文。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


# Router 在分析过程中可能生成的与用户消息文本相关的槽位键名集合。
# 这些键对应的值是 Router 对用户输入的改写/归一化版本，
# 不应被保留到技能槽位（Skill slots）中。
ROUTER_GENERATED_MESSAGE_SLOT_KEYS = {
    "message_content",
    "user_message",
    "rewritten_message",
    "normalized_message",
    "current_message",
    "source_message",
}


def strip_router_generated_message_slots(slots: Mapping[str, Any] | None) -> dict[str, Any]:
    """从槽位字典中移除 Router 生成的用户消息类槽位。

    Router 对用户消息的改写、归一化等中间产物不应作为技能槽位持久化，
    本函数负责将这些键过滤掉，只保留真正的业务槽位。

    Args:
        slots: 原始槽位映射，可能为 None 或非 Mapping 类型。

    Returns:
        过滤后的槽位字典，不包含 ``ROUTER_GENERATED_MESSAGE_SLOT_KEYS`` 中的任何键。
    """
    if not isinstance(slots, Mapping):
        return {}
    return {
        str(key): value
        for key, value in slots.items()
        if str(key).strip() not in ROUTER_GENERATED_MESSAGE_SLOT_KEYS
    }
