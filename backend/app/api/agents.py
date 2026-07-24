"""Agent 管理 API 模块。

提供 Agent 配置的全生命周期管理接口，包括：

1. **Agent CRUD**：创建（支持从空白/整体/其他 Agent 复制）、查询、更新、删除。
2. **资源绑定管理**：查看/更新 Agent 绑定的技能、知识库、工具等资源。
3. **资源导入**：从其他 Agent 或整体 Agent 导入资源到目标 Agent。
4. **技能分支管理**：技能分支的同步、推送、回滚、版本查询。
5. **模型绑定**：为 Agent 的不同角色配置 LLM 模型。
6. **聊天侧 Agent 列表**：面向聊天界面的 Agent 选择与使用标记。
7. **工作记录**：Agent 的回复统计和资源/任务时间线。

模块包含三个路由器：
- ``enterprise_router``: 企业管理端接口（/api/enterprise/agents）
- ``chat_router``: 聊天侧接口（/api/chat/agents）
- ``scope_router``: Agent 作用域查询接口（/api/enterprise/agent-scope）
"""

from __future__ import annotations

from datetime import UTC, datetime
from time import sleep
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlmodel import Session, select

from app.agents.schema import (
    AgentModelsUpdateRequest,
    AgentProfileCreateRequest,
    AgentProfileRead,
    AgentProfileUpdateRequest,
    AgentResourceBindingInput,
    AgentResourceImportRequest,
    AgentResourceBindingRead,
    AgentResourcesUpdateRequest,
    AgentScopeRead,
    AgentSkillRollbackRequest,
    AgentWorkRecordEventRead,
    AgentWorkRecordRead,
    AgentWorkRecordReplyStatsRead,
)
from app.agents.branching import (
    agent_private_metadata,
    branch_versions,
    copy_overall_scope_to_agent,
    ensure_agent_skill_branch,
    ensure_knowledge_base_version,
    get_overall_agent,
    is_bound_resource_visible_for_agent,
    is_open_gallery_resource,
    promote_branch_to_overall,
    promote_knowledge_branch_to_overall,
    rollback_branch,
    sync_branch_from_overall,
    visible_skill_rows,
)
from app.db import get_session
from app.db.models import (
    AgentModelBinding,
    AgentKnowledgeBranch,
    AgentProfile,
    AgentResourceBinding,
    AgentSkillBranch,
    AgentSkillBranchVersion,
    AgentUsage,
    ChatSession,
    GeneralSkill,
    KnowledgeBase,
    KnowledgeBucket,
    KnowledgeChunk,
    KnowledgeDocument,
    Message,
    ScheduledTask,
    Skill,
    Tool,
    utc_now,
    User,
)
from app.security.auth import get_current_user
from app.security.permissions import agent_owned_by_user as _agent_owned_by_user
from app.security.permissions import is_admin_user as _is_admin_user
from app.security.tenant import ensure_tenant

IMPORT_LOCK_RETRY_ATTEMPTS = 2
IMPORT_LOCK_RETRY_DELAY_SECONDS = 0.5

# 企业管理端路由器，处理 Agent 的 CRUD 和资源管理
enterprise_router = APIRouter(prefix="/api/enterprise/agents", tags=["enterprise:agents"])
# 聊天侧路由器，处理聊天界面中的 Agent 选择和使用
chat_router = APIRouter(prefix="/api/chat/agents", tags=["chat:agents"])
# Agent 作用域路由器，查询用户可见的 Agent 列表
scope_router = APIRouter(prefix="/api/enterprise/agent-scope", tags=["enterprise:agent-scope"])


@scope_router.get("", response_model=AgentScopeRead)
def get_agent_scope(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentScopeRead:
    """获取指定租户下当前用户可见的 Agent 作用域。

    Args:
        tenant_id: 租户ID。
        db: 数据库会话依赖。
        current_user: 当前登录用户。

    Returns:
        AgentScopeRead: 包含可见 Agent 列表的作用域对象。
    """
    ensure_tenant(db, tenant_id)
    _ensure_request_tenant(tenant_id, current_user)
    return AgentScopeRead(tenant_id=tenant_id, agents=list_agents(tenant_id, db, current_user))


@enterprise_router.get("", response_model=list[AgentProfileRead])
def list_agents(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[AgentProfileRead]:
    """获取指定租户下当前用户可见的 Agent 列表。

    非管理员用户只能看到整体 Agent 和自己拥有/已发布的 Agent。

    Args:
        tenant_id: 租户ID。
        db: 数据库会话依赖。
        current_user: 当前登录用户。

    Returns:
        list[AgentProfileRead]: Agent 配置列表，按 is_overall 和更新时间排序。
    """
    ensure_tenant(db, tenant_id)
    user = current_user
    _ensure_request_tenant(tenant_id, user)
    rows = db.exec(
        select(AgentProfile)
        .where(AgentProfile.tenant_id == tenant_id)
        .order_by(AgentProfile.is_overall.desc(), AgentProfile.updated_at.desc())
    ).all()
    rows = [row for row in rows if not _agent_hidden_from_staffdeck(row)]
    if not _is_admin_user(user):
        # Non-admin users still need the overall agent as a read-only open-gallery
        # source for copy/use flows. Mutations remain guarded by manage/update
        # endpoints, so this only exposes the source scope.
        rows = [row for row in rows if row.is_overall or _agent_visible_to_user(row, user)]
    bindings = _bindings_by_agent(db, tenant_id)
    used_agent_ids = _used_agent_ids_for_user(db, tenant_id, user)
    return [agent_read(row, bindings.get(row.id, []), row.id in used_agent_ids) for row in rows]


@enterprise_router.post("", response_model=AgentProfileRead)
def create_agent(
    request: AgentProfileCreateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentProfileRead:
    """创建新的 Agent 配置。

    支持三种创建模式：
    - ``blank``: 创建空白 Agent，不复制任何资源。
    - ``copy`` + ``copy_from_agent_id``: 从指定 Agent 复制资源范围。
    - ``copy``（无 source）: 从整体 Agent 复制公共画廊资源。

    Args:
        request: Agent 创建请求体。
        db: 数据库会话依赖。
        current_user: 当前登录用户。

    Returns:
        AgentProfileRead: 新创建的 Agent 配置。

    Raises:
        HTTPException 403: 非管理员尝试创建整体 Agent。
        HTTPException 400: Agent 名称为空。
        HTTPException 409: Agent 名称已存在。
    """
    ensure_tenant(db, request.tenant_id)
    user = current_user
    _ensure_request_tenant(request.tenant_id, user)
    if request.is_overall and not _is_admin_user(user):
        raise HTTPException(status_code=403, detail="Only administrator can create overall agent")
    name = str(request.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Agent name cannot be empty")
    existing = db.exec(
        select(AgentProfile).where(
            AgentProfile.tenant_id == request.tenant_id, AgentProfile.name == name
        )
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Agent name already exists")
    row = AgentProfile(
        tenant_id=request.tenant_id,
        name=name,
        description=request.description,
        persona_prompt=request.persona_prompt,
        is_overall=request.is_overall,
        status="active",
        metadata_json=_metadata_with_creator(request.metadata or {}, user),
    )
    db.add(row)
    db.flush()
    if not row.is_overall:
        copy_from_agent_id = request.copy_from_agent_id
        if request.source_mode == "blank":
            pass
        elif copy_from_agent_id:
            source_agent = _get_agent(db, request.tenant_id, copy_from_agent_id)
            _ensure_can_copy_from_agent(source_agent, user)
            if not row.persona_prompt:
                row.persona_prompt = source_agent.persona_prompt
            _copy_agent_scope_from_source(db, request.tenant_id, source_agent, row)
        else:
            overall = get_overall_agent(db, request.tenant_id)
            if overall and not row.persona_prompt:
                row.persona_prompt = overall.persona_prompt
            copy_overall_scope_to_agent(db, request.tenant_id, row)
            if overall:
                _copy_agent_models_from_source(db, request.tenant_id, overall, row)
    db.commit()
    db.refresh(row)
    return agent_read(row, _bindings_by_agent(db, request.tenant_id).get(row.id, []))


@enterprise_router.get("/{agent_id}", response_model=AgentProfileRead)
def get_agent(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentProfileRead:
    """获取指定 Agent 的详细信息。

    Args:
        agent_id: Agent ID。
        tenant_id: 租户ID。
        db: 数据库会话依赖。
        current_user: 当前登录用户。

    Returns:
        AgentProfileRead: Agent 配置详情。

    Raises:
        HTTPException 404: Agent 不存在。
        HTTPException 403: 当前用户无权访问该 Agent。
    """
    row = _get_agent(db, tenant_id, agent_id)
    _ensure_can_access_agent(row, current_user)
    return agent_read(row, _bindings_by_agent(db, tenant_id).get(row.id, []))


@enterprise_router.get("/{agent_id}/work-record", response_model=AgentWorkRecordRead)
def get_agent_work_record(
    agent_id: str,
    tenant_id: str = Query(...),
    timezone: str = Query("Asia/Shanghai"),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentWorkRecordRead:
    """获取 Agent 的工作记录，包含回复统计和时间线事件。

    汇总 Agent 的对话回复、资源绑定、定时任务等事件，生成时间线视图。
    回复统计按日期分组，时区可自定义。

    Args:
        agent_id: Agent ID。
        tenant_id: 租户ID。
        timezone: 时区标识符（如 Asia/Shanghai）。
        db: 数据库会话依赖。
        current_user: 当前登录用户。

    Returns:
        AgentWorkRecordRead: 工作记录对象。

    Raises:
        HTTPException 400: 时区无效。
        HTTPException 404: Agent 不存在。
    """
    agent = _get_agent(db, tenant_id, agent_id)
    _ensure_can_access_agent(agent, current_user)
    try:
        local_timezone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid timezone") from exc

    now = utc_now()
    reply_rows = db.exec(
        select(Message)
        .join(ChatSession, Message.session_id == ChatSession.id)
        .where(
            Message.tenant_id == tenant_id,
            Message.role == "assistant",
            ChatSession.tenant_id == tenant_id,
            ChatSession.agent_id == agent_id,
            ChatSession.user_id == current_user.id,
        )
        .order_by(Message.created_at.asc())
    ).all()
    by_day: dict[str, int] = {}
    events = [
        AgentWorkRecordEventRead(
            id=f"{message.id}:reply",
            kind="chat",
            phase="reply",
            timestamp=_iso_utc(message.created_at),
            label="对话回复",
        )
        for message in reply_rows
    ]
    for message in reply_rows:
        day = _as_utc(message.created_at).astimezone(local_timezone).date().isoformat()
        by_day[day] = by_day.get(day, 0) + 1

    events.extend(_agent_resource_timeline_events(db, tenant_id, agent_id))
    events.extend(_agent_scheduled_task_timeline_events(db, tenant_id, agent_id, current_user))
    events.sort(key=lambda item: (item.timestamp, item.id))
    today = _as_utc(now).astimezone(local_timezone).date().isoformat()
    return AgentWorkRecordRead(
        agent_id=agent_id,
        timezone=timezone,
        generated_at=_iso_utc(now),
        reply_stats=AgentWorkRecordReplyStatsRead(
            total=len(reply_rows),
            today=by_day.get(today, 0),
            by_day=dict(sorted(by_day.items())),
        ),
        events=events,
    )


@enterprise_router.put("/{agent_id}", response_model=AgentProfileRead)
def update_agent(
    agent_id: str,
    request: AgentProfileUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> AgentProfileRead:
    """更新 Agent 配置信息。

    支持更新名称、描述、人设提示词、状态和元数据。
    更新元数据时保留原始创建者信息。

    Args:
        agent_id: Agent ID。
        request: 更新请求体。
        db: 数据库会话依赖。
        current_user: 当前登录用户。

    Returns:
        AgentProfileRead: 更新后的 Agent 配置。

    Raises:
        HTTPException 403: 当前用户无权管理该 Agent。
        HTTPException 400: 名称为空。
        HTTPException 409: 名称已存在。
    """
    row = _get_agent(db, request.tenant_id, agent_id)
    user = current_user
    _ensure_can_manage_agent(row, user)
    if request.name is not None:
        name = request.name.strip()
        if not name:
            raise HTTPException(status_code=400, detail="Agent name cannot be empty")
        conflict = db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == request.tenant_id,
                AgentProfile.name == name,
                AgentProfile.id != row.id,
            )
        ).first()
        if conflict:
            raise HTTPException(status_code=409, detail="Agent name already exists")
        row.name = name
    if request.description is not None:
        row.description = request.description
    if request.persona_prompt is not None:
        row.persona_prompt = request.persona_prompt
    if request.status is not None:
        row.status = request.status
    if request.metadata is not None:
        row.metadata_json = _metadata_preserving_creator(
            row.metadata_json or {}, request.metadata, user
        )
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return agent_read(row, _bindings_by_agent(db, request.tenant_id).get(row.id, []))


@enterprise_router.delete("/{agent_id}")
def delete_agent(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    """删除指定 Agent 及其所有资源绑定。

    不允许删除整体 Agent。

    Args:
        agent_id: Agent ID。
        tenant_id: 租户ID。
        db: 数据库会话依赖。
        current_user: 当前登录用户。

    Returns:
        dict[str, str]: 操作结果 ``{"status": "deleted"}``。

    Raises:
        HTTPException 400: 尝试删除整体 Agent。
        HTTPException 403: 当前用户无权管理该 Agent。
    """
    row = _get_agent(db, tenant_id, agent_id)
    _ensure_can_manage_agent(row, current_user)
    if row.is_overall:
        raise HTTPException(status_code=400, detail="Overall agent cannot be deleted")
    bindings = db.exec(
        select(AgentResourceBinding).where(AgentResourceBinding.agent_id == row.id)
    ).all()
    for binding in bindings:
        db.delete(binding)
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@enterprise_router.get("/{agent_id}/resources", response_model=list[AgentResourceBindingRead])
def get_agent_resources(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[AgentResourceBindingRead]:
    """获取 Agent 绑定的所有资源列表。

    Args:
        agent_id: Agent ID。
        tenant_id: 租户ID。
        db: 数据库会话依赖。
        current_user: 当前登录用户。

    Returns:
        list[AgentResourceBindingRead]: 资源绑定列表，按类型和创建时间排序。
    """
    _ensure_can_access_agent(_get_agent(db, tenant_id, agent_id), current_user)
    rows = db.exec(
        select(AgentResourceBinding)
        .where(
            AgentResourceBinding.tenant_id == tenant_id, AgentResourceBinding.agent_id == agent_id
        )
        .order_by(AgentResourceBinding.resource_type, AgentResourceBinding.created_at)
    ).all()
    return [binding_read(row) for row in rows]


@enterprise_router.put("/{agent_id}/resources", response_model=list[AgentResourceBindingRead])
def update_agent_resources(
    agent_id: str,
    request: AgentResourcesUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[AgentResourceBindingRead]:
    """全量更新 Agent 的资源绑定（替换式）。

    传入完整的资源列表，不在列表中的现有绑定将被删除。

    Args:
        agent_id: Agent ID。
        request: 包含资源绑定列表的请求体。
        db: 数据库会话依赖。
        current_user: 当前登录用户。

    Returns:
        list[AgentResourceBindingRead]: 更新后的资源绑定列表。

    Raises:
        HTTPException 400: 目标为整体 Agent（不支持直接修改）。
        HTTPException 403: 当前用户无权管理该 Agent。
        HTTPException 404: 资源不存在。
    """
    agent = _get_agent(db, request.tenant_id, agent_id)
    _ensure_can_manage_agent(agent, current_user)
    if agent.is_overall:
        raise HTTPException(status_code=400, detail="Overall agent uses the global resource pool")
    existing = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == request.tenant_id,
            AgentResourceBinding.agent_id == agent_id,
        )
    ).all()
    by_key = {(row.resource_type, row.resource_id): row for row in existing}
    desired_keys: set[tuple[str, str]] = set()
    for item in request.resources:
        _ensure_resource_exists(db, request.tenant_id, item)
        key = (item.resource_type, item.resource_id)
        desired_keys.add(key)
        row = by_key.get(key)
        if row:
            row.status = item.status
            row.metadata_json = item.metadata
            row.updated_at = utc_now()
        else:
            row = AgentResourceBinding(
                tenant_id=request.tenant_id,
                agent_id=agent_id,
                resource_type=item.resource_type,
                resource_id=item.resource_id,
                status=item.status,
                metadata_json=item.metadata,
            )
        db.add(row)
    for key, row in by_key.items():
        if key not in desired_keys:
            db.delete(row)
    db.commit()
    return get_agent_resources(agent_id, request.tenant_id, db, current_user)


@enterprise_router.post("/{agent_id}/resources/import")
def import_agent_resources(
    agent_id: str,
    request: AgentResourceImportRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    """从源 Agent 导入资源到目标 Agent（支持数据库锁重试）。

    当遇到数据库锁冲突时，自动重试最多 IMPORT_LOCK_RETRY_ATTEMPTS 次。

    Args:
        agent_id: 目标 Agent ID。
        request: 资源导入请求体。
        db: 数据库会话依赖。
        current_user: 当前登录用户。

    Returns:
        dict[str, object]: 包含导入结果（imported/missing 列表）。

    Raises:
        HTTPException 503: 资源导入繁忙（重试后仍失败）。
    """
    for attempt in range(IMPORT_LOCK_RETRY_ATTEMPTS):
        try:
            return _import_agent_resources_once(agent_id, request, db, current_user)
        except OperationalError as exc:
            db.rollback()
            if not _is_database_locked_error(exc) or attempt >= IMPORT_LOCK_RETRY_ATTEMPTS - 1:
                raise
            sleep(IMPORT_LOCK_RETRY_DELAY_SECONDS * (attempt + 1))
    raise HTTPException(status_code=503, detail="Resource import is temporarily busy")


def _import_agent_resources_once(
    agent_id: str,
    request: AgentResourceImportRequest,
    db: Session,
    current_user: User | object,
) -> dict[str, object]:
    """执行一次资源导入操作（不含重试逻辑）。

    逐个解析资源 ID，检查源 Agent 可见性和学习限制，
    将符合条件的资源绑定到目标 Agent（含分支复制）。

    Args:
        agent_id: 目标 Agent ID。
        request: 资源导入请求体。
        db: 数据库会话依赖。
        current_user: 当前登录用户。

    Returns:
        dict[str, object]: 包含 imported 和 missing 列表的导入结果。
    """
    target_agent = _get_agent(db, request.tenant_id, agent_id)
    source_agent = _get_agent(db, request.tenant_id, request.source_agent_id)
    user = current_user
    _ensure_can_import_to_agent(target_agent, user)
    _ensure_can_copy_from_agent(source_agent, user)
    if source_agent.id == target_agent.id:
        raise HTTPException(status_code=400, detail="Source and target agent cannot be the same")
    resource_ids = _dedupe_ids(request.resource_ids)
    if not resource_ids:
        raise HTTPException(status_code=400, detail="No resources selected")
    imported: list[dict[str, object]] = []
    missing: list[dict[str, str]] = []
    for identifier in resource_ids:
        resolved = _resolve_resource(db, request.tenant_id, request.resource_type, identifier)
        if not resolved:
            missing.append({"resource_id": identifier, "reason": "resource_not_found"})
            continue
        source_binding = _source_resource_binding(
            db, request.tenant_id, source_agent, request.resource_type, resolved.id
        )
        if not source_agent.is_overall and not source_binding:
            missing.append({"resource_id": identifier, "reason": "not_visible_in_source_agent"})
            continue
        block_reason = _blocked_learning_reason(
            db,
            request.tenant_id,
            source_agent,
            request.resource_type,
            resolved,
            source_binding,
        )
        if block_reason:
            missing.append({"resource_id": identifier, "reason": block_reason})
            continue
        if target_agent.is_overall:
            _import_resource_to_overall(
                db, request.tenant_id, source_agent, request.resource_type, resolved
            )
        else:
            _upsert_imported_resource_binding(
                db,
                request.tenant_id,
                source_agent,
                target_agent,
                request.resource_type,
                resolved,
                source_binding,
            )
        imported.append(
            {
                "resource_type": request.resource_type,
                "resource_id": resolved.id,
                "display_id": _resource_display_id(request.resource_type, resolved),
                "name": getattr(resolved, "name", getattr(resolved, "slug", resolved.id)),
            }
        )
    db.commit()
    return {
        "status": "imported",
        "target_agent_id": target_agent.id,
        "source_agent_id": source_agent.id,
        "imported": imported,
        "missing": missing,
    }


@enterprise_router.get("/{agent_id}/skills")
def get_agent_skills(
    agent_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[dict[str, object]]:
    """获取 Agent 的技能列表（含分支元数据）。

    Args:
        agent_id: Agent ID。
        tenant_id: 租户ID。
        db: 数据库会话依赖。
        current_user: 当前登录用户。

    Returns:
        list[dict[str, object]]: 技能列表，每个技能包含分支状态信息。
    """
    _ensure_can_access_agent(_get_agent(db, tenant_id, agent_id), current_user)
    return [
        _skill_branch_read(skill)
        for skill in visible_skill_rows(db, tenant_id, agent_id, include_inactive=True)
    ]


@enterprise_router.post("/{agent_id}/skills/{skill_id}/sync-from-overall")
def sync_agent_skill_from_overall(
    agent_id: str,
    skill_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    """将 Agent 技能分支从整体版本同步。

    用整体版本的最新内容覆盖 Agent 的分支。

    Args:
        agent_id: Agent ID。
        skill_id: 技能标识符。
        tenant_id: 租户ID。
        db: 数据库会话依赖。
        current_user: 当前登录用户。

    Returns:
        dict[str, object]: 同步结果，包含 skill_id 和 head_version。

    Raises:
        HTTPException 400: Agent 为整体 Agent 或技能未发布。
    """
    agent = _get_agent(db, tenant_id, agent_id)
    _ensure_can_manage_agent(agent, current_user)
    if agent.is_overall:
        raise HTTPException(status_code=400, detail="Overall agent is already the trunk")
    skill = _get_global_skill(db, tenant_id, skill_id)
    if skill.status != "published":
        raise HTTPException(
            status_code=400, detail="Disabled SOP cannot be learned from the open gallery"
        )
    branch = sync_branch_from_overall(db, tenant_id, agent_id, skill)
    db.commit()
    return {"status": "synced", "skill_id": skill_id, "head_version": branch.head_version}


@enterprise_router.post("/{agent_id}/skills/{skill_id}/promote-to-overall")
def promote_agent_skill_to_overall(
    agent_id: str,
    skill_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    """将 Agent 技能分支推送到整体版本（仅管理员）。

    将分支内容提升为全局技能的新版本。

    Args:
        agent_id: Agent ID。
        skill_id: 技能标识符。
        tenant_id: 租户ID。
        db: 数据库会话依赖。
        current_user: 当前登录用户。

    Returns:
        dict[str, object]: 推送结果，包含 skill_id 和 version。

    Raises:
        HTTPException 403: 当前用户不是管理员。
        HTTPException 400: Agent 为整体 Agent。
        HTTPException 404: 分支不存在。
    """
    _ensure_admin_user(tenant_id, current_user)
    agent = _get_agent(db, tenant_id, agent_id)
    if agent.is_overall:
        raise HTTPException(
            status_code=400, detail="Overall agent does not have a branch to promote"
        )
    branch = db.exec(
        select(AgentSkillBranch).where(
            AgentSkillBranch.tenant_id == tenant_id,
            AgentSkillBranch.agent_id == agent_id,
            AgentSkillBranch.skill_id == skill_id,
        )
    ).first()
    if not branch:
        raise HTTPException(status_code=404, detail="Branch not found")
    skill = promote_branch_to_overall(db, tenant_id, branch)
    db.commit()
    return {"status": "promoted", "skill_id": skill_id, "version": skill.version}


@enterprise_router.post("/{agent_id}/skills/{skill_id}/rollback")
def rollback_agent_skill(
    agent_id: str,
    skill_id: str,
    request: AgentSkillRollbackRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    """将 Agent 技能分支回滚到指定版本。

    Args:
        agent_id: Agent ID。
        skill_id: 技能标识符。
        request: 回滚请求体（包含目标版本号）。
        db: 数据库会话依赖。
        current_user: 当前登录用户。

    Returns:
        dict[str, object]: 回滚结果，包含 skill_id 和 head_version。

    Raises:
        HTTPException 400: Agent 为整体 Agent。
        HTTPException 403: 当前用户无权管理该 Agent。
    """
    agent = _get_agent(db, request.tenant_id, agent_id)
    _ensure_can_manage_agent(agent, current_user)
    if agent.is_overall:
        raise HTTPException(
            status_code=400, detail="Use the global skill rollback endpoint for overall agent"
        )
    branch = rollback_branch(db, request.tenant_id, agent_id, skill_id, request.version)
    db.commit()
    return {"status": "rolled_back", "skill_id": skill_id, "head_version": branch.head_version}


@enterprise_router.get("/{agent_id}/skills/{skill_id}/versions")
def list_agent_skill_versions(
    agent_id: str,
    skill_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> list[dict[str, object]]:
    """获取 Agent 技能分支的所有历史版本列表。

    Args:
        agent_id: Agent ID。
        skill_id: 技能标识符。
        tenant_id: 租户ID。
        db: 数据库会话依赖。
        current_user: 当前登录用户。

    Returns:
        list[dict[str, object]]: 版本记录列表，每条记录包含版本号、内容、变更摘要等。
    """
    _ensure_can_access_agent(_get_agent(db, tenant_id, agent_id), current_user)
    return [
        {
            "id": row.id,
            "tenant_id": row.tenant_id,
            "agent_id": row.agent_id,
            "skill_id": row.skill_id,
            "version": row.version,
            "base_version": row.base_version,
            "sync_state": row.sync_state,
            "status": row.status,
            "content": row.content_json,
            "change_summary": row.change_summary,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
        }
        for row in branch_versions(db, tenant_id, agent_id, skill_id)
    ]


@enterprise_router.put("/{agent_id}/models")
def update_agent_models(
    agent_id: str,
    request: AgentModelsUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> dict[str, object]:
    """更新 Agent 的模型绑定配置。

    为 Agent 的不同角色（default/router/step/response/general_skill）配置 LLM 模型。
    同角色的现有绑定会被更新，不存在则创建。

    Args:
        agent_id: Agent ID。
        request: 模型绑定更新请求体。
        db: 数据库会话依赖。
        current_user: 当前登录用户。

    Returns:
        dict[str, object]: 操作结果 ``{"status": "updated", "agent_id": ...}``。

    Raises:
        HTTPException 403: 当前用户无权管理该 Agent。
    """
    _ensure_can_manage_agent(_get_agent(db, request.tenant_id, agent_id), current_user)
    for item in request.bindings:
        existing = db.exec(
            select(AgentModelBinding).where(
                AgentModelBinding.tenant_id == request.tenant_id,
                AgentModelBinding.agent_id == agent_id,
                AgentModelBinding.role == item.role,
            )
        ).first()
        if existing:
            existing.model_config_id = item.model_config_id
            existing.updated_at = utc_now()
            db.add(existing)
            continue
        db.add(
            AgentModelBinding(
                tenant_id=request.tenant_id,
                agent_id=agent_id,
                role=item.role,
                model_config_id=item.model_config_id,
            )
        )
    db.commit()
    return {"status": "updated", "agent_id": agent_id}


@chat_router.get("", response_model=list[AgentProfileRead])
def list_chat_agents(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> list[AgentProfileRead]:
    """获取聊天界面中可选的 Agent 列表。

    仅返回非整体、活跃状态的 Agent。筛选逻辑：
    用户拥有的 Agent 或已发布到画廊且用户已使用的 Agent。

    Args:
        tenant_id: 租户ID。
        current_user: 当前登录用户。
        db: 数据库会话依赖。

    Returns:
        list[AgentProfileRead]: 可选 Agent 列表。

    Raises:
        HTTPException 403: 租户不匹配。
    """
    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")
    ensure_tenant(db, tenant_id)
    rows = db.exec(
        select(AgentProfile)
        .where(
            AgentProfile.tenant_id == tenant_id,
            AgentProfile.status == "active",
            AgentProfile.is_overall == False,  # noqa: E712
        )
        .order_by(AgentProfile.updated_at.desc())
    ).all()
    rows = [row for row in rows if not _agent_hidden_from_staffdeck(row)]
    used_agent_ids = _used_agent_ids_for_user(db, tenant_id, current_user)
    rows = [
        row for row in rows if _chat_agent_selectable_to_user(row, current_user, used_agent_ids)
    ]
    bindings = _bindings_by_agent(db, tenant_id)
    return [agent_read(row, bindings.get(row.id, []), row.id in used_agent_ids) for row in rows]


@chat_router.post("/{agent_id}/use", response_model=AgentProfileRead)
def use_chat_agent(
    agent_id: str,
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_session),
) -> AgentProfileRead:
    """标记用户使用了指定 Agent（用于聊天界面选择 Agent）。

    记录用户-Agent 的使用关系，用于后续列表筛选。

    Args:
        agent_id: Agent ID。
        tenant_id: 租户ID。
        current_user: 当前登录用户。
        db: 数据库会话依赖。

    Returns:
        AgentProfileRead: Agent 配置信息。

    Raises:
        HTTPException 403: 租户不匹配或无权使用该 Agent。
    """
    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")
    ensure_tenant(db, tenant_id)
    row = _get_agent(db, tenant_id, agent_id)
    if (
        row.is_overall
        or row.status != "active"
        or not _chat_agent_visible_to_user(row, current_user)
    ):
        raise HTTPException(status_code=403, detail="Cannot access this agent")
    _mark_agent_used(db, tenant_id, current_user, row.id)
    bindings = _bindings_by_agent(db, tenant_id)
    return agent_read(row, bindings.get(row.id, []), True)


def _agent_resource_timeline_events(
    db: Session,
    tenant_id: str,
    agent_id: str,
) -> list[AgentWorkRecordEventRead]:
    """构建 Agent 资源绑定的时间线事件列表。

    遍历 Agent 的活跃绑定（技能/通用技能/知识库/工具），
    为每条绑定生成一条 assigned 阶段的事件。

    Args:
        db: 数据库会话依赖。
        tenant_id: 租户ID。
        agent_id: Agent ID。

    Returns:
        list[AgentWorkRecordEventRead]: 资源绑定事件列表。
    """
    bindings = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent_id,
            AgentResourceBinding.status == "active",
        )
    ).all()
    ids_by_type = {
        resource_type: {
            binding.resource_id
            for binding in bindings
            if binding.resource_type == resource_type
        }
        for resource_type in ("skill", "general_skill", "knowledge_base", "tool")
    }
    skills = {
        row.id: row
        for row in db.exec(
            select(Skill).where(
                Skill.tenant_id == tenant_id,
                Skill.id.in_(ids_by_type["skill"]),
            )
        ).all()
    } if ids_by_type["skill"] else {}
    general_skills = {
        row.id: row
        for row in db.exec(
            select(GeneralSkill).where(
                GeneralSkill.tenant_id == tenant_id,
                GeneralSkill.id.in_(ids_by_type["general_skill"]),
            )
        ).all()
    } if ids_by_type["general_skill"] else {}
    knowledge_bases = {
        row.id: row
        for row in db.exec(
            select(KnowledgeBase).where(
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.id.in_(ids_by_type["knowledge_base"]),
            )
        ).all()
    } if ids_by_type["knowledge_base"] else {}
    tools = {
        row.id: row
        for row in db.exec(
            select(Tool).where(
                Tool.tenant_id == tenant_id,
                Tool.id.in_(ids_by_type["tool"]),
            )
        ).all()
    } if ids_by_type["tool"] else {}
    skill_branches = {
        row.skill_id: row
        for row in db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == tenant_id,
                AgentSkillBranch.agent_id == agent_id,
            )
        ).all()
    }
    knowledge_branches = {
        row.knowledge_base_id: row
        for row in db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == agent_id,
            )
        ).all()
    }

    events: list[AgentWorkRecordEventRead] = []
    for binding in bindings:
        kind: str
        label: str
        if binding.resource_type == "skill":
            resource = skills.get(binding.resource_id)
            branch = skill_branches.get(resource.skill_id) if resource else None
            if not resource or resource.status != "published" or (branch and branch.status != "active"):
                continue
            kind, label = "sop", resource.name
        elif binding.resource_type == "general_skill":
            resource = general_skills.get(binding.resource_id)
            if not resource or resource.status != "published":
                continue
            kind, label = "skill", resource.name
        elif binding.resource_type == "knowledge_base":
            resource = knowledge_bases.get(binding.resource_id)
            branch = knowledge_branches.get(binding.resource_id)
            if not resource or resource.status != "active" or (branch and branch.status != "active"):
                continue
            kind, label = "knowledge", resource.name
        elif binding.resource_type == "tool":
            resource = tools.get(binding.resource_id)
            if not resource or not resource.enabled:
                continue
            kind, label = "tool", resource.display_name or resource.name
        else:
            continue
        events.append(
            AgentWorkRecordEventRead(
                id=f"{binding.id}:assigned",
                kind=kind,  # type: ignore[arg-type]
                phase="assigned",
                timestamp=_iso_utc(binding.created_at),
                label=label,
            )
        )
    return events


def _agent_scheduled_task_timeline_events(
    db: Session,
    tenant_id: str,
    agent_id: str,
    current_user: User,
) -> list[AgentWorkRecordEventRead]:
    """构建 Agent 定时任务的时间线事件列表。

    为每条定时任务生成 last_run 和 next_run 事件（如果存在对应时间戳）。

    Args:
        db: 数据库会话依赖。
        tenant_id: 租户ID。
        agent_id: Agent ID。
        current_user: 当前登录用户（非管理员只能看自己的任务）。

    Returns:
        list[AgentWorkRecordEventRead]: 定时任务事件列表。
    """
    conditions = [
        ScheduledTask.tenant_id == tenant_id,
        ScheduledTask.agent_id == agent_id,
        ScheduledTask.status != "archived",
    ]
    if not _is_admin_user(current_user):
        conditions.append(ScheduledTask.created_by_user_id == current_user.id)
    tasks = db.exec(select(ScheduledTask).where(*conditions)).all()
    events: list[AgentWorkRecordEventRead] = []
    for task in tasks:
        for phase, timestamp in (("last_run", task.last_run_at), ("next_run", task.next_run_at)):
            if not timestamp:
                continue
            events.append(
                AgentWorkRecordEventRead(
                    id=f"{task.id}:{phase}",
                    kind="task",
                    phase=phase,  # type: ignore[arg-type]
                    timestamp=_iso_utc(timestamp),
                    label=task.title,
                )
            )
    return events


def _as_utc(value: datetime) -> datetime:
    """将 datetime 转换为 UTC 时区（如果无时区信息则视为 UTC）。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    """将 datetime 格式化为 ISO 8601 UTC 字符串（以 Z 结尾）。"""
    return _as_utc(value).isoformat().replace("+00:00", "Z")


def agent_read(
    row: AgentProfile,
    bindings: list[AgentResourceBinding],
    used_by_current_user: bool | None = None,
) -> AgentProfileRead:
    """将数据库 AgentProfile 对象转换为 API 响应模型。

    Args:
        row: 数据库 Agent 配置对象。
        bindings: 该 Agent 的资源绑定列表。
        used_by_current_user: 当前用户是否已使用该 Agent（可选，写入元数据）。

    Returns:
        AgentProfileRead: Agent 配置读取模型。
    """
    metadata = dict(row.metadata_json or {})
    if used_by_current_user is not None:
        metadata["used_by_current_user"] = used_by_current_user
        metadata["chat_used_by_current_user"] = used_by_current_user
    return AgentProfileRead(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        description=row.description,
        persona_prompt=row.persona_prompt,
        is_overall=row.is_overall,
        status=row.status,
        metadata=metadata,
        resources=[binding_read(binding) for binding in bindings],
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _is_database_locked_error(exc: OperationalError) -> bool:
    """检查异常是否为 SQLite 数据库锁错误。"""
    return "database is locked" in str(exc).lower()


def _ensure_request_tenant(tenant_id: str, user: User) -> None:
    """验证请求的租户 ID 与当前用户的租户 ID 匹配。"""
    if user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")


def _agent_visible_to_user(row: AgentProfile, user: User) -> bool:
    """判断 Agent 是否对指定用户可见。

    可见条件：未被隐藏 + 满足以下之一：管理员、整体 Agent、用户拥有、已发布到画廊。

    Args:
        row: Agent 配置对象。
        user: 当前用户。

    Returns:
        True 表示 Agent 对该用户可见。
    """
    if _agent_hidden_from_staffdeck(row):
        return False
    if _is_admin_user(user):
        return True
    if row.is_overall:
        return True
    metadata = row.metadata_json or {}
    return _agent_owned_by_user(row, user) or metadata.get("published_to_gallery") is True


def _agent_hidden_from_staffdeck(row: AgentProfile) -> bool:
    """检查 Agent 是否被标记为从 StaffDeck 界面隐藏。"""
    return (row.metadata_json or {}).get("hidden_from_staffdeck") is True


def _agent_published_to_gallery(row: AgentProfile) -> bool:
    """检查 Agent 是否已发布到公共画廊。"""
    return (row.metadata_json or {}).get("published_to_gallery") is True


def _used_agent_ids_for_user(db: Session, tenant_id: str, user: User) -> set[str]:
    """获取用户已使用的 Agent ID 集合（来自 AgentUsage 记录和 ChatSession 记录）。"""
    usage_rows = db.exec(
        select(AgentUsage.agent_id).where(
            AgentUsage.tenant_id == tenant_id,
            AgentUsage.user_id == user.id,
            AgentUsage.agent_id != None,  # noqa: E711
        )
    ).all()
    session_rows = db.exec(
        select(ChatSession.agent_id).where(
            ChatSession.tenant_id == tenant_id,
            ChatSession.user_id == user.id,
            ChatSession.agent_id != None,  # noqa: E711
        )
    ).all()
    return {str(agent_id) for agent_id in [*usage_rows, *session_rows] if agent_id}


def _mark_agent_used(db: Session, tenant_id: str, user: User, agent_id: str) -> AgentUsage:
    """记录用户使用了某个 Agent（创建或更新使用记录）。

    处理并发场景下的唯一约束冲突（IntegrityError），冲突时回退查询现有记录。

    Args:
        db: 数据库会话依赖。
        tenant_id: 租户ID。
        user: 当前用户。
        agent_id: Agent ID。

    Returns:
        AgentUsage 使用记录对象。
    """
    row = db.exec(
        select(AgentUsage).where(
            AgentUsage.tenant_id == tenant_id,
            AgentUsage.user_id == user.id,
            AgentUsage.agent_id == agent_id,
        )
    ).first()
    if row:
        row.updated_at = utc_now()
    else:
        row = AgentUsage(tenant_id=tenant_id, user_id=user.id, agent_id=agent_id)
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        row = db.exec(
            select(AgentUsage).where(
                AgentUsage.tenant_id == tenant_id,
                AgentUsage.user_id == user.id,
                AgentUsage.agent_id == agent_id,
            )
        ).first()
        if not row:
            raise
    db.refresh(row)
    return row


def _chat_agent_selectable_to_user(row: AgentProfile, user: User, used_agent_ids: set[str]) -> bool:
    """判断 Agent 在聊天界面是否可被用户选择。

    不可选条件：整体 Agent。可选条件：用户拥有、已发布到画廊且用户已使用、或用户是管理员。

    Args:
        row: Agent 配置对象。
        user: 当前用户。
        used_agent_ids: 用户已使用的 Agent ID 集合。

    Returns:
        True 表示该 Agent 在聊天界面可被选择。
    """
    if row.is_overall:
        return False
    if _agent_owned_by_user(row, user):
        return True
    if _agent_published_to_gallery(row):
        return row.id in used_agent_ids
    return _is_admin_user(user)


def _ensure_can_access_agent(row: AgentProfile, user: User) -> None:
    """验证用户是否有权访问指定 Agent。"""
    _ensure_request_tenant(row.tenant_id, user)
    if not _agent_visible_to_user(row, user):
        raise HTTPException(status_code=403, detail="Cannot access this agent")


def _ensure_can_copy_from_agent(row: AgentProfile, user: User) -> None:
    """验证用户是否有权从指定 Agent 复制资源（整体 Agent 或可见 Agent）。"""
    _ensure_request_tenant(row.tenant_id, user)
    if row.is_overall or _agent_visible_to_user(row, user):
        return
    raise HTTPException(status_code=403, detail="Cannot copy resources from this agent")


def _ensure_can_manage_agent(row: AgentProfile, user: User) -> None:
    """验证用户是否有权管理（修改/删除）指定 Agent。

    管理权限：管理员、或 Agent 的创建者。

    Raises:
        HTTPException 403: 无管理权限。
    """
    _ensure_request_tenant(row.tenant_id, user)
    if _is_admin_user(user):
        return
    if row.is_overall:
        raise HTTPException(status_code=403, detail="Only administrator can manage overall agent")
    if _agent_owned_by_user(row, user):
        return
    raise HTTPException(
        status_code=403, detail="Only the creator or administrator can manage this staff"
    )


def _ensure_can_import_to_agent(row: AgentProfile, user: User) -> None:
    """验证用户是否有权向目标 Agent 导入资源（整体 Agent 需管理员，其他需管理权限）。"""
    if row.is_overall:
        _ensure_admin_user(row.tenant_id, user)
        return
    _ensure_can_manage_agent(row, user)


def _ensure_admin_user(tenant_id: str, user: User) -> None:
    """验证当前用户是否为指定租户的管理员。"""
    _ensure_request_tenant(tenant_id, user)
    if not _is_admin_user(user):
        raise HTTPException(
            status_code=403, detail="Only administrator can update the open gallery"
        )


def _metadata_with_creator(metadata: dict[str, object], user: User) -> dict[str, object]:
    """在元数据中添加当前用户的创建者信息。"""
    normalized = dict(metadata or {})
    display_name = user.display_name or user.username
    normalized["owner_user_id"] = user.id
    normalized["owner_username"] = user.username
    normalized["owner_display_name"] = display_name
    normalized["created_by_user_id"] = user.id
    normalized["created_by_username"] = user.username
    normalized["created_by"] = user.username
    normalized["created_by_display_name"] = display_name
    normalized["creator_name"] = user.username
    return normalized


def _metadata_preserving_creator(
    existing_metadata: dict[str, object],
    next_metadata: dict[str, object],
    user: User,
) -> dict[str, object]:
    """更新元数据但保留原始创建者字段（防止编辑操作覆盖创建者信息）。"""
    normalized = dict(next_metadata or {})
    for key in (
        "owner_user_id",
        "owner_username",
        "owner_display_name",
        "created_by_user_id",
        "created_by_username",
        "created_by",
        "created_by_display_name",
        "creator_name",
    ):
        existing_value = existing_metadata.get(key)
        if isinstance(existing_value, str) and existing_value.strip():
            normalized[key] = existing_value
    return normalized


def _chat_agent_visible_to_user(row: AgentProfile, user: User) -> bool:
    """判断 Agent 在聊天侧是否对用户可见（委托给 _agent_visible_to_user）。"""
    return _agent_visible_to_user(row, user)


def binding_read(row: AgentResourceBinding) -> AgentResourceBindingRead:
    """将数据库绑定对象转换为 API 响应模型。"""
    return AgentResourceBindingRead(
        id=row.id,
        tenant_id=row.tenant_id,
        agent_id=row.agent_id,
        resource_type=row.resource_type,  # type: ignore[arg-type]
        resource_id=row.resource_id,
        status=row.status,
        metadata=dict(row.metadata_json or {}),
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


def _copy_agent_scope_from_source(
    db: Session, tenant_id: str, source: AgentProfile, target: AgentProfile
) -> None:
    """从源 Agent 复制完整的资源范围到目标 Agent。

    如果源是整体 Agent：复制公共画廊资源。
    如果源是普通 Agent：逐条复制绑定的资源（含技能和知识库分支）。
    最后复制模型绑定。

    Args:
        db: 数据库会话依赖。
        tenant_id: 租户ID。
        source: 源 Agent 对象。
        target: 目标 Agent 对象。
    """
    if source.is_overall:
        copy_overall_scope_to_agent(db, tenant_id, target)
    else:
        bindings = db.exec(
            select(AgentResourceBinding).where(
                AgentResourceBinding.tenant_id == tenant_id,
                AgentResourceBinding.agent_id == source.id,
            )
        ).all()
        for binding in bindings:
            _copy_resource_binding(db, tenant_id, source.id, target.id, binding)
    _copy_agent_models_from_source(db, tenant_id, source, target)


def _copy_resource_binding(
    db: Session,
    tenant_id: str,
    source_agent_id: str,
    target_agent_id: str,
    binding: AgentResourceBinding,
) -> None:
    """复制单条资源绑定（含技能/知识库分支的复制）到目标 Agent。"""
    if binding.status != "active":
        return
    copied_binding = AgentResourceBinding(
        tenant_id=tenant_id,
        agent_id=target_agent_id,
        resource_type=binding.resource_type,
        resource_id=binding.resource_id,
        status=binding.status,
        metadata_json=dict(binding.metadata_json or {}),
    )
    db.add(copied_binding)
    if binding.resource_type == "skill":
        skill = db.get(Skill, binding.resource_id)
        if skill and skill.tenant_id == tenant_id:
            _copy_skill_branch(db, tenant_id, source_agent_id, target_agent_id, skill)
    elif binding.resource_type == "knowledge_base":
        kb = db.get(KnowledgeBase, binding.resource_id)
        if kb and kb.tenant_id == tenant_id:
            _copy_knowledge_branch(db, tenant_id, source_agent_id, target_agent_id, kb)


def _dedupe_ids(resource_ids: list[str]) -> list[str]:
    """对资源 ID 列表去重并过滤空值。"""
    seen: set[str] = set()
    deduped: list[str] = []
    for raw_id in resource_ids:
        resource_id = str(raw_id or "").strip()
        if not resource_id or resource_id in seen:
            continue
        seen.add(resource_id)
        deduped.append(resource_id)
    return deduped


def _source_resource_binding(
    db: Session,
    tenant_id: str,
    source_agent: AgentProfile,
    resource_type: str,
    resource_id: str,
) -> AgentResourceBinding | None:
    if source_agent.is_overall:
        return None
    return db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == source_agent.id,
            AgentResourceBinding.resource_type == resource_type,
            AgentResourceBinding.resource_id == resource_id,
        )
    ).first()


AgentResource = Skill | GeneralSkill | KnowledgeBase | Tool


def _blocked_learning_reason(
    db: Session,
    tenant_id: str,
    source_agent: AgentProfile,
    resource_type: str,
    resolved: AgentResource,
    source_binding: AgentResourceBinding | None,
) -> str | None:
    if source_agent.is_overall:
        return (
            None
            if _open_gallery_resource_enabled(db, tenant_id, resource_type, resolved)
            else "disabled_in_open_gallery"
        )
    if not source_binding or source_binding.status != "active":
        return "inactive_in_source_agent"
    if resource_type == "skill" and isinstance(resolved, Skill):
        branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == tenant_id,
                AgentSkillBranch.agent_id == source_agent.id,
                AgentSkillBranch.skill_id == resolved.skill_id,
            )
        ).first()
        if branch and branch.status != "active":
            return "inactive_in_source_agent"
    if resource_type == "knowledge_base" and isinstance(resolved, KnowledgeBase):
        branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == source_agent.id,
                AgentKnowledgeBranch.knowledge_base_id == resolved.id,
            )
        ).first()
        if branch and branch.status != "active":
            return "inactive_in_source_agent"
    return None


def _open_gallery_resource_enabled(
    db: Session,
    tenant_id: str,
    resource_type: str,
    resolved: AgentResource,
) -> bool:
    if not is_open_gallery_resource(db, tenant_id, resource_type, resolved):
        return False
    if resource_type == "skill" and isinstance(resolved, Skill):
        return resolved.status == "published"
    if resource_type == "general_skill" and isinstance(resolved, GeneralSkill):
        return resolved.status == "published"
    if resource_type == "knowledge_base" and isinstance(resolved, KnowledgeBase):
        return resolved.status == "active"
    if resource_type == "tool" and isinstance(resolved, Tool):
        return resolved.enabled
    return False


def _import_resource_to_overall(
    db: Session,
    tenant_id: str,
    source_agent: AgentProfile,
    resource_type: str,
    resolved: AgentResource,
) -> None:
    if source_agent.is_overall:
        return
    if resource_type == "skill" and isinstance(resolved, Skill):
        branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == tenant_id,
                AgentSkillBranch.agent_id == source_agent.id,
                AgentSkillBranch.skill_id == resolved.skill_id,
            )
        ).first()
        if branch:
            promote_branch_to_overall(db, tenant_id, branch)
        return
    if resource_type == "knowledge_base" and isinstance(resolved, KnowledgeBase):
        promote_knowledge_branch_to_overall(db, tenant_id, source_agent.id, resolved.id)


def _upsert_imported_resource_binding(
    db: Session,
    tenant_id: str,
    source_agent: AgentProfile,
    target_agent: AgentProfile,
    resource_type: str,
    resolved: AgentResource,
    source_binding: AgentResourceBinding | None,
) -> None:
    status = source_binding.status if source_binding else "active"
    metadata = agent_private_metadata(
        target_agent.id,
        dict(source_binding.metadata_json or {}) if source_binding else {},
    )
    existing = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == target_agent.id,
            AgentResourceBinding.resource_type == resource_type,
            AgentResourceBinding.resource_id == resolved.id,
        )
    ).first()
    if existing:
        existing.status = status
        existing.metadata_json = metadata
        existing.updated_at = utc_now()
        db.add(existing)
    else:
        db.add(
            AgentResourceBinding(
                tenant_id=tenant_id,
                agent_id=target_agent.id,
                resource_type=resource_type,
                resource_id=resolved.id,
                status=status,
                metadata_json=metadata,
            )
        )
    if resource_type == "skill" and isinstance(resolved, Skill):
        _copy_or_update_skill_branch(db, tenant_id, source_agent.id, target_agent.id, resolved)
    elif resource_type == "knowledge_base" and isinstance(resolved, KnowledgeBase):
        _copy_or_update_knowledge_branch(db, tenant_id, source_agent.id, target_agent.id, resolved)


def _copy_or_update_skill_branch(
    db: Session, tenant_id: str, source_agent_id: str, target_agent_id: str, skill: Skill
) -> None:
    with db.no_autoflush:
        source_branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == tenant_id,
                AgentSkillBranch.agent_id == source_agent_id,
                AgentSkillBranch.skill_id == skill.skill_id,
            )
        ).first()
    if not source_branch:
        branch = sync_branch_from_overall(db, tenant_id, target_agent_id, skill)
        _ensure_copied_skill_branch_version(db, branch, "导入自整体智能体")
        return
    with db.no_autoflush:
        target_branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == tenant_id,
                AgentSkillBranch.agent_id == target_agent_id,
                AgentSkillBranch.skill_id == skill.skill_id,
            )
        ).first()
    if not target_branch:
        target_branch = AgentSkillBranch(
            tenant_id=tenant_id,
            agent_id=target_agent_id,
            skill_id=source_branch.skill_id,
            source_skill_id=source_branch.source_skill_id,
        )
    target_branch.base_version = source_branch.base_version
    target_branch.head_version = source_branch.head_version
    target_branch.content_json = dict(source_branch.content_json or {})
    target_branch.status = source_branch.status
    target_branch.sync_state = source_branch.sync_state
    target_branch.metadata_json = dict(source_branch.metadata_json or {})
    target_branch.updated_at = utc_now()
    db.add(target_branch)
    db.flush()
    _ensure_copied_skill_branch_version(db, target_branch, f"导入自 {source_agent_id}")


def _ensure_copied_skill_branch_version(
    db: Session, branch: AgentSkillBranch, change_summary: str
) -> None:
    existing = db.exec(
        select(AgentSkillBranchVersion).where(
            AgentSkillBranchVersion.tenant_id == branch.tenant_id,
            AgentSkillBranchVersion.agent_id == branch.agent_id,
            AgentSkillBranchVersion.skill_id == branch.skill_id,
            AgentSkillBranchVersion.version == branch.head_version,
        )
    ).first()
    if existing:
        existing.content_json = dict(branch.content_json or {})
        existing.status = branch.status
        existing.sync_state = branch.sync_state
        existing.updated_at = utc_now()
        db.add(existing)
        return
    db.add(
        AgentSkillBranchVersion(
            tenant_id=branch.tenant_id,
            agent_id=branch.agent_id,
            skill_id=branch.skill_id,
            source_skill_id=branch.source_skill_id,
            version=branch.head_version,
            base_version=branch.base_version,
            content_json=dict(branch.content_json or {}),
            status=branch.status,
            sync_state=branch.sync_state,
            change_summary=change_summary,
        )
    )


def _copy_or_update_knowledge_branch(
    db: Session,
    tenant_id: str,
    source_agent_id: str,
    target_agent_id: str,
    kb: KnowledgeBase,
) -> None:
    with db.no_autoflush:
        source_branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == source_agent_id,
                AgentKnowledgeBranch.knowledge_base_id == kb.id,
            )
        ).first()
        target_branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == target_agent_id,
                AgentKnowledgeBranch.knowledge_base_id == kb.id,
            )
        ).first()
    if source_branch:
        base_version = source_branch.base_version
        head_version = source_branch.head_version
        status = source_branch.status
        sync_state = source_branch.sync_state
        metadata = dict(source_branch.metadata_json or {})
    else:
        version = ensure_knowledge_base_version(db, kb).version
        base_version = version
        head_version = version
        status = "active"
        sync_state = "synced"
        metadata = {}
    if not target_branch:
        target_branch = AgentKnowledgeBranch(
            tenant_id=tenant_id,
            agent_id=target_agent_id,
            knowledge_base_id=kb.id,
        )
    target_branch.base_version = base_version
    target_branch.head_version = head_version
    target_branch.status = status
    target_branch.sync_state = sync_state
    target_branch.metadata_json = metadata
    target_branch.updated_at = utc_now()
    db.add(target_branch)


def _resource_display_id(resource_type: str, resolved: AgentResource) -> str:
    if resource_type == "skill" and isinstance(resolved, Skill):
        return resolved.skill_id
    if resource_type == "general_skill" and isinstance(resolved, GeneralSkill):
        return resolved.slug
    if resource_type == "tool" and isinstance(resolved, Tool):
        return resolved.name
    return resolved.id


def _copy_agent_models_from_source(
    db: Session, tenant_id: str, source: AgentProfile, target: AgentProfile
) -> None:
    bindings = db.exec(
        select(AgentModelBinding).where(
            AgentModelBinding.tenant_id == tenant_id,
            AgentModelBinding.agent_id == source.id,
        )
    ).all()
    for binding in bindings:
        db.add(
            AgentModelBinding(
                tenant_id=tenant_id,
                agent_id=target.id,
                role=binding.role,
                model_config_id=binding.model_config_id,
            )
        )


def _copy_skill_branch(
    db: Session, tenant_id: str, source_agent_id: str, target_agent_id: str, skill: Skill
) -> None:
    source_branch = db.exec(
        select(AgentSkillBranch).where(
            AgentSkillBranch.tenant_id == tenant_id,
            AgentSkillBranch.agent_id == source_agent_id,
            AgentSkillBranch.skill_id == skill.skill_id,
        )
    ).first()
    if not source_branch:
        ensure_agent_skill_branch(db, tenant_id, target_agent_id, skill)
        return
    target_branch = AgentSkillBranch(
        tenant_id=tenant_id,
        agent_id=target_agent_id,
        skill_id=source_branch.skill_id,
        source_skill_id=source_branch.source_skill_id,
        base_version=source_branch.base_version,
        head_version=source_branch.head_version,
        content_json=dict(source_branch.content_json or {}),
        status=source_branch.status,
        sync_state=source_branch.sync_state,
        metadata_json=dict(source_branch.metadata_json or {}),
    )
    db.add(target_branch)
    db.flush()
    db.add(
        AgentSkillBranchVersion(
            tenant_id=tenant_id,
            agent_id=target_agent_id,
            skill_id=target_branch.skill_id,
            source_skill_id=target_branch.source_skill_id,
            version=target_branch.head_version,
            base_version=target_branch.base_version,
            content_json=dict(target_branch.content_json or {}),
            status=target_branch.status,
            sync_state=target_branch.sync_state,
            change_summary=f"复制自 {source_agent_id}",
        )
    )


def _copy_knowledge_branch(
    db: Session, tenant_id: str, source_agent_id: str, target_agent_id: str, kb: KnowledgeBase
) -> None:
    source_branch = db.exec(
        select(AgentKnowledgeBranch).where(
            AgentKnowledgeBranch.tenant_id == tenant_id,
            AgentKnowledgeBranch.agent_id == source_agent_id,
            AgentKnowledgeBranch.knowledge_base_id == kb.id,
        )
    ).first()
    if not source_branch:
        return
    db.add(
        AgentKnowledgeBranch(
            tenant_id=tenant_id,
            agent_id=target_agent_id,
            knowledge_base_id=source_branch.knowledge_base_id,
            base_version=source_branch.base_version,
            head_version=source_branch.head_version,
            status=source_branch.status,
            sync_state=source_branch.sync_state,
            metadata_json=dict(source_branch.metadata_json or {}),
        )
    )


def _resolve_resource(
    db: Session, tenant_id: str, resource_type: str, identifier: str
) -> AgentResource | None:
    """根据资源类型和标识符解析资源对象（支持 ID 和业务标识符双重查找）。

    Args:
        db: 数据库会话依赖。
        tenant_id: 租户ID。
        resource_type: 资源类型（skill/general_skill/knowledge_base/tool）。
        identifier: 资源标识符（数据库 ID 或业务标识符如 skill_id/slug/name）。

    Returns:
        资源对象，不存在时返回 None。
    """
    if resource_type == "skill":
        return (
            db.get(Skill, identifier)
            or db.exec(
                select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id == identifier)
            ).first()
        )
    if resource_type == "general_skill":
        return (
            db.get(GeneralSkill, identifier)
            or db.exec(
                select(GeneralSkill).where(
                    GeneralSkill.tenant_id == tenant_id, GeneralSkill.slug == identifier
                )
            ).first()
        )
    if resource_type == "knowledge_base":
        return (
            db.get(KnowledgeBase, identifier)
            or db.exec(
                select(KnowledgeBase).where(
                    KnowledgeBase.tenant_id == tenant_id, KnowledgeBase.name == identifier
                )
            ).first()
        )
    if resource_type == "tool":
        return (
            db.get(Tool, identifier)
            or db.exec(
                select(Tool).where(Tool.tenant_id == tenant_id, Tool.name == identifier)
            ).first()
        )
    return None


def _get_agent(db: Session, tenant_id: str, agent_id: str) -> AgentProfile:
    """获取 Agent 对象，不存在时抛出 404 异常。"""
    ensure_tenant(db, tenant_id)
    row = db.get(AgentProfile, agent_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Agent not found")
    return row


def _bindings_by_agent(db: Session, tenant_id: str) -> dict[str, list[AgentResourceBinding]]:
    """按 Agent ID 分组获取所有可见的资源绑定。

    Args:
        db: 数据库会话依赖。
        tenant_id: 租户ID。

    Returns:
        以 agent_id 为键、绑定列表为值的字典。
    """
    rows = db.exec(
        select(AgentResourceBinding)
        .where(AgentResourceBinding.tenant_id == tenant_id)
        .order_by(AgentResourceBinding.created_at.asc())
    ).all()
    agent_ids = {row.agent_id for row in rows}
    agents_by_id = {
        row.id: row
        for row in db.exec(
            select(AgentProfile).where(
                AgentProfile.tenant_id == tenant_id,
                AgentProfile.id.in_(agent_ids) if agent_ids else AgentProfile.id == "__none__",
            )
        ).all()
    }
    grouped: dict[str, list[AgentResourceBinding]] = {}
    for row in rows:
        if not _resource_binding_visible_in_agent_summary(
            db, tenant_id, agents_by_id.get(row.agent_id), row
        ):
            continue
        grouped.setdefault(row.agent_id, []).append(row)
    return grouped


def _resource_binding_visible_in_agent_summary(
    db: Session,
    tenant_id: str,
    agent: AgentProfile | None,
    binding: AgentResourceBinding,
) -> bool:
    if not agent or binding.status == "deleted":
        return False

    model_by_type = {
        "skill": Skill,
        "general_skill": GeneralSkill,
        "knowledge_base": KnowledgeBase,
        "tool": Tool,
    }
    model = model_by_type.get(binding.resource_type)
    if model is None:
        return False
    resource = db.get(model, binding.resource_id)
    if not resource or resource.tenant_id != tenant_id:
        return False
    if isinstance(resource, KnowledgeBase) and _is_empty_default_knowledge_base(
        db, tenant_id, resource
    ):
        return False

    if agent.is_overall:
        if not is_open_gallery_resource(db, tenant_id, binding.resource_type, resource):
            return False
    elif not is_bound_resource_visible_for_agent(
        db, tenant_id, binding.resource_type, resource, binding
    ):
        return False

    if isinstance(resource, Skill) and not agent.is_overall:
        branch = db.exec(
            select(AgentSkillBranch).where(
                AgentSkillBranch.tenant_id == tenant_id,
                AgentSkillBranch.agent_id == agent.id,
                AgentSkillBranch.skill_id == resource.skill_id,
            )
        ).first()
        if branch and branch.status == "deleted":
            return False
        skill_status = branch.status if branch else resource.status
        if binding.status == "active" and skill_status not in {"active", "published"}:
            return False

    if isinstance(resource, KnowledgeBase) and not agent.is_overall:
        branch = db.exec(
            select(AgentKnowledgeBranch).where(
                AgentKnowledgeBranch.tenant_id == tenant_id,
                AgentKnowledgeBranch.agent_id == agent.id,
                AgentKnowledgeBranch.knowledge_base_id == resource.id,
                AgentKnowledgeBranch.status != "deleted",
            )
        ).first()
        if not branch:
            return False
        if binding.status == "active" and branch.status != "active":
            return False

    if binding.status != "active":
        return True
    if isinstance(resource, Skill):
        return agent.is_overall is False or resource.status == "published"
    if isinstance(resource, GeneralSkill):
        return resource.status == "published"
    if isinstance(resource, KnowledgeBase):
        return resource.status == "active"
    if isinstance(resource, Tool):
        return resource.enabled
    return False


def _is_empty_default_knowledge_base(db: Session, tenant_id: str, kb: KnowledgeBase) -> bool:
    metadata = kb.metadata_json or {}
    has_runtime_rows = any(
        db.exec(
            select(model.id).where(
                model.tenant_id == tenant_id,
                model.knowledge_base_id == kb.id,
            )
        ).first()
        for model in (KnowledgeDocument, KnowledgeBucket, KnowledgeChunk)
    )
    if has_runtime_rows:
        return False
    if metadata.get("created_from_document_upload") and not metadata.get("source_document_id"):
        return True
    return kb.name == "默认知识库"


def _ensure_resource_exists(db: Session, tenant_id: str, item: AgentResourceBindingInput) -> None:
    """验证指定的资源存在且属于当前租户。"""
    model = {
        "skill": Skill,
        "general_skill": GeneralSkill,
        "knowledge_base": KnowledgeBase,
        "tool": Tool,
    }[item.resource_type]
    row = db.get(model, item.resource_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(
            status_code=404, detail=f"Resource not found: {item.resource_type}:{item.resource_id}"
        )


def _get_global_skill(db: Session, tenant_id: str, skill_id: str) -> Skill:
    """获取全局技能对象，不存在时抛出 404 异常。"""
    row = db.exec(
        select(Skill).where(Skill.tenant_id == tenant_id, Skill.skill_id == skill_id)
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="Skill not found")
    return row


def _skill_branch_read(skill: Skill) -> dict[str, object]:
    """将技能对象（含分支投影）转换为包含分支元数据的字典。"""
    metadata = getattr(skill, "agent_branch_meta", {}) or {}
    content = skill.content_json or {}
    if not metadata and isinstance(content.get("metadata"), dict):
        metadata = content.get("metadata", {}).get("agent_branch", {}) or {}
    return {
        "id": skill.id,
        "tenant_id": skill.tenant_id,
        "skill_id": skill.skill_id,
        "version": skill.version,
        "name": skill.name,
        "business_domain": skill.business_domain,
        "description": skill.description,
        "content": skill.content_json,
        "status": skill.status,
        "agent_id": metadata.get("agent_id"),
        "branch_status": metadata.get("status"),
        "branch_sync_state": metadata.get("sync_state"),
        "branch_base_version": metadata.get("base_version"),
        "branch_head_version": metadata.get("head_version"),
        "metadata": dict(metadata.get("metadata") or {}),
        "created_at": skill.created_at.isoformat(),
        "updated_at": skill.updated_at.isoformat(),
    }
