"""工具（Tools）和 MCP Server API 模块。

提供两组路由：

1. **工具管理**（``/api/enterprise/tools``）：HTTP/MCP 工具的 CRUD、
   分桶查询（buckets）、工具探测（probe）和测试执行（test）。
   支持 agent 私有工具和开放广场（open gallery）工具的绑定与隔离。
2. **MCP Server 管理**（``/api/enterprise/mcp-servers``）：MCP Server 的 CRUD、
   工具发现（discover）和批量同步（sync）。
   支持四种传输方式：builtin、stdio、http（streamable_http）、sse。

工具与技能（Skill）之间通过 ``allowed_skills`` 字段建立关联，
``_sync_skill_tool_bindings`` 负责在技能变更时自动更新工具的技能白名单。
"""

from __future__ import annotations

from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select

from app.agents.branching import (
    ensure_open_gallery_binding,
    ensure_private_resource_binding,
    get_agent,
    hide_open_gallery_binding,
    is_bound_resource_visible_for_agent,
    is_open_gallery_resource,
    require_overall_agent,
    resource_binding_metadata,
    user_creator_metadata,
    visible_tool_rows,
)
from app.config import get_settings
from app.db import get_session
from app.db.models import AgentProfile, AgentResourceBinding, MCPServer, Tool, User, utc_now
from app.security.auth import ensure_current_user_tenant, get_current_user
from app.security.permissions import (
    ensure_agent_scope_manager,
    ensure_open_gallery_admin,
    require_agent_scope_viewer,
    require_tenant_admin,
)
from app.security.tenant import ensure_tenant
from app.tools import ToolExecutor
from app.tools.http_request import prepare_get_request
from app.tools.mcp_client import MCPClientError, execute_mcp_tool, list_mcp_tools
from app.tools.tool_schema import (
    MCPDiscoverRequest,
    MCPDiscoverResponse,
    MCPDiscoveredTool,
    MCPServerConnection,
    MCPServerCreateRequest,
    MCPServerRead,
    MCPServerUpdateRequest,
    MCPSyncRequest,
    MCPSyncResponse,
    ToolBucketRead,
    ToolCall,
    ToolCreateRequest,
    ToolError,
    ToolProbeRequest,
    ToolProbeResponse,
    ToolRead,
    ToolResult,
    ToolTestRequest,
    ToolUpdateRequest,
)

router = APIRouter(prefix="/api/enterprise/tools", tags=["enterprise:tools"])
mcp_router = APIRouter(prefix="/api/enterprise/mcp-servers", tags=["enterprise:mcp-servers"])


def tool_read(row: Tool, metadata: dict[str, Any] | None = None) -> ToolRead:
    """将 Tool 数据库行转换为前端可读的 ToolRead 响应对象。

    Args:
        row: Tool 数据库模型实例。
        metadata: 附加的绑定元数据（如创建者、可见范围），可选。

    Returns:
        ToolRead: 序列化后的工具视图对象。
    """
    return ToolRead(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        display_name=row.display_name,
        description=row.description,
        bucket=row.bucket or "未分桶",
        tool_type=row.tool_type or "http",
        method=row.method,
        url=row.url,
        headers=row.headers_json or {},
        auth=row.auth_json or {},
        mcp_config=row.config_json or {},
        input_schema=row.input_schema or {},
        output_schema=row.output_schema or {},
        allowed_skills=row.allowed_skills_json or [],
        mcp_server_id=row.mcp_server_id,
        enabled=row.enabled,
        metadata=dict(metadata or {}),
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


@router.get("", response_model=list[ToolRead], dependencies=[Depends(require_agent_scope_viewer)])
def list_tools(
    tenant_id: str = Query(...),
    bucket: str | None = Query(default=None),
    agent_id: str | None = Query(default=None),
    db: Session = Depends(get_session),
) -> list[ToolRead]:
    """列出当前租户下可见的工具列表。

    支持按分桶（bucket）和 agent 视角过滤，仅返回当前 agent 有权查看的工具。

    Args:
        tenant_id: 租户 ID。
        bucket: 可选的分桶过滤条件。
        agent_id: 可选的 agent ID，用于限定可见范围。
        db: 数据库会话。

    Returns:
        list[ToolRead]: 工具视图对象列表。

    Raises:
        HTTPException: 租户或 agent 不存在时抛出。
    """
    ensure_tenant(db, tenant_id)
    rows = _visible_tool_rows(db, tenant_id, bucket, agent_id)
    metadata_by_id = resource_binding_metadata(db, tenant_id, agent_id, "tool")
    return [tool_read(row, metadata_by_id.get(row.id)) for row in rows]


@router.get(
    "/buckets",
    response_model=list[ToolBucketRead],
    dependencies=[Depends(require_agent_scope_viewer)],
)
def list_tool_buckets(
    tenant_id: str = Query(...),
    agent_id: str | None = Query(default=None),
    db: Session = Depends(get_session),
) -> list[ToolBucketRead]:
    """按分桶（bucket）聚合统计工具数量。

    将可见工具按 bucket 字段分组，返回每个桶的总数、启用数和禁用数。

    Args:
        tenant_id: 租户 ID。
        agent_id: 可选的 agent ID，用于限定可见范围。
        db: 数据库会话。

    Returns:
        list[ToolBucketRead]: 分桶统计列表，按总数降序排列。
    """
    ensure_tenant(db, tenant_id)
    rows = _visible_tool_rows(db, tenant_id, None, agent_id)
    grouped: dict[str, ToolBucketRead] = {}
    for row in rows:
        bucket = row.bucket or "未分桶"
        item = grouped.setdefault(
            bucket, ToolBucketRead(bucket=bucket, total=0, enabled_count=0, disabled_count=0)
        )
        item.total += 1
        if row.enabled:
            item.enabled_count += 1
        else:
            item.disabled_count += 1
        item.tool_ids.append(row.id)
    return sorted(grouped.values(), key=lambda item: (-item.total, item.bucket))


@router.post("", response_model=ToolRead)
def create_tool(
    request: ToolCreateRequest,
    agent_id: str | None = Query(default=None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ToolRead:
    """创建新工具。

    根据当前 agent 范围决定工具的可见性：员工范围内为私有绑定，
    总管（overall）或未指定 agent 时落入开放广场（open gallery）。

    Args:
        request: 工具创建请求体。
        agent_id: 可选的 agent ID。
        db: 数据库会话。
        current_user: 当前登录用户。

    Returns:
        ToolRead: 新建的工具视图对象。

    Raises:
        HTTPException: 工具名重复（409）或权限不足时抛出。
    """
    ensure_tenant(db, request.tenant_id)
    existing = db.exec(
        select(Tool).where(Tool.tenant_id == request.tenant_id, Tool.name == request.name)
    ).first()
    if existing:
        raise HTTPException(status_code=409, detail="Tool name already exists for this tenant")
    agent = ensure_agent_scope_manager(db, request.tenant_id, agent_id, current_user)
    row = Tool(
        tenant_id=request.tenant_id,
        name=request.name,
        display_name=request.display_name,
        description=request.description,
        bucket=_normalize_bucket(request.bucket),
        tool_type=request.tool_type,
        method=request.method,
        url=request.url,
        headers_json=request.headers,
        auth_json=request.auth,
        config_json=request.mcp_config,
        input_schema=request.input_schema,
        output_schema=request.output_schema,
        allowed_skills_json=request.allowed_skills,
        enabled=request.enabled,
    )
    db.add(row)
    db.flush()
    creator_metadata = user_creator_metadata(current_user)
    if agent and not agent.is_overall:
        ensure_private_resource_binding(
            db,
            request.tenant_id,
            agent.id,
            "tool",
            row.id,
            "active" if request.enabled else "inactive",
            metadata_json=creator_metadata,
        )
    else:
        ensure_open_gallery_admin(request.tenant_id, current_user)
        ensure_open_gallery_binding(
            db,
            request.tenant_id,
            "tool",
            row.id,
            "active" if request.enabled else "inactive",
            metadata_json=creator_metadata,
        )
    db.commit()
    db.refresh(row)
    metadata_by_id = resource_binding_metadata(db, request.tenant_id, agent_id, "tool")
    return tool_read(row, metadata_by_id.get(row.id))


@router.post("/probe", response_model=ToolProbeResponse)
def probe_tool(
    request: ToolProbeRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ToolProbeResponse:
    """探测（probe）工具连通性和返回结构。

    支持 MCP 和 HTTP 两种工具类型，用示例参数发起一次请求，
    返回成功与否、状态码、数据预览和推断出的输出 schema。

    Args:
        request: 探测请求体，包含工具配置和示例参数。
        db: 数据库会话。
        current_user: 当前登录用户。

    Returns:
        ToolProbeResponse: 探测结果，含状态码、数据预览和推断的 schema。
    """
    ensure_current_user_tenant(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    if request.tool_type == "mcp":
        try:
            data = execute_mcp_tool(
                request.mcp_config,
                request.sample_arguments,
                timeout_seconds=get_settings().tool_timeout_seconds,
            )
        except MCPClientError as exc:
            return ToolProbeResponse(
                success=False,
                status_code=400,
                error=ToolError(code="MCP_ERROR", message=str(exc)),
            )
        except Exception as exc:
            return ToolProbeResponse(
                success=False,
                status_code=500,
                error=ToolError(code="MCP_PROBE_ERROR", message=str(exc)),
            )
        return ToolProbeResponse(
            success=True,
            status_code=200,
            data_preview=data,
            inferred_output_schema=_infer_json_schema(data),
            error=None,
        )
    headers = ToolExecutor(db)._resolve_headers(request.headers, request.auth)  # noqa: SLF001
    url = _normalize_probe_url(request.url)
    try:
        with httpx.Client(timeout=get_settings().tool_timeout_seconds) as client:
            if request.method.upper() == "GET":
                request_url, request_kwargs = prepare_get_request(url, request.sample_arguments)
                response = client.request(
                    request.method.upper(), request_url, headers=headers, **request_kwargs
                )
            else:
                response = client.request(
                    request.method.upper(), url, headers=headers, json=request.sample_arguments
                )
    except httpx.TimeoutException:
        return ToolProbeResponse(
            success=False,
            error=ToolError(code="TIMEOUT", message="工具探测超时。"),
        )
    except Exception as exc:
        return ToolProbeResponse(
            success=False,
            error=ToolError(code="PROBE_ERROR", message=str(exc)),
        )

    data_preview = _response_preview(response)
    success = 200 <= response.status_code < 300
    return ToolProbeResponse(
        success=success,
        status_code=response.status_code,
        data_preview=data_preview,
        inferred_output_schema=_infer_json_schema(data_preview) if success else {},
        error=None
        if success
        else ToolError(
            code="HTTP_ERROR", message=f"工具探测返回异常状态码：{response.status_code}"
        ),
    )


@router.get(
    "/{tool_id}", response_model=ToolRead, dependencies=[Depends(require_agent_scope_viewer)]
)
def get_tool(
    tool_id: str,
    tenant_id: str = Query(...),
    agent_id: str | None = Query(default=None),
    db: Session = Depends(get_session),
) -> ToolRead:
    """获取单个工具的详情。

    Args:
        tool_id: 工具 ID。
        tenant_id: 租户 ID。
        agent_id: 可选的 agent ID，用于权限校验。
        db: 数据库会话。

    Returns:
        ToolRead: 工具视图对象。

    Raises:
        HTTPException: 工具不存在或对当前 agent 不可见时抛出 404。
    """
    row = _get_tool(db, tenant_id, tool_id)
    _ensure_tool_visible(db, tenant_id, row, agent_id)
    metadata_by_id = resource_binding_metadata(db, tenant_id, agent_id, "tool")
    return tool_read(row, metadata_by_id.get(row.id))


@router.put("/{tool_id}", response_model=ToolRead)
def update_tool(
    tool_id: str,
    request: ToolUpdateRequest,
    agent_id: str | None = Query(default=None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ToolRead:
    """更新工具信息。

    员工 agent 编辑开放广场工具时会自动克隆为私有副本再修改；
    总管（overall）直接修改广场工具。工具名不可修改。

    Args:
        tool_id: 工具 ID。
        request: 工具更新请求体。
        agent_id: 可选的 agent ID。
        db: 数据库会话。
        current_user: 当前登录用户。

    Returns:
        ToolRead: 更新后的工具视图对象。

    Raises:
        HTTPException: 工具不存在（404）、工具名被修改（400）或权限不足时抛出。
    """
    row = _get_tool(db, request.tenant_id, tool_id)
    agent = ensure_agent_scope_manager(db, request.tenant_id, agent_id, current_user)
    _ensure_tool_visible(db, request.tenant_id, row, agent_id)
    if agent and not agent.is_overall:
        source_tool_id = row.id
        source_was_open_gallery = is_open_gallery_resource(db, request.tenant_id, "tool", row)
        row = _ensure_private_tool_for_agent(db, request.tenant_id, agent, row)
        if not source_was_open_gallery and request.name.strip() != row.name:
            raise HTTPException(status_code=400, detail="Tool name cannot be modified")
    else:
        ensure_open_gallery_admin(request.tenant_id, current_user)
        source_tool_id = row.id
        if request.name.strip() != row.name:
            raise HTTPException(status_code=400, detail="Tool name cannot be modified")
    row.display_name = request.display_name
    row.description = request.description
    row.bucket = _normalize_bucket(request.bucket)
    row.tool_type = request.tool_type
    row.method = request.method
    row.url = request.url
    row.headers_json = request.headers
    row.auth_json = request.auth
    row.config_json = request.mcp_config
    row.input_schema = request.input_schema
    row.output_schema = request.output_schema
    row.allowed_skills_json = request.allowed_skills
    row.enabled = request.enabled
    row.updated_at = utc_now()
    db.add(row)
    db.flush()
    creator_metadata = user_creator_metadata(current_user)
    if agent and not agent.is_overall:
        if source_tool_id != row.id:
            source_binding = _tool_binding(db, request.tenant_id, agent.id, source_tool_id)
            if source_binding:
                source_binding.status = "deleted"
                source_binding.updated_at = utc_now()
                db.add(source_binding)
        ensure_private_resource_binding(
            db,
            request.tenant_id,
            agent.id,
            "tool",
            row.id,
            "active" if request.enabled else "inactive",
            metadata_json=creator_metadata if source_tool_id != row.id else None,
        )
    else:
        ensure_open_gallery_binding(
            db,
            request.tenant_id,
            "tool",
            row.id,
            "active" if request.enabled else "inactive",
            metadata_json=creator_metadata,
        )
    db.commit()
    db.refresh(row)
    metadata_by_id = resource_binding_metadata(db, request.tenant_id, agent_id, "tool")
    return tool_read(row, metadata_by_id.get(row.id))


@router.delete("/{tool_id}")
def delete_tool(
    tool_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = None,
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    """删除或隐藏工具。

    行为取决于调用者身份：员工 agent 隐藏其私有绑定（status=hidden），
    总管（overall）隐藏广场绑定，系统级调用才真正删除工具行。

    Args:
        tool_id: 工具 ID。
        tenant_id: 租户 ID。
        db: 数据库会话。
        agent_id: 可选的 agent ID。
        current_user: 当前登录用户。

    Returns:
        dict[str, str]: 操作结果，status 为 "hidden" 或 "deleted"。

    Raises:
        HTTPException: 工具不存在或不可见时抛出 404。
    """
    row = _get_tool(db, tenant_id, tool_id)
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        binding = _tool_binding(db, tenant_id, agent.id, row.id)
        if binding:
            binding.status = "deleted"
            binding.updated_at = utc_now()
            db.add(binding)
            db.commit()
            return {"status": "hidden"}
        raise HTTPException(status_code=404, detail="Tool not visible to this agent")
    if agent and agent.is_overall:
        if not is_open_gallery_resource(db, tenant_id, "tool", row):
            raise HTTPException(status_code=404, detail="Tool not visible in open gallery")
        ensure_open_gallery_admin(tenant_id, current_user)
        hide_open_gallery_binding(db, tenant_id, "tool", row.id)
        db.commit()
        return {"status": "hidden"}
    require_overall_agent(db, tenant_id, agent_id)
    ensure_open_gallery_admin(tenant_id, current_user)
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@router.post("/{tool_id}/test", response_model=ToolResult)
def test_tool(
    tool_id: str,
    request: ToolTestRequest,
    agent_id: str | None = Query(default=None),
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> ToolResult:
    """执行工具测试调用。

    用提供的参数实际执行一次工具，返回执行结果。

    Args:
        tool_id: 工具 ID。
        request: 测试请求体，包含参数。
        agent_id: 可选的 agent ID。
        db: 数据库会话。
        current_user: 当前登录用户。

    Returns:
        ToolResult: 工具执行结果。

    Raises:
        HTTPException: 工具不存在或不可见时抛出 404。
    """
    ensure_current_user_tenant(request.tenant_id, current_user)
    row = _get_tool(db, request.tenant_id, tool_id)
    _ensure_tool_visible(db, request.tenant_id, row, agent_id)
    return ToolExecutor(db).execute(
        request.tenant_id,
        ToolCall(name=row.name, arguments=request.arguments),
        agent_id=agent_id,
    )


def _get_tool(db: Session, tenant_id: str, tool_id: str) -> Tool:
    """根据 ID 查找工具行，校验租户归属。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。
        tool_id: 工具 ID。

    Returns:
        Tool: 工具数据库行。

    Raises:
        HTTPException: 租户或工具不存在时抛出 404。
    """
    ensure_tenant(db, tenant_id)
    row = db.get(Tool, tool_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="Tool not found")
    return row


def _visible_tool_rows(
    db: Session,
    tenant_id: str,
    bucket: str | None = None,
    agent_id: str | None = None,
) -> list[Tool]:
    """获取当前 agent 视角下可见的工具行列表。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。
        bucket: 可选分桶过滤，值为 ``__all`` 时不过滤。
        agent_id: 可选的 agent ID。

    Returns:
        list[Tool]: 可见的工具行列表。

    Raises:
        HTTPException: 指定了 agent_id 但 agent 不存在时抛出 404。
    """
    agent = get_agent(db, tenant_id, agent_id)
    if agent_id and not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    rows = visible_tool_rows(db, tenant_id, agent_id, include_inactive=True)
    if bucket and bucket != "__all__":
        return [row for row in rows if row.bucket == bucket]
    return rows


def _ensure_tool_visible(db: Session, tenant_id: str, row: Tool, agent_id: str | None) -> None:
    """校验工具对指定 agent 是否可见，不可见则抛出 404。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。
        row: 工具数据库行。
        agent_id: 可选的 agent ID。

    Raises:
        HTTPException: agent 不存在、工具对 agent 不可见或不在广场时抛出 404。
    """
    agent = get_agent(db, tenant_id, agent_id)
    if agent_id and not agent:
        raise HTTPException(status_code=404, detail="Agent not found")
    if agent and not agent.is_overall:
        binding = _tool_binding(db, tenant_id, agent.id, row.id)
        if not binding or not is_bound_resource_visible_for_agent(
            db, tenant_id, "tool", row, binding
        ):
            raise HTTPException(status_code=404, detail="Tool not visible to this agent")
    if (not agent or agent.is_overall) and not is_open_gallery_resource(db, tenant_id, "tool", row):
        raise HTTPException(status_code=404, detail="Tool not visible in open gallery")


def _tool_binding(
    db: Session, tenant_id: str, agent_id: str, tool_id: str
) -> AgentResourceBinding | None:
    """查询 agent 与工具之间的资源绑定记录。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。
        agent_id: agent ID。
        tool_id: 工具 ID。

    Returns:
        AgentResourceBinding | None: 绑定记录（未删除的），不存在则返回 None。
    """
    return db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent_id,
            AgentResourceBinding.resource_type == "tool",
            AgentResourceBinding.resource_id == tool_id,
            AgentResourceBinding.status != "deleted",
        )
    ).first()


def _ensure_private_tool_for_agent(
    db: Session, tenant_id: str, agent: AgentProfile, row: Tool
) -> Tool:
    """为 agent 创建开放广场工具的私有克隆副本。

    仅当工具当前属于开放广场时才克隆；私有工具直接返回原行。
    克隆后副本获得带 agent 后缀的唯一名称。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。
        agent: agent 档案。
        row: 源工具行。

    Returns:
        Tool: agent 可编辑的工具行（克隆副本或原行）。
    """
    if not is_open_gallery_resource(db, tenant_id, "tool", row):
        return row
    now = utc_now()
    clone = Tool(
        tenant_id=tenant_id,
        name=_unique_tool_name(db, tenant_id, row.name, agent.id),
        display_name=row.display_name,
        description=row.description,
        bucket=row.bucket,
        tool_type=row.tool_type,
        method=row.method,
        url=row.url,
        headers_json=dict(row.headers_json or {}),
        auth_json=dict(row.auth_json or {}),
        config_json=dict(row.config_json or {}),
        input_schema=dict(row.input_schema or {}),
        output_schema=dict(row.output_schema or {}),
        allowed_skills_json=list(row.allowed_skills_json or []),
        mcp_server_id=row.mcp_server_id,
        enabled=row.enabled,
        created_at=now,
        updated_at=now,
    )
    db.add(clone)
    db.flush()
    return clone


def _tool_name_taken(db: Session, tenant_id: str, name: str, exclude_id: str | None = None) -> bool:
    """检查工具名在租户内是否已被占用。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。
        name: 待检查的工具名。
        exclude_id: 需排除的工具 ID（用于更新场景）。

    Returns:
        bool: 已被占用返回 True。
    """
    stmt = select(Tool).where(Tool.tenant_id == tenant_id, Tool.name == name)
    if exclude_id:
        stmt = stmt.where(Tool.id != exclude_id)
    return db.exec(stmt).first() is not None


def _unique_tool_name(
    db: Session,
    tenant_id: str,
    base_name: str,
    agent_id: str,
    exclude_id: str | None = None,
) -> str:
    """生成租户内唯一的工具名（带 agent 后缀，冲突时追加数字）。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。
        base_name: 基础工具名。
        agent_id: agent ID，取前 8 位作为后缀。
        exclude_id: 需排除的工具 ID。

    Returns:
        str: 可用的唯一工具名。
    """
    base = (base_name or "tool").strip() or "tool"
    suffix_base = f"{base}-{agent_id[:8]}"
    candidate = suffix_base
    suffix = 2
    while _tool_name_taken(db, tenant_id, candidate, exclude_id=exclude_id):
        candidate = f"{suffix_base}-{suffix}"
        suffix += 1
    return candidate


def _normalize_bucket(value: str | None) -> str:
    """规范化分桶名称，空值或纯空白回退为"未分桶"。

    Args:
        value: 原始分桶名称。

    Returns:
        str: 规范化后的分桶名称。
    """
    normalized = (value or "").strip()
    return normalized or "未分桶"


def _normalize_probe_url(url: str) -> str:
    """规范化探测 URL，相对路径自动拼接基础 URL 前缀。

    Args:
        url: 原始 URL 字符串。

    Returns:
        str: 规范化后的完整 URL。
    """
    stripped = url.strip()
    if stripped.startswith("/"):
        return f"{get_settings().normalized_tool_base_url}{stripped}"
    return stripped


def _response_preview(response: httpx.Response) -> Any:
    """提取 HTTP 响应预览数据。

    优先解析 JSON，失败时返回纯文本（截断至 2000 字符）。

    Args:
        response: httpx 响应对象。

    Returns:
        Any: JSON 解析结果或截断后的文本。
    """
    try:
        return response.json()
    except Exception:
        text = response.text
        return text[:2000] if len(text) > 2000 else text


def _infer_json_schema(value: Any) -> dict[str, Any]:
    """从 Python 值推断 JSON Schema。

    递归推断 dict/list/bool/int/float/None/str 对应的 JSON Schema 类型。
    用于工具探测（probe）后自动推断输出结构。

    Args:
        value: 待推断的 Python 值。

    Returns:
        JSON Schema 字典。
    """
    if isinstance(value, dict):
        properties = {str(key): _infer_json_schema(item) for key, item in value.items()}
        return {"type": "object", "properties": properties, "required": list(properties.keys())}
    if isinstance(value, list):
        item_schema = _infer_json_schema(value[0]) if value else {}
        return {"type": "array", "items": item_schema}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if value is None:
        return {"type": "null"}
    return {"type": "string"}


# --------------------------------------------------------------------------- #
# MCP Servers（工具集）
# --------------------------------------------------------------------------- #


def _server_connection(row: MCPServer) -> MCPServerConnection:
    """将 MCPServer 数据库行转换为结构化连接配置对象。

    Args:
        row: MCPServer 数据库模型实例。

    Returns:
        MCPServerConnection: 包含传输方式、URL、命令等信息的连接配置。
    """
    return MCPServerConnection(
        transport=row.transport,  # type: ignore[arg-type]
        url=row.url,
        headers=row.headers_json or {},
        command=row.command,
        args=row.args_json or [],
        env=row.env_json or {},
        cwd=row.cwd,
    )


def _connection_to_client_config(connection: MCPServerConnection) -> dict[str, Any]:
    """把结构化连接配置转成 mcp_client 认识的扁平 config。"""
    config: dict[str, Any] = {"transport": connection.transport}
    if connection.transport in {"streamable_http", "sse"}:
        config["url"] = connection.url or ""
        if connection.headers:
            config["headers"] = dict(connection.headers)
    elif connection.transport == "stdio":
        config["command"] = connection.command or ""
        config["args"] = list(connection.args or [])
        if connection.env:
            config["env"] = dict(connection.env)
        if connection.cwd:
            config["cwd"] = connection.cwd
    elif connection.transport == "builtin":
        config["server"] = "builtin.demo"
    return config


def mcp_server_read(row: MCPServer, db: Session) -> MCPServerRead:
    """将 MCPServer 数据库行转换为可读的 MCPServerRead 响应对象。

    同时统计该 Server 下已导入的工具数量。

    Args:
        row: MCPServer 数据库模型实例。
        db: 数据库会话。

    Returns:
        MCPServerRead: 序列化后的 MCP Server 视图对象。
    """
    tool_count = len(db.exec(select(Tool.id).where(Tool.mcp_server_id == row.id)).all())
    return MCPServerRead(
        id=row.id,
        tenant_id=row.tenant_id,
        name=row.name,
        display_name=row.display_name,
        description=row.description,
        bucket=row.bucket or "MCP 工具",
        connection=_server_connection(row),
        enabled=row.enabled,
        last_synced_at=row.last_synced_at.isoformat() if row.last_synced_at else None,
        tool_count=tool_count,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


@mcp_router.get(
    "", response_model=list[MCPServerRead], dependencies=[Depends(require_tenant_admin)]
)
def list_mcp_servers(
    tenant_id: str = Query(...), db: Session = Depends(get_session)
) -> list[MCPServerRead]:
    """列出租户下所有 MCP Server。

    Args:
        tenant_id: 租户 ID。
        db: 数据库会话。

    Returns:
        list[MCPServerRead]: MCP Server 视图对象列表，按名称排序。
    """
    ensure_tenant(db, tenant_id)
    rows = db.exec(
        select(MCPServer).where(MCPServer.tenant_id == tenant_id).order_by(MCPServer.name)
    ).all()
    return [mcp_server_read(row, db) for row in rows]


@mcp_router.post("", response_model=MCPServerRead)
def create_mcp_server(
    request: MCPServerCreateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> MCPServerRead:
    """创建新的 MCP Server。

    需要开放广场管理员权限。Server 名在租户内唯一。

    Args:
        request: MCP Server 创建请求体。
        db: 数据库会话。
        current_user: 当前登录用户。

    Returns:
        MCPServerRead: 新建的 MCP Server 视图对象。

    Raises:
        HTTPException: 名重复（409）或权限不足时抛出。
    """
    ensure_tenant(db, request.tenant_id)
    ensure_open_gallery_admin(request.tenant_id, current_user)
    existing = db.exec(
        select(MCPServer).where(
            MCPServer.tenant_id == request.tenant_id, MCPServer.name == request.name
        )
    ).first()
    if existing:
        raise HTTPException(
            status_code=409, detail="MCP server name already exists for this tenant"
        )
    conn = request.connection
    row = MCPServer(
        tenant_id=request.tenant_id,
        name=request.name,
        display_name=request.display_name,
        description=request.description,
        bucket=_normalize_bucket(request.bucket) if request.bucket else "MCP 工具",
        transport=conn.transport,
        url=conn.url,
        headers_json=conn.headers,
        command=conn.command,
        args_json=conn.args,
        env_json=conn.env,
        cwd=conn.cwd,
        enabled=request.enabled,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return mcp_server_read(row, db)


@mcp_router.get(
    "/{server_id}", response_model=MCPServerRead, dependencies=[Depends(require_tenant_admin)]
)
def get_mcp_server(
    server_id: str, tenant_id: str = Query(...), db: Session = Depends(get_session)
) -> MCPServerRead:
    """获取单个 MCP Server 的详情。

    Args:
        server_id: MCP Server ID。
        tenant_id: 租户 ID。
        db: 数据库会话。

    Returns:
        MCPServerRead: MCP Server 视图对象。

    Raises:
        HTTPException: Server 不存在时抛出 404。
    """
    row = _get_mcp_server(db, tenant_id, server_id)
    return mcp_server_read(row, db)


@mcp_router.put("/{server_id}", response_model=MCPServerRead)
def update_mcp_server(
    server_id: str,
    request: MCPServerUpdateRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> MCPServerRead:
    """更新 MCP Server 配置。

    需要开放广场管理员权限。

    Args:
        server_id: MCP Server ID。
        request: 更新请求体。
        db: 数据库会话。
        current_user: 当前登录用户。

    Returns:
        MCPServerRead: 更新后的 MCP Server 视图对象。

    Raises:
        HTTPException: Server 不存在或权限不足时抛出。
    """
    row = _get_mcp_server(db, request.tenant_id, server_id)
    ensure_open_gallery_admin(request.tenant_id, current_user)
    conn = request.connection
    row.name = request.name
    row.display_name = request.display_name
    row.description = request.description
    row.bucket = _normalize_bucket(request.bucket) if request.bucket else "MCP 工具"
    row.transport = conn.transport
    row.url = conn.url
    row.headers_json = conn.headers
    row.command = conn.command
    row.args_json = conn.args
    row.env_json = conn.env
    row.cwd = conn.cwd
    row.enabled = request.enabled
    row.updated_at = utc_now()
    db.add(row)
    db.commit()
    db.refresh(row)
    return mcp_server_read(row, db)


@mcp_router.delete("/{server_id}")
def delete_mcp_server(
    server_id: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = None,
    remove_tools: bool = Query(default=True),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    """删除 MCP Server，可选择同时删除其关联的工具。

    需要系统级总管权限和开放广场管理员权限。

    Args:
        server_id: MCP Server ID。
        tenant_id: 租户 ID。
        db: 数据库会话。
        agent_id: 可选的 agent ID。
        remove_tools: 是否同时删除关联工具，默认 True。
        current_user: 当前登录用户。

    Returns:
        dict[str, str]: 操作结果 {"status": "deleted"}。

    Raises:
        HTTPException: Server 不存在或权限不足时抛出。
    """
    require_overall_agent(db, tenant_id, agent_id)
    ensure_open_gallery_admin(tenant_id, current_user)
    row = _get_mcp_server(db, tenant_id, server_id)
    if remove_tools:
        tools = db.exec(select(Tool).where(Tool.mcp_server_id == server_id)).all()
        for tool in tools:
            db.delete(tool)
    db.delete(row)
    db.commit()
    return {"status": "deleted"}


@mcp_router.post("/discover", response_model=MCPDiscoverResponse)
def discover_mcp_tools_adhoc(
    request: MCPDiscoverRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> MCPDiscoverResponse:
    """未保存 Server 时，用连接配置直接探测 tools/list。"""
    ensure_current_user_tenant(request.tenant_id, current_user)
    ensure_tenant(db, request.tenant_id)
    if request.connection is None:
        return MCPDiscoverResponse(
            success=False,
            error=ToolError(code="MISSING_CONNECTION", message="缺少 MCP 连接配置。"),
        )
    return _discover_response(request.connection)


@mcp_router.post("/{server_id}/discover", response_model=MCPDiscoverResponse)
def discover_mcp_tools(
    server_id: str,
    request: MCPDiscoverRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> MCPDiscoverResponse:
    """已保存 Server：拉取 tools/list，并标注哪些已导入为 Tool。"""
    row = _get_mcp_server(db, request.tenant_id, server_id)
    ensure_open_gallery_admin(request.tenant_id, current_user)
    connection = request.connection or _server_connection(row)
    response = _discover_response(connection)
    if response.success:
        row.discovered_tools_json = [tool.model_dump() for tool in response.tools]
        row.updated_at = utc_now()
        db.add(row)
        db.commit()
        existing = _server_tools_by_leaf_name(db, server_id)
        for tool in response.tools:
            match = existing.get(tool.name)
            if match is not None:
                tool.imported = True
                tool.tool_id = match.id
                tool.enabled = match.enabled
    return response


@mcp_router.post("/{server_id}/sync", response_model=MCPSyncResponse)
def sync_mcp_tools(
    server_id: str,
    request: MCPSyncRequest,
    db: Session = Depends(get_session),
    agent_id: str | None = None,
    current_user: User = Depends(get_current_user),
) -> MCPSyncResponse:
    """把发现到的工具落成 Tool 行（新建/更新 schema），可选择导入的子集。"""
    row = _get_mcp_server(db, request.tenant_id, server_id)
    ensure_open_gallery_admin(request.tenant_id, current_user)
    connection = _server_connection(row)
    discovery = _discover_response(connection)
    if not discovery.success:
        return MCPSyncResponse(success=False, error=discovery.error)

    row.discovered_tools_json = [tool.model_dump() for tool in discovery.tools]
    row.last_synced_at = utc_now()
    row.updated_at = utc_now()

    selected = set(request.tool_names or [])
    existing = _server_tools_by_leaf_name(db, server_id)
    imported: list[str] = []
    updated: list[str] = []
    touched_tool_ids: list[str] = []

    for tool in discovery.tools:
        if selected and tool.name not in selected:
            continue
        current = existing.get(tool.name)
        if current is None:
            new_row = Tool(
                tenant_id=row.tenant_id,
                name=_scoped_tool_name(row.name, tool.name),
                display_name=tool.name,
                description=tool.description,
                bucket=row.bucket or "MCP 工具",
                tool_type="mcp",
                method="POST",
                url=f"mcp://{row.name}/{tool.name}",
                headers_json={},
                auth_json={},
                config_json={"tool": tool.name},
                input_schema=tool.input_schema or {},
                output_schema=tool.output_schema or {},
                allowed_skills_json=[],
                mcp_server_id=row.id,
                enabled=True,
            )
            db.add(new_row)
            db.flush()
            touched_tool_ids.append(new_row.id)
            imported.append(tool.name)
        else:
            current.description = tool.description or current.description
            current.input_schema = tool.input_schema or current.input_schema
            current.output_schema = tool.output_schema or current.output_schema
            current.config_json = {"tool": tool.name}
            current.updated_at = utc_now()
            db.add(current)
            touched_tool_ids.append(current.id)
            updated.append(tool.name)

    # 与 create_tool 一致：按当前 agent 范围绑定——员工范围内只对该员工私有可见，
    # 否则落到工具广场（open gallery），所有人可见。已存在的工具也一并补绑定，
    # 避免「先在广场导入，再切到员工同步」时员工侧仍然看不到。
    agent = get_agent(db, row.tenant_id, agent_id)
    creator_metadata = user_creator_metadata(current_user)
    for tool_id in touched_tool_ids:
        if agent and not agent.is_overall:
            ensure_private_resource_binding(
                db,
                row.tenant_id,
                agent.id,
                "tool",
                tool_id,
                "active",
                metadata_json=creator_metadata,
            )
        else:
            ensure_open_gallery_binding(
                db,
                row.tenant_id,
                "tool",
                tool_id,
                "active",
                metadata_json=creator_metadata,
            )

    db.add(row)
    db.commit()
    return MCPSyncResponse(success=True, imported=imported, updated=updated, removed=[])


def _discover_response(connection: MCPServerConnection) -> MCPDiscoverResponse:
    """调用 MCP Client 拉取工具列表并封装为发现响应。

    Args:
        connection: MCP Server 连接配置。

    Returns:
        MCPDiscoverResponse: 发现结果，成功时含工具列表，失败时含错误信息。
    """
    config = _connection_to_client_config(connection)
    try:
        tools = list_mcp_tools(config, timeout_seconds=get_settings().tool_timeout_seconds)
    except MCPClientError as exc:
        return MCPDiscoverResponse(
            success=False,
            error=ToolError(code="MCP_DISCOVER_ERROR", message=str(exc)),
        )
    except Exception as exc:  # noqa: BLE001
        return MCPDiscoverResponse(
            success=False,
            error=ToolError(code="MCP_DISCOVER_UNEXPECTED", message=str(exc)),
        )
    return MCPDiscoverResponse(
        success=True,
        tools=[
            MCPDiscoveredTool(
                name=item.get("name", ""),
                description=item.get("description", ""),
                input_schema=item.get("input_schema", {}),
                output_schema=item.get("output_schema", {}),
            )
            for item in tools
            if item.get("name")
        ],
    )


def _server_tools_by_leaf_name(db: Session, server_id: str) -> dict[str, Tool]:
    """按叶子工具名（config_json.tool）索引某 MCP Server 下已导入的工具。

    Args:
        db: 数据库会话。
        server_id: MCP Server ID。

    Returns:
        dict[str, Tool]: 叶子工具名到 Tool 行的映射。
    """
    rows = db.exec(select(Tool).where(Tool.mcp_server_id == server_id)).all()
    result: dict[str, Tool] = {}
    for row in rows:
        leaf = str((row.config_json or {}).get("tool") or "").strip()
        if leaf:
            result[leaf] = row
    return result


def _scoped_tool_name(server_name: str, tool_name: str) -> str:
    """生成带 Server 前缀的工具全名（格式：server.tool）。

    Args:
        server_name: MCP Server 名称。
        tool_name: 叶子工具名。

    Returns:
        str: 作用域内的工具全名。
    """
    return f"{server_name}.{tool_name}"


def _get_mcp_server(db: Session, tenant_id: str, server_id: str) -> MCPServer:
    """根据 ID 查找 MCP Server 行，校验租户归属。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。
        server_id: MCP Server ID。

    Returns:
        MCPServer: MCP Server 数据库行。

    Raises:
        HTTPException: 租户或 Server 不存在时抛出 404。
    """
    ensure_tenant(db, tenant_id)
    row = db.get(MCPServer, server_id)
    if not row or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="MCP server not found")
    return row
