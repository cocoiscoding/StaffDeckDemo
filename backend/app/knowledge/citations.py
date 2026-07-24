"""知识引用处理模块。

本模块负责从知识检索结果中提取、压缩和规范化引用信息（Citations），
生成供 LLM 回复使用的引用列表。主要功能包括：

1. **引用压缩**：将 LLM 回复中引用的来源按首次出现顺序重新编号。
2. **引用提取**：从检索结果的 OKF 概念、证据包和 OKF 引用中提取结构化引用。
"""

from __future__ import annotations

import re
from typing import Any

CITATION_EXCERPT_CHAR_LIMIT = 6000   # 引用摘录的字符数上限
CITATION_SUMMARY_CHAR_LIMIT = 800    # 引用摘要的字符数上限
CONCEPT_EXCERPT_CHAR_LIMIT = 2400    # 概念摘录的字符数上限


def compact_knowledge_citation_labels(
    content: str,
    citations: object,
) -> tuple[str, list[dict[str, Any]]]:
    """压缩引用标签：按在回复中首次出现的顺序重新编号。

    扫描 content 中的 ``[N]`` 引用标记，按首次出现顺序生成新的连续编号映射，
    然后替换 content 中的标签并重新排列 citations 列表。

    Args:
        content: LLM 生成的回复文本（含 ``[N]`` 引用标记）。
        citations: 原始引用列表。

    Returns:
        ``(compacted_content, compacted_citations)`` 元组，其中引用标签已重新编号。
    """
    if not isinstance(citations, list) or not citations:
        return content, []

    citations_by_label: dict[int, dict[str, Any]] = {}
    for index, citation in enumerate(citations, start=1):
        if not isinstance(citation, dict):
            continue
        label_match = re.fullmatch(r"\[(\d+)\]", str(citation.get("label") or "").strip())
        label = int(label_match.group(1)) if label_match else index
        citations_by_label.setdefault(label, citation)

    ordered_labels: list[int] = []
    for match in re.finditer(r"\[(\d+)\]", content):
        label = int(match.group(1))
        if label in citations_by_label and label not in ordered_labels:
            ordered_labels.append(label)

    if not ordered_labels:
        return content, []

    label_mapping = {old_label: index for index, old_label in enumerate(ordered_labels, start=1)}

    def replace_label(match: re.Match[str]) -> str:
        old_label = int(match.group(1))
        new_label = label_mapping.get(old_label)
        return f"[{new_label}]" if new_label is not None else match.group(0)

    compacted_content = re.sub(r"\[(\d+)\]", replace_label, content)
    compacted_citations = [
        {**citations_by_label[old_label], "label": f"[{label_mapping[old_label]}]"}
        for old_label in ordered_labels
    ]
    return compacted_content, compacted_citations


def _compact(value: str, limit: int) -> str:
    """压缩文本：合并空白并截断到 limit 字符，超长时追加省略号。"""
    text = re.sub(r"\s+", " ", (value or "").strip())
    if len(text) <= limit:
        return text
    return f"{text[: max(0, limit - 1)].rstrip()}…"


def _normalize_identity(value: str) -> str:
    """生成去重用的归一化标识：去除空白、标点和下划线，转小写。"""
    return re.sub(r"[\s\W_]+", "", (value or "").lower())


def _semantic_identity(value: str) -> str:
    """生成语义标识：仅合并空白，保留大小写和标点。"""
    return re.sub(r"\s+", " ", (value or "").strip())


def _display_title(value: str) -> str:
    """生成展示用标题：压缩到 72 字符。"""
    return _compact(value, 72)


def knowledge_citations_from_results(
    knowledge_results: list[dict[str, Any]],
    limit: int = 4,
) -> list[dict[str, Any]]:
    """从知识检索结果中提取结构化引用列表。

    按优先级从检索结果的以下字段中提取引用：
    1. selected_concepts（OKF 概念卡片）。
    2. evidence_pack（证据包）。
    3. okf_citations（仅当前两者无结果时作为兜底）。

    每个引用按归一化标识去重，最多返回 limit 条。

    Args:
        knowledge_results: 知识检索结果列表（每个元素是 KnowledgeSearchResponse 的字典形式）。
        limit: 最多返回的引用数量。

    Returns:
        结构化引用字典列表，每条包含 id、label、kind、title、source_path 等。
    """
    citations: list[dict[str, Any]] = []
    seen_identities: set[str] = set()

    def add(kind: str, identity: str, payload: dict[str, Any]) -> None:
        if len(citations) >= limit:
            return
        normalized = _normalize_identity(identity)
        title_identity = _normalize_identity(str(payload.get("title") or ""))
        if not normalized or normalized in seen_identities or (title_identity and title_identity in seen_identities):
            return
        seen_identities.add(normalized)
        if title_identity:
            seen_identities.add(title_identity)
        citations.append(
            {
                "id": f"kref_{len(citations) + 1}",
                "label": f"[{len(citations) + 1}]",
                "kind": kind,
                **payload,
            }
        )

    for result in knowledge_results:
        for concept in result.get("selected_concepts") or []:
            if not isinstance(concept, dict):
                continue
            concept_id = str(concept.get("concept_id") or concept.get("id") or "").strip()
            title = _display_title(str(concept.get("title") or concept_id or "Wiki 概念"))
            description = str(concept.get("description") or "").strip()
            content = str(concept.get("content") or concept.get("content_excerpt") or "").strip()
            excerpt = content or description
            source_refs = concept.get("source_refs") if isinstance(concept.get("source_refs"), list) else []
            source_path = ""
            if source_refs and isinstance(source_refs[0], dict):
                source_path = str(source_refs[0].get("source_path") or source_refs[0].get("document_id") or "")
            add(
                "concept",
                concept_id or title,
                {
                    "title": title,
                    "source_path": source_path,
                    "content": excerpt[:CITATION_EXCERPT_CHAR_LIMIT],
                    "excerpt": excerpt[:CITATION_EXCERPT_CHAR_LIMIT],
                    "summary": description[:CITATION_SUMMARY_CHAR_LIMIT],
                    "concept_id": concept_id,
                    "concept_type": concept.get("type"),
                },
            )

        for item in result.get("evidence_pack") or []:
            if not isinstance(item, dict):
                continue
            excerpt = str(item.get("content") or item.get("excerpt") or "").strip()
            summary = str(item.get("summary") or "").strip()
            section_path = str(item.get("section_path") or "").strip()
            source_path = str(item.get("source_path") or "").strip()
            chunk_id = str(item.get("chunk_id") or "").strip()
            title = _display_title(section_path or source_path or summary or "知识片段")
            identity = _semantic_identity(section_path or summary or f"{source_path}:{excerpt[:120]}" or chunk_id)
            add(
                "evidence",
                identity,
                {
                    "title": title,
                    "source_path": source_path,
                    "section_path": section_path,
                    "content": excerpt[:CITATION_EXCERPT_CHAR_LIMIT],
                    "excerpt": excerpt[:CITATION_EXCERPT_CHAR_LIMIT],
                    "summary": summary[:CITATION_SUMMARY_CHAR_LIMIT],
                    "confidence_reason": str(item.get("confidence_reason") or ""),
                    "document_id": item.get("document_id"),
                    "bucket_id": item.get("bucket_id"),
                    "chunk_id": chunk_id,
                },
            )

        if not citations:
            for item in result.get("okf_citations") or []:
                if not isinstance(item, dict):
                    continue
                concept_id = str(item.get("concept_id") or "").strip()
                target = str(item.get("target") or "").strip()
                label = str(item.get("label") or "").strip()
                title = _display_title(str(item.get("title") or concept_id or "OKF 引用"))
                add(
                    "okf",
                    f"{concept_id}:{target or label}",
                    {
                        "title": title,
                        "source_path": target,
                        "excerpt": label,
                        "concept_id": concept_id,
                    },
                )
    return citations
