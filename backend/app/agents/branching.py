"""Agent 资源分支管理系统核心模块。

本模块实现了 Agent 资源的分支化（branching）管理体系，包含以下核心能力：

1. **资源作用域管理**：区分「公共画廊」(open gallery) 和「Agent 私有」(agent private) 两种资源作用域，
   支持资源的可见性控制和元数据标记。
2. **技能分支管理**：为每个 Agent 维护独立的技能(Skill)分支，支持分支创建、改写、
   与整体版本同步(sync)、推送回整体(promote)、版本回滚(rollback)等操作。
3. **知识库分支管理**：为每个 Agent 维护独立的知识库(KnowledgeBase)分支，支持版本快照、
   资产克隆（文档/桶/分块/概念/建议）、分支同步与推送。
4. **模型绑定解析**：根据 Agent 配置和角色，解析出运行时使用的 LLM 模型配置。
5. **创建者元数据保护**：确保资源在复制/迁移过程中不丢失原始创建者信息。

核心设计模式：
- 整体 Agent (overall agent) 作为公共资源的中央仓库
- 非 Agent 通过分支(branch)机制拥有资源的私有副本
- 分支可以与整体版本双向同步（sync / promote）
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, Iterable

from sqlmodel import Session, select

from app.db.models import (
    AgentKnowledgeBranch,
    AgentModelBinding,
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
    ModelConfig,
    Skill,
    SkillVersion,
    Tool,
    utc_now,
)
from app.llm.model_config_resolver import (
    ResolvedModelConfig,
    resolve_model_config_for_runtime,
)


# Agent 支持的模型绑定角色列表
DEFAULT_AGENT_ROLES = ("default", "router", "step", "response", "general_skill")
# 公共画廊作用域标识，标记为所有 Agent 共享的公共资源
OPEN_GALLERY_SCOPE = "open_gallery"
# Agent 私有作用域标识，标记为某个 Agent 专属的私有资源
AGENT_PRIVATE_SCOPE = "agent_private"
# 标准创建者元数据键名集合
STANDARD_CREATOR_METADATA_KEYS = (
    "creator_name",
    "created_by",
    "created_by_display_name",
    "created_by_username",
)
# 资源来源相关的创建者元数据键名集合
CREATOR_SOURCE_METADATA_KEYS = (
    "gallery_published_by",
    "owner_display_name",
    "owner_username",
    "created_by_user_id",
    "owner_user_id",
)
# 所有创建者元数据键名的合集，用于创建者信息保护逻辑
CREATOR_METADATA_KEYS = STANDARD_CREATOR_METADATA_KEYS + CREATOR_SOURCE_METADATA_KEYS


def _valid_creator_value(value: Any) -> bool:
    """检查创建者元数据值是否有效（非空字符串）。"""
    return isinstance(value, str) and bool(value.strip())


def user_creator_metadata(
    user: object | None, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    """从用户对象提取创建者元数据信息。

    将用户的 ID、用户名、显示名称等信息填充到标准的创建者元数据键中。

    Args:
        user: 用户对象（需具有 id、username、display_name 属性），可为 None。
        extra: 额外的元数据，将与创建者信息合并。

    Returns:
        包含创建者信息的元数据字典。
    """
    metadata = dict(extra or {})
    if user is None:
        return metadata
    user_id = getattr(user, "id", None)
    username = getattr(user, "username", None)
    display_name = getattr(user, "display_name", None) or username
    if _valid_creator_value(user_id):
        metadata["owner_user_id"] = str(user_id).strip()
        metadata["created_by_user_id"] = str(user_id).strip()
    if _valid_creator_value(username):
        normalized_username = str(username).strip()
        metadata["owner_username"] = normalized_username
        metadata["created_by_username"] = normalized_username
        metadata["created_by"] = normalized_username
        metadata["creator_name"] = normalized_username
    if _valid_creator_value(display_name):
        normalized_display_name = str(display_name).strip()
        metadata["owner_display_name"] = normalized_display_name
        metadata["created_by_display_name"] = normalized_display_name
    return metadata


def metadata_preserving_creator(
    existing: dict[str, Any] | None,
    replacement: dict[str, Any] | None,
) -> dict[str, Any]:
    """Replace editable metadata without changing the original creator.

    用替换元数据覆盖现有元数据，但保留原始创建者相关的字段。
    用于资源更新场景，防止编辑操作覆盖资源的原始创建者信息。

    Args:
        existing: 现有的元数据字典。
        replacement: 要替换的元数据字典。

    Returns:
        合并后的元数据字典，原始创建者字段被保留。
    """
    metadata = dict(replacement or {})
    current = dict(existing or {})
    for key in CREATOR_METADATA_KEYS:
        value = current.get(key)
        if _valid_creator_value(value):
            metadata[key] = value
    return metadata


def get_overall_agent(db: Session, tenant_id: str) -> AgentProfile | None:
    """获取指定租户下的整体 Agent（Overall Agent）。

    整体 Agent 是公共资源的中央仓库，每个租户应该只有一个。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。

    Returns:
        整体 Agent 配置对象，若不存在则返回 None。
    """
    return db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.is_overall == True,  # noqa: E712
            AgentProfile.status != "archived",
        )
    ).first()


def get_agent(db: Session, tenant_id: str, agent_id: str | None) -> AgentProfile | None:
    """按ID获取指定租户下未归档的 Agent 配置。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID，为 None 时直接返回 None。

    Returns:
        Agent 配置对象，若不存在或已归档则返回 None。
    """
    if not agent_id:
        return None
    return db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.id == agent_id,
            AgentProfile.status != "archived",
        )
    ).first()


def _agent_creator_metadata(agent: AgentProfile | None) -> dict[str, Any]:
    """从 Agent 配置的 metadata_json 中提取创建者相关的元数据。

    Args:
        agent: Agent 配置对象，可为 None。

    Returns:
        仅包含有效创建者字段的元数据字典。
    """
    if not agent:
        return {}
    source = dict(agent.metadata_json or {})
    metadata = {
        key: value
        for key, value in source.items()
        if key in CREATOR_METADATA_KEYS and _valid_creator_value(value)
    }
    return metadata


def _agent_private_metadata_for(
    db: Session,
    tenant_id: str,
    agent_id: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """为指定 Agent 生成私有作用域的元数据，包含 Agent 创建者信息。

    合并 Agent 的创建者元数据与额外传入的元数据，并标记为 Agent 私有。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID。
        extra: 额外的元数据。

    Returns:
        包含 Agent 私有作用域标记和创建者信息的元数据字典。
    """
    agent_metadata = _agent_creator_metadata(get_agent(db, tenant_id, agent_id))
    return agent_private_metadata(agent_id, {**agent_metadata, **(extra or {})})


def is_overall_agent(db: Session, tenant_id: str, agent_id: str | None) -> bool:
    """判断指定的 Agent 是否为整体 Agent。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID。

    Returns:
        True 表示该 Agent 是整体 Agent。
    """
    agent = get_agent(db, tenant_id, agent_id)
    return bool(agent and agent.is_overall)


def require_overall_agent(db: Session, tenant_id: str, agent_id: str | None) -> None:
    """要求指定的 Agent 为整体 Agent，否则抛出 HTTP 异常。

    用于保护只有整体 Agent 才能执行的操作（如删除全局资源）。
    若租户下不存在整体 Agent 且未指定 agent_id，则跳过检查。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID。

    Raises:
        HTTPException 403: 指定的 Agent 不是整体 Agent。
    """
    if not agent_id and not get_overall_agent(db, tenant_id):
        return
    if not is_overall_agent(db, tenant_id, agent_id):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=403, detail="Only the overall agent can delete global resources"
        )


def open_gallery_metadata(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """生成公共画廊作用域的元数据。

    将资源标记为公共可见，并清除所有 Agent 私有相关的标记。

    Args:
        extra: 额外的元数据，将与公共画廊标记合并。

    Returns:
        包含公共画廊作用域标记的元数据字典。
    """
    metadata = dict(extra or {})
    metadata["scope"] = OPEN_GALLERY_SCOPE
    metadata["visibility"] = OPEN_GALLERY_SCOPE
    metadata.pop("owner_agent_id", None)
    metadata.pop("created_from_agent", None)
    metadata.pop("created_from_upload", None)
    return metadata


def agent_private_metadata(agent_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """生成 Agent 私有作用域的元数据。

    将资源标记为某个 Agent 私有专属，设置所有者 Agent ID。

    Args:
        agent_id: 所有者 Agent ID。
        extra: 额外的元数据，将与私有标记合并。

    Returns:
        包含 Agent 私有作用域标记的元数据字典。
    """
    metadata = dict(extra or {})
    metadata["scope"] = AGENT_PRIVATE_SCOPE
    metadata["visibility"] = AGENT_PRIVATE_SCOPE
    metadata["owner_agent_id"] = agent_id
    metadata["created_from_agent"] = True
    return metadata


def mark_resource_open_gallery(
    resource: object, metadata_json: dict[str, Any] | None = None
) -> None:
    """将资源对象的 metadata_json 标记为公共画廊作用域（就地修改）。

    Args:
        resource: 任意具有 metadata_json 属性的资源对象。
        metadata_json: 要合并的额外元数据。
    """
    if not hasattr(resource, "metadata_json"):
        return
    metadata = open_gallery_metadata(
        {**(getattr(resource, "metadata_json", None) or {}), **(metadata_json or {})}
    )
    setattr(resource, "metadata_json", metadata)


def mark_resource_private_for_agent(
    resource: object,
    agent_id: str,
    metadata_json: dict[str, Any] | None = None,
) -> None:
    """将资源对象的 metadata_json 标记为指定 Agent 私有作用域（就地修改）。

    Args:
        resource: 任意具有 metadata_json 属性的资源对象。
        agent_id: 所有者 Agent ID。
        metadata_json: 要合并的额外元数据。
    """
    if not hasattr(resource, "metadata_json"):
        return
    metadata = agent_private_metadata(
        agent_id, {**(getattr(resource, "metadata_json", None) or {}), **(metadata_json or {})}
    )
    setattr(resource, "metadata_json", metadata)


def ensure_open_gallery_binding(
    db: Session,
    tenant_id: str,
    resource_type: str,
    resource_id: str,
    status: str = "active",
    metadata_json: dict[str, Any] | None = None,
) -> None:
    """为资源在整体 Agent 上创建或更新公共画廊绑定。

    如果存在整体 Agent，则在整体 Agent 下为指定资源建立绑定关系。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        resource_type: 资源类型（skill/knowledge_base/tool 等）。
        resource_id: 资源ID。
        status: 绑定状态，默认 active。
        metadata_json: 额外的元数据。
    """
    overall = get_overall_agent(db, tenant_id)
    if overall:
        _ensure_binding(
            db,
            tenant_id,
            overall.id,
            resource_type,
            resource_id,
            status,
            metadata_json=open_gallery_metadata(metadata_json),
        )


def hide_open_gallery_binding(
    db: Session,
    tenant_id: str,
    resource_type: str,
    resource_id: str,
) -> bool:
    """将资源在整体 Agent 上的绑定状态设为 deleted（隐藏）。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        resource_type: 资源类型。
        resource_id: 资源ID。

    Returns:
        True 表示操作成功（存在整体 Agent），False 表示无整体 Agent。
    """
    overall = get_overall_agent(db, tenant_id)
    if not overall:
        return False
    _ensure_binding(
        db,
        tenant_id,
        overall.id,
        resource_type,
        resource_id,
        "deleted",
        metadata_json=open_gallery_metadata(),
    )
    return True


def ensure_private_resource_binding(
    db: Session,
    tenant_id: str,
    agent_id: str,
    resource_type: str,
    resource_id: str,
    status: str = "active",
    metadata_json: dict[str, Any] | None = None,
) -> None:
    """为资源在指定 Agent 下创建或更新私有绑定。

    绑定的元数据中会包含 Agent 的创建者信息。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: 目标 Agent ID。
        resource_type: 资源类型。
        resource_id: 资源ID。
        status: 绑定状态，默认 active。
        metadata_json: 额外的元数据。
    """
    _ensure_binding(
        db,
        tenant_id,
        agent_id,
        resource_type,
        resource_id,
        status,
        metadata_json=_agent_private_metadata_for(db, tenant_id, agent_id, metadata_json),
    )


def resource_binding_metadata(
    db: Session,
    tenant_id: str,
    agent_id: str | None,
    resource_type: str,
) -> dict[str, dict[str, Any]]:
    """获取指定 Agent 下某种资源类型的所有绑定元数据。

    如果指定 agent_id 的 Agent 不存在或为整体 Agent，则查找整体 Agent 的绑定。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID（可为 None）。
        resource_type: 资源类型。

    Returns:
        以 resource_id 为键、绑定元数据为值的字典。
    """
    agent = get_agent(db, tenant_id, agent_id)
    if not agent:
        agent = get_overall_agent(db, tenant_id)
    if not agent:
        return {}
    bindings = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent.id,
            AgentResourceBinding.resource_type == resource_type,
            AgentResourceBinding.status != "deleted",
        )
    ).all()
    return {binding.resource_id: dict(binding.metadata_json or {}) for binding in bindings}


def is_open_gallery_resource(
    db: Session, tenant_id: str, resource_type: str, resource: object
) -> bool:
    """判断资源是否属于公共画廊（在整体 Agent 上有 active 且非私有的绑定）。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        resource_type: 资源类型。
        resource: 资源对象（需有 id 和 tenant_id 属性）。

    Returns:
        True 表示该资源在公共画廊中可见。
    """
    resource_id = getattr(resource, "id", None)
    if not resource_id or getattr(resource, "tenant_id", None) != tenant_id:
        return False
    overall = get_overall_agent(db, tenant_id)
    if not overall:
        return False
    overall_binding = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == overall.id,
            AgentResourceBinding.resource_type == resource_type,
            AgentResourceBinding.resource_id == resource_id,
        )
    ).first()
    if not overall_binding:
        return False
    return overall_binding.status != "deleted" and not _binding_is_private(overall_binding)


def is_bound_resource_visible_for_agent(
    db: Session,
    tenant_id: str,
    resource_type: str,
    resource: object,
    binding: AgentResourceBinding,
) -> bool:
    """判断已绑定的资源对当前 Agent 是否可见。

    可见条件：绑定状态非 deleted、资源属于当前租户、且满足以下之一：
    - 绑定或资源元数据标记为私有（私有资源始终可见）
    - 资源在公共画廊中可见

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        resource_type: 资源类型。
        resource: 资源对象。
        binding: Agent 与该资源的绑定记录。

    Returns:
        True 表示资源对该 Agent 可见。
    """
    if binding.status == "deleted":
        return False
    if getattr(resource, "tenant_id", None) != tenant_id:
        return False
    if _binding_is_private(binding) or _metadata_is_private(_resource_metadata(resource)):
        return True
    return is_open_gallery_resource(db, tenant_id, resource_type, resource)


def project_skill_with_branch(
    skill: Skill,
    branch: AgentSkillBranch | None,
    binding_status: str | None = None,
) -> Skill:
    """将技能分支的内容投影到一个新的 Skill 对象上。

    根据 branch 的内容构建一个投影 Skill，包含分支特有的版本、内容、元数据等信息。
    投影后的 Skill 通过 ``agent_branch_meta`` 属性携带分支元数据。

    Args:
        skill: 基础技能对象。
        branch: 技能分支对象（为 None 时直接返回原 skill）。
        binding_status: 绑定状态（影响投影后技能的 published/archived 状态）。

    Returns:
        包含分支内容的投影 Skill 对象。
    """
    if not branch:
        return skill
    content = dict(branch.content_json or {})
    content["version"] = branch.head_version
    is_visible = branch.status == "active" and (binding_status in {None, "active"})
    metadata = {
        "agent_id": branch.agent_id,
        "base_version": branch.base_version,
        "head_version": branch.head_version,
        "sync_state": branch.sync_state,
        "status": branch.status,
        "binding_status": binding_status,
        "metadata": dict(branch.metadata_json or {}),
    }
    projected = Skill(
        id=skill.id,
        tenant_id=skill.tenant_id,
        skill_id=skill.skill_id,
        version=branch.head_version,
        name=str(branch.content_json.get("name") or skill.name),
        business_domain=branch.content_json.get("business_domain") or skill.business_domain,
        description=branch.content_json.get("description") or skill.description,
        content_json=content,
        status="published" if is_visible else "archived",
        created_at=skill.created_at,
        updated_at=branch.updated_at,
    )
    object.__setattr__(projected, "agent_branch_meta", metadata)
    return projected


def visible_skill_rows(
    db: Session,
    tenant_id: str,
    agent_id: str | None = None,
    include_inactive: bool = True,
) -> list[Skill]:
    """获取对指定 Agent 可见的技能列表（含分支投影）。

    对于整体 Agent 或无 agent_id：返回公共画廊中所有技能。
    对于普通 Agent：返回其绑定的技能，并投影各自的分支内容。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID（为 None 或整体 Agent 时查公共画廊）。
        include_inactive: 是否包含非活跃的技能/绑定。

    Returns:
        可见的技能对象列表（普通 Agent 的技能含分支投影），按更新时间倒序排列。
    """
    agent = get_agent(db, tenant_id, agent_id)
    if not agent or agent.is_overall:
        status_clause = (
            Skill.status != "deleted" if include_inactive else Skill.status == "published"
        )
        rows = list(
            db.exec(
                select(Skill)
                .where(Skill.tenant_id == tenant_id, status_clause)
                .order_by(Skill.updated_at.desc())
            ).all()
        )
        metadata_by_id = resource_binding_metadata(
            db, tenant_id, agent.id if agent else None, "skill"
        )
        visible_rows = [
            row for row in rows if is_open_gallery_resource(db, tenant_id, "skill", row)
        ]
        for row in visible_rows:
            object.__setattr__(
                row, "agent_branch_meta", {"metadata": metadata_by_id.get(row.id, {})}
            )
        return visible_rows
    rows: list[Skill] = []
    bindings = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent.id,
            AgentResourceBinding.resource_type == "skill",
        )
    ).all()
    for binding in bindings:
        if binding.status == "deleted":
            continue
        if not include_inactive and binding.status != "active":
            continue
        skill = db.get(Skill, binding.resource_id)
        if not skill or skill.tenant_id != tenant_id:
            continue
        if not is_bound_resource_visible_for_agent(db, tenant_id, "skill", skill, binding):
            continue
        branch = ensure_agent_skill_branch(db, tenant_id, agent.id, skill)
        if not include_inactive and branch.status != "active":
            continue
        rows.append(project_skill_with_branch(skill, branch, binding.status))
    return sorted(rows, key=lambda item: item.updated_at, reverse=True)


def visible_published_skills(
    db: Session, tenant_id: str, agent_id: str | None = None
) -> list[Skill]:
    """获取对指定 Agent 可见且状态为 published 的技能列表。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID。

    Returns:
        状态为 published 的可见技能列表。
    """
    return [
        skill
        for skill in visible_skill_rows(db, tenant_id, agent_id)
        if skill.status == "published"
    ]


def visible_skill(
    db: Session, tenant_id: str, skill_id: str, agent_id: str | None = None
) -> Skill | None:
    """获取对指定 Agent 可见的单个技能（含分支投影）。

    通过 skill_id 查找技能，并根据 Agent 的角色（整体/普通）返回对应的可见性结果。
    对于普通 Agent，返回技能的分支投影版本。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        skill_id: 技能标识符（业务ID）。
        agent_id: Agent ID。

    Returns:
        可见的技能对象（含分支投影），不可见或不存在时返回 None。
    """
    skill = db.exec(
        select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id == skill_id)
    ).first()
    if not skill or skill.status == "deleted":
        return None
    agent = get_agent(db, tenant_id, agent_id)
    if not agent or agent.is_overall:
        if skill.status == "archived":
            return None
        if not is_open_gallery_resource(db, tenant_id, "skill", skill):
            return None
        metadata_by_id = resource_binding_metadata(
            db, tenant_id, agent.id if agent else None, "skill"
        )
        object.__setattr__(
            skill, "agent_branch_meta", {"metadata": metadata_by_id.get(skill.id, {})}
        )
        return skill
    binding = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent.id,
            AgentResourceBinding.resource_type == "skill",
            AgentResourceBinding.resource_id == skill.id,
            AgentResourceBinding.status == "active",
        )
    ).first()
    if not binding:
        return None
    if not is_bound_resource_visible_for_agent(db, tenant_id, "skill", skill, binding):
        return None
    branch = ensure_agent_skill_branch(db, tenant_id, agent.id, skill)
    if branch.status != "active":
        return None
    return project_skill_with_branch(skill, branch)


def visible_tool_rows(
    db: Session,
    tenant_id: str,
    agent_id: str | None = None,
    include_inactive: bool = True,
) -> list[Tool]:
    """获取对指定 Agent 可见的工具列表。

    对于整体 Agent 或无 agent_id：返回公共画廊中所有工具。
    对于普通 Agent：返回其绑定的可见工具。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID。
        include_inactive: 是否包含非活跃的绑定/未启用的工具。

    Returns:
        可见的工具列表，按 bucket 和 name 排序。
    """
    agent = get_agent(db, tenant_id, agent_id)
    if agent_id and not agent:
        return []
    if not agent or agent.is_overall:
        rows = db.exec(
            select(Tool).where(Tool.tenant_id == tenant_id).order_by(Tool.bucket, Tool.name)
        ).all()
        return [
            row
            for row in rows
            if is_open_gallery_resource(db, tenant_id, "tool", row)
            and (include_inactive or row.enabled)
        ]

    bindings = db.exec(
        select(AgentResourceBinding)
        .where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent.id,
            AgentResourceBinding.resource_type == "tool",
            AgentResourceBinding.status != "deleted",
        )
        .order_by(AgentResourceBinding.updated_at.desc())
    ).all()
    visible: list[Tool] = []
    for binding in bindings:
        if not include_inactive and binding.status != "active":
            continue
        row = db.get(Tool, binding.resource_id)
        if not row or row.tenant_id != tenant_id:
            continue
        if not is_bound_resource_visible_for_agent(db, tenant_id, "tool", row, binding):
            continue
        if not include_inactive and not row.enabled:
            continue
        visible.append(row)
    return sorted(visible, key=lambda row: (row.bucket, row.name))


def ensure_agent_skill_branch(
    db: Session,
    tenant_id: str,
    agent_id: str,
    skill: Skill,
    metadata_json: dict[str, Any] | None = None,
) -> AgentSkillBranch:
    """确保 Agent 拥有指定技能的分支，不存在则创建。

    如果分支已存在，更新其元数据；如果不存在，基于当前技能内容创建新分支，
    并生成初始版本记录。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID。
        skill: 技能对象。
        metadata_json: 要合并到分支的额外元数据。

    Returns:
        技能分支对象。
    """
    branch = db.exec(
        select(AgentSkillBranch).where(
            AgentSkillBranch.tenant_id == tenant_id,
            AgentSkillBranch.agent_id == agent_id,
            AgentSkillBranch.skill_id == skill.skill_id,
        )
    ).first()
    branch_metadata = dict(getattr(branch, "metadata_json", None) or {})
    if metadata_json:
        branch_metadata.update(metadata_json)
    metadata = _agent_private_metadata_for(db, tenant_id, agent_id, branch_metadata)
    if branch:
        if branch.metadata_json != metadata:
            branch.metadata_json = metadata
            branch.updated_at = utc_now()
            db.add(branch)
        return branch
    branch = AgentSkillBranch(
        tenant_id=tenant_id,
        agent_id=agent_id,
        skill_id=skill.skill_id,
        source_skill_id=skill.id,
        base_version=skill.version,
        head_version=skill.version,
        content_json=dict(skill.content_json),
        status="active" if skill.status == "published" else "inactive",
        sync_state="synced",
        metadata_json=metadata,
    )
    db.add(branch)
    db.flush()
    _ensure_branch_version(db, branch, "初始化分支")
    return branch


def update_branch_skill(
    db: Session,
    tenant_id: str,
    agent_id: str,
    skill: Skill,
    content: dict[str, Any],
    change_summary: str = "分支改写",
) -> AgentSkillBranch:
    """更新 Agent 技能分支的内容，生成新的版本。

    将新的内容写入分支，递增版本号，标记为 diverged（已与整体版本分叉），
    并创建对应的版本记录。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID。
        skill: 技能对象。
        content: 新的技能内容 JSON。
        change_summary: 变更摘要说明。

    Returns:
        更新后的技能分支对象。
    """
    branch = ensure_agent_skill_branch(db, tenant_id, agent_id, skill)
    previous_status = branch.status
    next_version = next_unique_branch_version(db, branch, str(content.get("version") or ""))
    next_content = dict(content)
    next_content["version"] = next_version
    branch.content_json = next_content
    branch.head_version = next_version
    branch.status = previous_status
    branch.sync_state = "diverged"
    branch.updated_at = utc_now()
    _ensure_branch_version(db, branch, change_summary)
    return branch


def sync_branch_from_overall(
    db: Session, tenant_id: str, agent_id: str, skill: Skill
) -> AgentSkillBranch:
    """将 Agent 技能分支从整体版本同步（覆盖分支内容为整体最新版本）。

    同步后分支的 base_version 和 head_version 都指向整体版本，
    sync_state 被重置为 synced。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID。
        skill: 要同步的技能对象。

    Returns:
        同步后的技能分支对象。

    Raises:
        HTTPException 400: 技能未发布或不在公共画廊中。
    """
    if skill.status != "published" or not is_open_gallery_resource(db, tenant_id, "skill", skill):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=400, detail="Disabled skill cannot be learned from the open gallery"
        )
    branch = ensure_agent_skill_branch(db, tenant_id, agent_id, skill)
    branch.base_version = skill.version
    branch.head_version = skill.version
    branch.content_json = dict(skill.content_json)
    branch.status = "active" if skill.status == "published" else "inactive"
    branch.sync_state = "synced"
    branch.updated_at = utc_now()
    _ensure_branch_version(db, branch, "同步整体版本")
    return branch


def promote_branch_to_overall(db: Session, tenant_id: str, branch: AgentSkillBranch) -> Skill:
    """将 Agent 技能分支推送到整体版本（覆盖全局技能为分支内容）。

    生成新的全局版本号，更新全局技能内容、状态，创建 SkillVersion 记录，
    同时将分支标记为 synced。操作后该分支与整体版本保持一致。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        branch: 要推送的技能分支对象。

    Returns:
        更新后的全局技能对象。

    Raises:
        HTTPException 404: 全局技能不存在。
    """
    skill = db.exec(
        select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id == branch.skill_id)
    ).first()
    if not skill:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Skill not found")
    next_version = next_global_version(skill.version)
    content = dict(branch.content_json)
    content["version"] = next_version
    skill.version = next_version
    skill.name = str(content.get("name") or skill.name)
    skill.business_domain = content.get("business_domain") or skill.business_domain
    skill.description = content.get("description") or skill.description
    skill.content_json = content
    skill.status = "published"
    skill.updated_at = utc_now()
    mark_resource_open_gallery(skill)
    ensure_open_gallery_binding(db, tenant_id, "skill", skill.id, "active")
    db.add(
        SkillVersion(
            tenant_id=tenant_id,
            skill_id=skill.skill_id,
            version=next_version,
            name=skill.name,
            business_domain=skill.business_domain,
            description=skill.description,
            content_json=content,
            status="published",
        )
    )
    branch.base_version = next_version
    branch.head_version = next_version
    branch.content_json = content
    branch.sync_state = "synced"
    branch.updated_at = utc_now()
    _ensure_branch_version(db, branch, "推送到整体")
    return skill


def rollback_branch(
    db: Session, tenant_id: str, agent_id: str, skill_id: str, version: str
) -> AgentSkillBranch:
    """将 Agent 技能分支回滚到指定的历史版本。

    从历史版本记录中恢复内容，并根据是否回退到 base_version 自动判定 sync_state。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID。
        skill_id: 技能标识符。
        version: 要回滚到的目标版本号。

    Returns:
        回滚后的技能分支对象。

    Raises:
        HTTPException 404: 分支或指定版本不存在。
    """
    branch = db.exec(
        select(AgentSkillBranch).where(
            AgentSkillBranch.tenant_id == tenant_id,
            AgentSkillBranch.agent_id == agent_id,
            AgentSkillBranch.skill_id == skill_id,
        )
    ).first()
    if not branch:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Branch not found")
    version_row = db.exec(
        select(AgentSkillBranchVersion).where(
            AgentSkillBranchVersion.tenant_id == tenant_id,
            AgentSkillBranchVersion.agent_id == agent_id,
            AgentSkillBranchVersion.skill_id == skill_id,
            AgentSkillBranchVersion.version == version,
        )
    ).first()
    if not version_row:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Branch version not found")
    branch.content_json = dict(version_row.content_json)
    branch.head_version = version_row.version
    branch.status = version_row.status
    branch.sync_state = "synced" if version_row.version == branch.base_version else "diverged"
    branch.updated_at = utc_now()
    return branch


def branch_versions(
    db: Session, tenant_id: str, agent_id: str, skill_id: str
) -> list[AgentSkillBranchVersion]:
    """获取 Agent 指定技能分支的所有历史版本记录。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID。
        skill_id: 技能标识符。

    Returns:
        版本记录列表，按创建时间倒序排列。
    """
    return list(
        db.exec(
            select(AgentSkillBranchVersion)
            .where(
                AgentSkillBranchVersion.tenant_id == tenant_id,
                AgentSkillBranchVersion.agent_id == agent_id,
                AgentSkillBranchVersion.skill_id == skill_id,
            )
            .order_by(AgentSkillBranchVersion.created_at.desc())
        ).all()
    )


def visible_knowledge_base_ids(
    db: Session,
    tenant_id: str,
    agent_id: str | None = None,
    include_inactive: bool = False,
) -> list[str]:
    """获取对指定 Agent 可见的知识库 ID 列表。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID。
        include_inactive: 是否包含非活跃的知识库。

    Returns:
        可见的知识库 ID 列表。
    """
    return list(visible_knowledge_base_versions(db, tenant_id, agent_id, include_inactive).keys())


def visible_knowledge_base_versions(
    db: Session,
    tenant_id: str,
    agent_id: str | None = None,
    include_inactive: bool = False,
) -> dict[str, KnowledgeBaseVersion]:
    """获取对指定 Agent 可见的知识库版本映射。

    对于整体 Agent 或无 agent_id：返回公共画廊中的知识库及其当前版本。
    对于普通 Agent：返回其分支绑定的知识库及其分支版本。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID。
        include_inactive: 是否包含非活跃的知识库/分支。

    Returns:
        以知识库 ID 为键、对应版本对象为值的字典。
    """
    agent = get_agent(db, tenant_id, agent_id)
    if not agent or agent.is_overall:
        status_clause = (
            KnowledgeBase.status != "deleted"
            if include_inactive
            else KnowledgeBase.status == "active"
        )
        rows = db.exec(
            select(KnowledgeBase).where(
                KnowledgeBase.tenant_id == tenant_id,
                status_clause,
            )
        ).all()
        rows = [
            row for row in rows if is_open_gallery_resource(db, tenant_id, "knowledge_base", row)
        ]
        return {
            row.id: ensure_knowledge_base_version(db, row, _current_knowledge_version(row))
            for row in rows
        }
    branch_status_clause = (
        AgentKnowledgeBranch.status != "deleted"
        if include_inactive
        else AgentKnowledgeBranch.status == "active"
    )
    branches = db.exec(
        select(AgentKnowledgeBranch).where(
            AgentKnowledgeBranch.tenant_id == tenant_id,
            AgentKnowledgeBranch.agent_id == agent.id,
            branch_status_clause,
        )
    ).all()
    result: dict[str, KnowledgeBaseVersion] = {}
    for branch in branches:
        kb = db.get(KnowledgeBase, branch.knowledge_base_id)
        if not kb or kb.tenant_id != tenant_id or kb.status == "deleted":
            continue
        if not include_inactive and kb.status != "active":
            continue
        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == tenant_id,
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "knowledge_base",
                AgentResourceBinding.resource_id == kb.id,
            )
        ).first()
        if not binding or not is_bound_resource_visible_for_agent(
            db, tenant_id, "knowledge_base", kb, binding
        ):
            continue
        if not include_inactive and binding.status != "active":
            continue
        result[kb.id] = ensure_knowledge_base_version(db, kb, branch.head_version)
    return result


def visible_knowledge_base_version_ids(
    db: Session,
    tenant_id: str,
    agent_id: str | None = None,
    include_inactive: bool = False,
) -> list[str]:
    """获取对指定 Agent 可见的知识库版本 ID 列表。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID。
        include_inactive: 是否包含非活跃的知识库。

    Returns:
        可见的知识库版本 ID 列表。
    """
    return [
        row.id
        for row in visible_knowledge_base_versions(
            db, tenant_id, agent_id, include_inactive
        ).values()
    ]


def ensure_knowledge_base_version(
    db: Session, kb: KnowledgeBase, version: str | None = None
) -> KnowledgeBaseVersion:
    """确保知识库版本记录存在，不存在则创建。

    根据指定版本号查找知识库版本，如果不存在则创建一条版本快照记录。

    Args:
        db: 数据库会话。
        kb: 知识库对象。
        version: 版本号（为 None 时使用知识库当前版本）。

    Returns:
        知识库版本记录对象。
    """
    normalized_version = version or _current_knowledge_version(kb)
    row = db.exec(
        select(KnowledgeBaseVersion).where(
            KnowledgeBaseVersion.tenant_id == kb.tenant_id,
            KnowledgeBaseVersion.knowledge_base_id == kb.id,
            KnowledgeBaseVersion.version == normalized_version,
        )
    ).first()
    if row:
        return row
    row = KnowledgeBaseVersion(
        id=f"kbver_{kb.id}_{_safe_version_id(normalized_version)}",
        tenant_id=kb.tenant_id,
        knowledge_base_id=kb.id,
        version=normalized_version,
        name=kb.name,
        description=kb.description,
        status=kb.status,
        metadata_json=dict(kb.metadata_json or {}),
    )
    db.add(row)
    db.flush()
    return row


def _apply_knowledge_version_metadata(
    db: Session,
    tenant_id: str,
    agent: AgentProfile | None,
    version: KnowledgeBaseVersion,
    metadata_json: dict[str, Any] | None,
) -> None:
    """将元数据应用到知识库版本（区分 Agent 私有与公共画廊）。

    对于普通 Agent：标记为私有作用域。对于整体 Agent：标记为公共画廊。
    现有版本元数据与新元数据合并。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent: Agent 配置对象。
        version: 知识库版本对象。
        metadata_json: 要应用的元数据。
    """
    if not metadata_json:
        return
    merged = {**(version.metadata_json or {}), **metadata_json}
    if agent and not agent.is_overall:
        version.metadata_json = _agent_private_metadata_for(db, tenant_id, agent.id, merged)
    else:
        version.metadata_json = open_gallery_metadata(merged)
    version.updated_at = utc_now()
    db.add(version)


def knowledge_version_for_upload(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    agent_id: str | None,
    metadata_json: dict[str, Any] | None = None,
) -> KnowledgeBaseVersion:
    """为知识库上传获取目标版本（自动创建分支版本并克隆资产）。

    对于整体 Agent：返回当前版本。对于普通 Agent：创建新的分支版本，
    克隆源版本的所有资产（文档/桶/分块/概念/建议），标记分支为 diverged。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        knowledge_base_id: 知识库 ID。
        agent_id: Agent ID。
        metadata_json: 要应用到版本的额外元数据。

    Returns:
        目标知识库版本对象。

    Raises:
        HTTPException 404: 知识库不存在。
    """
    kb = db.get(KnowledgeBase, knowledge_base_id)
    if not kb or kb.tenant_id != tenant_id or kb.status == "archived":
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Knowledge base not found")
    agent = get_agent(db, tenant_id, agent_id)
    if not agent or agent.is_overall:
        version = ensure_knowledge_base_version(db, kb, _current_knowledge_version(kb))
        _apply_knowledge_version_metadata(db, tenant_id, agent, version, metadata_json)
        return version
    branch = _ensure_knowledge_branch(db, tenant_id, agent.id, kb)
    source_version = ensure_knowledge_base_version(db, kb, branch.head_version)
    next_version = _next_knowledge_branch_version(branch)
    target_version = ensure_knowledge_base_version(db, kb, next_version)
    clone_knowledge_version_assets(
        db, tenant_id, knowledge_base_id, source_version.id, target_version.id
    )
    _apply_knowledge_version_metadata(db, tenant_id, agent, target_version, metadata_json)
    branch.head_version = next_version
    branch.sync_state = "diverged"
    branch.status = "active"
    branch.updated_at = utc_now()
    return target_version


def ensure_agent_private_knowledge_branch(
    db: Session,
    tenant_id: str,
    agent_id: str,
    knowledge_base: KnowledgeBase,
    metadata_json: dict[str, Any] | None = None,
) -> AgentKnowledgeBranch:
    """为 Agent 创建知识库的私有分支（绑定+标记+初始化分支）。

    将知识库标记为 Agent 私有，创建私有绑定，确保当前版本存在并应用元数据，
    最后创建知识库分支并标记为 synced。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID。
        knowledge_base: 知识库对象。
        metadata_json: 额外的元数据。

    Returns:
        知识库分支对象。
    """
    mark_resource_private_for_agent(knowledge_base, agent_id, metadata_json)
    ensure_private_resource_binding(
        db,
        tenant_id,
        agent_id,
        "knowledge_base",
        knowledge_base.id,
        "active",
        metadata_json=metadata_json,
    )
    current_version = _current_knowledge_version(knowledge_base)
    version = ensure_knowledge_base_version(db, knowledge_base, current_version)
    _apply_knowledge_version_metadata(
        db, tenant_id, get_agent(db, tenant_id, agent_id), version, metadata_json
    )
    branch = _ensure_knowledge_branch(db, tenant_id, agent_id, knowledge_base)
    branch.base_version = current_version
    branch.head_version = current_version
    branch.status = "active"
    branch.sync_state = "synced"
    branch.updated_at = utc_now()
    return branch


def sync_knowledge_branch_from_overall(
    db: Session,
    tenant_id: str,
    agent_id: str,
    knowledge_base_id: str,
) -> AgentKnowledgeBranch:
    """将 Agent 知识库分支从整体版本同步（重置为整体最新版本）。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID。
        knowledge_base_id: 知识库 ID。

    Returns:
        同步后的知识库分支对象。

    Raises:
        HTTPException 400: 知识库未激活或不在公共画廊中。
    """
    kb = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    if kb.status != "active" or not is_open_gallery_resource(db, tenant_id, "knowledge_base", kb):
        from fastapi import HTTPException

        raise HTTPException(
            status_code=400,
            detail="Disabled knowledge base cannot be learned from the open gallery",
        )
    branch = _ensure_knowledge_branch(db, tenant_id, agent_id, kb)
    current_version = _current_knowledge_version(kb)
    ensure_knowledge_base_version(db, kb, current_version)
    branch.base_version = current_version
    branch.head_version = current_version
    branch.status = "active"
    branch.sync_state = "synced"
    branch.updated_at = utc_now()
    return branch


def promote_knowledge_branch_to_overall(
    db: Session,
    tenant_id: str,
    agent_id: str,
    knowledge_base_id: str,
) -> KnowledgeBaseVersion:
    """将 Agent 知识库分支推送到整体版本。

    将分支版本的内容提升为新的全局版本，重新标记资产归属，
    更新全局知识库信息和绑定，将分支标记为 synced。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID。
        knowledge_base_id: 知识库 ID。

    Returns:
        新创建的全局知识库版本对象。

    Raises:
        HTTPException 404: 知识库分支不存在。
    """
    kb = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    branch = db.exec(
        select(AgentKnowledgeBranch).where(
            AgentKnowledgeBranch.tenant_id == tenant_id,
            AgentKnowledgeBranch.agent_id == agent_id,
            AgentKnowledgeBranch.knowledge_base_id == knowledge_base_id,
        )
    ).first()
    if not branch:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Knowledge branch not found")
    source = ensure_knowledge_base_version(db, kb, branch.head_version)
    next_version = next_global_version(_current_knowledge_version(kb))
    target = ensure_knowledge_base_version(db, kb, next_version)
    target.name = source.name
    target.description = source.description
    target.metadata_json = dict(source.metadata_json or {})
    target.status = "active"
    target.updated_at = utc_now()
    _retag_knowledge_version(db, tenant_id, knowledge_base_id, source.id, target.id)
    kb.name = source.name
    kb.description = source.description
    kb.metadata_json = open_gallery_metadata(
        {**(kb.metadata_json or {}), "current_version": next_version}
    )
    kb.updated_at = utc_now()
    ensure_open_gallery_binding(db, tenant_id, "knowledge_base", kb.id, "active")
    branch.base_version = next_version
    branch.head_version = next_version
    branch.sync_state = "synced"
    branch.updated_at = utc_now()
    return target


def rollback_knowledge_branch(
    db: Session,
    tenant_id: str,
    agent_id: str,
    knowledge_base_id: str,
    version: str,
) -> AgentKnowledgeBranch:
    """将 Agent 知识库分支回滚到指定版本。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID。
        knowledge_base_id: 知识库 ID。
        version: 目标版本号。

    Returns:
        回滚后的知识库分支对象。
    """
    kb = _get_knowledge_base(db, tenant_id, knowledge_base_id)
    target = ensure_knowledge_base_version(db, kb, version)
    branch = _ensure_knowledge_branch(db, tenant_id, agent_id, kb)
    branch.head_version = target.version
    branch.status = "active"
    branch.sync_state = "synced" if target.version == branch.base_version else "diverged"
    branch.updated_at = utc_now()
    return branch


def model_for_agent(
    db: Session, tenant_id: str, agent_id: str | None, role: str = "default"
) -> ResolvedModelConfig | None:
    """根据 Agent 配置和角色解析运行时使用的 LLM 模型。

    优先查找 Agent 特定角色的模型绑定，回退到 default 角色绑定，
    最后回退到租户级的默认模型配置。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID。
        role: 模型角色（default/router/step/response/general_skill）。

    Returns:
        解析后的运行时模型配置，不存在时返回 None。
    """
    agent = get_agent(db, tenant_id, agent_id)
    roles: Iterable[str] = (role, "default") if role != "default" else ("default",)
    if agent:
        for candidate_role in roles:
            binding = db.exec(
                select(AgentModelBinding).where(
                    AgentModelBinding.tenant_id == tenant_id,
                    AgentModelBinding.agent_id == agent.id,
                    AgentModelBinding.role == candidate_role,
                )
            ).first()
            if binding:
                model = db.get(ModelConfig, binding.model_config_id)
                if model and model.enabled:
                    return _runtime_model(db, tenant_id, model)
    model = db.exec(
        select(ModelConfig).where(
            ModelConfig.tenant_id == tenant_id,
            ModelConfig.is_default == True,  # noqa: E712
            ModelConfig.enabled == True,  # noqa: E712
        )
    ).first()
    return _runtime_model(db, tenant_id, model) if model else None


def _runtime_model(
    db: Session, tenant_id: str, model: ModelConfig
) -> ResolvedModelConfig:
    """将数据库模型配置解析为运行时可用的模型配置对象。"""
    return resolve_model_config_for_runtime(db, tenant_id, model.id)


def copy_overall_scope_to_agent(db: Session, tenant_id: str, agent: AgentProfile) -> None:
    """将整体 Agent 的公共画廊资源范围复制到新建的普通 Agent。

    复制公共画廊中所有已发布的技能（含分支创建）和通用技能绑定。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent: 目标 Agent 对象。
    """
    skills = db.exec(
        select(Skill).where(Skill.tenant_id == tenant_id, Skill.status == "published")
    ).all()
    for skill in skills:
        if not is_open_gallery_resource(db, tenant_id, "skill", skill):
            continue
        _ensure_binding(
            db,
            tenant_id,
            agent.id,
            "skill",
            skill.id,
            _binding_status_from_resource_status(skill.status),
            metadata_json=_agent_private_metadata_for(db, tenant_id, agent.id),
        )
        ensure_agent_skill_branch(db, tenant_id, agent.id, skill)
    general_skills = db.exec(
        select(GeneralSkill).where(
            GeneralSkill.tenant_id == tenant_id, GeneralSkill.status == "published"
        )
    ).all()
    for general_skill in general_skills:
        if not is_open_gallery_resource(db, tenant_id, "general_skill", general_skill):
            continue
        _ensure_binding(
            db,
            tenant_id,
            agent.id,
            "general_skill",
            general_skill.id,
            _binding_status_from_resource_status(general_skill.status),
            metadata_json=_agent_private_metadata_for(db, tenant_id, agent.id),
        )


def copy_open_gallery_tools_to_agent(db: Session, tenant_id: str, agent: AgentProfile) -> None:
    """将公共画廊中的所有工具绑定复制到新建的 Agent。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent: 目标 Agent 对象。
    """
    tools = db.exec(select(Tool).where(Tool.tenant_id == tenant_id)).all()
    for tool in tools:
        if not is_open_gallery_resource(db, tenant_id, "tool", tool):
            continue
        _ensure_binding(
            db,
            tenant_id,
            agent.id,
            "tool",
            tool.id,
            "active" if tool.enabled else "inactive",
            metadata_json=_agent_private_metadata_for(db, tenant_id, agent.id),
        )


def next_branch_version(version: str, requested_version: str | None = None) -> str:
    """计算下一个分支版本号。

    如果请求的版本是合法的 semver 且与当前版本不同，直接使用请求版本；
    否则基于当前版本计算下一个全局版本号。

    Args:
        version: 当前版本号。
        requested_version: 用户请求的版本号（可为 None）。

    Returns:
        计算后的下一个版本号。
    """
    requested = (requested_version or "").strip()
    if _is_semver(requested) and requested != version:
        return requested
    base = version.partition("-branch.")[0]
    return next_global_version(base)


def next_unique_branch_version(
    db: Session, branch: AgentSkillBranch, requested_version: str | None = None
) -> str:
    """计算下一个唯一的分支版本号，避免与已有版本记录冲突。

    先计算候选版本号，如果已存在同名版本则持续递增直到找到唯一的版本号。

    Args:
        db: 数据库会话。
        branch: 技能分支对象。
        requested_version: 用户请求的版本号。

    Returns:
        唯一的下一个版本号。
    """
    candidate = next_branch_version(branch.head_version, requested_version)
    while db.exec(
        select(AgentSkillBranchVersion).where(
            AgentSkillBranchVersion.tenant_id == branch.tenant_id,
            AgentSkillBranchVersion.agent_id == branch.agent_id,
            AgentSkillBranchVersion.skill_id == branch.skill_id,
            AgentSkillBranchVersion.version == candidate,
        )
    ).first():
        candidate = next_global_version(candidate.partition("-branch.")[0])
    return candidate


def next_global_version(version: str) -> str:
    """计算下一个全局版本号（递增 minor 版本，重置 patch）。

    例如 ``1.2.3`` -> ``1.3.0``。如果格式不符合标准 semver，则追加 ``.1``。

    Args:
        version: 当前版本号。

    Returns:
        递增后的版本号。
    """
    parts = version.split(".")
    if len(parts) >= 3 and all(part.isdigit() for part in parts[:3]):
        return f"{parts[0]}.{int(parts[1]) + 1}.0"
    return f"{version}.1"


def _is_semver(version: str) -> bool:
    """检查版本号是否为标准的三段式 semver 格式（X.Y.Z）。"""
    parts = version.split(".")
    return len(parts) == 3 and all(part.isdigit() for part in parts)


def _ensure_branch_version(db: Session, branch: AgentSkillBranch, change_summary: str) -> None:
    """确保分支版本记录存在（若该版本号尚无记录则创建一条）。

    Args:
        db: 数据库会话。
        branch: 技能分支对象。
        change_summary: 版本变更摘要。
    """
    existing = db.exec(
        select(AgentSkillBranchVersion).where(
            AgentSkillBranchVersion.tenant_id == branch.tenant_id,
            AgentSkillBranchVersion.agent_id == branch.agent_id,
            AgentSkillBranchVersion.skill_id == branch.skill_id,
            AgentSkillBranchVersion.version == branch.head_version,
        )
    ).first()
    if existing:
        return
    db.add(
        AgentSkillBranchVersion(
            tenant_id=branch.tenant_id,
            agent_id=branch.agent_id,
            skill_id=branch.skill_id,
            source_skill_id=branch.source_skill_id,
            version=branch.head_version,
            base_version=branch.base_version,
            content_json=dict(branch.content_json),
            status=branch.status,
            sync_state=branch.sync_state,
            change_summary=change_summary,
        )
    )


def _binding_status_from_resource_status(status: str | None) -> str:
    """将资源状态映射为绑定状态（active/published -> active，其他 -> inactive）。"""
    return "active" if status in {"active", "published"} else "inactive"


def _resource_metadata(resource: object) -> dict[str, Any]:
    """安全获取资源对象的 metadata_json 字典。"""
    metadata = getattr(resource, "metadata_json", None)
    return dict(metadata) if isinstance(metadata, dict) else {}


def _metadata_is_private(metadata: dict[str, Any]) -> bool:
    """检查元数据是否标记为 Agent 私有作用域。"""
    return (
        metadata.get("scope") == AGENT_PRIVATE_SCOPE
        or metadata.get("visibility") == AGENT_PRIVATE_SCOPE
        or metadata.get("created_from_agent") is True
        or metadata.get("created_from_upload") is True
    )


def _binding_is_private(binding: AgentResourceBinding) -> bool:
    """检查绑定记录是否为 Agent 私有作用域。"""
    metadata = dict(binding.metadata_json or {})
    return _metadata_is_private(metadata)


def _ensure_binding(
    db: Session,
    tenant_id: str,
    agent_id: str,
    resource_type: str,
    resource_id: str,
    status: str = "active",
    metadata_json: dict[str, Any] | None = None,
) -> None:
    """创建或更新 Agent 资源绑定记录。

    如果绑定已存在：更新状态和元数据（保留创建者信息）。
    如果绑定不存在：创建新的绑定记录。
    特殊处理：当现有绑定状态为 deleted 且新状态非 deleted 时，仅更新元数据。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID。
        resource_type: 资源类型。
        resource_id: 资源ID。
        status: 绑定状态。
        metadata_json: 元数据。
    """
    existing = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent_id,
            AgentResourceBinding.resource_type == resource_type,
            AgentResourceBinding.resource_id == resource_id,
        )
    ).first()
    if existing:
        if existing.status == "deleted" and status != "deleted":
            if metadata_json is not None and not existing.metadata_json:
                existing.metadata_json = metadata_json
                existing.updated_at = utc_now()
                db.add(existing)
            return
        existing.status = status
        if metadata_json is not None:
            merged_metadata = {
                **(existing.metadata_json or {}),
                **metadata_json,
            }
            existing.metadata_json = metadata_preserving_creator(
                existing.metadata_json,
                merged_metadata,
            )
        existing.updated_at = utc_now()
        db.add(existing)
        return
    db.add(
        AgentResourceBinding(
            tenant_id=tenant_id,
            agent_id=agent_id,
            resource_type=resource_type,
            resource_id=resource_id,
            status=status,
            metadata_json=metadata_json or {},
        )
    )


def _ensure_knowledge_branch(
    db: Session, tenant_id: str, agent_id: str, kb: KnowledgeBase
) -> AgentKnowledgeBranch:
    """确保 Agent 拥有指定知识库的分支，不存在则创建。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        agent_id: Agent ID。
        kb: 知识库对象。

    Returns:
        知识库分支对象。
    """
    branch = db.exec(
        select(AgentKnowledgeBranch).where(
            AgentKnowledgeBranch.tenant_id == tenant_id,
            AgentKnowledgeBranch.agent_id == agent_id,
            AgentKnowledgeBranch.knowledge_base_id == kb.id,
        )
    ).first()
    if branch:
        return branch
    branch = AgentKnowledgeBranch(
        tenant_id=tenant_id,
        agent_id=agent_id,
        knowledge_base_id=kb.id,
        base_version="1.0.0",
        head_version="1.0.0",
        status=_binding_status_from_resource_status(kb.status),
        sync_state="synced",
    )
    db.add(branch)
    return branch


def _get_knowledge_base(db: Session, tenant_id: str, knowledge_base_id: str) -> KnowledgeBase:
    """获取知识库对象，不存在或已归档时抛出异常。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        knowledge_base_id: 知识库 ID。

    Returns:
        知识库对象。

    Raises:
        HTTPException 404: 知识库不存在或已归档。
    """
    kb = db.get(KnowledgeBase, knowledge_base_id)
    if not kb or kb.tenant_id != tenant_id or kb.status == "archived":
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return kb


def _current_knowledge_version(kb: KnowledgeBase) -> str:
    """从知识库元数据中获取当前版本号（默认为 1.0.0）。"""
    metadata = kb.metadata_json or {}
    version = metadata.get("current_version") if isinstance(metadata, dict) else None
    return str(version or "1.0.0")


def _next_knowledge_branch_version(branch: AgentKnowledgeBranch) -> str:
    """计算知识库分支的下一个版本号。

    版本格式为 ``{base_version}-branch.{agent_id_safe}.{N}``，
    N 从 1 开始递增。

    Args:
        branch: 知识库分支对象。

    Returns:
        下一个分支版本号。
    """
    prefix = f"{branch.base_version}-branch.{_safe_version_id(branch.agent_id)}."
    if branch.head_version.startswith(prefix):
        suffix = branch.head_version.removeprefix(prefix)
        if suffix.isdigit():
            return f"{prefix}{int(suffix) + 1}"
    return f"{prefix}1"


def _retag_knowledge_version(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    source_version_id: str,
    target_version_id: str,
) -> None:
    """将知识库资产从源版本重新标记到目标版本（就地修改）。

    遍历文档、桶、分块、概念、建议等表，将属于源版本的资产迁移到目标版本。
    用于 promote 操作中的资产归属变更。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        knowledge_base_id: 知识库 ID。
        source_version_id: 源版本 ID。
        target_version_id: 目标版本 ID。
    """
    tables = (
        KnowledgeDocument,
        KnowledgeBucket,
        KnowledgeChunk,
        KnowledgeConcept,
        KnowledgeDiscoverySuggestion,
    )
    for model in tables:
        rows = db.exec(
            select(model).where(
                model.tenant_id == tenant_id,
                model.knowledge_base_id == knowledge_base_id,
                model.knowledge_base_version_id == source_version_id,
            )
        ).all()
        for row in rows:
            row.knowledge_base_version_id = target_version_id
            row.updated_at = utc_now()
            db.add(row)


def clone_knowledge_version_assets(
    db: Session,
    tenant_id: str,
    knowledge_base_id: str,
    source_version_id: str,
    target_version_id: str,
) -> None:
    """将源版本的所有知识库资产深度克隆到目标版本。

    克隆范围包括：文档、桶、分块、发现建议和概念。
    如果目标版本已有文档，则采用匹配策略而非克隆（按文件名/类型/标题匹配）。
    克隆过程中维护源→目标的 ID 映射，确保外键引用的正确性。

    Args:
        db: 数据库会话。
        tenant_id: 租户ID。
        knowledge_base_id: 知识库 ID。
        source_version_id: 源版本 ID。
        target_version_id: 目标版本 ID。
    """
    if source_version_id == target_version_id:
        return

    document_id_map: dict[str, str] = {}
    bucket_id_map: dict[str, str] = {}
    source_documents = db.exec(
        select(KnowledgeDocument)
        .where(
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
            KnowledgeDocument.knowledge_base_version_id == source_version_id,
        )
        .order_by(KnowledgeDocument.created_at.asc())
    ).all()
    target_has_documents = db.exec(
        select(KnowledgeDocument.id).where(
            KnowledgeDocument.tenant_id == tenant_id,
            KnowledgeDocument.knowledge_base_id == knowledge_base_id,
            KnowledgeDocument.knowledge_base_version_id == target_version_id,
        )
    ).first()

    if not target_has_documents:
        for document in source_documents:
            clone = KnowledgeDocument(
                tenant_id=document.tenant_id,
                knowledge_base_id=document.knowledge_base_id,
                knowledge_base_version_id=target_version_id,
                filename=document.filename,
                file_type=document.file_type,
                title=document.title,
                status=document.status,
                bucket_count=document.bucket_count,
                chunk_count=document.chunk_count,
                metadata_json=deepcopy(document.metadata_json or {}),
                error=document.error,
                created_at=document.created_at,
                updated_at=utc_now(),
            )
            db.add(clone)
            db.flush()
            document_id_map[document.id] = clone.id

        source_buckets = db.exec(
            select(KnowledgeBucket)
            .where(
                KnowledgeBucket.tenant_id == tenant_id,
                KnowledgeBucket.knowledge_base_id == knowledge_base_id,
                KnowledgeBucket.knowledge_base_version_id == source_version_id,
            )
            .order_by(KnowledgeBucket.created_at.asc())
        ).all()
        for bucket in source_buckets:
            clone = KnowledgeBucket(
                tenant_id=bucket.tenant_id,
                knowledge_base_id=bucket.knowledge_base_id,
                knowledge_base_version_id=target_version_id,
                document_id=document_id_map.get(bucket.document_id, bucket.document_id),
                bucket_key=bucket.bucket_key,
                title=bucket.title,
                summary=bucket.summary,
                token_estimate=bucket.token_estimate,
                metadata_json=deepcopy(bucket.metadata_json or {}),
                created_at=bucket.created_at,
                updated_at=utc_now(),
            )
            db.add(clone)
            db.flush()
            bucket_id_map[bucket.id] = clone.id

        source_chunks = db.exec(
            select(KnowledgeChunk)
            .where(
                KnowledgeChunk.tenant_id == tenant_id,
                KnowledgeChunk.knowledge_base_id == knowledge_base_id,
                KnowledgeChunk.knowledge_base_version_id == source_version_id,
            )
            .order_by(KnowledgeChunk.bucket_id.asc(), KnowledgeChunk.chunk_index.asc())
        ).all()
        for chunk in source_chunks:
            clone = KnowledgeChunk(
                tenant_id=chunk.tenant_id,
                knowledge_base_id=chunk.knowledge_base_id,
                knowledge_base_version_id=target_version_id,
                document_id=document_id_map.get(chunk.document_id, chunk.document_id),
                bucket_id=bucket_id_map.get(chunk.bucket_id, chunk.bucket_id),
                chunk_index=chunk.chunk_index,
                content=chunk.content,
                summary=chunk.summary,
                source_ref=chunk.source_ref,
                metadata_json=deepcopy(chunk.metadata_json or {}),
                created_at=chunk.created_at,
                updated_at=utc_now(),
            )
            db.add(clone)

        source_suggestions = db.exec(
            select(KnowledgeDiscoverySuggestion)
            .where(
                KnowledgeDiscoverySuggestion.tenant_id == tenant_id,
                KnowledgeDiscoverySuggestion.knowledge_base_id == knowledge_base_id,
                KnowledgeDiscoverySuggestion.knowledge_base_version_id == source_version_id,
            )
            .order_by(KnowledgeDiscoverySuggestion.created_at.asc())
        ).all()
        for suggestion in source_suggestions:
            clone = KnowledgeDiscoverySuggestion(
                tenant_id=suggestion.tenant_id,
                knowledge_base_id=suggestion.knowledge_base_id,
                knowledge_base_version_id=target_version_id,
                document_id=document_id_map.get(suggestion.document_id, suggestion.document_id),
                bucket_id=bucket_id_map.get(suggestion.bucket_id or "", suggestion.bucket_id),
                suggestion_type=suggestion.suggestion_type,
                title=suggestion.title,
                status=suggestion.status,
                payload_json=deepcopy(suggestion.payload_json or {}),
                source_refs_json=_remap_source_refs(
                    suggestion.source_refs_json or [], document_id_map
                ),
                reason=suggestion.reason,
                created_at=suggestion.created_at,
                updated_at=utc_now(),
            )
            db.add(clone)

    else:
        target_documents = db.exec(
            select(KnowledgeDocument).where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.knowledge_base_id == knowledge_base_id,
                KnowledgeDocument.knowledge_base_version_id == target_version_id,
            )
        ).all()
        for source_document in source_documents:
            match = next(
                (
                    target_document
                    for target_document in target_documents
                    if target_document.filename == source_document.filename
                    and target_document.file_type == source_document.file_type
                    and target_document.title == source_document.title
                ),
                None,
            )
            if match:
                document_id_map[source_document.id] = match.id

    if document_id_map:
        existing_target_concepts = db.exec(
            select(KnowledgeConcept).where(
                KnowledgeConcept.tenant_id == tenant_id,
                KnowledgeConcept.knowledge_base_id == knowledge_base_id,
                KnowledgeConcept.knowledge_base_version_id == target_version_id,
            )
        ).all()
        for concept in existing_target_concepts:
            changed = False
            mapped_document_id = document_id_map.get(concept.document_id or "")
            if mapped_document_id:
                concept.document_id = mapped_document_id
                changed = True
            next_source_refs = _remap_source_refs(concept.source_refs_json or [], document_id_map)
            if next_source_refs != (concept.source_refs_json or []):
                concept.source_refs_json = next_source_refs
                changed = True
            if changed:
                concept.updated_at = utc_now()
                db.add(concept)

    target_concept_ids = {
        concept_id
        for concept_id in db.exec(
            select(KnowledgeConcept.concept_id).where(
                KnowledgeConcept.tenant_id == tenant_id,
                KnowledgeConcept.knowledge_base_id == knowledge_base_id,
                KnowledgeConcept.knowledge_base_version_id == target_version_id,
            )
        ).all()
    }
    source_concepts = db.exec(
        select(KnowledgeConcept)
        .where(
            KnowledgeConcept.tenant_id == tenant_id,
            KnowledgeConcept.knowledge_base_id == knowledge_base_id,
            KnowledgeConcept.knowledge_base_version_id == source_version_id,
            KnowledgeConcept.status != "deleted",
        )
        .order_by(KnowledgeConcept.created_at.asc())
    ).all()
    for concept in source_concepts:
        if concept.concept_id in target_concept_ids:
            continue
        clone = KnowledgeConcept(
            tenant_id=concept.tenant_id,
            knowledge_base_id=concept.knowledge_base_id,
            knowledge_base_version_id=target_version_id,
            document_id=document_id_map.get(concept.document_id or "", concept.document_id),
            concept_id=concept.concept_id,
            concept_type=concept.concept_type,
            title=concept.title,
            description=concept.description,
            content_md=concept.content_md,
            frontmatter_json=deepcopy(concept.frontmatter_json or {}),
            links_json=deepcopy(concept.links_json or []),
            citations_json=deepcopy(concept.citations_json or []),
            source_refs_json=_remap_source_refs(concept.source_refs_json or [], document_id_map),
            status=concept.status,
            created_at=concept.created_at,
            updated_at=utc_now(),
        )
        db.add(clone)


def _remap_source_refs(
    source_refs: list[dict[str, Any]], document_id_map: dict[str, str]
) -> list[dict[str, Any]]:
    """根据文档 ID 映射表重写引用列表中的 document_id。

    用于知识库版本克隆后，将概念和建议中的文档引用更新为新克隆的文档 ID。

    Args:
        source_refs: 原始引用列表。
        document_id_map: 源文档 ID 到目标文档 ID 的映射表。

    Returns:
        重写后的引用列表。
    """
    rows: list[dict[str, Any]] = []
    for source_ref in source_refs:
        if not isinstance(source_ref, dict):
            continue
        next_ref = deepcopy(source_ref)
        document_id = next_ref.get("document_id")
        if isinstance(document_id, str) and document_id in document_id_map:
            next_ref["document_id"] = document_id_map[document_id]
        rows.append(next_ref)
    return rows


def _safe_version_id(value: str) -> str:
    """将字符串转换为安全的版本 ID 片段（非字母数字字符替换为下划线）。"""
    return "".join(ch if ch.isalnum() else "_" for ch in value)
