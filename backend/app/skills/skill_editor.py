"""技能编辑器（Skill Editor）模块。

本模块负责在已有技能基础上，根据用户的自然语言指令进行局部或整体改写。
支持以下特性：
- 局部改写（patches）：仅修改指定路径的字段（如 basic 字段、特定节点等）。
- 整体改写（all）：替换整个技能草稿。
- 工具建议解析与未配置工具的自动移除。
- 改写后自动进行反思校验。
- 流式输出支持。

改写过程：
1. 将当前技能、改写指令、目标路径和上下文提交给 LLM。
2. 模型可以返回 patches（局部修改）或完整 draft_skill（整体修改）。
3. 应用 patches 并做目标路径合并——只合并用户指定的部分。
4. 移除引用了未配置工具的 allowed_actions。
5. 交由反思模块做多轮校验。
"""

from __future__ import annotations

import json
import re
from time import sleep
from typing import Any, Iterator

from app import paths
from app.db.models import ModelConfig
from app.llm import LLMClient, LLMError
from app.skills.llm_limits import skill_model_config
from app.skills.skill_reflection import reflect_skill_response, reflect_skill_response_stream
from app.skills.skill_schema import SkillCard, SkillRewriteRequest, SkillRewriteResponse
from app.skills.skill_distiller import (
    _compact_warnings,
    _normalize_tool_suggestions,
    _remove_unknown_tool_actions,
    _tool_action_names_from_suggestions,
    _tool_resolution_warnings,
)
from app.skills.step_ids import skill_card_with_unique_step_ids


# 技能编辑提示词模板路径
PROMPT_PATH = paths.resource_dir() / "app" / "llm" / "prompts" / "skill_editor_prompt.md"
# 流式输出时每个 chunk 之间的间隔（秒）
STREAM_INTERVAL_SECONDS = 0.035
# 技能 basic 字段集合——这些字段可以通过 patch 路径 ``basic.xxx`` 修改
BASIC_FIELDS = {
    "name",
    "version",
    "business_domain",
    "description",
    "trigger_intents",
    "user_utterance_examples",
    "goal",
    "required_info",
    "slot_filling_policy",
    "interruption_policy",
    "response_rules",
}
# 技能节点字段集合——这些字段可以通过 patch 路径 ``nodes[i].xxx`` 修改
NODE_FIELDS = {
    "node_id",
    "type",
    "name",
    "instruction",
    "optional",
    "condition",
    "expected_user_info",
    "allowed_actions",
    "knowledge_scope",
    "retry_policy",
    "metadata",
}


class SkillEditor:
    """技能编辑器——根据用户指令对已有技能进行局部或整体改写。"""

    def rewrite(self, request: SkillRewriteRequest, model_config: ModelConfig) -> SkillRewriteResponse:
        """对已有技能执行改写（非流式版本）。

        Args:
            request: 技能改写请求。
            model_config: 模型配置。

        Returns:
            改写后的技能响应（含反思校验结果）。
        """
        client = LLMClient(skill_model_config(model_config))
        payload = self._payload(request)
        raw = client.generate_json(PROMPT_PATH.read_text(encoding="utf-8"), payload)
        response = self._normalize_response(raw, request)
        return reflect_skill_response(
            client=client,
            source_kind="rewrite",
            source_payload=payload,
            response=response,
            candidate_skill=response.draft_skill,
            current_warnings=response.warnings,
            tool_suggestions=response.tool_suggestions,
            normalize_response=lambda review_raw: self._normalize_response(review_raw, request),
        )

    def stream_text(
        self, request: SkillRewriteRequest, model_config: ModelConfig
    ) -> Iterator[dict[str, object]]:
        """对已有技能执行改写（流式版本），逐步 yield 事件。

        流程：
        1. 流式生成模型输出（chunk 事件）。
        2. 解析模型输出为响应。
        3. 若解析失败则尝试一次修复。
        4. 进入反思校验流程（status 事件）。
        5. 逐块输出 assistant_message（message_chunk 事件）。
        6. 最终返回完整响应（complete 事件）。

        Args:
            request: 技能改写请求。
            model_config: 模型配置。

        Yields:
            dict: 事件字典，类型包括 ``status``、``message_chunk``、``complete``。
        """
        chunks: list[str] = []
        prompt = PROMPT_PATH.read_text(encoding="utf-8")
        payload = self._payload(request)
        client = LLMClient(skill_model_config(model_config))
        try:
            yield {"event": "status", "data": {"text": "模型正在分析改写范围"}}
            for chunk in client.generate_text_stream(prompt, payload):
                chunks.append(chunk)
            yield {"event": "status", "data": {"text": "正在校验局部改写结果"}}
            response = self._response_from_text("".join(chunks), request)
        except (LLMError, json.JSONDecodeError, TypeError, ValueError) as exc:
            # 模型输出解析失败，尝试一次修复
            try:
                yield {"event": "status", "data": {"text": "模型输出需要修复，正在重试一次"}}
                repair_text = client.generate_text(
                    prompt,
                    {
                        **payload,
                        "previous_output": "".join(chunks),
                        "previous_error": str(exc),
                        "repair_instruction": (
                            "请基于 current_skill、instruction 和 target_paths 修复上一次输出。"
                            "只输出合法 JSON，可以使用 patches 做局部修改，或返回完整 draft_skill。"
                        ),
                    },
                )
                response = self._response_from_text(repair_text, request)
            except (LLMError, json.JSONDecodeError, TypeError, ValueError) as repair_exc:
                # 修复也失败，保留原始技能
                yield {"event": "status", "data": {"text": "模型改写失败，正在保留原版本"}}
                response = SkillRewriteResponse(
                    draft_skill=request.current_skill,
                    assistant_message="改写失败，已保留当前技能内容。",
                    changed_paths=[],
                    warnings=[f"模型未能完成局部改写：{repair_exc}"],
                )
        yield {"event": "status", "data": {"text": "正在校验改写范围与工具接入"}}
        response = yield from reflect_skill_response_stream(
            client=client,
            source_kind="rewrite",
            source_payload=payload,
            response=response,
            candidate_skill=response.draft_skill,
            current_warnings=response.warnings,
            tool_suggestions=response.tool_suggestions,
            normalize_response=lambda review_raw: self._normalize_response(review_raw, request),
        )
        yield {"event": "status", "data": {"text": "正在整理校验后的改写结果"}}
        # 逐块输出 assistant_message
        for chunk in _chunk_text(response.assistant_message):
            yield {"event": "message_chunk", "data": {"content": chunk}}
            sleep(STREAM_INTERVAL_SECONDS)
        yield {"event": "complete", "data": response.model_dump(mode="json")}

    def _response_from_text(self, text: str, request: SkillRewriteRequest) -> SkillRewriteResponse:
        """从模型输出的原始文本解析响应。

        Args:
            text: 模型输出的原始文本。
            request: 技能改写请求。

        Returns:
            解析后的改写响应。

        Raises:
            ValueError: 模型输出不是 JSON object 时。
        """
        raw = json.loads(_extract_json(text))
        if not isinstance(raw, dict):
            raise ValueError("模型输出不是 JSON object")
        return self._normalize_response(raw, request)

    def _payload(self, request: SkillRewriteRequest) -> dict[str, Any]:
        """构建提交给 LLM 的负载字典。

        Args:
            request: 技能改写请求。

        Returns:
            包含当前技能、改写指令、目标路径和上下文的负载字典。
        """
        return {
            "current_skill": request.current_skill.model_dump(mode="json"),
            "instruction": request.instruction,
            "target_path": request.target_path,
            "target_paths": _target_paths(request),
            "target_label": request.target_label,
            "conversation": request.conversation[-12:],  # 仅取最近 12 条对话
            "available_tools": request.available_tools,
        }

    def _normalize_response(
        self, raw: dict[str, Any], request: SkillRewriteRequest
    ) -> SkillRewriteResponse:
        """将模型返回的原始字典归一化为标准 SkillRewriteResponse。

        处理步骤：
        1. 尝试从 raw 中提取 patches 并应用到当前技能（局部修改）。
        2. 若无 patches 则使用 raw 中的 draft_skill（整体修改）。
        3. 将候选技能与当前技能做目标路径合并——只替换用户指定的部分。
        4. 移除引用了未配置工具的 allowed_actions。
        5. 确保节点 ID 唯一。
        6. 整合警告和工具建议。

        Args:
            raw: 模型返回的原始字典。
            request: 技能改写请求。

        Returns:
            归一化后的 SkillRewriteResponse。
        """
        target_paths = _target_paths(request)
        # 尝试从 patches 应用局部修改
        patched = _skill_from_patches(raw, request, target_paths)
        # 若 patches 有效则使用 patches 结果，否则使用模型返回的 draft_skill 或 raw 本身
        draft = (
            patched.model_dump(mode="json")
            if patched is not None
            else raw.get("draft_skill")
            if isinstance(raw.get("draft_skill"), dict)
            else raw
        )
        candidate = SkillCard.model_validate(draft)
        # 将候选技能与当前技能按目标路径合并
        merged = _merge_targets(request.current_skill, candidate, target_paths)
        merged_data = merged.model_dump(mode="json")
        raw_tool_mentions = raw.get("tool_mentions") if isinstance(raw.get("tool_mentions"), list) else raw.get("tool_suggestions")
        tool_resolutions = _normalize_tool_suggestions(raw_tool_mentions, request, [])
        # 移除引用了未配置工具的 allowed_actions
        nodes, missing_tool_names = _remove_unknown_tool_actions(
            [node for node in merged_data.get("nodes", []) if isinstance(node, dict)],
            request.available_tools,
            _tool_action_names_from_suggestions(tool_resolutions),
        )
        if nodes:
            merged_data["nodes"] = nodes
            merged = SkillCard.model_validate(merged_data)
        # 确保节点 ID 唯一
        merged, id_warnings = skill_card_with_unique_step_ids(merged)
        assistant_message = str(raw.get("assistant_message") or "已完成选中部分的改写。").strip()
        warnings = [str(item) for item in raw.get("warnings", []) if str(item).strip()]
        warnings.extend(warning for warning in id_warnings if warning not in warnings)
        # 为每个缺失工具生成警告
        for tool_name in missing_tool_names:
            warning = (
                f"改写结果引用了未配置工具 {tool_name}，已移出 allowed_actions；"
                "如确需该工具，模型必须在 tool_mentions 中提供来自上下文的完整工具提及。"
            )
            if warning not in warnings:
                warnings.append(warning)
        warnings = _compact_warnings(warnings)
        # 计算 changed_paths：若模型未提供则自动推断
        changed_paths = [str(item) for item in raw.get("changed_paths", []) if str(item).strip()]
        if not changed_paths and merged.model_dump() != request.current_skill.model_dump():
            changed_paths = _changed_paths(request.current_skill, merged)
        # 若有缺失工具，重新解析工具建议（传入缺失名单以获取更详细的信息）
        if missing_tool_names:
            tool_resolutions = _normalize_tool_suggestions(raw_tool_mentions, request, missing_tool_names)
        warnings = _compact_warnings([*warnings, *_tool_resolution_warnings(tool_resolutions)])
        tool_suggestions = [
            item for item in tool_resolutions if item.resolution_status in {"existing", "new_candidate"}
        ]
        return SkillRewriteResponse(
            draft_skill=merged,
            assistant_message=assistant_message,
            changed_paths=changed_paths,
            warnings=warnings,
            tool_suggestions=tool_suggestions,
        )


def _target_paths(request: SkillRewriteRequest) -> list[str]:
    """从请求中解析规范化的改写目标路径列表。

    处理逻辑：
    1. 优先使用 ``target_paths`` 字段。
    2. 若为空则回退到 ``target_path`` 字段。
    3. 若列表中包含 ``"all"`` 则直接返回 ``["all"]``（整体改写）。
    4. 否则去重后返回。

    Args:
        request: 技能改写请求。

    Returns:
        规范化后的目标路径列表。
    """
    paths = [path.strip() for path in request.target_paths if path.strip()]
    if not paths:
        paths = [request.target_path.strip() or "all"]
    if "all" in paths:
        return ["all"]
    deduped: list[str] = []
    for path in paths:
        if path not in deduped:
            deduped.append(path)
    return deduped or ["all"]


def _skill_from_patches(
    raw: dict[str, Any],
    request: SkillRewriteRequest,
    target_paths: list[str],
) -> SkillCard | None:
    """从模型返回的 patches 列表中应用局部修改，返回修改后的 SkillCard。

    patches 是一种轻量的局部修改方式，每个 patch 包含 ``path`` 和 ``value``。
    仅允许修改目标路径范围内的字段，越界的 patch 会被忽略并记录警告。

    Args:
        raw: 模型返回的原始字典。
        request: 技能改写请求。
        target_paths: 允许修改的目标路径列表。

    Returns:
        应用 patches 后的 SkillCard；若无 patches 或全部被忽略则返回 None。
    """
    patches = raw.get("patches")
    if not isinstance(patches, list):
        return None
    data = request.current_skill.model_dump(mode="json")
    applied = False
    ignored_paths: list[str] = []
    for item in patches:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        # 检查 patch 路径是否在允许的目标范围内
        if not _patch_allowed(data, path, target_paths):
            ignored_paths.append(path)
            continue
        if _apply_patch(data, path, item.get("value")):
            applied = True
    # 将越界 patch 的警告追加到 raw 中
    if ignored_paths:
        warnings = raw.get("warnings")
        if not isinstance(warnings, list):
            warnings = []
            raw["warnings"] = warnings
        warnings.append(f"已忽略越界改写路径：{', '.join(ignored_paths)}")
    if not applied:
        return None
    return SkillCard.model_validate(data)


def _patch_allowed(data: dict[str, Any], path: str, target_paths: list[str]) -> bool:
    """检查单个 patch 路径是否在允许的改写目标范围内。

    Args:
        data: 当前技能的 JSON 字典。
        path: patch 路径，如 ``basic.name``、``nodes[0].instruction``。
        target_paths: 用户指定的改写目标列表。

    Returns:
        允许修改返回 True，否则返回 False。
    """
    if "all" in target_paths:
        return _patch_path_is_known(data, path)
    if _basic_patch_field(path):
        return "basic" in target_paths
    if path == "nodes":
        return any(_is_node_target(target) for target in target_paths)
    node_index = _patch_node_index(data, path)
    if node_index is None:
        return False
    nodes = [node for node in data.get("nodes", []) if isinstance(node, dict)]
    node_id = str(nodes[node_index].get("node_id") or "")
    # 允许通过索引或节点 ID 引用
    return f"nodes[{node_index}]" in target_paths or f"nodes.{node_id}" in target_paths


def _patch_path_is_known(data: dict[str, Any], path: str) -> bool:
    """检查 patch 路径是否指向一个已知的字段（basic、nodes 或节点子字段）。

    Args:
        data: 当前技能的 JSON 字典。
        path: patch 路径。

    Returns:
        路径已知返回 True，否则返回 False。
    """
    return bool(_basic_patch_field(path)) or path == "nodes" or _patch_node_index(data, path) is not None


def _apply_patch(data: dict[str, Any], path: str, value: Any) -> bool:
    """将单个 patch 的值应用到技能数据字典中。

    支持三种路径模式：
    - ``basic.xxx``：设置 basic 字段值。
    - ``nodes``：替换整个节点列表。
    - ``nodes[i].xxx`` / ``nodes.node_id.xxx``：设置单个节点的字段值。

    Args:
        data: 技能的 JSON 字典（会被原地修改）。
        path: patch 路径。
        value: 要设置的值。

    Returns:
        成功应用返回 True，路径无法识别返回 False。
    """
    basic_field = _basic_patch_field(path)
    if basic_field:
        data[basic_field] = value
        return True
    if path == "nodes" and isinstance(value, list):
        data["nodes"] = value
        return True
    node_index = _patch_node_index(data, path)
    if node_index is None:
        return False
    node_field = _patch_node_field(path)
    nodes = [node for node in data.get("nodes", []) if isinstance(node, dict)]
    if not (0 <= node_index < len(nodes)):
        return False
    if node_field is None:
        # 整个节点替换
        if not isinstance(value, dict):
            return False
        nodes[node_index] = value
    else:
        # 单个字段替换
        nodes[node_index][node_field] = value
    data["nodes"] = nodes
    return True


def _basic_patch_field(path: str) -> str | None:
    """从 patch 路径中提取 basic 字段名。

    路径格式为 ``basic.xxx`` 或 ``xxx``（不带前缀时也尝试匹配）。

    Args:
        path: patch 路径。

    Returns:
        匹配到的 BASIC_FIELDS 中的字段名，不匹配则返回 None。
    """
    normalized = path.removeprefix("basic.")
    return normalized if normalized in BASIC_FIELDS else None


def _patch_node_index(data: dict[str, Any], path: str) -> int | None:
    """从 patch 路径中解析节点索引。

    支持两种路径格式：
    - ``nodes[i]`` 或 ``nodes[i].field``：通过数字索引引用。
    - ``nodes.node_id`` 或 ``nodes.node_id.field``：通过节点 ID 引用。

    Args:
        data: 技能的 JSON 字典。
        path: patch 路径。

    Returns:
        节点在列表中的索引；路径不匹配或索引越界时返回 None。
    """
    nodes = [node for node in data.get("nodes", []) if isinstance(node, dict)]
    # 尝试匹配 nodes[i] 或 nodes[i].field 格式
    bracket_match = re.fullmatch(r"nodes\[(\d+)\](?:\.[A-Za-z_][A-Za-z0-9_]*)?", path)
    if bracket_match:
        index = int(bracket_match.group(1))
        return index if 0 <= index < len(nodes) else None
    # 尝试匹配 nodes.node_id 或 nodes.node_id.field 格式
    dot_match = re.fullmatch(r"nodes\.([^.]+)(?:\.[A-Za-z_][A-Za-z0-9_]*)?", path)
    if not dot_match:
        return None
    node_id = dot_match.group(1)
    return next((index for index, node in enumerate(nodes) if str(node.get("node_id") or "") == node_id), None)


def _patch_node_field(path: str) -> str | None:
    """从 patch 路径中提取节点子字段名。

    Args:
        path: patch 路径。

    Returns:
        匹配到的 NODE_FIELDS 中的字段名，不匹配则返回 None。
    """
    bracket_match = re.fullmatch(r"nodes\[\d+\]\.([A-Za-z_][A-Za-z0-9_]*)", path)
    dot_match = re.fullmatch(r"nodes\.[^.]+\.([A-Za-z_][A-Za-z0-9_]*)", path)
    field = bracket_match.group(1) if bracket_match else dot_match.group(1) if dot_match else None
    return field if field in NODE_FIELDS else None


def _merge_targets(current: SkillCard, candidate: SkillCard, target_paths: list[str]) -> SkillCard:
    """将候选技能按目标路径合并到当前技能中。

    合并策略：
    - ``all``：直接使用候选技能。
    - 节点结构发生变化（增删或重排）时：替换整个节点/边结构。
    - ``basic``：仅替换 basic 字段。
    - ``nodes[i]`` / ``nodes.node_id``：仅替换指定节点。

    Args:
        current: 当前技能。
        candidate: 候选技能（改写后的）。
        target_paths: 目标路径列表。

    Returns:
        合并后的技能。
    """
    if "all" in target_paths:
        return candidate
    # 检查节点结构是否发生了变化（增删或重排）
    if _has_node_structure_change(current, candidate, target_paths):
        current_data = current.model_dump(mode="json")
        candidate_data = candidate.model_dump(mode="json")
        current_data["nodes"] = [
            node for node in candidate_data.get("nodes", []) if isinstance(node, dict)
        ]
        current_data["edges"] = [edge for edge in candidate_data.get("edges", []) if isinstance(edge, dict)]
        current_data["start_node_id"] = candidate_data.get("start_node_id") or current_data.get("start_node_id")
        current_data["terminal_node_ids"] = candidate_data.get("terminal_node_ids") or current_data.get("terminal_node_ids")
        # 若目标中包含 basic，则同时替换 basic 字段
        if "basic" in target_paths:
            for field in BASIC_FIELDS:
                if field in candidate_data:
                    current_data[field] = candidate_data[field]
        return SkillCard.model_validate(current_data)
    # 逐路径合并
    merged = current
    for path in target_paths:
        merged = _merge_target(merged, candidate, path)
    return merged


def _has_node_structure_change(current: SkillCard, candidate: SkillCard, target_paths: list[str]) -> bool:
    """检查候选技能相对于当前技能是否发生了节点结构变化。

    结构变化包括：
    - 节点数量不同（增删节点）。
    - 节点 ID 集合相同但顺序不同（重排节点）。

    Args:
        current: 当前技能。
        candidate: 候选技能。
        target_paths: 目标路径列表。

    Returns:
        发生结构变化返回 True，否则返回 False。
    """
    if not any(_is_node_target(path) for path in target_paths):
        return False
    current_nodes = [node for node in current.model_dump(mode="json").get("nodes", []) if isinstance(node, dict)]
    candidate_nodes = [node for node in candidate.model_dump(mode="json").get("nodes", []) if isinstance(node, dict)]
    if len(candidate_nodes) != len(current_nodes):
        return True
    current_ids = [str(node.get("node_id") or "") for node in current_nodes]
    candidate_ids = [str(node.get("node_id") or "") for node in candidate_nodes]
    # ID 集合相同但顺序不同 = 重排
    return sorted(current_ids) == sorted(candidate_ids) and current_ids != candidate_ids


def _is_node_target(path: str) -> bool:
    """判断目标路径是否指向节点。

    Args:
        path: 目标路径。

    Returns:
        指向节点返回 True。
    """
    return path.startswith("nodes.") or path.startswith("nodes[")


def _merge_target(current: SkillCard, candidate: SkillCard, target_path: str) -> SkillCard:
    """将候选技能中的单个目标路径合并到当前技能。

    Args:
        current: 当前技能。
        candidate: 候选技能。
        target_path: 单个目标路径。

    Returns:
        合并后的技能。
    """
    normalized_path = target_path.strip() or "all"
    if normalized_path == "all":
        return candidate

    current_data = current.model_dump(mode="json")
    candidate_data = candidate.model_dump(mode="json")
    if normalized_path == "basic":
        # 合并所有 basic 字段
        for field in BASIC_FIELDS:
            if field in candidate_data:
                current_data[field] = candidate_data[field]
        return SkillCard.model_validate(current_data)

    # 合并单个节点
    target_index = _node_target_index(current_data, normalized_path)
    if target_index is not None:
        candidate_nodes = [node for node in candidate_data.get("nodes", []) if isinstance(node, dict)]
        current_nodes = [node for node in current_data.get("nodes", []) if isinstance(node, dict)]
        replacement = _replacement_node(
            candidate_nodes,
            current_nodes[target_index],
            target_index,
            prefer_index=normalized_path.startswith("nodes["),
        )
        if isinstance(replacement, dict):
            # 逐字段合并：仅替换 replacement 中存在的字段
            next_node = dict(current_nodes[target_index])
            previous_node_id = str(next_node.get("node_id") or "")
            for field in NODE_FIELDS:
                if field in replacement:
                    next_node[field] = replacement[field]
            current_nodes[target_index] = next_node
            current_data["nodes"] = current_nodes
            # 若节点 ID 发生变化，同步修正引用
            next_node_id = str(next_node.get("node_id") or "")
            if previous_node_id and next_node_id and previous_node_id != next_node_id:
                _replace_node_reference(current_data, previous_node_id, next_node_id)
            return SkillCard.model_validate(current_data)

    return current


def _replace_node_reference(data: dict[str, Any], old_node_id: str, new_node_id: str) -> None:
    """在技能数据中将旧节点 ID 的所有引用替换为新节点 ID。

    引用位置包括：start_node_id、terminal_node_ids、edges 的 source/next。

    Args:
        data: 技能的 JSON 字典（会被原地修改）。
        old_node_id: 旧节点 ID。
        new_node_id: 新节点 ID。
    """
    if data.get("start_node_id") == old_node_id:
        data["start_node_id"] = new_node_id
    data["terminal_node_ids"] = [
        new_node_id if node_id == old_node_id else node_id
        for node_id in data.get("terminal_node_ids", [])
    ]
    for edge in data.get("edges", []):
        if not isinstance(edge, dict):
            continue
        if edge.get("source_node_id") == old_node_id:
            edge["source_node_id"] = new_node_id
        if edge.get("next_node_id") == old_node_id:
            edge["next_node_id"] = new_node_id


def _changed_paths(previous: SkillCard, next_skill: SkillCard) -> list[str]:
    """比较两个技能之间的差异，返回发生变化的路径列表。

    Args:
        previous: 变更前的技能。
        next_skill: 变更后的技能。

    Returns:
        变化路径列表，如 ``["basic", "nodes[0]", "nodes[2]"]``。
    """
    previous_data = previous.model_dump(mode="json")
    next_data = next_skill.model_dump(mode="json")
    changed: list[str] = []
    # 检查 basic 字段
    if any(previous_data.get(field) != next_data.get(field) for field in BASIC_FIELDS):
        changed.append("basic")
    # 逐节点比较
    previous_nodes = [node for node in previous_data.get("nodes", []) if isinstance(node, dict)]
    next_nodes = [node for node in next_data.get("nodes", []) if isinstance(node, dict)]
    for index in range(max(len(previous_nodes), len(next_nodes))):
        previous_node = previous_nodes[index] if index < len(previous_nodes) else None
        next_node = next_nodes[index] if index < len(next_nodes) else None
        if previous_node != next_node:
            changed.append(f"nodes[{index}]")
    return changed


def _node_target_index(current_data: dict[str, Any], path: str) -> int | None:
    """从目标路径中解析节点索引。

    Args:
        current_data: 当前技能的 JSON 字典。
        path: 目标路径，如 ``nodes[2]`` 或 ``nodes.understand_request``。

    Returns:
        节点索引；路径不匹配或索引越界时返回 None。
    """
    current_nodes = [node for node in current_data.get("nodes", []) if isinstance(node, dict)]
    bracket_match = re.fullmatch(r"nodes\[(\d+)\]", path)
    if bracket_match:
        index = int(bracket_match.group(1))
        return index if 0 <= index < len(current_nodes) else None
    if path.startswith("nodes."):
        node_id = path.split(".", 1)[1]
        return next(
            (index for index, node in enumerate(current_nodes) if node.get("node_id") == node_id),
            None,
        )
    return None


def _replacement_node(
    candidate_nodes: list[dict[str, Any]],
    current_node: dict[str, Any],
    target_index: int,
    prefer_index: bool = False,
) -> dict[str, Any] | None:
    """从候选节点列表中找到最佳替换节点。

    查找策略（按优先级）：
    1. 若 ``prefer_index`` 为 True 且索引有效，直接按索引取。
    2. 按 node_id 匹配（若有且唯一匹配）。
    3. 按索引取（兜底）。

    Args:
        candidate_nodes: 候选节点列表。
        current_node: 当前要被替换的节点。
        target_index: 目标索引。
        prefer_index: 是否优先按索引匹配。

    Returns:
        最佳替换节点字典，找不到则返回 None。
    """
    if prefer_index and target_index < len(candidate_nodes):
        return candidate_nodes[target_index]
    node_id = str(current_node.get("node_id") or "")
    if node_id:
        matching_nodes = [node for node in candidate_nodes if str(node.get("node_id") or "") == node_id]
        if len(matching_nodes) == 1:
            return matching_nodes[0]
    if target_index < len(candidate_nodes):
        return candidate_nodes[target_index]
    return None


def _extract_json(text: str) -> str:
    """从模型输出文本中提取 JSON 字符串。

    处理 Markdown 代码围栏并截取花括号内容。

    Args:
        text: 模型输出的原始文本。

    Returns:
        提取出的 JSON 字符串。
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end >= start:
        return stripped[start : end + 1]
    return stripped


def _chunk_text(text: str, size: int = 12) -> Iterator[str]:
    """将文本切分为固定大小的块，用于流式输出。

    Args:
        text: 要切分的文本。
        size: 每块的字符数，默认 12。

    Yields:
        str: 文本块。
    """
    for index in range(0, len(text), size):
        yield text[index : index + size]
