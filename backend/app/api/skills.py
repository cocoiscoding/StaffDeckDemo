"""技能（SOP Skills）API 模块。

提供结构化操作流程（SOP）技能的完整管理能力，包括：

- **CRUD**：创建、列表、详情、更新、删除技能。
- **生命周期**：发布（publish）、归档（archive）、草稿（draft）状态切换。
- **版本管理**：列出历史版本、获取指定版本、删除版本、回滚到历史版本。
- **文件解析**：从上传的 Markdown/Word（docx/doc）文件中提取文本。
- **技能蒸馏**（distill）：调用 LLM 从自然语言描述生成技能卡片，
  支持同步和流式两种模式。
- **技能改写**（rewrite）：调用 LLM 对现有技能进行改写优化，
  支持同步和流式两种模式。
- **流式任务**：蒸馏和改写均支持异步任务模式（create job → poll/stream events → cancel）。

分支模型：技能支持 agent 私有分支（branch），非 overall agent 的修改会写入
独立分支，通过 publish 操作提升为主干版本。
统计聚合：``_skill_stats`` 聚合调用次数和反馈数据，支持按版本和近 N 版本统计。
"""

from __future__ import annotations

import base64
import json
import re
import zipfile
from collections.abc import Iterator
from io import BytesIO
from time import sleep
from xml.etree import ElementTree

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select

from app.agents.branching import (
    branch_versions,
    ensure_agent_skill_branch,
    ensure_open_gallery_binding,
    ensure_private_resource_binding,
    get_agent,
    hide_open_gallery_binding,
    is_open_gallery_resource,
    mark_resource_open_gallery,
    project_skill_with_branch,
    require_overall_agent,
    rollback_branch,
    update_branch_skill,
    user_creator_metadata,
    visible_skill_rows,
)
from app.async_jobs import enqueue_async_job
from app.db import get_session
from app.db.models import (
    AgentEvent,
    AgentResourceBinding,
    AgentSkillBranchVersion,
    ModelConfig,
    Skill,
    SkillFeedback,
    SkillVersion,
    Tool,
    User,
    utc_now,
)
from app.llm.model_config_resolver import resolve_model_config_for_runtime
from app.llm import LLMError
from app.security.auth import ensure_current_user_tenant, get_current_user
from app.security.permissions import (
    ensure_agent_scope_manager,
    ensure_open_gallery_admin,
    require_agent_scope_viewer,
)
from app.security.tenant import ensure_tenant
from app.skills import SkillDistiller, SkillEditor
from app.skills.skill_schema import (
    SkillCard,
    SkillCreateRequest,
    SkillDistillRequest,
    SkillDistillResponse,
    SkillFileExtractRequest,
    SkillFileExtractResponse,
    SkillRead,
    SkillRewriteRequest,
    SkillRewriteResponse,
    SkillVersionRead,
    SkillUpdateRequest,
)
from app.skills.stream_jobs import SkillStreamEvent, SkillStreamJob, stream_jobs
from app.skills.step_ids import skill_card_with_unique_step_ids

router = APIRouter(
    prefix="/api/enterprise/skills",
    tags=["enterprise:skills"],
    dependencies=[Depends(get_current_user)],
)


def skill_read(
    row: Skill,
    stats: dict[str, dict[str, float | int]] | None = None,
    recent_stats: dict[str, dict[str, object]] | None = None,
) -> SkillRead:
    """将 Skill 数据库记录转换为 API 响应模型（含统计数据和分支元数据）。

    Args:
        row: 技能的数据库行记录（可能携带 agent_branch_meta 属性）。
        stats: 按 skill_id / skill_id@version 维度的统计字典。
        recent_stats: 近 N 版本的聚合统计数据。

    Returns:
        包含内容、统计和分支信息的 ``SkillRead`` 响应对象。
    """
    all_stats = stats or {}
    skill_stats = _stats_for(all_stats, row.skill_id, row.version)
    total_stats = all_stats.get(row.skill_id, {})
    recent_skill_stats = (recent_stats or {}).get(row.skill_id, {})
    content, _warnings = skill_card_with_unique_step_ids(SkillCard.model_validate(row.content_json))
    branch_meta = getattr(row, "agent_branch_meta", {}) or {}
    return SkillRead(
        id=row.id,
        tenant_id=row.tenant_id,
        skill_id=row.skill_id,
        version=row.version,
        name=row.name,
        business_domain=row.business_domain,
        description=row.description,
        content=content,
        status=row.status,
        call_count=int(skill_stats.get("call_count", 0)),
        positive_feedback_count=int(skill_stats.get("positive_feedback_count", 0)),
        negative_feedback_count=int(skill_stats.get("negative_feedback_count", 0)),
        positive_rate=float(skill_stats.get("positive_rate", 0.0)),
        negative_rate=float(skill_stats.get("negative_rate", 0.0)),
        total_call_count=int(total_stats.get("call_count", 0)),
        total_positive_feedback_count=int(total_stats.get("positive_feedback_count", 0)),
        total_negative_feedback_count=int(total_stats.get("negative_feedback_count", 0)),
        total_positive_rate=float(total_stats.get("positive_rate", 0.0)),
        total_negative_rate=float(total_stats.get("negative_rate", 0.0)),
        recent_versions=list(recent_skill_stats.get("recent_versions", [])),
        recent_call_count=int(recent_skill_stats.get("call_count", 0)),
        recent_positive_feedback_count=int(recent_skill_stats.get("positive_feedback_count", 0)),
        recent_negative_feedback_count=int(recent_skill_stats.get("negative_feedback_count", 0)),
        recent_positive_rate=float(recent_skill_stats.get("positive_rate", 0.0)),
        recent_negative_rate=float(recent_skill_stats.get("negative_rate", 0.0)),
        agent_id=branch_meta.get("agent_id"),
        branch_status=branch_meta.get("status"),
        branch_sync_state=branch_meta.get("sync_state"),
        branch_base_version=branch_meta.get("base_version"),
        branch_head_version=branch_meta.get("head_version"),
        metadata=dict(branch_meta.get("metadata") or {}),
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def skill_version_read(
    row: SkillVersion, stats: dict[str, dict[str, float | int]] | None = None
) -> SkillVersionRead:
    """将 SkillVersion 数据库记录转换为版本响应模型。

    Args:
        row: 技能版本数据库行记录。
        stats: 统计数据字典。

    Returns:
        ``SkillVersionRead`` 响应对象。
    """
    skill_stats = _stats_for(stats or {}, row.skill_id, row.version)
    content, _warnings = skill_card_with_unique_step_ids(SkillCard.model_validate(row.content_json))
    return SkillVersionRead(
        id=row.id,
        tenant_id=row.tenant_id,
        skill_id=row.skill_id,
        version=row.version,
        name=row.name,
        business_domain=row.business_domain,
        description=row.description,
        content=content,
        status=row.status,
        call_count=int(skill_stats.get("call_count", 0)),
        positive_feedback_count=int(skill_stats.get("positive_feedback_count", 0)),
        negative_feedback_count=int(skill_stats.get("negative_feedback_count", 0)),
        positive_rate=float(skill_stats.get("positive_rate", 0.0)),
        negative_rate=float(skill_stats.get("negative_rate", 0.0)),
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _branch_version_read(row: AgentSkillBranchVersion) -> SkillVersionRead:
    """将 Agent 分支版本记录转换为版本响应模型。

    Args:
        row: Agent 技能分支版本数据库行记录。

    Returns:
        ``SkillVersionRead`` 响应对象（分支版本无统计数据）。
    """
    content, _warnings = skill_card_with_unique_step_ids(SkillCard.model_validate(row.content_json))
    return SkillVersionRead(
        id=row.id,
        tenant_id=row.tenant_id,
        skill_id=row.skill_id,
        version=row.version,
        name=content.name,
        business_domain=content.business_domain,
        description=content.description,
        content=content,
        status=row.status,
        call_count=0,
        positive_feedback_count=0,
        negative_feedback_count=0,
        positive_rate=0.0,
        negative_rate=0.0,
        agent_id=row.agent_id,
        branch_sync_state=row.sync_state,
        branch_base_version=row.base_version,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


@router.get("", response_model=list[SkillRead], dependencies=[Depends(require_agent_scope_viewer)])
def list_skills(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = None,
) -> list[SkillRead]:
    """列出当前租户下当前 Agent 可见的技能列表。

    包含非活跃状态的技能，附带调用统计和近期版本统计。

    Args:
        tenant_id: 租户 ID。
        db: 数据库会话。
        agent_id: 可选的 Agent ID，用于过滤可见范围。

    Returns:
        ``SkillRead`` 响应对象列表。
    """
    ensure_tenant(db, tenant_id)
    rows = visible_skill_rows(db, tenant_id, agent_id, include_inactive=True)
    stats = _skill_stats(db, tenant_id)
    recent_stats = _recent_skill_stats(db, tenant_id, stats)
    return [skill_read(row, stats, recent_stats) for row in rows]


@router.post("", response_model=SkillRead)
def create_skill(
    request: SkillCreateRequest,
    agent_id: str | None = None,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> SkillRead:
    """创建新的 SOP 技能。

    校验 skill_id 唯一性后创建技能记录，同步工具绑定，
    并根据 Agent 作用域建立私有分支或开放画廊绑定。

    Args:
        request: 技能创建请求体。
        agent_id: 可选的 Agent ID。
        db: 数据库会话。
        current_user: 当前登录用户。

    Returns:
        创建后的 ``SkillRead`` 响应对象。

    Raises:
        HTTPException 409: skill_id 在租户内已存在。
        HTTPException 403: 无开放画廊管理员权限。
    """
    ensure_tenant(db, request.tenant_id)
    existing = db.exec(
        select(Skill).where(
            Skill.tenant_id == request.tenant_id, Skill.skill_id == request.content.skill_id
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Skill ID already exists for this tenant")
    normalized_content, _warnings = skill_card_with_unique_step_ids(request.content)
    content = normalized_content.model_dump()
    agent = ensure_agent_scope_manager(db, request.tenant_id, agent_id, current_user)
    row = Skill(
        tenant_id=request.tenant_id,
        skill_id=normalized_content.skill_id,
        version=normalized_content.version,
        name=normalized_content.name,
        business_domain=normalized_content.business_domain,
        description=normalized_content.description,
        content_json=content,
        status=request.status,
    )
    db.add(row)
    db.flush()
    _sync_skill_tool_bindings(db, request.tenant_id, row.skill_id, row.content_json)
    branch = None
    binding_status = "active" if request.status == "published" else "inactive"
    creator_metadata = user_creator_metadata(current_user)
    if agent and not agent.is_overall:
        ensure_private_resource_binding(
            db,
            request.tenant_id,
            agent.id,
            "skill",
            row.id,
            binding_status,
            metadata_json=creator_metadata,
        )
        branch = ensure_agent_skill_branch(
            db,
            request.tenant_id,
            agent.id,
            row,
            metadata_json=creator_metadata,
        )
    else:
        ensure_open_gallery_admin(request.tenant_id, current_user)
        mark_resource_open_gallery(row, creator_metadata)
        ensure_open_gallery_binding(
            db,
            request.tenant_id,
            "skill",
            row.id,
            binding_status,
            metadata_json=creator_metadata,
        )
    db.commit()
    db.refresh(row)
    _upsert_skill_version(db, row)
    stats = _skill_stats(db, request.tenant_id)
    if branch:
        row = project_skill_with_branch(row, branch, binding_status)
    return skill_read(row, stats, _recent_skill_stats(db, request.tenant_id, stats))


@router.get(
    "/{skill_id}", response_model=SkillRead, dependencies=[Depends(require_agent_scope_viewer)]
)
def get_skill(
    skill_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = None,
    db: Session = Depends(get_session),
) -> SkillRead:
    """按 skill_id 获取单个技能详情（含统计）。

    Args:
        skill_id: 技能唯一标识。
        tenant_id: 租户 ID。
        agent_id: 可选的 Agent ID，用于可见性过滤。
        db: 数据库会话。

    Returns:
        ``SkillRead`` 响应对象。

    Raises:
        HTTPException 404: 技能不存在或当前 Agent 不可见。
    """
    row = _get_visible_skill_for_scope(db, tenant_id, skill_id, agent_id)
    stats = _skill_stats(db, tenant_id)
    return skill_read(row, stats, _recent_skill_stats(db, tenant_id, stats))


@router.put("/{skill_id}", response_model=SkillRead)
def update_skill(
    skill_id: str,
    request: SkillUpdateRequest,
    agent_id: str | None = None,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> SkillRead:
    """更新指定技能的内容和状态。

    Agent 私有作用域下写入分支；开放画廊下直接更新主干并创建版本快照。

    Args:
        skill_id: 技能唯一标识。
        request: 技能更新请求体。
        agent_id: 可选的 Agent ID。
        db: 数据库会话。
        current_user: 当前登录用户。

    Returns:
        更新后的 ``SkillRead`` 响应对象。

    Raises:
        HTTPException 400: 请求中的 skill_id 与路径参数不一致。
        HTTPException 404: 技能不存在或 Agent 不可见。
        HTTPException 403: 无开放画廊管理员权限。
    """
    if request.content.skill_id != skill_id:
        raise HTTPException(status_code=400, detail="SOP skill_id cannot be modified")
    row = _get_skill(db, request.tenant_id, skill_id)
    normalized_content, _warnings = skill_card_with_unique_step_ids(request.content)
    agent = ensure_agent_scope_manager(db, request.tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == request.tenant_id,
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "skill",
                AgentResourceBinding.resource_id == row.id,
                AgentResourceBinding.status != "deleted",
            )
        ).first()
        if not binding:
            raise HTTPException(status_code=404, detail="Skill not visible to this agent")
        branch = update_branch_skill(
            db,
            request.tenant_id,
            agent.id,
            row,
            normalized_content.model_dump(),
            "技能分支改写",
        )
        _sync_skill_tool_bindings(
            db,
            request.tenant_id,
            row.skill_id,
            normalized_content.model_dump(),
        )
        db.commit()
        projected = project_skill_with_branch(row, branch, binding.status)
        stats = _skill_stats(db, request.tenant_id)
        return skill_read(projected, stats, _recent_skill_stats(db, request.tenant_id, stats))
    ensure_open_gallery_admin(request.tenant_id, current_user)
    row.version = normalized_content.version
    row.name = normalized_content.name
    row.business_domain = normalized_content.business_domain
    row.description = normalized_content.description
    row.content_json = normalized_content.model_dump()
    _sync_skill_tool_bindings(db, request.tenant_id, row.skill_id, row.content_json)
    if request.status:
        row.status = request.status
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    _upsert_skill_version(db, row)
    stats = _skill_stats(db, request.tenant_id)
    return skill_read(row, stats, _recent_skill_stats(db, request.tenant_id, stats))


@router.post("/{skill_id}/publish", response_model=SkillRead)
def publish_skill(
    skill_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = None,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> SkillRead:
    """发布（上线）指定技能。

    Agent 私有分支置为 active；开放画廊主干置为 published 并创建版本快照。

    Args:
        skill_id: 技能唯一标识。
        tenant_id: 租户 ID。
        agent_id: 可选的 Agent ID。
        db: 数据库会话。
        current_user: 当前登录用户。

    Returns:
        发布后的 ``SkillRead`` 响应对象。

    Raises:
        HTTPException 404: 技能不存在。
        HTTPException 403: 无开放画廊管理员权限。
    """
    row = _get_skill(db, tenant_id, skill_id)
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        branch = ensure_agent_skill_branch(db, tenant_id, agent.id, row)
        branch.status = "active"
        branch.updated_at = utc_now()
        db.add(branch)
        _sync_skill_tool_bindings(db, tenant_id, row.skill_id, branch.content_json)
        ensure_private_resource_binding(db, tenant_id, agent.id, "skill", row.id, "active")
        db.commit()
        projected = project_skill_with_branch(row, branch, "active")
        stats = _skill_stats(db, tenant_id)
        return skill_read(projected, stats, _recent_skill_stats(db, tenant_id, stats))
    ensure_open_gallery_admin(tenant_id, current_user)
    row.status = "published"
    _sync_skill_tool_bindings(db, tenant_id, row.skill_id, row.content_json)
    mark_resource_open_gallery(row)
    row.updated_at = utc_now()
    db.add(row)
    db.flush()
    ensure_open_gallery_binding(db, tenant_id, "skill", row.id, "active")
    db.commit()
    db.refresh(row)
    _upsert_skill_version(db, row)
    stats = _skill_stats(db, tenant_id)
    return skill_read(row, stats, _recent_skill_stats(db, tenant_id, stats))


@router.post("/{skill_id}/archive", response_model=SkillRead)
def archive_skill(
    skill_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = None,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> SkillRead:
    """归档（下线）指定技能。

    Agent 私有分支置为 inactive；开放画廊主干置为 archived 并创建版本快照。

    Args:
        skill_id: 技能唯一标识。
        tenant_id: 租户 ID。
        agent_id: 可选的 Agent ID。
        db: 数据库会话。
        current_user: 当前登录用户。

    Returns:
        归档后的 ``SkillRead`` 响应对象。

    Raises:
        HTTPException 404: 技能不存在。
        HTTPException 403: 无开放画廊管理员权限。
    """
    row = _get_skill(db, tenant_id, skill_id)
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        branch = ensure_agent_skill_branch(db, tenant_id, agent.id, row)
        branch.status = "inactive"
        branch.updated_at = utc_now()
        db.add(branch)
        ensure_private_resource_binding(db, tenant_id, agent.id, "skill", row.id, "inactive")
        db.commit()
        projected = project_skill_with_branch(row, branch, "inactive")
        stats = _skill_stats(db, tenant_id)
        return skill_read(projected, stats, _recent_skill_stats(db, tenant_id, stats))
    ensure_open_gallery_admin(tenant_id, current_user)
    row.status = "archived"
    row.updated_at = utc_now()
    db.add(row)
    db.flush()
    ensure_open_gallery_binding(db, tenant_id, "skill", row.id, "inactive")
    db.commit()
    db.refresh(row)
    _upsert_skill_version(db, row)
    stats = _skill_stats(db, tenant_id)
    return skill_read(row, stats, _recent_skill_stats(db, tenant_id, stats))


@router.post("/{skill_id}/draft", response_model=SkillRead)
def draft_skill(
    skill_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = None,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> SkillRead:
    """将指定技能切换为草稿（draft）状态。

    仅全局技能支持草稿状态，Agent 私有分支不支持。

    Args:
        skill_id: 技能唯一标识。
        tenant_id: 租户 ID。
        agent_id: 可选的 Agent ID。
        db: 数据库会话。
        current_user: 当前登录用户。

    Returns:
        草稿状态下的 ``SkillRead`` 响应对象。

    Raises:
        HTTPException 403: Agent 分支技能不支持草稿，或无管理员权限。
    """
    row = _get_skill(db, tenant_id, skill_id)
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        raise HTTPException(status_code=403, detail="Only overall SOPs can be moved to draft")
    ensure_open_gallery_admin(tenant_id, current_user)
    row.status = "draft"
    row.updated_at = utc_now()
    db.add(row)
    db.flush()
    ensure_open_gallery_binding(db, tenant_id, "skill", row.id, "inactive")
    db.commit()
    db.refresh(row)
    _upsert_skill_version(db, row)
    stats = _skill_stats(db, tenant_id)
    return skill_read(row, stats, _recent_skill_stats(db, tenant_id, stats))


@router.delete("/{skill_id}")
def delete_skill(
    skill_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = None,
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    """删除或隐藏指定技能。

    Agent 私有分支将绑定和分支标记为 deleted（仅隐藏）；
    全局 Agent 下隐藏开放画廊绑定；
    超级管理员权限下级联删除反馈、版本和技能记录。

    Args:
        skill_id: 技能唯一标识。
        tenant_id: 租户 ID。
        db: 数据库会话。
        agent_id: 可选的 Agent ID。
        current_user: 当前登录用户。

    Returns:
        包含操作状态的字典：
        - ``hidden``：已隐藏（Agent 或全局 Agent）。
        - ``deleted``：已彻底删除（超级管理员）。

    Raises:
        HTTPException 404: 技能不存在或不可见。
        HTTPException 403: 无操作权限。
    """
    row = _get_skill(db, tenant_id, skill_id)
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        binding = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == tenant_id,
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "skill",
                AgentResourceBinding.resource_id == row.id,
            )
        ).first()
        if not binding:
            binding = AgentResourceBinding(
                tenant_id=tenant_id,
                agent_id=agent.id,
                resource_type="skill",
                resource_id=row.id,
                status="deleted",
            )
        else:
            binding.status = "deleted"
            binding.updated_at = utc_now()
        branch = ensure_agent_skill_branch(db, tenant_id, agent.id, row)
        branch.status = "deleted"
        branch.updated_at = utc_now()
        db.add(binding)
        db.add(branch)
        db.commit()
        return {"status": "hidden"}
    if agent and agent.is_overall:
        if not is_open_gallery_resource(db, tenant_id, "skill", row):
            raise HTTPException(status_code=404, detail="Skill not visible in open gallery")
        ensure_open_gallery_admin(tenant_id, current_user)
        hide_open_gallery_binding(db, tenant_id, "skill", row.id)
        db.commit()
        return {"status": "hidden"}

    require_overall_agent(db, tenant_id, agent_id)
    ensure_open_gallery_admin(tenant_id, current_user)
    feedback_rows = db.exec(
        select(SkillFeedback).where(
            SkillFeedback.tenant_id == tenant_id,
            SkillFeedback.skill_id == skill_id,
        )
    ).all()
    for feedback in feedback_rows:
        db.delete(feedback)
    version_rows = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == tenant_id, SkillVersion.skill_id == skill_id
        )
    ).all()
    for version_row in version_rows:
        db.delete(version_row)
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@router.get(
    "/{skill_id}/versions",
    response_model=list[SkillVersionRead],
    dependencies=[Depends(require_agent_scope_viewer)],
)
def list_skill_versions(
    skill_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = None,
) -> list[SkillVersionRead]:
    """列出指定技能的历史版本列表。

    Agent 私有作用域返回分支版本列表；开放画廊返回主干版本快照列表。

    Args:
        skill_id: 技能唯一标识。
        tenant_id: 租户 ID。
        db: 数据库会话。
        agent_id: 可选的 Agent ID。

    Returns:
        ``SkillVersionRead`` 响应对象列表，按创建时间倒序。

    Raises:
        HTTPException 404: 技能不存在或不可见。
    """
    row = _get_visible_skill_for_scope(db, tenant_id, skill_id, agent_id)
    agent = get_agent(db, tenant_id, agent_id)
    if agent and not agent.is_overall:
        rows = branch_versions(db, tenant_id, agent.id, skill_id)
        return [_branch_version_read(row) for row in rows]
    current_snapshot = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == tenant_id,
            SkillVersion.skill_id == skill_id,
            SkillVersion.version == row.version,
        )
    ).first()
    if not current_snapshot:
        _upsert_skill_version(db, row)
    rows = db.exec(
        select(SkillVersion)
        .where(SkillVersion.tenant_id == tenant_id, SkillVersion.skill_id == skill_id)
        .order_by(SkillVersion.created_at.desc())
    ).all()
    stats = _skill_stats(db, tenant_id)
    return [skill_version_read(version_row, stats) for version_row in rows]


@router.get(
    "/{skill_id}/versions/{version}",
    response_model=SkillVersionRead,
    dependencies=[Depends(require_agent_scope_viewer)],
)
def get_skill_version(
    skill_id: str,
    version: str,
    tenant_id: str = Query(...),
    agent_id: str | None = None,
    db: Session = Depends(get_session),
) -> SkillVersionRead:
    """获取指定技能的特定版本详情。

    Args:
        skill_id: 技能唯一标识。
        version: 版本号。
        tenant_id: 租户 ID。
        agent_id: 可选的 Agent ID。
        db: 数据库会话。

    Returns:
        ``SkillVersionRead`` 响应对象。

    Raises:
        HTTPException 404: 技能或版本不存在。
    """
    _get_visible_skill_for_scope(db, tenant_id, skill_id, agent_id)
    agent = get_agent(db, tenant_id, agent_id)
    if agent and not agent.is_overall:
        row = next(
            (
                item
                for item in branch_versions(db, tenant_id, agent.id, skill_id)
                if item.version == version
            ),
            None,
        )
        if not row:
            raise HTTPException(status_code=404, detail="Skill version not found")
        return _branch_version_read(row)
    row = _get_skill_version(db, tenant_id, skill_id, version)
    return skill_version_read(row, _skill_stats(db, tenant_id))


@router.delete("/{skill_id}/versions/{version}")
def delete_skill_version(
    skill_id: str,
    version: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    """删除指定技能的历史版本快照。

    不允许删除当前活动版本。需要开放画廊管理员权限。

    Args:
        skill_id: 技能唯一标识。
        version: 待删除的版本号。
        tenant_id: 租户 ID。
        db: 数据库会话。
        current_user: 当前登录用户。

    Returns:
        包含 ``status: deleted`` 的字典。

    Raises:
        HTTPException 409: 尝试删除活动版本。
        HTTPException 404: 技能或版本不存在。
        HTTPException 403: 无开放画廊管理员权限。
    """
    skill = _get_skill(db, tenant_id, skill_id)
    ensure_open_gallery_admin(tenant_id, current_user)
    if skill.version == version:
        raise HTTPException(status_code=409, detail="Cannot delete the active skill version")
    row = _get_skill_version(db, tenant_id, skill_id, version)
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@router.post("/{skill_id}/versions/{version}/rollback", response_model=SkillRead)
def rollback_skill_version(
    skill_id: str,
    version: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = None,
    current_user: User = Depends(get_current_user),
) -> SkillRead:
    """将技能回滚到指定的历史版本。

    Agent 私有作用域下回滚分支版本；开放画廊下回滚主干版本。

    Args:
        skill_id: 技能唯一标识。
        version: 回滚目标版本号。
        tenant_id: 租户 ID。
        db: 数据库会话。
        agent_id: 可选的 Agent ID。
        current_user: 当前登录用户。

    Returns:
        回滚后的 ``SkillRead`` 响应对象。

    Raises:
        HTTPException 404: 技能或版本不存在。
        HTTPException 403: 无开放画廊管理员权限。
    """
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        branch = rollback_branch(db, tenant_id, agent.id, skill_id, version)
        db.commit()
        skill = _get_skill(db, tenant_id, skill_id)
        projected = project_skill_with_branch(skill, branch)
        stats = _skill_stats(db, tenant_id)
        return skill_read(projected, stats, _recent_skill_stats(db, tenant_id, stats))
    ensure_open_gallery_admin(tenant_id, current_user)
    row = _get_skill(db, tenant_id, skill_id)
    version_row = _get_skill_version(db, tenant_id, skill_id, version)
    normalized_content, _warnings = skill_card_with_unique_step_ids(
        SkillCard.model_validate(version_row.content_json)
    )
    normalized_content = normalized_content.model_copy(
        update={
            "version": version_row.version,
            "name": version_row.name,
            "business_domain": version_row.business_domain,
            "description": version_row.description or normalized_content.description,
        }
    )
    row.version = version_row.version
    row.name = version_row.name
    row.business_domain = version_row.business_domain
    row.description = version_row.description
    row.content_json = normalized_content.model_dump()
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    stats = _skill_stats(db, tenant_id)
    return skill_read(row, stats, _recent_skill_stats(db, tenant_id, stats))


@router.post("/files/extract", response_model=SkillFileExtractResponse)
def extract_skill_file(request: SkillFileExtractRequest) -> SkillFileExtractResponse:
    """从上传的文件（Markdown/Word）中提取纯文本内容。

    Args:
        request: 包含 base64 编码文件内容和文件名的请求体。

    Returns:
        提取结果响应对象。

    Raises:
        HTTPException 400: 文件内容无效或无可用文本。
        HTTPException 413: 文件过大（超过 5MB）。
    """
    try:
        data = base64.b64decode(request.content_base64, validate=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid file content") from exc
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File is too large")
    text = _extract_uploaded_skill_file(request.filename, data)
    if not text.strip():
        raise HTTPException(status_code=400, detail="No readable text found in file")
    return SkillFileExtractResponse(filename=request.filename, text=text)


@router.post("/distill", response_model=SkillDistillResponse)
def distill_skill(
    request: SkillDistillRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> SkillDistillResponse:
    """同步调用 LLM 从自然语言描述蒸馏生成技能卡片。

    Args:
        request: 蒸馏请求体，包含描述文本和模型配置。
        db: 数据库会话。
        current_user: 当前登录用户。

    Returns:
        蒸馏生成的技能卡片响应对象。

    Raises:
        HTTPException 502: LLM 调用失败。
    """
    ensure_current_user_tenant(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    model_config = _get_request_model(db, request.tenant_id, request.model_config_id)
    request = _with_available_tools(db, request)
    try:
        return SkillDistiller().distill(request, model_config)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/distill/stream")
def distill_skill_stream(
    request: SkillDistillRequest,
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """以 SSE 流式调用 LLM 蒸馏生成技能卡片。

    创建异步蒸馏任务后立即返回事件流。

    Args:
        request: 蒸馏请求体。
        current_user: 当前登录用户。

    Returns:
        ``text/event-stream`` 类型的流式响应。
    """
    ensure_current_user_tenant(request.tenant_id, current_user)
    job_id = _start_distill_stream_job(request, current_user)
    return StreamingResponse(_stream_skill_job(job_id), media_type="text/event-stream")


@router.post("/{skill_id}/rewrite/stream")
def rewrite_skill_stream(
    skill_id: str,
    request: SkillRewriteRequest,
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """以 SSE 流式调用 LLM 改写指定技能。

    Args:
        skill_id: 技能唯一标识。
        request: 改写请求体。
        current_user: 当前登录用户。

    Returns:
        ``text/event-stream`` 类型的流式响应。

    Raises:
        HTTPException 400: 路径 skill_id 与请求体中不一致。
    """
    if request.current_skill.skill_id != skill_id:
        raise HTTPException(
            status_code=400, detail="Path skill_id must match current_skill.skill_id"
        )
    ensure_current_user_tenant(request.tenant_id, current_user)
    job_id = _start_rewrite_stream_job(skill_id, request, current_user)
    return StreamingResponse(_stream_skill_job(job_id), media_type="text/event-stream")


@router.post("/distill/jobs")
def create_distill_job(
    request: SkillDistillRequest,
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    """创建异步技能蒸馏任务，返回任务 ID。

    Args:
        request: 蒸馏请求体。
        current_user: 当前登录用户。

    Returns:
        包含 ``job_id`` 的字典。
    """
    ensure_current_user_tenant(request.tenant_id, current_user)
    return {"job_id": _start_distill_stream_job(request, current_user)}


@router.post("/{skill_id}/rewrite/jobs")
def create_rewrite_job(
    skill_id: str,
    request: SkillRewriteRequest,
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    """创建异步技能改写任务，返回任务 ID。

    Args:
        skill_id: 技能唯一标识。
        request: 改写请求体。
        current_user: 当前登录用户。

    Returns:
        包含 ``job_id`` 的字典。

    Raises:
        HTTPException 400: 路径 skill_id 与请求体中不一致。
    """
    if request.current_skill.skill_id != skill_id:
        raise HTTPException(
            status_code=400, detail="Path skill_id must match current_skill.skill_id"
        )
    ensure_current_user_tenant(request.tenant_id, current_user)
    return {"job_id": _start_rewrite_stream_job(skill_id, request, current_user)}


@router.get("/jobs/{job_id}")
def get_skill_stream_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    """查询异步流式任务的状态信息。

    Args:
        job_id: 任务 ID。
        current_user: 当前登录用户。

    Returns:
        包含任务 ID、名称、状态、错误信息和事件序列号等字段的字典。

    Raises:
        HTTPException 404: 任务不存在或不属于当前用户。
    """
    job = _owned_stream_job(job_id, current_user)
    return {
        "job_id": job.id,
        "name": job.name,
        "status": job.status,
        "error": job.error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
        "last_seq": job.events[-1].seq if job.events else 0,
    }


@router.get("/jobs/{job_id}/stream")
def stream_existing_skill_job(
    job_id: str,
    after_seq: int = Query(0),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """以 SSE 流式获取已有任务的事件流（支持断点续传）。

    Args:
        job_id: 任务 ID。
        after_seq: 从此序列号之后开始获取事件（用于断点续传）。
        current_user: 当前登录用户。

    Returns:
        ``text/event-stream`` 类型的流式响应。

    Raises:
        HTTPException 404: 任务不存在或不属于当前用户。
    """
    _owned_stream_job(job_id, current_user)
    return StreamingResponse(_stream_skill_job(job_id, after_seq), media_type="text/event-stream")


@router.post("/jobs/{job_id}/cancel")
def cancel_skill_stream_job(
    job_id: str,
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    """请求取消正在进行的流式任务。

    Args:
        job_id: 任务 ID。
        current_user: 当前登录用户。

    Returns:
        包含 ``status: cancel_requested`` 的字典。

    Raises:
        HTTPException 404: 任务不存在或不属于当前用户。
    """
    _owned_stream_job(job_id, current_user)
    stream_jobs.cancel(job_id)
    stream_jobs.append(job_id, "status", {"text": "已请求停止生成"})
    return {"status": "cancel_requested", "job_id": job_id}


@router.post("/{skill_id}/rewrite", response_model=SkillRewriteResponse)
def rewrite_skill(
    skill_id: str,
    request: SkillRewriteRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> SkillRewriteResponse:
    """同步调用 LLM 改写优化指定技能。

    Args:
        skill_id: 技能唯一标识。
        request: 改写请求体。
        db: 数据库会话。
        current_user: 当前登录用户。

    Returns:
        改写后的技能卡片响应对象。

    Raises:
        HTTPException 400: 路径 skill_id 与请求体中不一致。
        HTTPException 502: LLM 调用失败。
    """
    if request.current_skill.skill_id != skill_id:
        raise HTTPException(
            status_code=400, detail="Path skill_id must match current_skill.skill_id"
        )
    ensure_current_user_tenant(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    model_config = _get_request_model(db, request.tenant_id, request.model_config_id)
    request = _with_available_tools_for_rewrite(db, request)
    try:
        return SkillEditor().rewrite(request, model_config)
    except LLMError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _owned_stream_job(job_id: str, current_user: User) -> SkillStreamJob:
    """获取属于当前用户的流式任务，校验归属权。

    Args:
        job_id: 任务 ID。
        current_user: 当前登录用户。

    Returns:
        ``SkillStreamJob`` 任务对象。

    Raises:
        HTTPException 404: 任务不存在或不属于当前用户的租户。
    """
    job = stream_jobs.get(job_id)
    if not job or job.tenant_id != current_user.tenant_id or job.user_id != current_user.id:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _start_distill_stream_job(request: SkillDistillRequest, current_user: User) -> str:
    """创建蒸馏流式任务并入队异步执行。

    Args:
        request: 蒸馏请求体。
        current_user: 当前登录用户。

    Returns:
        新创建的任务 ID。
    """
    job = stream_jobs.create("skill.distill", request.tenant_id, current_user.id)
    stream_jobs.append(job.id, "job_started", {"job_id": job.id, "name": job.name})
    enqueue_async_job(
        "skill.distill_stream",
        _run_distill_stream_job,
        job.id,
        request.model_dump(mode="json"),
        metadata={"tenant_id": request.tenant_id, "job_id": job.id},
    )
    return job.id


def _start_rewrite_stream_job(
    skill_id: str, request: SkillRewriteRequest, current_user: User
) -> str:
    """创建改写流式任务并入队异步执行。

    Args:
        skill_id: 技能唯一标识。
        request: 改写请求体。
        current_user: 当前登录用户。

    Returns:
        新创建的任务 ID。
    """
    job = stream_jobs.create("skill.rewrite", request.tenant_id, current_user.id)
    stream_jobs.append(
        job.id, "job_started", {"job_id": job.id, "name": job.name, "skill_id": skill_id}
    )
    enqueue_async_job(
        "skill.rewrite_stream",
        _run_rewrite_stream_job,
        job.id,
        skill_id,
        request.model_dump(mode="json"),
        metadata={"tenant_id": request.tenant_id, "job_id": job.id, "skill_id": skill_id},
    )
    return job.id


def _run_distill_stream_job(job_id: str, request_data: dict[str, object]) -> None:
    """异步执行技能蒸馏流式任务的实际逻辑。

    在独立数据库会话中调用蒸馏器，将流式事件实时写入任务队列，
    支持取消检测和异常捕获。

    Args:
        job_id: 任务 ID。
        request_data: 序列化的蒸馏请求数据。
    """
    stream_jobs.start(job_id)
    try:
        request = SkillDistillRequest.model_validate(request_data)
        with Session(get_session_engine()) as db:
            ensure_tenant(db, request.tenant_id)
            model_config = _get_request_model(db, request.tenant_id, request.model_config_id)
            enriched_request = _with_available_tools(db, request)
            stream_jobs.append(job_id, "status", {"text": "正在调用模型生成新技能"})
            for item in SkillDistiller().stream_text(enriched_request, model_config):
                if stream_jobs.is_cancelled(job_id):
                    stream_jobs.append(job_id, "status", {"text": "已停止生成"})
                    stream_jobs.complete(job_id)
                    return
                stream_jobs.append(job_id, str(item["event"]), dict(item["data"]))
        stream_jobs.complete(job_id)
    except Exception as exc:  # noqa: BLE001 - expose stable job failure to UI.
        stream_jobs.fail(job_id, str(exc))


def _run_rewrite_stream_job(job_id: str, skill_id: str, request_data: dict[str, object]) -> None:
    """异步执行技能改写流式任务的实际逻辑。

    在独立数据库会话中调用改写器，将流式事件实时写入任务队列，
    支持取消检测和异常捕获。

    Args:
        job_id: 任务 ID。
        skill_id: 技能唯一标识。
        request_data: 序列化的改写请求数据。
    """
    stream_jobs.start(job_id)
    try:
        request = SkillRewriteRequest.model_validate(request_data)
        if request.current_skill.skill_id != skill_id:
            raise ValueError("Path skill_id must match current_skill.skill_id")
        with Session(get_session_engine()) as db:
            ensure_tenant(db, request.tenant_id)
            model_config = _get_request_model(db, request.tenant_id, request.model_config_id)
            enriched_request = _with_available_tools_for_rewrite(db, request)
            stream_jobs.append(job_id, "status", {"text": "正在调用模型分析改写要求"})
            for item in SkillEditor().stream_text(enriched_request, model_config):
                if stream_jobs.is_cancelled(job_id):
                    stream_jobs.append(job_id, "status", {"text": "已停止改写"})
                    stream_jobs.complete(job_id)
                    return
                stream_jobs.append(job_id, str(item["event"]), dict(item["data"]))
        stream_jobs.complete(job_id)
    except Exception as exc:  # noqa: BLE001 - expose stable job failure to UI.
        stream_jobs.fail(job_id, str(exc))


def _stream_skill_job(job_id: str, after_seq: int = 0) -> Iterator[str]:
    """以生成器形式持续输出流式任务的 SSE 事件。

    轮询任务事件队列，直到任务完成或失败。以 0.15 秒间隔休眠降低 CPU 占用。

    Args:
        job_id: 任务 ID。
        after_seq: 仅输出此序列号之后的事件。

    Yields:
        SSE 格式的事件字符串。
    """
    last_seq = max(0, after_seq)
    yield _sse("job_attached", {"job_id": job_id, "after_seq": after_seq})
    while True:
        job, events = stream_jobs.snapshot(job_id, last_seq)
        if not job:
            yield _sse("error", {"message": "Job not found"})
            return
        for event in events:
            last_seq = event.seq
            yield _sse_event(event, job_id)
        if job.status in {"succeeded", "failed"} and not events:
            yield _sse("job_complete", {"job_id": job_id, "status": job.status, "error": job.error})
            return
        sleep(0.15)


def _sse_event(event: SkillStreamEvent, job_id: str) -> str:
    """将流式事件对象转换为 SSE 格式字符串。

    Args:
        event: 流式事件对象。
        job_id: 任务 ID。

    Returns:
        SSE 格式的事件字符串。
    """
    data = {"job_id": job_id, "seq": event.seq, **event.data}
    return _sse(event.event, data)


def get_session_engine():
    """延迟获取数据库引擎（避免循环导入）。

    Returns:
        SQLAlchemy 数据库引擎对象。
    """
    from app.db import engine

    return engine


def _get_default_model(db: Session, tenant_id: str) -> ModelConfig:
    model_config = db.exec(
        select(ModelConfig).where(
            ModelConfig.tenant_id == tenant_id,
            ModelConfig.is_default == True,  # noqa: E712
            ModelConfig.enabled == True,  # noqa: E712
        )
    ).first()
    if not model_config:
        raise HTTPException(status_code=400, detail="No enabled default model config")
    return _model_runtime_config(db, tenant_id, model_config)


def _model_runtime_config(db: Session, tenant_id: str, row: ModelConfig):
    return resolve_model_config_for_runtime(db, tenant_id, row.id)


def _get_request_model(
    db: Session, tenant_id: str, model_config_id: str | None = None
) -> ModelConfig:
    if not model_config_id:
        return _get_default_model(db, tenant_id)
    model_config = db.get(ModelConfig, model_config_id)
    if not model_config or model_config.tenant_id != tenant_id or not model_config.enabled:
        raise HTTPException(status_code=404, detail="Model config not found")
    return _model_runtime_config(db, tenant_id, model_config)


def _sync_skill_tool_bindings(
    db: Session,
    tenant_id: str,
    skill_id: str,
    content: dict[str, object],
) -> None:
    tool_names: set[str] = set()
    for key in ("nodes", "steps"):
        items = content.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            actions = item.get("allowed_actions")
            if not isinstance(actions, list):
                continue
            for action in actions:
                value = str(action or "").strip()
                if not value.startswith("call_tool:"):
                    continue
                tool_name = value.split(":", 1)[1].strip()
                if tool_name:
                    tool_names.add(tool_name)
    if not tool_names:
        return

    rows = db.exec(
        select(Tool).where(
            Tool.tenant_id == tenant_id,
            Tool.name.in_(sorted(tool_names)),
        )
    ).all()
    for row in rows:
        allowed_skills = [
            str(item)
            for item in (row.allowed_skills_json or [])
            if str(item).strip()
        ]
        if skill_id in allowed_skills:
            continue
        row.allowed_skills_json = [*allowed_skills, skill_id]
        row.updated_at = utc_now()
        db.add(row)


def _with_available_tools(db: Session, request: SkillDistillRequest) -> SkillDistillRequest:
    tools = db.exec(
        select(Tool).where(Tool.tenant_id == request.tenant_id, Tool.enabled == True)  # noqa: E712
    ).all()
    available_tools = [
        *request.available_tools,
        *[
            {
                "id": tool.id,
                "name": tool.name,
                "display_name": tool.display_name,
                "description": tool.description,
                "bucket": tool.bucket or "未分桶",
                "method": tool.method,
                "url": tool.url,
                "input_schema": tool.input_schema,
                "output_schema": tool.output_schema,
            }
            for tool in tools
        ],
    ]
    return request.model_copy(update={"available_tools": available_tools})


def _with_available_tools_for_rewrite(
    db: Session, request: SkillRewriteRequest
) -> SkillRewriteRequest:
    tools = db.exec(
        select(Tool).where(Tool.tenant_id == request.tenant_id, Tool.enabled == True)  # noqa: E712
    ).all()
    available_tools = [
        *request.available_tools,
        *[
            {
                "id": tool.id,
                "name": tool.name,
                "display_name": tool.display_name,
                "description": tool.description,
                "bucket": tool.bucket or "未分桶",
                "method": tool.method,
                "url": tool.url,
                "input_schema": tool.input_schema,
                "output_schema": tool.output_schema,
            }
            for tool in tools
        ],
    ]
    return request.model_copy(update={"available_tools": available_tools})


def _sse(event: object, data: object) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _extract_uploaded_skill_file(filename: str, data: bytes) -> str:
    suffix = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if suffix in {"md", "txt"}:
        return _decode_text_bytes(data)
    if suffix == "docx":
        return _extract_docx_text(data)
    if suffix == "doc":
        return _decode_legacy_doc_text(data)
    raise HTTPException(status_code=400, detail="Unsupported file type")


def _decode_text_bytes(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="ignore")


def _decode_legacy_doc_text(data: bytes) -> str:
    text = _decode_text_bytes(data)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]+", " ", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _extract_docx_text(data: bytes) -> str:
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            document_xml = archive.read("word/document.xml")
    except (KeyError, zipfile.BadZipFile) as exc:
        raise HTTPException(status_code=400, detail="Invalid docx file") from exc

    namespace = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    try:
        root = ElementTree.fromstring(document_xml)
    except ElementTree.ParseError as exc:
        raise HTTPException(status_code=400, detail="Invalid docx xml") from exc
    paragraphs: list[str] = []
    for paragraph in root.iter(f"{namespace}p"):
        parts: list[str] = []
        for element in paragraph.iter():
            if element.tag == f"{namespace}t" and element.text:
                parts.append(element.text)
            elif element.tag == f"{namespace}tab":
                parts.append("\t")
            elif element.tag in {f"{namespace}br", f"{namespace}cr"}:
                parts.append("\n")
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def _skill_stats(db: Session, tenant_id: str) -> dict[str, dict[str, float | int]]:
    """聚合租户下所有技能的调用次数和反馈统计。

    从 ``AgentEvent``（skill_started/skill_resumed 事件）统计调用次数，
    从 ``SkillFeedback`` 统计点赞/点踩数。统计按 skill_id 和 skill_id@version
    两个维度分别聚合，并计算正/负反馈率。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。

    Returns:
        统计字典，键为 ``skill_id`` 或 ``skill_id@version``，
        值为包含 call_count/positive_feedback_count 等字段的字典。
    """
    stats: dict[str, dict[str, float | int]] = {}
    events = db.exec(
        select(AgentEvent).where(
            AgentEvent.tenant_id == tenant_id,
            AgentEvent.event_type.in_(["skill_started", "skill_resumed"]),  # type: ignore[attr-defined]
        )
    ).all()
    for event in events:
        payload = event.payload_json or {}
        skill_id = str(payload.get("to_skill_id") or "")
        if not skill_id:
            continue
        skill_version = (
            str(payload.get("to_skill_version") or payload.get("skill_version") or "") or None
        )
        _increment_call(stats, skill_id, skill_version)

    feedback_rows = db.exec(select(SkillFeedback).where(SkillFeedback.tenant_id == tenant_id)).all()
    flow_feedback: dict[tuple[str, str | None, str, str], set[str]] = {}
    for feedback in feedback_rows:
        skill_version = feedback.skill_version
        flow_key = (feedback.skill_id, skill_version, feedback.session_id, feedback.user_id)
        flow_feedback.setdefault(flow_key, set()).add(feedback.rating)

    for (skill_id, skill_version, _session_id, _user_id), ratings in flow_feedback.items():
        entries = [stats.setdefault(skill_id, _empty_stats())]
        if skill_version:
            entries.append(stats.setdefault(_stats_key(skill_id, skill_version), _empty_stats()))
        for entry in entries:
            if "down" in ratings:
                entry["negative_feedback_count"] = int(entry["negative_feedback_count"]) + 1
            elif "up" in ratings:
                entry["positive_feedback_count"] = int(entry["positive_feedback_count"]) + 1

    for entry in stats.values():
        positive = int(entry["positive_feedback_count"])
        negative = int(entry["negative_feedback_count"])
        calls = int(entry["call_count"])
        entry["positive_rate"] = round(positive / calls, 4) if calls else 0.0
        entry["negative_rate"] = round(negative / calls, 4) if calls else 0.0
    return stats


def _increment_call(
    stats: dict[str, dict[str, float | int]], skill_id: str, version: str | None
) -> None:
    entries = [stats.setdefault(skill_id, _empty_stats())]
    if version:
        entries.append(stats.setdefault(_stats_key(skill_id, version), _empty_stats()))
    for entry in entries:
        entry["call_count"] = int(entry["call_count"]) + 1


def _stats_key(skill_id: str, version: str) -> str:
    return f"{skill_id}@{version}"


def _stats_for(
    stats: dict[str, dict[str, float | int]], skill_id: str, version: str
) -> dict[str, float | int]:
    return stats.get(_stats_key(skill_id, version), {})


def _recent_skill_stats(
    db: Session,
    tenant_id: str,
    stats: dict[str, dict[str, float | int]],
) -> dict[str, dict[str, object]]:
    recent_versions: dict[str, list[str]] = {}
    version_rows = db.exec(
        select(SkillVersion)
        .where(SkillVersion.tenant_id == tenant_id)
        .order_by(
            SkillVersion.skill_id.asc(), SkillVersion.created_at.desc(), SkillVersion.version.desc()
        )
    ).all()
    for row in version_rows:
        versions = recent_versions.setdefault(row.skill_id, [])
        if len(versions) < 3:
            versions.append(row.version)

    skill_rows = db.exec(select(Skill).where(Skill.tenant_id == tenant_id)).all()
    for row in skill_rows:
        recent_versions.setdefault(row.skill_id, [row.version])

    recent_stats: dict[str, dict[str, object]] = {}
    for skill_id, versions in recent_versions.items():
        entry: dict[str, object] = {
            **_empty_stats(),
            "recent_versions": versions,
        }
        for version in versions:
            version_stats = stats.get(_stats_key(skill_id, version), {})
            entry["call_count"] = int(entry["call_count"]) + int(version_stats.get("call_count", 0))
            entry["positive_feedback_count"] = int(entry["positive_feedback_count"]) + int(
                version_stats.get("positive_feedback_count", 0)
            )
            entry["negative_feedback_count"] = int(entry["negative_feedback_count"]) + int(
                version_stats.get("negative_feedback_count", 0)
            )
        positive = int(entry["positive_feedback_count"])
        negative = int(entry["negative_feedback_count"])
        calls = int(entry["call_count"])
        entry["positive_rate"] = round(positive / calls, 4) if calls else 0.0
        entry["negative_rate"] = round(negative / calls, 4) if calls else 0.0
        recent_stats[skill_id] = entry
    return recent_stats


def _upsert_skill_version(db: Session, row: Skill) -> SkillVersion:
    existing = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == row.tenant_id,
            SkillVersion.skill_id == row.skill_id,
            SkillVersion.version == row.version,
        )
    ).first()
    if existing:
        existing.name = row.name
        existing.business_domain = row.business_domain
        existing.description = row.description
        existing.content_json = row.content_json
        existing.status = row.status
        existing.updated_at = utc_now()
        db.add(existing)
        db.commit()
        db.refresh(existing)
        return existing
    version_row = SkillVersion(
        tenant_id=row.tenant_id,
        skill_id=row.skill_id,
        version=row.version,
        name=row.name,
        business_domain=row.business_domain,
        description=row.description,
        content_json=row.content_json,
        status=row.status,
    )
    db.add(version_row)
    db.commit()
    db.refresh(version_row)
    return version_row


def _empty_stats() -> dict[str, float | int]:
    return {
        "call_count": 0,
        "positive_feedback_count": 0,
        "negative_feedback_count": 0,
        "positive_rate": 0.0,
        "negative_rate": 0.0,
    }


def _get_skill(db: Session, tenant_id: str, skill_id: str) -> Skill:
    ensure_tenant(db, tenant_id)
    row = db.exec(
        select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id == skill_id)
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Skill not found")
    return row


def _get_visible_skill_for_scope(
    db: Session,
    tenant_id: str,
    skill_id: str,
    agent_id: str | None,
) -> Skill:
    row = next(
        (
            item
            for item in visible_skill_rows(db, tenant_id, agent_id, include_inactive=True)
            if item.skill_id == skill_id
        ),
        None,
    )
    if not row:
        raise HTTPException(status_code=404, detail="Skill not found")
    return row


def _get_skill_version(db: Session, tenant_id: str, skill_id: str, version: str) -> SkillVersion:
    ensure_tenant(db, tenant_id)
    row = db.exec(
        select(SkillVersion).where(
            SkillVersion.tenant_id == tenant_id,
            SkillVersion.skill_id == skill_id,
            SkillVersion.version == version,
        )
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Skill version not found")
    return row
