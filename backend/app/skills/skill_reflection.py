"""技能反思（Reflection）模块。

本模块在技能蒸馏或编辑生成结果后，对技能草稿进行多轮模型校验与自动修正。
校验基于一组预定义的 Rubric（评分准则），涵盖来源一致性、闭环能力、
自适应推进、工具依据、工具调用格式、副作用确认、中断恢复等维度。

核心流程：
1. 将候选技能草稿和原始来源提交给 LLM 做反思校验。
2. 若校验通过（``passed=True``）则返回最终结果。
3. 若未通过但模型返回了修正后的草稿，则应用修正并进入下一轮。
4. 若达到最大轮次仍未通过，保留最后一版草稿并附带警告。

同时支持流式（stream）和非流式两种调用方式。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, TypeVar

from app import paths
from app.llm import LLMClient, LLMError
from app.skills.skill_schema import SkillCard, ToolSuggestion


# 反思提示词模板文件路径
PROMPT_PATH = paths.resource_dir() / "app" / "llm" / "prompts" / "skill_reflection_prompt.md"
# 最大反思轮次
MAX_REFLECTION_ROUNDS = 3
# Rubric 名称 → 中文标签的映射
RUBRIC_LABELS: dict[str, str] = {
    "source_alignment": "来源一致性",
    "closed_loop": "闭环能力",
    "adaptive_progression": "自适应推进",
    "tool_grounding": "工具依据",
    "tool_call_format": "工具调用格式",
    "side_effect_confirmation": "副作用确认",
    "interruption_and_recovery": "中断恢复",
}
# Rubric 列表，供 LLM 评分时引用
RUBRICS = [
    {
        "name": name,
        "label": label,
    }
    for name, label in RUBRIC_LABELS.items()
]

# 响应类型的泛型参数（蒸馏用 SkillDistillResponse，改写用 SkillRewriteResponse）
ResponseT = TypeVar("ResponseT")
# 状态回调函数类型：接收一段状态文本
StatusCallback = Callable[[str], None]
# 将模型返回的原始字典归一化为具体响应类型的函数
NormalizeResponse = Callable[[dict[str, Any]], ResponseT]


def reflect_skill_response(
    *,
    client: LLMClient,
    source_kind: str,
    source_payload: dict[str, Any],
    response: ResponseT,
    candidate_skill: SkillCard,
    current_warnings: list[str],
    tool_suggestions: list[ToolSuggestion],
    normalize_response: NormalizeResponse[ResponseT],
    status_callback: StatusCallback | None = None,
) -> ResponseT:
    """对技能生成结果进行反思校验（非流式版本）。

    内部委托给 ``reflect_skill_response_stream``，但仅消费 status 事件
    并通过 ``status_callback`` 转发，最终返回校验后的响应。

    Args:
        client: LLM 客户端实例。
        source_kind: 来源类型，``"distill"`` 或 ``"rewrite"``。
        source_payload: 原始来源数据的负载（供 LLM 参考原始内容）。
        response: 初始响应（蒸馏或改写的初步结果）。
        candidate_skill: 候选技能草稿。
        current_warnings: 当前已有的警告列表。
        tool_suggestions: 当前的工具建议列表。
        normalize_response: 将原始字典归一化为响应类型的回调。
        status_callback: 可选的状态文本回调。

    Returns:
        校验/修正后的响应（与 ``response`` 同类型）。
    """
    events = reflect_skill_response_stream(
        client=client,
        source_kind=source_kind,
        source_payload=source_payload,
        response=response,
        candidate_skill=candidate_skill,
        current_warnings=current_warnings,
        tool_suggestions=tool_suggestions,
        normalize_response=normalize_response,
    )
    while True:
        try:
            event = next(events)
            if event.get("event") == "status":
                text = event.get("data", {}).get("text") if isinstance(event.get("data"), dict) else None
                _emit(status_callback, str(text or ""))
        except StopIteration as stop:
            # 生成器通过 return 返回最终结果，会触发 StopIteration.value
            return stop.value


def reflect_skill_response_stream(
    *,
    client: LLMClient,
    source_kind: str,
    source_payload: dict[str, Any],
    response: ResponseT,
    candidate_skill: SkillCard,
    current_warnings: list[str],
    tool_suggestions: list[ToolSuggestion],
    normalize_response: NormalizeResponse[ResponseT],
):
    """对技能生成结果进行反思校验（流式版本）。

    通过 yield 生成器逐步返回状态事件，最终通过 ``return`` 返回校验后的响应。
    每轮调用 LLM 进行 Rubric 评分，若未通过则应用模型返回的修正并进入下一轮。

    Args:
        client: LLM 客户端实例。
        source_kind: 来源类型。
        source_payload: 原始来源数据负载。
        response: 初始响应。
        candidate_skill: 候选技能草稿。
        current_warnings: 当前警告列表。
        tool_suggestions: 工具建议列表。
        normalize_response: 响应归一化回调。

    Yields:
        dict: 状态事件字典，格式为 ``{"event": "status", "data": {"text": ...}}``。

    Returns:
        校验/修正后的响应。
    """
    prompt = PROMPT_PATH.read_text(encoding="utf-8")
    reviewed = response                   # 当前已审阅的响应
    reviewed_skill = candidate_skill      # 当前已审阅的技能草稿
    warnings = list(current_warnings)     # 累积的警告列表（副本）
    suggestions = list(tool_suggestions)  # 累积的工具建议列表（副本）
    reflection_history: list[dict[str, Any]] = []  # 每轮反思的历史记录

    for round_index in range(1, MAX_REFLECTION_ROUNDS + 1):
        yield _status_event(f"正在校验技能结果（{round_index}/{MAX_REFLECTION_ROUNDS}）")
        yield _status_event("校验范围：来源一致性、闭环能力、自适应推进、工具依据、工具调用格式、副作用确认、中断恢复")
        try:
            review = _model_review(
                client,
                prompt,
                {
                    "source_kind": source_kind,
                    "source": source_payload,
                    "candidate_skill": reviewed_skill.model_dump(mode="json"),
                    "current_warnings": warnings,
                    "tool_suggestions": [item.model_dump(mode="json") for item in suggestions],
                    "rubrics": RUBRICS,
                    "reflection_round": round_index,
                    "max_reflection_rounds": MAX_REFLECTION_ROUNDS,
                    "reflection_history": reflection_history,
                },
            )
        except (LLMError, json.JSONDecodeError, TypeError, ValueError) as exc:
            # LLM 调用或解析失败，保留当前草稿并附带错误说明
            yield _status_event("校验失败，保留当前技能草稿")
            return normalize_response(
                {
                    "draft_skill": reviewed_skill.model_dump(mode="json"),
                    "warnings": [*warnings, f"模型校验未能完成，已保留当前技能草稿：{exc}"],
                    "tool_mentions": [item.model_dump(mode="json") for item in suggestions],
                }
            )

        # 记录本轮反思的历史
        reflection_history.append(_reflection_history_item(review))
        # 从校验结果中提取新的警告
        review_warnings = _warnings_from_review(review, source_kind)
        if review_warnings:
            warnings.extend(review_warnings)

        # 输出未通过的 Rubric 详情
        failed = _failed_rubrics(review)
        if failed:
            for item in failed[:4]:
                yield _status_event(f"校验发现：{_rubric_label(item)} - {_finding_text(item)}")
        summary = str(review.get("summary") or "").strip()
        if summary:
            yield _status_event(f"校验结论：{summary}")

        # 校验通过，返回最终结果
        if bool(review.get("passed")):
            yield _status_event("校验通过，技能草稿满足当前要求")
            return normalize_response(
                {
                    "draft_skill": reviewed_skill.model_dump(mode="json"),
                    "warnings": warnings,
                    "tool_mentions": [
                        *[item.model_dump(mode="json") for item in suggestions],
                        *_list_of_dicts(review.get("tool_mentions")),
                    ],
                }
            )

        # 校验未通过但模型未返回修正草稿，保留当前草稿
        revised_skill = review.get("draft_skill")
        if not isinstance(revised_skill, dict):
            yield _status_event("校验未通过，但模型未返回可修正草稿")
            return normalize_response(
                {
                    "draft_skill": reviewed_skill.model_dump(mode="json"),
                    "warnings": [
                        *warnings,
                        "模型校验未通过，但未返回可修正 Skill Card，已保留当前草稿。",
                    ],
                    "tool_mentions": [
                        *[item.model_dump(mode="json") for item in suggestions],
                        *_list_of_dicts(review.get("tool_mentions")),
                    ],
                }
            )

        # 应用模型返回的修正草稿，进入下一轮
        yield _status_event(f"校验未通过，正在应用第 {round_index} 轮修正")
        reviewed = normalize_response(
            {
                "draft_skill": revised_skill,
                "warnings": warnings,
                "tool_mentions": [
                    *[item.model_dump(mode="json") for item in suggestions],
                    *_list_of_dicts(review.get("tool_mentions")),
                ],
            }
        )
        reviewed_skill = getattr(reviewed, "draft_skill")
        warnings = list(getattr(reviewed, "warnings", warnings))
        suggestions = list(getattr(reviewed, "tool_suggestions", suggestions))

    # 达到最大轮次仍未通过
    yield _status_event("校验达到上限，保留最后一版技能草稿")
    return normalize_response(
        {
            "draft_skill": reviewed_skill.model_dump(mode="json"),
            "warnings": [*warnings, f"模型校验已达到 {MAX_REFLECTION_ROUNDS} 轮上限，保留最后一版技能草稿。"],
            "tool_mentions": [item.model_dump(mode="json") for item in suggestions],
        }
    )


def _model_review(client: LLMClient, prompt: str, payload: dict[str, Any]) -> dict[str, Any]:
    """调用 LLM 进行一轮 Rubric 评分，返回解析后的 JSON 字典。

    Args:
        client: LLM 客户端实例。
        prompt: 反思提示词模板。
        payload: 评分所需的上下文负载。

    Returns:
        模型返回的评分结果字典。

    Raises:
        ValueError: 模型输出不是 JSON object 时。
    """
    text = client.generate_text(prompt, payload)
    raw = json.loads(_extract_json(text))
    if not isinstance(raw, dict):
        raise ValueError("反思模型输出不是 JSON object")
    return raw


def _warnings_from_review(review: dict[str, Any], source_kind: str) -> list[str]:
    """从反思结果中提取所有警告消息。

    来源包括：
    - 模型显式返回的 ``source_warnings``（标注为来源本身的问题）。
    - 模型返回的通用 ``warnings``。
    - 未通过 Rubric 中 origin 为 ``source_input`` 的条目。

    Args:
        review: 模型评分结果字典。
        source_kind: 来源类型，用于生成警告中的来源标签。

    Returns:
        去重后的警告消息列表。
    """
    warnings: list[str] = []
    for item in _string_list(review.get("source_warnings")):
        warnings.append(f"{_source_label(source_kind)}本身可能存在问题：{item}")
    for item in _string_list(review.get("warnings")):
        warnings.append(item)
    for item in _failed_rubrics(review):
        origin = str(item.get("origin") or "").strip()
        if origin != "source_input":
            continue
        finding = _finding_text(item)
        if finding:
            warnings.append(f"{_source_label(source_kind)}本身可能存在问题：{_rubric_label(item)} - {finding}")
    return _dedupe(warnings)


def _failed_rubrics(review: dict[str, Any]) -> list[dict[str, Any]]:
    """从评分结果中提取所有未通过的 Rubric 条目。

    Args:
        review: 模型评分结果字典。

    Returns:
        未通过的 Rubric 条目列表（每个元素为字典，含 name / finding / origin 等字段）。
    """
    results = review.get("rubric_results")
    if not isinstance(results, list):
        return []
    return [item for item in results if isinstance(item, dict) and not bool(item.get("passed"))]


def _reflection_history_item(review: dict[str, Any]) -> dict[str, Any]:
    """将一轮评分结果压缩为反思历史条目。

    Args:
        review: 模型评分结果字典。

    Returns:
        压缩后的历史条目，包含 passed、summary 和未通过的 Rubric 列表。
    """
    return {
        "passed": bool(review.get("passed")),
        "summary": str(review.get("summary") or ""),
        "failed_rubrics": [
            {
                "name": str(item.get("name") or ""),
                "finding": _finding_text(item),
                "origin": str(item.get("origin") or ""),
            }
            for item in _failed_rubrics(review)
        ],
    }


def _source_label(source_kind: str) -> str:
    """根据来源类型返回中文来源标签。

    Args:
        source_kind: 来源类型字符串。

    Returns:
        ``"原始技能"``（改写场景）或 ``"原始文档"``（蒸馏场景）。
    """
    if source_kind == "rewrite":
        return "原始技能"
    return "原始文档"


def _rubric_label(item: dict[str, Any]) -> str:
    """从 Rubric 条目中获取中文标签。

    Args:
        item: Rubric 条目字典，应含 ``name`` 字段。

    Returns:
        对应的中文标签，若名称不在映射表中则返回原始名称或"未知 Rubric"。
    """
    name = str(item.get("name") or "")
    return RUBRIC_LABELS.get(name, name or "未知 Rubric")


def _finding_text(item: dict[str, Any]) -> str:
    """从 Rubric 条目中提取问题描述文本。

    兼容 ``finding`` 和 ``issue`` 两种字段名。

    Args:
        item: Rubric 条目字典。

    Returns:
        去除首尾空白后的问题描述文本。
    """
    return str(item.get("finding") or item.get("issue") or "").strip()


def _string_list(value: Any) -> list[str]:
    """将任意值安全地转换为非空字符串列表。

    Args:
        value: 输入值。

    Returns:
        字符串列表；若输入不是列表或元素全为空，返回空列表。
    """
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    """将任意值安全地转换为字典列表。

    Args:
        value: 输入值。

    Returns:
        字典列表；若输入不是列表则返回空列表，非字典元素被过滤。
    """
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _dedupe(values: list[str]) -> list[str]:
    """对字符串列表做去重（保留顺序）。

    Args:
        values: 待去重的字符串列表。

    Returns:
        去重后的字符串列表。
    """
    deduped: list[str] = []
    for value in values:
        text = value.strip()
        if text and text not in deduped:
            deduped.append(text)
    return deduped


def _emit(status_callback: StatusCallback | None, text: str) -> None:
    """安全地调用状态回调函数。

    Args:
        status_callback: 状态回调函数，为 None 时不执行。
        text: 要发送的状态文本。
    """
    if status_callback is not None:
        status_callback(text)


def _status_event(text: str) -> dict[str, object]:
    """构建一个标准状态事件字典。

    Args:
        text: 状态文本。

    Returns:
        事件字典 ``{"event": "status", "data": {"text": text}}``。
    """
    return {"event": "status", "data": {"text": text}}


def _extract_json(text: str) -> str:
    """从模型输出文本中提取 JSON 字符串。

    处理以下情况：
    - 去除 Markdown 代码围栏（```json ... ```）。
    - 截取第一个 ``{`` 到最后一个 ``}`` 之间的内容。

    Args:
        text: 模型输出的原始文本。

    Returns:
        提取出的 JSON 字符串。若找不到花括号则返回原始文本。
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
