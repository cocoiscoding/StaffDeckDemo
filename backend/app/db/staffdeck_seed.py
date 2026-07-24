"""StaffDeck 管理员展厅（Admin Gallery）种子数据加载模块。

本模块负责从预置的 JSON 夹具（fixture）中读取精选的 StaffDeck 展厅数据包，
并将其作为管理员名下资源写入数据库。涵盖代理（Agent）、技能（Skill）、
通用技能（GeneralSkill）、工具（Tool）、知识库（KnowledgeBase）及其子资源
（版本、文档、分桶、分块、概念、发现建议、摄入任务），以及代理-资源绑定、
技能分支与知识分支的创建和幂等更新。

该模块的设计原则是**幂等可重入**：重复执行不会产生重复数据，
而是按业务键（名称、slug、skill_id 等）或 ID 匹配现有记录并更新。
仅标记为 ``managed_by_seed`` 的记录才会被种子流程覆盖。
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any, Iterable, TypeVar

from sqlmodel import Session, SQLModel, select

from app.agents.branching import (
    agent_private_metadata,
    ensure_open_gallery_binding,
    open_gallery_metadata,
)
from app.db.models import (
    AgentKnowledgeBranch,
    AgentProfile,
    AgentResourceBinding,
    AgentSkillBranch,
    AgentSkillBranchVersion,
    GeneralSkill,
    KnowledgeBase,
    KnowledgeBaseVersion,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeConcept,
    KnowledgeDiscoverySuggestion,
    KnowledgeDocument,
    KnowledgeIngestJob,
    Skill,
    SkillVersion,
    Tool,
    User,
    utc_now,
)


# === 全局常量 ===

#: 种子数据所属的租户 ID（演示租户）。
TENANT_ID = "tenant_demo"
#: 管理员用户 ID。
ADMIN_USER_ID = "admin"
#: 管理员用户名。
ADMIN_USERNAME = "admin"
#: 管理员显示名称。
ADMIN_DISPLAY_NAME = "Administrator"
#: 标记数据来源的标识符，用于区分种子数据与用户手动创建的数据。
SEED_SOURCE = "staffdeck_admin_gallery_seed"
#: 种子夹具文件的绝对路径，指向 seed_fixtures 目录下的 JSON 文件。
FIXTURE_PATH = Path(__file__).resolve().parent / "seed_fixtures" / "staffdeck_admin_gallery_seed.json"

#: 需要从夹具中筛选的代理名称集合；只有这些代理会被写入数据库。
SELECTED_AGENT_NAMES = {"IT", "人事", "法务", "行政", "财务"}

#: JSON 字典的类型别名。
JsonDict = dict[str, Any]
#: 泛型类型变量，约束为 SQLModel 子类，用于工具函数的模型类型标注。
ModelT = TypeVar("ModelT", bound=SQLModel)


def seed_staffdeck_admin_gallery(session: Session) -> None:
    """将精选的 StaffDeck 展厅数据包作为管理员名下资源写入数据库。

    该函数是种子加载的入口，按以下顺序依次处理各类资源：
    1. 从 JSON 夹具加载数据并筛选出选定代理及其关联的活跃资源绑定。
    2. 写入代理、技能、通用技能、工具与知识库等基础资源。
    3. 刷写（flush）后写入代理-资源绑定与技能/知识分支。
    4. 将已创建的资源发布到公共展厅（open gallery）。
    5. 将种子代理的归属信息同步到当前管理员账户。

    所有写入操作均为幂等的：重复执行时会匹配现有记录并更新，
    不会产生重复数据。

    Args:
        session: SQLModel 数据库会话，由调用方负责提交（commit）。

    Note:
        若夹具文件不存在，函数直接返回而不执行任何操作。
    """

    if not FIXTURE_PATH.exists():
        # 夹具文件缺失时静默退出
        return
    data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    # 源 ID → 目标 ID 的映射表，按资源类型分组。
    # 用于在不同资源之间建立外键引用时，将夹具中的源 ID 转换为实际数据库 ID。
    id_maps: dict[str, dict[str, str]] = {
        "agent": {},
        "skill": {},
        "skill_business": {},  # 业务键（skill_id）映射
        "general_skill": {},
        "tool": {},
        "knowledge_base": {},
        "knowledge_base_version": {},
        "knowledge_document": {},
        "knowledge_bucket": {},
    }

    # 仅保留选定名称的代理
    agents = [
        row for row in data.get("agent_profiles", []) if row.get("name") in SELECTED_AGENT_NAMES
    ]
    selected_agent_ids = {str(row.get("id") or "") for row in agents}
    # 仅保留属于选定代理的活跃绑定记录
    active_bindings = [
        row
        for row in data.get("agent_resource_bindings", [])
        if row.get("status") == "active" and str(row.get("agent_id") or "") in selected_agent_ids
    ]
    # 按资源类型提取需写入的资源 ID 集合
    resource_ids = _resource_ids_by_type(active_bindings)

    # === 第一阶段：写入基础资源 ===
    _seed_agents(session, agents, id_maps)
    _seed_skills(session, data.get("skills", []), data.get("skill_versions", []), resource_ids, id_maps)
    _seed_general_skills(session, data.get("general_skills", []), resource_ids, id_maps)
    _seed_tools(session, data.get("tools", []), resource_ids, id_maps)
    _seed_knowledge(session, data, resource_ids, id_maps)
    session.flush()  # 刷写以获取已分配的主键，供后续绑定引用

    # === 第二阶段：写入绑定关系与分支 ===
    _seed_agent_resource_bindings(session, active_bindings, id_maps)
    _seed_skill_branches(
        session,
        data.get("agent_skill_branches", []),
        data.get("agent_skill_branch_versions", []),
        id_maps,
    )
    _seed_knowledge_branches(session, active_bindings, id_maps)
    session.flush()

    # === 第三阶段：发布与归属同步 ===
    _publish_gallery_resources(session, id_maps)
    _sync_seed_agents_to_current_admin(session, id_maps)


def _seed_agents(session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]) -> None:
    """写入代理（AgentProfile）记录。

    对每条代理记录，按源 ID 和（租户 + 名称）双重匹配现有记录。
    仅当目标记录由种子流程管理时才执行更新；否则若记录已存在则跳过，
    若不存在则新建。

    Args:
        session: 数据库会话。
        rows: 夹具中的代理记录迭代器。
        id_maps: ID 映射表，函数会将 ``agent`` 映射写入其中。
    """
    for row in rows:
        source_id = str(row.get("id") or "")
        name = str(row.get("name") or "").strip()
        if not source_id or not name:
            # 缺少 ID 或名称的记录直接跳过
            continue
        existing_by_id = session.get(AgentProfile, source_id)
        existing_by_name = session.exec(
            select(AgentProfile).where(AgentProfile.tenant_id == TENANT_ID, AgentProfile.name == name)
        ).first()
        # 判断已有记录是否可被种子流程更新
        existing = _seed_update_target(existing_by_id, existing_by_name, source_id)
        metadata = _agent_metadata(row.get("metadata_json"))
        payload = {
            "tenant_id": TENANT_ID,
            "name": name,
            "description": row.get("description"),
            "persona_prompt": row.get("persona_prompt"),
            "is_overall": bool(row.get("is_overall", False)),
            "status": row.get("status") or "active",
            "metadata_json": metadata,
        }
        if existing:
            # 已存在且由种子管理 → 更新
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["agent"][source_id] = existing.id
        elif existing_by_id or existing_by_name:
            # 已存在但非种子管理 → 跳过，避免覆盖用户手动创建的数据
            continue
        else:
            # 不存在 → 新建
            agent = AgentProfile(id=source_id, **payload)
            session.add(agent)
            id_maps["agent"][source_id] = source_id


def _seed_skills(
    session: Session,
    skill_rows: Iterable[JsonDict],
    version_rows: Iterable[JsonDict],
    resource_ids: dict[str, set[str]],
    id_maps: dict[str, dict[str, str]],
) -> None:
    """写入技能（Skill）及其版本（SkillVersion）记录。

    分两步执行：
    1. 先写入技能主体记录（Skill），按源 ID 和（租户 + skill_id）匹配。
    2. 再写入技能版本记录（SkillVersion），按源 ID 和（租户 + skill_id + version）匹配。

    Args:
        session: 数据库会话。
        skill_rows: 夹具中的技能记录迭代器。
        version_rows: 夹具中的技能版本记录迭代器。
        resource_ids: 按资源类型分组的资源 ID 集合，用于筛选需写入的技能。
        id_maps: ID 映射表，函数会写入 ``skill`` 和 ``skill_business`` 映射。
    """
    selected_ids = resource_ids.get("skill", set())
    selected_skill_ids: set[str] = set()
    for row in skill_rows:
        source_id = str(row.get("id") or "")
        if source_id not in selected_ids:
            # 该技能不在选定代理的绑定范围内 → 跳过
            continue
        skill_id = str(row.get("skill_id") or "").strip()
        if not skill_id:
            continue
        existing_by_id = session.get(Skill, source_id)
        existing_by_skill_id = session.exec(
            select(Skill).where(Skill.tenant_id == TENANT_ID, Skill.skill_id == skill_id)
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_skill_id, source_id)
        content = _json_object(row.get("content_json"))
        # 将关键标识字段合并进 content_json，确保技能内容自包含
        content.update({"skill_id": skill_id, "name": row.get("name"), "version": row.get("version") or "1.0.0"})
        payload = {
            "tenant_id": TENANT_ID,
            "skill_id": skill_id,
            "version": row.get("version") or "1.0.0",
            "name": row.get("name") or skill_id,
            "business_domain": row.get("business_domain"),
            "description": row.get("description"),
            "content_json": content,
            "status": "published",
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            target_id = existing.id
        elif existing_by_id or existing_by_skill_id:
            continue
        else:
            skill = Skill(id=source_id, **payload)
            session.add(skill)
            target_id = source_id
        # 记录源 ID 到目标 ID 以及业务键的映射
        id_maps["skill"][source_id] = target_id
        id_maps["skill_business"][skill_id] = skill_id
        selected_skill_ids.add(skill_id)
    session.flush()

    # 第二步：写入技能版本记录
    for row in version_rows:
        skill_id = str(row.get("skill_id") or "").strip()
        if skill_id not in selected_skill_ids:
            continue
        version = str(row.get("version") or "1.0.0")
        source_id = str(row.get("id") or "")
        existing_by_id = session.get(SkillVersion, source_id)
        existing_by_version = session.exec(
            select(SkillVersion).where(
                SkillVersion.tenant_id == TENANT_ID,
                SkillVersion.skill_id == skill_id,
                SkillVersion.version == version,
            )
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_version, source_id)
        content = _json_object(row.get("content_json"))
        content.update({"skill_id": skill_id, "name": row.get("name"), "version": version})
        payload = {
            "tenant_id": TENANT_ID,
            "skill_id": skill_id,
            "version": version,
            "name": row.get("name") or skill_id,
            "business_domain": row.get("business_domain"),
            "description": row.get("description"),
            "content_json": content,
            "status": "published",
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
        elif existing_by_id or existing_by_version:
            continue
        else:
            session.add(SkillVersion(id=source_id, **payload))


def _seed_general_skills(
    session: Session,
    rows: Iterable[JsonDict],
    resource_ids: dict[str, set[str]],
    id_maps: dict[str, dict[str, str]],
) -> None:
    """写入通用技能（GeneralSkill）记录。

    按源 ID 和（租户 + slug）双重匹配现有记录。

    Args:
        session: 数据库会话。
        rows: 夹具中的通用技能记录迭代器。
        resource_ids: 按资源类型分组的资源 ID 集合，用于筛选需写入的记录。
        id_maps: ID 映射表，函数会写入 ``general_skill`` 映射。
    """
    selected_ids = resource_ids.get("general_skill", set())
    for row in rows:
        source_id = str(row.get("id") or "")
        if source_id not in selected_ids:
            continue
        slug = str(row.get("slug") or "").strip()
        if not slug:
            continue
        existing_by_id = session.get(GeneralSkill, source_id)
        existing_by_slug = session.exec(
            select(GeneralSkill).where(GeneralSkill.tenant_id == TENANT_ID, GeneralSkill.slug == slug)
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_slug, source_id)
        payload = {
            "tenant_id": TENANT_ID,
            "slug": slug,
            "name": row.get("name") or slug,
            "description": row.get("description"),
            "homepage": row.get("homepage"),
            "skill_markdown": row.get("skill_markdown") or "",
            "skill_files_json": _json_list(row.get("skill_files_json")),
            "metadata_json": _open_gallery_seed_metadata(row.get("metadata_json")),
            "status": "published",
            "permissions_json": _json_object(row.get("permissions_json")),
            "runtime_config_json": _json_object(row.get("runtime_config_json")),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["general_skill"][source_id] = existing.id
        elif existing_by_id or existing_by_slug:
            continue
        else:
            session.add(GeneralSkill(id=source_id, **payload))
            id_maps["general_skill"][source_id] = source_id


def _seed_tools(
    session: Session,
    rows: Iterable[JsonDict],
    resource_ids: dict[str, set[str]],
    id_maps: dict[str, dict[str, str]],
) -> None:
    """写入工具（Tool）记录。

    按源 ID 和（租户 + 名称）双重匹配现有记录。

    Args:
        session: 数据库会话。
        rows: 夹具中的工具记录迭代器。
        resource_ids: 按资源类型分组的资源 ID 集合，用于筛选需写入的记录。
        id_maps: ID 映射表，函数会写入 ``tool`` 映射。
    """
    selected_ids = resource_ids.get("tool", set())
    for row in rows:
        source_id = str(row.get("id") or "")
        if source_id not in selected_ids:
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        existing_by_id = session.get(Tool, source_id)
        existing_by_name = session.exec(
            select(Tool).where(Tool.tenant_id == TENANT_ID, Tool.name == name)
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_name, source_id)
        payload = {
            "tenant_id": TENANT_ID,
            "name": name,
            "display_name": row.get("display_name"),
            "description": row.get("description"),
            "bucket": row.get("bucket") or "未分桶",  # 默认归入"未分桶"桶
            "tool_type": row.get("tool_type") or "http",  # 默认工具类型为 HTTP
            "method": row.get("method") or "POST",  # 默认 HTTP 方法为 POST
            "url": row.get("url") or "",
            "headers_json": _json_object(row.get("headers_json")),
            "auth_json": _json_object(row.get("auth_json")),
            "config_json": _json_object(row.get("config_json")),
            "input_schema": _json_object(row.get("input_schema")),
            "output_schema": _json_object(row.get("output_schema")),
            "allowed_skills_json": _json_list(row.get("allowed_skills_json")),
            "mcp_server_id": row.get("mcp_server_id"),
            "enabled": bool(row.get("enabled", True)),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["tool"][source_id] = existing.id
        elif existing_by_id or existing_by_name:
            continue
        else:
            session.add(Tool(id=source_id, **payload))
            id_maps["tool"][source_id] = source_id


def _seed_knowledge(
    session: Session,
    data: JsonDict,
    resource_ids: dict[str, set[str]],
    id_maps: dict[str, dict[str, str]],
) -> None:
    """写入知识库及其全部子资源。

    按依赖顺序依次写入：知识库 → 版本 → 文档 → 分桶 → 分块 → 概念 →
    发现建议 → 摄入任务。每个子资源通过 ``id_maps`` 引用其父资源的实际 ID。

    Args:
        session: 数据库会话。
        data: 完整的夹具数据字典。
        resource_ids: 按资源类型分组的资源 ID 集合，用于筛选需写入的知识库。
        id_maps: ID 映射表，函数会写入知识库及其子资源的映射。
    """
    selected_ids = resource_ids.get("knowledge_base", set())
    # === 知识库主体 ===
    for row in data.get("knowledge_bases", []):
        source_id = str(row.get("id") or "")
        if source_id not in selected_ids:
            continue
        name = str(row.get("name") or "").strip()
        if not name:
            continue
        existing_by_id = session.get(KnowledgeBase, source_id)
        existing_by_name = session.exec(
            select(KnowledgeBase).where(KnowledgeBase.tenant_id == TENANT_ID, KnowledgeBase.name == name)
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_name, source_id)
        payload = {
            "tenant_id": TENANT_ID,
            "name": name,
            "description": row.get("description"),
            "status": row.get("status") or "active",
            "metadata_json": _open_gallery_seed_metadata(row.get("metadata_json")),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["knowledge_base"][source_id] = existing.id
        elif existing_by_id or existing_by_name:
            continue
        else:
            session.add(KnowledgeBase(id=source_id, **payload))
            id_maps["knowledge_base"][source_id] = source_id
    session.flush()

    # === 知识库子资源（按依赖顺序写入） ===
    _seed_knowledge_versions(session, data.get("knowledge_base_versions", []), id_maps)
    _seed_knowledge_documents(session, data.get("knowledge_documents", []), id_maps)
    _seed_knowledge_buckets(session, data.get("knowledge_buckets", []), id_maps)
    _seed_knowledge_chunks(session, data.get("knowledge_chunks", []), id_maps)
    _seed_knowledge_concepts(session, data.get("knowledge_concepts", []), id_maps)
    _seed_knowledge_discovery(session, data.get("knowledge_discovery_suggestions", []), id_maps)
    _seed_knowledge_jobs(session, data.get("knowledge_ingest_jobs", []), id_maps)


def _seed_knowledge_versions(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    """写入知识库版本（KnowledgeBaseVersion）记录。

    通过 ``id_maps["knowledge_base"]`` 将夹具中的源知识库 ID 映射为实际数据库 ID。

    Args:
        session: 数据库会话。
        rows: 夹具中的知识库版本记录迭代器。
        id_maps: ID 映射表，函数会写入 ``knowledge_base_version`` 映射。
    """
    for row in rows:
        # 将源知识库 ID 映射为目标 ID；若知识库本身未写入则跳过
        kb_id = id_maps["knowledge_base"].get(str(row.get("knowledge_base_id") or ""))
        if not kb_id:
            continue
        version = str(row.get("version") or "1.0.0")
        source_id = str(row.get("id") or "")
        existing_by_id = session.get(KnowledgeBaseVersion, source_id)
        existing_by_version = session.exec(
            select(KnowledgeBaseVersion).where(
                KnowledgeBaseVersion.tenant_id == TENANT_ID,
                KnowledgeBaseVersion.knowledge_base_id == kb_id,
                KnowledgeBaseVersion.version == version,
            )
        ).first()
        existing = _seed_update_target(existing_by_id, existing_by_version, source_id)
        payload = {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": kb_id,
            "version": version,
            "name": row.get("name") or version,
            "description": row.get("description"),
            "status": row.get("status") or "active",
            "metadata_json": _open_gallery_seed_metadata(row.get("metadata_json")),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["knowledge_base_version"][source_id] = existing.id
        elif existing_by_id or existing_by_version:
            continue
        else:
            session.add(KnowledgeBaseVersion(id=source_id, **payload))
            id_maps["knowledge_base_version"][source_id] = source_id
    session.flush()


def _seed_knowledge_documents(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    """写入知识文档（KnowledgeDocument）记录。

    通过 ``id_maps`` 映射知识库 ID 和版本 ID。

    Args:
        session: 数据库会话。
        rows: 夹具中的知识文档记录迭代器。
        id_maps: ID 映射表，函数会写入 ``knowledge_document`` 映射。
    """
    for row in rows:
        kb_id = id_maps["knowledge_base"].get(str(row.get("knowledge_base_id") or ""))
        kb_ver_id = id_maps["knowledge_base_version"].get(str(row.get("knowledge_base_version_id") or ""))
        # 知识库和版本必须都已写入，文档才能正确关联
        if not kb_id or not kb_ver_id:
            continue
        source_id = str(row.get("id") or "")
        existing = session.get(KnowledgeDocument, source_id)
        payload = {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": kb_id,
            "knowledge_base_version_id": kb_ver_id,
            "filename": row.get("filename") or "document",
            "file_type": row.get("file_type") or "text",
            "title": row.get("title"),
            "status": row.get("status") or "ready",
            "bucket_count": int(row.get("bucket_count") or 0),
            "chunk_count": int(row.get("chunk_count") or 0),
            "metadata_json": _seed_metadata(row.get("metadata_json")),
            "error": row.get("error"),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["knowledge_document"][source_id] = existing.id
        else:
            session.add(KnowledgeDocument(id=source_id, **payload))
            id_maps["knowledge_document"][source_id] = source_id
    session.flush()


def _seed_knowledge_buckets(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    """写入知识分桶（KnowledgeBucket）记录。

    分桶是文档内容的逻辑分组，通过 ``id_maps`` 关联知识库、版本与文档。

    Args:
        session: 数据库会话。
        rows: 夹具中的知识分桶记录迭代器。
        id_maps: ID 映射表，函数会写入 ``knowledge_bucket`` 映射。
    """
    for row in rows:
        kb_id = id_maps["knowledge_base"].get(str(row.get("knowledge_base_id") or ""))
        kb_ver_id = id_maps["knowledge_base_version"].get(str(row.get("knowledge_base_version_id") or ""))
        doc_id = id_maps["knowledge_document"].get(str(row.get("document_id") or ""))
        # 知识库、版本、文档三者缺一不可
        if not kb_id or not kb_ver_id or not doc_id:
            continue
        source_id = str(row.get("id") or "")
        existing = session.get(KnowledgeBucket, source_id)
        payload = {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": kb_id,
            "knowledge_base_version_id": kb_ver_id,
            "document_id": doc_id,
            "bucket_key": row.get("bucket_key") or source_id,
            "title": row.get("title") or "Untitled",
            "summary": row.get("summary") or "",
            "token_estimate": int(row.get("token_estimate") or 0),
            "metadata_json": _seed_metadata(row.get("metadata_json")),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
            id_maps["knowledge_bucket"][source_id] = existing.id
        else:
            session.add(KnowledgeBucket(id=source_id, **payload))
            id_maps["knowledge_bucket"][source_id] = source_id
    session.flush()


def _seed_knowledge_chunks(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    """写入知识分块（KnowledgeChunk）记录。

    分块是分桶内的最小检索单元，通过 ``id_maps`` 关联知识库、版本、文档与分桶。

    Args:
        session: 数据库会话。
        rows: 夹具中的知识分块记录迭代器。
        id_maps: ID 映射表，用于将父资源 ID 映射为实际数据库 ID。
    """
    for row in rows:
        kb_id = id_maps["knowledge_base"].get(str(row.get("knowledge_base_id") or ""))
        kb_ver_id = id_maps["knowledge_base_version"].get(str(row.get("knowledge_base_version_id") or ""))
        doc_id = id_maps["knowledge_document"].get(str(row.get("document_id") or ""))
        bucket_id = id_maps["knowledge_bucket"].get(str(row.get("bucket_id") or ""))
        # 四级外键必须全部有效
        if not kb_id or not kb_ver_id or not doc_id or not bucket_id:
            continue
        existing = session.get(KnowledgeChunk, str(row.get("id") or ""))
        payload = {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": kb_id,
            "knowledge_base_version_id": kb_ver_id,
            "document_id": doc_id,
            "bucket_id": bucket_id,
            "chunk_index": int(row.get("chunk_index") or 0),
            "content": row.get("content") or "",
            "summary": row.get("summary"),
            "source_ref": row.get("source_ref"),
            "metadata_json": _seed_metadata(row.get("metadata_json")),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
        else:
            session.add(KnowledgeChunk(id=str(row.get("id") or ""), **payload))


def _seed_knowledge_concepts(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    """写入知识概念（KnowledgeConcept）记录。

    概念是从知识文档中抽取的结构化实体，按源 ID 或（租户 + 版本 + concept_id）匹配。

    Args:
        session: 数据库会话。
        rows: 夹具中的知识概念记录迭代器。
        id_maps: ID 映射表，用于将知识库和版本 ID 映射为实际数据库 ID。
    """
    for row in rows:
        kb_id = id_maps["knowledge_base"].get(str(row.get("knowledge_base_id") or ""))
        kb_ver_id = id_maps["knowledge_base_version"].get(str(row.get("knowledge_base_version_id") or ""))
        if not kb_id or not kb_ver_id:
            continue
        doc_id = id_maps["knowledge_document"].get(str(row.get("document_id") or ""))
        concept_id = row.get("concept_id") or str(row.get("id") or "")
        # 优先按 ID 查找，其次按业务键（版本 + concept_id）查找
        existing = session.get(KnowledgeConcept, str(row.get("id") or "")) or session.exec(
            select(KnowledgeConcept).where(
                KnowledgeConcept.tenant_id == TENANT_ID,
                KnowledgeConcept.knowledge_base_version_id == kb_ver_id,
                KnowledgeConcept.concept_id == concept_id,
            )
        ).first()
        payload = {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": kb_id,
            "knowledge_base_version_id": kb_ver_id,
            "document_id": doc_id,
            "concept_id": concept_id,
            "concept_type": row.get("concept_type") or "Concept",
            "title": row.get("title") or concept_id,
            "description": row.get("description"),
            "content_md": row.get("content_md") or "",
            "frontmatter_json": _json_object(row.get("frontmatter_json")),
            "links_json": _json_list(row.get("links_json")),
            "citations_json": _json_list(row.get("citations_json")),
            "source_refs_json": _json_list(row.get("source_refs_json")),
            "status": row.get("status") or "active",
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
        else:
            session.add(KnowledgeConcept(id=str(row.get("id") or ""), **payload))


def _seed_knowledge_discovery(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    """写入知识发现建议（KnowledgeDiscoverySuggestion）记录。

    发现建议是系统自动从知识文档中提取的结构化提示（如缺失知识点、
    关系建议等），供人工审核采纳。

    Args:
        session: 数据库会话。
        rows: 夹具中的发现建议记录迭代器。
        id_maps: ID 映射表，用于将知识库、版本、文档与分桶 ID 映射为实际数据库 ID。
    """
    for row in rows:
        kb_id = id_maps["knowledge_base"].get(str(row.get("knowledge_base_id") or ""))
        kb_ver_id = id_maps["knowledge_base_version"].get(str(row.get("knowledge_base_version_id") or ""))
        doc_id = id_maps["knowledge_document"].get(str(row.get("document_id") or ""))
        bucket_id = id_maps["knowledge_bucket"].get(str(row.get("bucket_id") or ""))
        if not kb_id or not kb_ver_id or not doc_id:
            continue
        existing = session.get(KnowledgeDiscoverySuggestion, str(row.get("id") or ""))
        payload = {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": kb_id,
            "knowledge_base_version_id": kb_ver_id,
            "document_id": doc_id,
            "bucket_id": bucket_id,
            "suggestion_type": row.get("suggestion_type") or "suggestion",
            "title": row.get("title") or "Suggestion",
            "status": row.get("status") or "pending",
            "payload_json": _json_object(row.get("payload_json")),
            "source_refs_json": _json_list(row.get("source_refs_json")),
            "reason": row.get("reason"),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
        else:
            session.add(KnowledgeDiscoverySuggestion(id=str(row.get("id") or ""), **payload))


def _seed_knowledge_jobs(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    """写入知识摄入任务（KnowledgeIngestJob）记录。

    摄入任务记录文档被解析为知识结构时的执行轨迹（状态、阶段、进度等）。

    Args:
        session: 数据库会话。
        rows: 夹具中的摄入任务记录迭代器。
        id_maps: ID 映射表，用于将知识库、版本与文档 ID 映射为实际数据库 ID。
    """
    for row in rows:
        kb_id = id_maps["knowledge_base"].get(str(row.get("knowledge_base_id") or ""))
        kb_ver_id = id_maps["knowledge_base_version"].get(str(row.get("knowledge_base_version_id") or ""))
        doc_id = id_maps["knowledge_document"].get(str(row.get("document_id") or ""))
        if not kb_id or not kb_ver_id:
            continue
        existing = session.get(KnowledgeIngestJob, str(row.get("id") or ""))
        payload = {
            "tenant_id": TENANT_ID,
            "knowledge_base_id": kb_id,
            "knowledge_base_version_id": kb_ver_id,
            "document_id": doc_id,
            "filename": row.get("filename") or "document",
            "status": row.get("status") or "completed",
            "stage": row.get("stage") or "completed",
            "progress": float(row.get("progress") or 0),
            "error": row.get("error"),
            "metadata_json": _seed_metadata(row.get("metadata_json")),
            "started_at": _parse_datetime(row.get("started_at")),
            "finished_at": _parse_datetime(row.get("finished_at")),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
        else:
            session.add(KnowledgeIngestJob(id=str(row.get("id") or ""), **payload))


def _seed_agent_resource_bindings(
    session: Session, rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    """写入代理-资源绑定（AgentResourceBinding）记录。

    将代理与技能、工具、知识库等资源建立绑定关系。
    通过 ``id_maps`` 同时映射代理 ID 和资源 ID。

    Args:
        session: 数据库会话。
        rows: 夹具中的绑定记录迭代器。
        id_maps: ID 映射表，用于将代理 ID 与资源 ID 映射为实际数据库 ID。
    """
    for row in rows:
        agent_id = id_maps["agent"].get(str(row.get("agent_id") or ""))
        resource_type = str(row.get("resource_type") or "")
        resource_id = _mapped_resource_id(resource_type, str(row.get("resource_id") or ""), id_maps)
        # 代理或资源未写入时跳过
        if not agent_id or not resource_id:
            continue
        # 为绑定注入代理私有的元数据标记
        metadata = agent_private_metadata(agent_id, _seed_metadata(row.get("metadata_json")))
        existing = session.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == TENANT_ID,
                AgentResourceBinding.agent_id == agent_id,
                AgentResourceBinding.resource_type == resource_type,
                AgentResourceBinding.resource_id == resource_id,
            )
        ).first()
        payload = {
            "tenant_id": TENANT_ID,
            "agent_id": agent_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "status": "active",
            "metadata_json": metadata,
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
        else:
            session.add(AgentResourceBinding(id=str(row.get("id") or ""), **payload))


def _seed_skill_branches(
    session: Session,
    branch_rows: Iterable[JsonDict],
    version_rows: Iterable[JsonDict],
    id_maps: dict[str, dict[str, str]],
) -> None:
    """写入代理技能分支（AgentSkillBranch）及其版本（AgentSkillBranchVersion）。

    分支代表某个代理对特定技能的定制化副本。分两步执行：
    1. 写入分支主体记录。
    2. 写入分支的版本历史记录。

    Args:
        session: 数据库会话。
        branch_rows: 夹具中的分支记录迭代器。
        version_rows: 夹具中的分支版本记录迭代器。
        id_maps: ID 映射表，用于将代理 ID 映射为实际数据库 ID，
            并通过 ``skill_business`` 验证技能是否存在。
    """
    # 记录已创建的分支键（agent_id, skill_id），供版本写入时校验
    branch_keys: set[tuple[str, str]] = set()
    for row in branch_rows:
        agent_id = id_maps["agent"].get(str(row.get("agent_id") or ""))
        skill_id = str(row.get("skill_id") or "")
        if not agent_id or skill_id not in id_maps["skill_business"]:
            continue
        branch_keys.add((agent_id, skill_id))
        existing = session.get(AgentSkillBranch, str(row.get("id") or "")) or session.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == TENANT_ID,
                AgentSkillBranch.agent_id == agent_id,
                AgentSkillBranch.skill_id == skill_id,
            )
        ).first()
        payload = {
            "tenant_id": TENANT_ID,
            "agent_id": agent_id,
            "skill_id": skill_id,
            "source_skill_id": row.get("source_skill_id") or skill_id,
            "base_version": row.get("base_version") or "1.0.0",
            "head_version": row.get("head_version") or row.get("base_version") or "1.0.0",
            "content_json": _json_object(row.get("content_json")),
            "status": row.get("status") or "active",
            "sync_state": row.get("sync_state") or "synced",
            "metadata_json": agent_private_metadata(agent_id, _seed_metadata(row.get("metadata_json"))),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
        else:
            session.add(AgentSkillBranch(id=str(row.get("id") or ""), **payload))
    session.flush()

    # 第二步：写入分支版本记录
    for row in version_rows:
        agent_id = id_maps["agent"].get(str(row.get("agent_id") or ""))
        skill_id = str(row.get("skill_id") or "")
        version = str(row.get("version") or "1.0.0")
        # 校验对应的分支是否存在
        if not agent_id or (agent_id, skill_id) not in branch_keys:
            continue
        existing = session.get(AgentSkillBranchVersion, str(row.get("id") or "")) or session.exec(
            select(AgentSkillBranchVersion).where(
                AgentSkillBranchVersion.tenant_id == TENANT_ID,
                AgentSkillBranchVersion.agent_id == agent_id,
                AgentSkillBranchVersion.skill_id == skill_id,
                AgentSkillBranchVersion.version == version,
            )
        ).first()
        payload = {
            "tenant_id": TENANT_ID,
            "agent_id": agent_id,
            "skill_id": skill_id,
            "source_skill_id": row.get("source_skill_id") or skill_id,
            "version": version,
            "base_version": row.get("base_version") or "1.0.0",
            "content_json": _json_object(row.get("content_json")),
            "status": row.get("status") or "active",
            "sync_state": row.get("sync_state") or "synced",
            "change_summary": row.get("change_summary"),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
        else:
            session.add(AgentSkillBranchVersion(id=str(row.get("id") or ""), **payload))


def _seed_knowledge_branches(
    session: Session, binding_rows: Iterable[JsonDict], id_maps: dict[str, dict[str, str]]
) -> None:
    """写入代理知识分支（AgentKnowledgeBranch）记录。

    从知识库绑定记录中派生出知识分支，表示某个代理对特定知识库的定制化引用。
    同时更新知识库元数据中的 ``current_version`` 字段。

    Args:
        session: 数据库会话。
        binding_rows: 夹具中的绑定记录迭代器，仅处理 ``resource_type == "knowledge_base"`` 的记录。
        id_maps: ID 映射表，用于将代理 ID 和知识库 ID 映射为实际数据库 ID。
    """
    for row in binding_rows:
        if row.get("resource_type") != "knowledge_base":
            # 仅处理知识库类型的绑定
            continue
        agent_id = id_maps["agent"].get(str(row.get("agent_id") or ""))
        kb_id = id_maps["knowledge_base"].get(str(row.get("resource_id") or ""))
        if not agent_id or not kb_id:
            continue
        kb = session.get(KnowledgeBase, kb_id)
        # 确定该代理对应的知识库版本
        current_version = _knowledge_version_with_seed_content(session, kb_id, agent_id)
        if kb:
            # 更新知识库元数据中的当前版本指针
            kb_metadata = dict(kb.metadata_json or {})
            kb_metadata["current_version"] = current_version
            kb.metadata_json = kb_metadata
            kb.updated_at = utc_now()
            session.add(kb)
        existing = session.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == TENANT_ID,
                AgentKnowledgeBranch.agent_id == agent_id,
                AgentKnowledgeBranch.knowledge_base_id == kb_id,
            )
        ).first()
        payload = {
            "tenant_id": TENANT_ID,
            "agent_id": agent_id,
            "knowledge_base_id": kb_id,
            "base_version": current_version,
            "head_version": current_version,
            "status": "active",
            "sync_state": "synced",
            "metadata_json": agent_private_metadata(agent_id, _seed_metadata(row.get("metadata_json"))),
        }
        if existing:
            _apply_payload(existing, payload)
            existing.updated_at = utc_now()
            session.add(existing)
        else:
            session.add(AgentKnowledgeBranch(**payload))


def _knowledge_version_with_seed_content(session: Session, kb_id: str, agent_id: str) -> str:
    """查找包含种子内容的、属于指定代理的知识库版本。

    优先匹配 owner_agent_id 为当前代理或版本号中包含 ``branch.<agent_id>.`` 的版本；
    若未找到，回退到该知识库的第一个版本；最终回退到知识库元数据中的
    ``current_version`` 或默认值 ``"1.0.0"``。

    Args:
        session: 数据库会话。
        kb_id: 知识库 ID。
        agent_id: 代理 ID。

    Returns:
        匹配到的版本号字符串。
    """
    documents = session.exec(
        select(KnowledgeDocument).where(
            KnowledgeDocument.tenant_id == TENANT_ID,
            KnowledgeDocument.knowledge_base_id == kb_id,
            KnowledgeDocument.knowledge_base_version_id != None,  # noqa: E711
        )
    ).all()
    fallback_version: str | None = None
    for document in documents:
        version = session.get(KnowledgeBaseVersion, document.knowledge_base_version_id)
        if not version:
            continue
        # 记录第一个有效版本作为回退值
        fallback_version = fallback_version or version.version
        # 优先匹配拥有者为当前代理的版本
        if (version.metadata_json or {}).get("owner_agent_id") == agent_id:
            return version.version
        # 其次匹配版本号中包含 branch.<agent_id>. 前缀的版本
        if f"branch.{agent_id}." in version.version:
            return version.version
    if fallback_version:
        return fallback_version
    # 最终回退到知识库元数据中记录的当前版本
    kb = session.get(KnowledgeBase, kb_id)
    if kb:
        return str((kb.metadata_json or {}).get("current_version") or "1.0.0")
    return "1.0.0"


def _publish_gallery_resources(session: Session, id_maps: dict[str, dict[str, str]]) -> None:
    """将已写入的资源发布到公共展厅（open gallery）。

    对技能、通用技能、工具与知识库四类资源，更新其元数据为展厅发布标记，
    并通过 ``ensure_open_gallery_binding`` 创建对应的展厅绑定记录。

    Args:
        session: 数据库会话。
        id_maps: ID 映射表，用于获取各类资源的目标 ID 集合。
    """
    metadata = _open_gallery_seed_metadata({})
    for resource_type, model, map_key in (
        ("skill", Skill, "skill"),
        ("general_skill", GeneralSkill, "general_skill"),
        ("tool", Tool, "tool"),
        ("knowledge_base", KnowledgeBase, "knowledge_base"),
    ):
        for resource_id in set(id_maps[map_key].values()):
            resource = session.get(model, resource_id)
            if resource is None:
                continue
            if hasattr(resource, "metadata_json"):
                # 更新资源的元数据为展厅发布标记
                resource.metadata_json = _open_gallery_seed_metadata(getattr(resource, "metadata_json", None))
                resource.updated_at = utc_now()
                session.add(resource)
            # 确保该资源在公共展厅中存在绑定记录
            ensure_open_gallery_binding(
                session,
                TENANT_ID,
                resource_type,
                resource_id,
                "active",
                metadata_json=metadata,
            )


def _sync_seed_agents_to_current_admin(
    session: Session, id_maps: dict[str, dict[str, str]]
) -> None:
    """将种子代理的归属信息同步到当前数据库中的管理员账户。

    查找当前管理员用户，若存在则将其身份信息（ID、用户名、显示名）写入
    所有种子代理的元数据中，确保代理的 owner/created_by 指向真实的管理员。

    Args:
        session: 数据库会话。
        id_maps: ID 映射表，通过 ``id_maps["agent"]`` 获取种子代理 ID 集合。
    """
    admin = session.exec(
        select(User).where(User.tenant_id == TENANT_ID, User.username == ADMIN_USERNAME)
    ).first()
    if not admin:
        # 管理员账户不存在时静默退出
        return
    # 构建归属元数据，包含 owner 和 created_by 两组信息
    admin_metadata = {
        "owner_user_id": admin.id,
        "owner_username": admin.username,
        "owner_display_name": admin.display_name or ADMIN_DISPLAY_NAME,
        "created_by_user_id": admin.id,
        "created_by_username": admin.username,
        "created_by": admin.username,
        "created_by_display_name": admin.display_name or ADMIN_DISPLAY_NAME,
        "creator_name": admin.username,
    }
    for agent_id in set(id_maps["agent"].values()):
        agent = session.get(AgentProfile, agent_id)
        if agent is None:
            continue
        metadata = dict(agent.metadata_json or {})
        metadata.update(admin_metadata)
        # 标记为已发布到展厅且由种子流程管理
        metadata["published_to_gallery"] = True
        metadata["gallery_published_by"] = admin.username
        metadata["seed_source"] = SEED_SOURCE
        metadata["managed_by_seed"] = True
        agent.metadata_json = metadata
        agent.updated_at = utc_now()
        session.add(agent)


def _resource_ids_by_type(rows: Iterable[JsonDict]) -> dict[str, set[str]]:
    """从绑定记录中按资源类型提取资源 ID 集合。

    Args:
        rows: 绑定记录迭代器，每条记录包含 ``resource_type`` 和 ``resource_id`` 字段。

    Returns:
        以资源类型为 key、资源 ID 集合为 value 的字典。
    """
    result: dict[str, set[str]] = {}
    for row in rows:
        resource_type = str(row.get("resource_type") or "")
        resource_id = str(row.get("resource_id") or "")
        if resource_type and resource_id:
            result.setdefault(resource_type, set()).add(resource_id)
    return result


def _mapped_resource_id(
    resource_type: str, source_id: str, id_maps: dict[str, dict[str, str]]
) -> str | None:
    """根据资源类型将夹具中的源资源 ID 映射为目标数据库 ID。

    Args:
        resource_type: 资源类型，如 ``"skill"``、``"tool"``。
        source_id: 夹具中的源资源 ID。
        id_maps: ID 映射表。

    Returns:
        映射后的目标 ID；若资源类型不受支持或源 ID 未在映射表中，返回 None。
    """
    map_key = {
        "skill": "skill",
        "general_skill": "general_skill",
        "tool": "tool",
        "knowledge_base": "knowledge_base",
    }.get(resource_type)
    if map_key is None:
        return None
    return id_maps[map_key].get(source_id)


def _agent_metadata(value: Any) -> JsonDict:
    """构建代理专用的种子元数据。

    在通用种子元数据的基础上，追加展厅发布与种子管理标记，
    并移除与代理语义冲突的字段（scope、visibility、owner_agent_id、created_from_agent）。

    Args:
        value: 夹具中原始的元数据（dict 或 JSON 字符串）。

    Returns:
        合并后的代理元数据字典。
    """
    metadata = _seed_metadata(value)
    metadata.update(
        {
            "published_to_gallery": True,
            "gallery_published_by": ADMIN_USERNAME,
            "seed_source": SEED_SOURCE,
            "managed_by_seed": True,
        }
    )
    # 移除与代理本体语义冲突的继承字段
    metadata.pop("scope", None)
    metadata.pop("visibility", None)
    metadata.pop("owner_agent_id", None)
    metadata.pop("created_from_agent", None)
    return metadata


def _open_gallery_seed_metadata(value: Any) -> JsonDict:
    """构建展厅资源的种子元数据。

    在通用种子元数据的基础上，应用展厅公开标记（open_gallery_metadata），
    并追加种子来源标记。

    Args:
        value: 夹具中原始的元数据（dict 或 JSON 字符串）。

    Returns:
        合并后的展厅资源元数据字典。
    """
    metadata = open_gallery_metadata(_seed_metadata(value))
    metadata["seed_source"] = SEED_SOURCE
    metadata["managed_by_seed"] = True
    return metadata


def _seed_metadata(value: Any) -> JsonDict:
    """构建通用的种子元数据基线。

    将原始元数据解析为 dict 后，注入管理员归属与创建者信息，
    作为所有种子资源的统一元数据前缀。

    Args:
        value: 夹具中原始的元数据（dict 或 JSON 字符串）。

    Returns:
        包含管理员归属信息的元数据字典。
    """
    metadata = _json_object(value)
    metadata.update(
        {
            "owner_user_id": ADMIN_USER_ID,
            "owner_username": ADMIN_USERNAME,
            "owner_display_name": ADMIN_DISPLAY_NAME,
            "created_by_user_id": ADMIN_USER_ID,
            "created_by_username": ADMIN_USERNAME,
            "created_by": ADMIN_USERNAME,
            "created_by_display_name": ADMIN_DISPLAY_NAME,
            "creator_name": ADMIN_USERNAME,
        }
    )
    return metadata


def _seed_update_target(
    existing_by_id: ModelT | None,
    existing_by_key: ModelT | None,
    source_id: str,
) -> ModelT | None:
    """判断哪条已有记录应作为种子更新的目标。

    匹配策略：
    - 若按 ID 和按业务键各找到一条**不同**的记录，优先选择由种子管理的业务键记录；
      若该记录不由种子管理，则返回 None（表示跳过，避免覆盖非种子数据）。
    - 若只有一条记录存在且由种子管理，则返回该记录。
    - 其他情况返回 None。

    Args:
        existing_by_id: 按主键 ID 查找到的记录（可能为 None）。
        existing_by_key: 按业务键（名称/slug 等）查找到的记录（可能为 None）。
        source_id: 夹具中的源 ID，用于判断记录 ID 是否一致。

    Returns:
        可被种子流程安全更新的记录；若不存在合适的记录则返回 None。
    """
    if (
        existing_by_id is not None
        and existing_by_key is not None
        and getattr(existing_by_id, "id", None) != getattr(existing_by_key, "id", None)
    ):
        # ID 和业务键指向不同记录：仅当业务键记录由种子管理时才更新它
        return existing_by_key if _is_seed_managed(existing_by_key, source_id) else None
    for candidate in (existing_by_id, existing_by_key):
        if candidate is not None and _is_seed_managed(candidate, source_id):
            return candidate
    return None


def _is_seed_managed(row: object, source_id: str) -> bool:
    """判断一条记录是否由种子流程管理（可被安全更新）。

    判定条件（满足任一即为种子管理）：
    - 记录的 ID 与源 ID 完全一致。
    - 记录的元数据中包含 ``seed_source == SEED_SOURCE``。
    - 记录的元数据中 ``managed_by_seed`` 为 True。

    Args:
        row: 待检查的 ORM 模型实例。
        source_id: 夹具中的源 ID。

    Returns:
        若该记录由种子流程管理则返回 True，否则返回 False。
    """
    if getattr(row, "id", None) == source_id:
        return True
    metadata = getattr(row, "metadata_json", None) or {}
    if not isinstance(metadata, dict):
        return False
    return metadata.get("seed_source") == SEED_SOURCE or metadata.get("managed_by_seed") is True


def _json_object(value: Any) -> JsonDict:
    """安全地将任意输入解析为 JSON 字典。

    支持以下输入类型：
    - dict：直接复制返回。
    - 合法的 JSON 对象字符串：解析后返回。
    - JSON 数组字符串或非法 JSON：返回空字典。
    - None 或空值：返回空字典。

    Args:
        value: 待解析的输入值。

    Returns:
        解析后的字典；无法解析时返回空字典。
    """
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _json_list(value: Any) -> list[Any]:
    """安全地将任意输入解析为 JSON 列表。

    支持以下输入类型：
    - list：直接复制返回。
    - 合法的 JSON 数组字符串：解析后返回。
    - JSON 对象字符串或非法 JSON：返回空列表。
    - None 或空值：返回空列表。

    Args:
        value: 待解析的输入值。

    Returns:
        解析后的列表；无法解析时返回空列表。
    """
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return list(parsed) if isinstance(parsed, list) else []
    return []


def _parse_datetime(value: Any) -> datetime | None:
    """安全地将输入解析为 datetime 对象。

    支持 datetime 实例直接返回，以及 ISO 8601 格式字符串的解析。

    Args:
        value: 待解析的输入值（datetime、字符串或其他）。

    Returns:
        解析后的 datetime 对象；无法解析时返回 None。
    """
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _apply_payload(row: Any, payload: JsonDict) -> None:
    """将 payload 字典中的字段批量赋值到 ORM 模型实例上。

    Args:
        row: 待更新的 ORM 模型实例。
        payload: 字段名到字段值的映射。
    """
    for key, value in payload.items():
        setattr(row, key, value)
