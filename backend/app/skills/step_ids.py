"""技能图谱节点 / 步骤 ID 去重与修正工具。

LLM 生成的技能图谱中经常出现节点 ID 缺失、重复或引用断裂等问题。
本模块负责对节点 ID 做唯一性保证，并同步修正边、起始节点、终态节点中
对旧 ID 的引用，确保最终 SkillCard 在结构上自洽。
"""

from __future__ import annotations

from typing import Any

from app.skills.skill_schema import SkillCard


def skill_card_with_unique_step_ids(card: SkillCard) -> tuple[SkillCard, list[str]]:
    """确保 SkillCard 中所有节点 ID 唯一，并同步修正图结构中的 ID 引用。

    将 SkillCard 序列化为 JSON 字典后，调用 ``ensure_unique_node_ids`` 去重；
    然后构建旧→新 ID 映射，依次修正 start_node_id、terminal_node_ids 以及
    所有 edge 的 source_node_id / next_node_id。

    Args:
        card: 原始的 SkillCard，可能包含重复或缺失的节点 ID。

    Returns:
        一个二元组 ``(SkillCard, list[str])``：
        - 修正后的 SkillCard（所有节点 ID 唯一且引用自洽）。
        - 警告消息列表，描述每一处被修正 / 补全的节点。
    """
    content = card.model_dump(mode="json")
    # 对节点列表做唯一性去重，拿到去重后的节点数据和对应的修正警告
    nodes, warnings = ensure_unique_node_ids(content.get("nodes", []))
    # 构建原始 ID → 修正后 ID 的映射表，用于同步修正边和入口/出口引用
    id_map = {
        str(original): str(node.get("node_id") or "")
        for original, node in zip(
            [item.get("node_id") for item in content.get("nodes", []) if isinstance(item, dict)],
            nodes,
            strict=False,
        )
        if original and node.get("node_id")
    }
    content["nodes"] = nodes
    if id_map:
        # 修正起始节点 ID：若旧 ID 在映射表中则替换为新 ID
        content["start_node_id"] = id_map.get(content.get("start_node_id"), content.get("start_node_id"))
        # 修正所有终态节点 ID
        content["terminal_node_ids"] = [
            id_map.get(node_id, node_id) for node_id in content.get("terminal_node_ids", [])
        ]
        # 遍历每条边，修正 source 和 next 引用
        for edge in content.get("edges", []):
            if not isinstance(edge, dict):
                continue
            edge["source_node_id"] = id_map.get(edge.get("source_node_id"), edge.get("source_node_id"))
            edge["next_node_id"] = id_map.get(edge.get("next_node_id"), edge.get("next_node_id"))
    return SkillCard.model_validate(content), warnings


def ensure_unique_step_ids(steps: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """确保步骤列表中的 ``step_id`` 唯一。

    本函数是 ``ensure_unique_node_ids`` 的便捷封装，将 ID 字段固定为 ``step_id``，
    标签固定为"步骤"。

    Args:
        steps: 原始步骤列表，元素可为任意类型（非字典元素将被跳过）。

    Returns:
        一个二元组 ``(list, list[str])``：去重后的步骤字典列表和警告列表。
    """
    return ensure_unique_node_ids(steps, id_field="step_id", label="步骤")


def ensure_unique_node_ids(
    nodes: list[Any],
    id_field: str = "node_id",
    label: str = "节点",
) -> tuple[list[dict[str, Any]], list[str]]:
    """确保节点列表中指定字段的值唯一，对重复或缺失的 ID 自动补全。

    对于已有 ID 的节点，若 ID 与已用集合冲突，则在末尾追加 ``_2``、``_3`` 等后缀
    直到唯一；对于没有 ID 的节点，则按 ``node_{index}`` 格式自动生成。
    每一次修正或补全都会在返回的 warnings 列表中生成一条描述。

    Args:
        nodes: 原始节点列表，元素可为任意类型（非字典元素将被跳过）。
        id_field: 用于去重的字段名，默认 ``"node_id"``。
        label: 警告消息中使用的中文标签，如"节点"或"步骤"。

    Returns:
        一个二元组 ``(list[dict], list[str])``：
        - 归一化后的节点字典列表（所有元素的 id_field 字段唯一且非空）。
        - 警告消息列表，每条消息对应一个被修正或补全的节点。
    """
    used: set[str] = set()          # 已使用的 ID 集合，用于检测冲突
    normalized_nodes: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, raw_node in enumerate(nodes):
        if not isinstance(raw_node, dict):
            continue
        node = dict(raw_node)
        original = str(node.get(id_field) or "").strip()
        base = original or f"node_{index + 1}"   # 无 ID 时使用序号作为基础名
        candidate = base
        suffix = 2
        # 冲突时不断追加递增后缀，直到找到一个未使用的候选 ID
        while candidate in used:
            candidate = f"{base}_{suffix}"
            suffix += 1
        if candidate != original:
            if original:
                # ID 存在但重复，记录"修正"
                warnings.append(f"{label} {index + 1} 的 {id_field} 已修正为 `{candidate}`。")
            else:
                # ID 缺失，记录"补全"
                warnings.append(f"{label} {index + 1} 的 {id_field} 已补全为 `{candidate}`。")
            node[id_field] = candidate
        else:
            # ID 无冲突，直接使用原始值
            node[id_field] = original
        used.add(candidate)
        normalized_nodes.append(node)
    return normalized_nodes, warnings
