"""用户记忆服务模块。

提供长期记忆的提取、存储、去重和查询能力。

核心类 ``MemoryService`` 封装了：

1. **记忆召回**（``recall`` / ``context_memories``）：在对话上下文中
   返回用户的 profile、preference、fact 类型记忆。
2. **记忆捕获**（``capture_turn``）：在对话轮次结束后，
   调用 LLM 从对话中提取新的记忆或更新/删除已有记忆，
   支持按 key 去重和按 agent 隔离。

记忆类型（``ALLOWED_MEMORY_KINDS``）：``profile``、``preference``、``fact``。
每条记忆通过 ``kind + key`` 实现幂等 upsert，避免重复记忆堆积。

模块还提供记忆读取格式化（``memory_read``）、行级去重（``memory_rows_for_read``）
和 agent 匹配（``memory_matches_agent``）等辅助函数。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from sqlmodel import Session, select

from app import paths
from app.db.models import ChatSession, MemoryRecord, ModelConfig, Tool, User, utc_now
from app.llm import LLMClient
from app.observability.spans import llm_operation
from app.session.session_schema import ChatTurnRequest, StepAgentResult
from app.tools.tool_schema import ToolResult


PROMPT_PATH = paths.resource_dir() / "app" / "llm" / "prompts" / "memory_extractor_prompt.md"
MEMORY_SOURCE = "model_memory_extractor"
PROFILE_NAME_KEY = "preferred_name"
ALLOWED_MEMORY_KINDS = {"profile", "preference", "fact"}


class MemoryService:
    """用户记忆服务，负责记忆的提取、存储和召回。

    记忆类型（``ALLOWED_MEMORY_KINDS``）：``profile``（用户档案）、
    ``preference``（偏好）、``fact``（事实）。

    每条记忆通过 ``kind + key`` 实现幂等 upsert：相同 kind/key 的记忆
    会被更新而非重复创建。记忆支持按 agent 隔离（通过 metadata 中的
    ``agent_id`` 字段）。

    Attributes:
        db: SQLModel 数据库会话。
    """

    def __init__(self, db: Session):
        """初始化记忆服务。

        Args:
            db: SQLModel 数据库会话。
        """
        self.db = db

    def recall(
        self,
        tenant_id: str,
        user_id: str,
        query: str,
        limit: int | None = None,
        agent_id: str | None = None,
    ) -> list[MemoryRecord]:
        """召回用户的相关记忆（当前实现返回全部上下文记忆）。

        注意：``query`` 和 ``limit`` 参数当前被忽略，
        实际行为等同于 ``context_memories``。

        Args:
            tenant_id: 租户 ID。
            user_id: 用户 ID。
            query: 查询文本（当前未使用）。
            limit: 返回数量上限（当前未使用）。
            agent_id: 可选的 agent ID，用于过滤记忆范围。

        Returns:
            匹配的记忆记录列表。
        """
        del query, limit
        return self.context_memories(tenant_id, user_id, agent_id=agent_id)

    def context_memories(
        self,
        tenant_id: str,
        user_id: str,
        *,
        agent_id: str | None = None,
    ) -> list[MemoryRecord]:
        """获取用户的上下文记忆（profile、preference、fact 类型）。

        Args:
            tenant_id: 租户 ID。
            user_id: 用户 ID。
            agent_id: 可选的 agent ID 过滤条件。

        Returns:
            过滤后的记忆记录列表。
        """
        return [
            row
            for row in self._list_user_memories(
                tenant_id,
                user_id,
                limit=None,
                agent_id=agent_id,
            )
            if row.kind in ALLOWED_MEMORY_KINDS
        ]

    def capture_turn(
        self,
        request: ChatTurnRequest,
        session: ChatSession,
        step_result: StepAgentResult,
        tool_result: ToolResult | None,
        model_config: ModelConfig,
        conversation_messages: list[dict[str, str]],
    ) -> list[MemoryRecord]:
        """从对话轮次中提取、更新或删除记忆。

        调用 LLM 分析对话上下文，返回记忆增量操作（upsert/delete），
        然后逐条执行：upsert 按 kind+key 幂等写入，delete 按 key 删除。

        Args:
            request: 原始对话轮次请求。
            session: 当前会话对象。
            step_result: Agent 步骤执行结果。
            tool_result: 工具调用结果（可选）。
            model_config: 用于记忆提取的 LLM 模型配置。
            conversation_messages: 本轮完整对话消息列表。

        Returns:
            本轮 upsert 的记忆记录列表（不包含 delete 操作）。
        """
        from app.core.context_projection import compact_step_result

        if not request.user_id:
            return []

        agent_id = session.agent_id
        user = self.db.get(User, request.user_id)
        username = user.username if user else request.user_id
        existing_rows = self._list_user_memories(
            request.tenant_id,
            request.user_id,
            limit=30,
            normalize=False,
            agent_id=agent_id,
        )
        with llm_operation("memory.capture", existing_count=len(existing_rows)):
            raw_delta = LLMClient(model_config).generate_json(
                PROMPT_PATH.read_text(encoding="utf-8"),
                {
                    "conversation_context": {
                        "messages": conversation_messages
                    },
                    "existing_memories": _memories_for_model(existing_rows),
                    "step_result": compact_step_result(step_result.model_dump(mode="json")),
                    "tool_result": tool_result.model_dump(mode="json") if tool_result else None,
                },
            )
        records: list[MemoryRecord] = []
        for update in _normalize_memory_updates(raw_delta):
            if update["operation"] == "delete":
                self._delete_keyed_memory(
                    request.tenant_id,
                    request.user_id,
                    update["kind"],
                    update["key"],
                    agent_id=agent_id,
                )
                continue
            records.append(
                self._upsert_keyed_memory(
                    tenant_id=request.tenant_id,
                    user_id=request.user_id,
                    username=username,
                    session_id=session.id,
                    kind=update["kind"],
                    key=update["key"],
                    content=update["content"],
                    importance=update["importance"],
                    metadata={
                        "source": MEMORY_SOURCE,
                        "key": update["key"],
                        "reason": update.get("reason"),
                        "agent_id": agent_id,
                    },
                    agent_id=agent_id,
                )
            )

        return records

    def _list_user_memories(
        self,
        tenant_id: str,
        user_id: str,
        limit: int | None = 80,
        normalize: bool = True,
        agent_id: str | None = None,
    ) -> list[MemoryRecord]:
        """查询用户的记忆记录列表（排除 conversation 类型）。

        支持按 agent 过滤和去重。当指定 agent_id 时多取一些行用于内存过滤。

        Args:
            tenant_id: 租户 ID。
            user_id: 用户 ID。
            limit: 返回数量上限，None 表示不限。
            normalize: 是否对结果执行行级去重。
            agent_id: 可选的 agent ID 过滤条件。

        Returns:
            list[MemoryRecord]: 记忆记录列表。
        """
        statement = (
            select(MemoryRecord)
            .where(
                MemoryRecord.tenant_id == tenant_id,
                MemoryRecord.user_id == user_id,
                MemoryRecord.kind != "conversation",
            )
            .order_by(MemoryRecord.updated_at.desc())
        )
        if limit is not None:
            statement = statement.limit(limit * 5 if agent_id else limit)
        rows = list(self.db.exec(statement).all())
        if agent_id:
            rows = [row for row in rows if self._memory_matches_agent(row, agent_id)]
        if limit is not None:
            rows = rows[:limit]
        return memory_rows_for_read(rows) if normalize else rows

    def _upsert_keyed_memory(
        self,
        tenant_id: str,
        user_id: str,
        username: str | None,
        session_id: str,
        kind: str,
        key: str,
        content: str,
        importance: float,
        metadata: dict[str, Any],
        agent_id: str | None = None,
    ) -> MemoryRecord:
        """按 kind+key 幂等写入记忆记录。

        若已有匹配记录则更新内容，否则新建。重复记录会被删除。

        Args:
            tenant_id: 租户 ID。
            user_id: 用户 ID。
            username: 用户名。
            session_id: 会话 ID。
            kind: 记忆类型。
            key: 去重键。
            content: 记忆内容（截断至 1200 字符）。
            importance: 重要性分数（0-1）。
            metadata: 元数据字典。
            agent_id: 可选的 agent ID。

        Returns:
            MemoryRecord: 写入后的记忆记录。
        """
        existing, duplicates = self._find_keyed_memory_candidates(tenant_id, user_id, kind, key, agent_id=agent_id)
        now = utc_now()
        if existing:
            existing.content = content[:1200]
            existing.username = username
            existing.session_id = session_id
            existing.importance = importance
            existing.updated_at = now
            existing.metadata_json = {**(existing.metadata_json or {}), **metadata}
            record = existing
        else:
            record = MemoryRecord(
                tenant_id=tenant_id,
                user_id=user_id,
                username=username,
                session_id=session_id,
                kind=kind,
                content=content[:1200],
                importance=importance,
                metadata_json=metadata,
            )
            self.db.add(record)

        for duplicate in duplicates:
            if duplicate.id != record.id:
                self.db.delete(duplicate)
        self.db.add(record)
        return record

    def _delete_keyed_memory(
        self,
        tenant_id: str,
        user_id: str,
        kind: str,
        key: str,
        agent_id: str | None = None,
    ) -> None:
        """按 kind+key 删除用户的记忆记录。

        删除匹配主记录及所有重复记录。

        Args:
            tenant_id: 租户 ID。
            user_id: 用户 ID。
            kind: 记忆类型。
            key: 去重键。
            agent_id: 可选的 agent ID。
        """
        existing, duplicates = self._find_keyed_memory_candidates(tenant_id, user_id, kind, key, agent_id=agent_id)
        for row in [existing, *duplicates]:
            if row:
                self.db.delete(row)

    def _find_keyed_memory_candidates(
        self,
        tenant_id: str,
        user_id: str,
        kind: str,
        key: str,
        agent_id: str | None = None,
    ) -> tuple[MemoryRecord | None, list[MemoryRecord]]:
        """查找指定 kind+key 的记忆候选记录。

        返回主记录（最新一条）和其余重复记录。

        Args:
            tenant_id: 租户 ID。
            user_id: 用户 ID。
            kind: 记忆类型。
            key: 去重键。
            agent_id: 可选的 agent ID 过滤条件。

        Returns:
            tuple[MemoryRecord | None, list[MemoryRecord]]: (主记录, 重复记录列表)。
        """
        rows = list(
            self.db.exec(
                select(MemoryRecord)
                .where(
                    MemoryRecord.tenant_id == tenant_id,
                    MemoryRecord.user_id == user_id,
                    MemoryRecord.kind == kind,
                )
                .order_by(MemoryRecord.updated_at.desc())
            ).all()
        )
        if agent_id:
            rows = [row for row in rows if self._memory_matches_agent(row, agent_id)]
        candidates = [row for row in rows if _memory_matches_key(row, key)]
        if not candidates:
            return None, []
        return candidates[0], candidates[1:]

    def _upsert_summary(
        self,
        tenant_id: str,
        user_id: str,
        username: str | None,
        session_id: str,
        summary: str,
        metadata: dict[str, Any],
        agent_id: str | None = None,
    ) -> MemoryRecord:
        """更新或创建用户对话摘要记忆（kind=summary）。

        每个 agent 维护一份摘要，存在则更新并递增 turn_count。

        Args:
            tenant_id: 租户 ID。
            user_id: 用户 ID。
            username: 用户名。
            session_id: 会话 ID。
            summary: 摘要文本（截断至 1800 字符）。
            metadata: 元数据字典。
            agent_id: 可选的 agent ID。

        Returns:
            MemoryRecord: 写入后的摘要记录。
        """
        summary_rows = list(
            self.db.exec(
                select(MemoryRecord)
                .where(
                    MemoryRecord.tenant_id == tenant_id,
                    MemoryRecord.user_id == user_id,
                    MemoryRecord.kind == "summary",
                )
                .order_by(MemoryRecord.updated_at.desc())
            ).all()
        )
        if agent_id:
            existing = next((row for row in summary_rows if self._memory_matches_agent(row, agent_id)), None)
        else:
            existing = summary_rows[0] if summary_rows else None
        now = utc_now()
        if existing:
            existing.content = summary[:1800]
            existing.username = username
            existing.session_id = session_id
            existing.importance = 0.8
            existing.updated_at = now
            existing.metadata_json = {
                **(existing.metadata_json or {}),
                **metadata,
                "agent_id": agent_id,
                "turn_count": int((existing.metadata_json or {}).get("turn_count", 0)) + 1,
            }
            self.db.add(existing)
            return existing
        record = MemoryRecord(
            tenant_id=tenant_id,
            user_id=user_id,
            username=username,
            session_id=session_id,
            kind="summary",
            content=summary[:1800],
            importance=0.8,
            metadata_json={**metadata, "agent_id": agent_id, "turn_count": 1},
        )
        self.db.add(record)
        return record

    def _memory_matches_agent(self, record: MemoryRecord, agent_id: str | None) -> bool:
        """判断记忆记录是否属于指定 agent。

        先检查 metadata 中的 agent_id，再通过关联会话的 agent_id 回溯匹配。

        Args:
            record: 记忆记录。
            agent_id: agent ID。

        Returns:
            bool: 匹配返回 True。
        """
        if memory_matches_agent(record, agent_id):
            return True
        if not agent_id or memory_agent_id(record) or not record.session_id:
            return False
        session = self.db.get(ChatSession, record.session_id)
        return bool(session and session.agent_id == agent_id)


def memory_read(record: MemoryRecord) -> dict[str, Any]:
    """将记忆记录转换为 API 响应字典。

    Args:
        record: 记忆数据库行。

    Returns:
        dict[str, Any]: 序列化后的记忆字典。
    """
    return {
        "id": record.id,
        "tenant_id": record.tenant_id,
        "user_id": record.user_id,
        "username": record.username,
        "session_id": record.session_id,
        "kind": record.kind,
        "content": record.content,
        "importance": record.importance,
        "metadata": record.metadata_json or {},
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def _memories_for_model(records: list[MemoryRecord]) -> str:
    """将记忆记录列表格式化为供 LLM 参考的文本行。

    每条记录格式为 "- kind/key: content"。

    Args:
        records: 记忆记录列表。

    Returns:
        str: 格式化的记忆文本。
    """
    lines: list[str] = []
    for record in records:
        key = str((record.metadata_json or {}).get("key") or "").strip()
        label = "/".join(part for part in (record.kind, key) if part)
        lines.append(f"- {label}: {record.content}" if label else f"- {record.content}")
    return "\n".join(lines)


def memory_rows_for_read(rows: list[MemoryRecord]) -> list[MemoryRecord]:
    """对记忆记录列表执行行级去重。

    按 (user_id, kind, agent_id, dedupe_key) 四元组去重，
    保留每组的首次出现。

    Args:
        rows: 原始记忆记录列表。

    Returns:
        list[MemoryRecord]: 去重后的记录列表。
    """
    visible: list[MemoryRecord] = []
    seen_keys: set[tuple[str, str, str | None, str]] = set()
    for row in rows:
        dedupe_key = (row.user_id, row.kind, memory_agent_id(row), _read_dedupe_key(row))
        if dedupe_key in seen_keys:
            continue
        seen_keys.add(dedupe_key)
        visible.append(row)
    return visible


def memory_agent_id(record: MemoryRecord) -> str | None:
    """从记忆记录的 metadata 中提取 agent_id。

    Args:
        record: 记忆记录。

    Returns:
        str | None: agent_id 字符串，不存在时返回 None。
    """
    metadata = record.metadata_json or {}
    value = metadata.get("agent_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def memory_matches_agent(record: MemoryRecord, agent_id: str | None) -> bool:
    """判断记忆记录是否匹配指定 agent（无 agent_id 时匹配所有）。

    Args:
        record: 记忆记录。
        agent_id: agent ID，None 表示匹配所有。

    Returns:
        bool: 匹配返回 True。
    """
    if not agent_id:
        return True
    return memory_agent_id(record) == agent_id


def tool_read_for_activity(tool: Tool | None, result: ToolResult | None = None) -> dict[str, Any]:
    """将工具及其执行结果转换为活动日志用的摘要字典。

    Args:
        tool: 工具对象（可选）。
        result: 工具执行结果（可选）。

    Returns:
        dict[str, Any]: 包含工具名称、显示名、描述和成功状态的字典。
    """
    return {
        "name": result.tool_name if result else tool.name if tool else "",
        "display_name": tool.display_name if tool else None,
        "description": tool.description if tool else None,
        "success": result.success if result else None,
    }


def _normalize_memory_updates(raw: dict[str, Any]) -> list[dict[str, Any]]:
    """规范化 LLM 返回的记忆增量操作列表。

    过滤无效 kind，规范化 operation（upsert/delete）、key 和 importance，
    补全缺失的默认值。

    Args:
        raw: LLM 返回的原始字典。

    Returns:
        list[dict[str, Any]]: 规范化后的记忆更新操作列表。
    """
    items = raw.get("memories")
    if not isinstance(items, list):
        return []

    updates: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip()
        if kind not in ALLOWED_MEMORY_KINDS:
            continue
        content = str(item.get("content") or "").strip()
        operation = str(item.get("operation") or "upsert").strip().lower()
        if operation not in {"upsert", "delete"}:
            operation = "upsert"
        if operation == "upsert" and not content:
            continue
        key = _normalize_memory_key(item.get("key"), kind, content)
        updates.append(
            {
                "operation": operation,
                "kind": kind,
                "key": key,
                "content": content,
                "importance": _normalize_importance(item.get("importance")),
                "reason": str(item.get("reason") or "").strip()[:300],
            }
        )
    return updates


def _normalize_summary(raw: dict[str, Any]) -> str:
    """从 LLM 返回中提取并规范化摘要文本。

    Args:
        raw: LLM 返回的原始字典。

    Returns:
        str: 截断至 1800 字符的摘要文本，无效时返回空字符串。
    """
    value = raw.get("updated_summary") or raw.get("summary")
    if not isinstance(value, str):
        return ""
    return value.strip()[:1800]


def _normalize_memory_key(value: Any, kind: str, content: str) -> str:
    """规范化记忆去重键。

    优先使用提供的 key（清理为 snake_case），为空时基于 kind+content 的 MD5 生成。

    Args:
        value: 原始 key 值。
        kind: 记忆类型。
        content: 记忆内容。

    Returns:
        str: 规范化后的去重键（最多 80 字符）。
    """
    if isinstance(value, str):
        normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower()).strip("_")
        if normalized:
            return normalized[:80]
    digest = hashlib.md5(f"{kind}:{content}".encode("utf-8"), usedforsecurity=False).hexdigest()[:12]
    return f"{kind}_{digest}"


def _normalize_importance(value: Any) -> float:
    """将重要性值规范化为 0.0-1.0 范围的浮点数。

    Args:
        value: 原始重要性值。

    Returns:
        float: 限制在 [0.0, 1.0] 范围内的重要性，默认 0.7。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.7
    return min(max(number, 0.0), 1.0)


def _memory_matches_key(record: MemoryRecord, key: str) -> bool:
    """判断记忆记录的 metadata.key 是否等于指定 key。

    Args:
        record: 记忆记录。
        key: 待匹配的去重键。

    Returns:
        bool: 匹配返回 True。
    """
    metadata = record.metadata_json or {}
    return metadata.get("key") == key


def _read_dedupe_key(record: MemoryRecord) -> str:
    """提取记忆记录的读取去重键。

    优先使用 metadata.key，summary 类型固定为 "summary"，其余使用记录 ID。

    Args:
        record: 记忆记录。

    Returns:
        str: 去重键字符串。
    """
    metadata = record.metadata_json or {}
    key = metadata.get("key")
    if isinstance(key, str) and key.strip():
        return key.strip()
    if record.kind == "summary":
        return "summary"
    return record.id
