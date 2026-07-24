"""通用技能（General Skills）API 模块。

提供基于 Markdown 驱动的通用技能管理，支持从多种来源导入和运行：

1. **直接导入**（``POST /import``）：从 Markdown 文本或文件列表导入技能。
2. **远程导入**（``POST /import-skillhub``、``/import-clawhub``）：
   从 ClawHub/SkillHub 等开源平台下载技能包（zip 或 Markdown）。
3. **包上传**（``POST /import-package``）：上传本地 zip 或 Markdown 文件。
4. **GitHub 导入**：支持 GitHub 仓库目录、blob、archive 等多种 URL 格式。

导入后的技能支持：列表、详情、发布（publish）、归档（archive）、删除、
运行（run）和流式运行（run/stream）。

远程下载支持重定向追踪、HTML 页面解析、GitHub API 目录遍历和 zip 归档下载。
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
import zipfile
import base64
import binascii
from collections.abc import Iterator
from html import unescape
from io import BytesIO
from types import SimpleNamespace
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urljoin, urlparse
from urllib.request import Request, urlopen

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlmodel import Session, select

from app.agents.branching import (
    ensure_open_gallery_binding,
    ensure_private_resource_binding,
    get_agent,
    hide_open_gallery_binding,
    is_bound_resource_visible_for_agent,
    is_open_gallery_resource,
    mark_resource_open_gallery,
    mark_resource_private_for_agent,
    metadata_preserving_creator,
    require_overall_agent,
    user_creator_metadata,
)
from app.db import get_session
from app.db.models import AgentResourceBinding, GeneralSkill, ModelConfig, User, utc_now
from app.general_skills import (
    GeneralSkillClawHubImportRequest,
    GeneralSkillImportRequest,
    GeneralSkillPackageUploadRequest,
    GeneralSkillRead,
    GeneralSkillRunRequest,
    GeneralSkillRunResponse,
)
from app.general_skills.schema import GeneralSkillFile
from app.general_skills.runner import GeneralSkillRunner
from app.llm.model_config_resolver import resolve_model_config_for_runtime
from app.security.auth import get_current_user
from app.security.permissions import (
    ensure_agent_scope_manager,
    ensure_open_gallery_admin,
    require_agent_scope_viewer,
)
from app.security.tenant import ensure_tenant

router = APIRouter(
    prefix="/api/enterprise/general-skills",
    tags=["enterprise:general-skills"],
    dependencies=[Depends(get_current_user)],
)

MAX_CLAWHUB_PACKAGE_BYTES = 96 * 1024 * 1024
MAX_CLAWHUB_FILE_BYTES = 2 * 1024 * 1024
MAX_CLAWHUB_FILES = 240
REMOTE_SKILL_DOWNLOAD_TIMEOUT_SECONDS = 120
GENERAL_SKILL_STREAM_IDLE_TIMEOUT_SECONDS = 120
GITHUB_HOSTS = {"github.com", "www.github.com"}
RAW_GITHUB_HOST = "raw.githubusercontent.com"
CLAWHUB_HOSTS = {"clawhub.ai", "www.clawhub.ai"}
SKILLHUB_HOSTS = {"skillhub.ai", "www.skillhub.ai"}
REMOTE_SKILLHUB_HOSTS = CLAWHUB_HOSTS | SKILLHUB_HOSTS
CLAWHUB_DOWNLOAD_ENDPOINT = "https://wry-manatee-359.convex.site/api/v1/download"


def _agent_id_or_none(agent_id: object | None) -> str | None:
    """将入参中的 agent_id 规整为有效的字符串或 None。

    Args:
        agent_id: 原始 agent 标识，可能为空字符串或非字符串类型。

    Returns:
        当 agent_id 为非空字符串时原样返回，否则返回 ``None``。
    """
    return agent_id if isinstance(agent_id, str) and agent_id else None


def general_skill_read(row: GeneralSkill, status_override: str | None = None) -> GeneralSkillRead:
    """将 GeneralSkill 数据库记录转换为 API 响应模型。

    Args:
        row: 通用技能的数据库行记录。
        status_override: 可选的状态覆盖值，用于在 Agent 私有作用域下
            动态计算发布/归档状态。

    Returns:
        可序列化的 ``GeneralSkillRead`` 响应对象。
    """
    return GeneralSkillRead(
        id=row.id,
        tenant_id=row.tenant_id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        homepage=row.homepage,
        skill_markdown=row.skill_markdown,
        skill_files=[
            GeneralSkillFile.model_validate(item) for item in _skill_files_or_markdown(row)
        ],
        metadata=dict(row.metadata_json or {}),
        status=status_override or row.status,
        permissions=row.permissions_json or {},
        runtime_config=row.runtime_config_json or {},
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


@router.post("/import", response_model=GeneralSkillRead)
def import_general_skill(
    request: GeneralSkillImportRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillRead:
    """从 Markdown 文本或文件列表导入通用技能（支持新建和更新）。

    当请求中携带 ``original_slug`` 时执行更新逻辑，否则创建新技能。
    技能可绑定到特定 Agent（私有作用域）或发布到开放画廊（公共作用域）。

    Args:
        request: 导入请求体，包含租户、slug、Markdown 内容等。
        db: 数据库会话。
        current_user: 当前登录用户。

    Returns:
        创建或更新后的通用技能响应对象。

    Raises:
        HTTPException 404: 指定更新的技能不存在或当前 Agent 不可见。
        HTTPException 400: slug 格式非法或 slug 不可修改。
        HTTPException 409: slug 冲突（已存在同名 slug）。
    """
    ensure_tenant(db, request.tenant_id)
    files = _normalize_skill_files(request.files, request.markdown)
    markdown = _skill_markdown_from_files(files)
    parsed_metadata = _parse_skill_metadata(markdown)
    metadata = user_creator_metadata(current_user, parsed_metadata)
    name = (
        _optional_text(request.name)
        or _metadata_text(metadata, "name", "title")
        or "未命名通用技能"
    )
    slug = _optional_text(request.slug) or _metadata_text(metadata, "slug", "id") or _slugify(name)
    description = _optional_text(request.description) or _metadata_text(
        metadata, "description", "summary"
    )
    homepage = _optional_text(request.homepage) or _metadata_text(
        metadata, "homepage", "url", "source"
    )
    _validate_slug(slug)
    lookup_slug = _optional_text(request.original_slug)
    agent_id = _agent_id_or_none(request.agent_id)
    agent = ensure_agent_scope_manager(db, request.tenant_id, agent_id, current_user)
    is_private_agent_scope = bool(agent and not agent.is_overall)
    if not is_private_agent_scope:
        ensure_open_gallery_admin(request.tenant_id, current_user)
    row = None
    if lookup_slug:
        row = db.exec(
            select(GeneralSkill).where(
                GeneralSkill.tenant_id == request.tenant_id,
                GeneralSkill.slug == lookup_slug,
            )
        ).first()
        if not row:
            raise HTTPException(status_code=404, detail="General skill to update was not found")
        if slug != row.slug:
            raise HTTPException(status_code=400, detail="General skill slug cannot be modified")
        if is_private_agent_scope:
            if is_open_gallery_resource(db, request.tenant_id, "general_skill", row):
                row = None
                slug = _unique_slug(db, request.tenant_id, slug)
            elif not _general_skill_editable_by_agent(db, request.tenant_id, agent.id, row):
                raise HTTPException(
                    status_code=404, detail="General skill not visible to this agent"
                )
    else:
        conflict = db.exec(
            select(GeneralSkill).where(
                GeneralSkill.tenant_id == request.tenant_id,
                GeneralSkill.slug == slug,
            )
        ).first()
        if conflict:
            if is_private_agent_scope and is_open_gallery_resource(
                db,
                request.tenant_id,
                "general_skill",
                conflict,
            ):
                slug = _unique_slug(db, request.tenant_id, slug)
            else:
                raise HTTPException(status_code=409, detail="General skill slug already exists")
    now = utc_now()
    if row:
        metadata = metadata_preserving_creator(row.metadata_json, parsed_metadata)
        if slug != row.slug:
            conflict = db.exec(
                select(GeneralSkill).where(
                    GeneralSkill.tenant_id == request.tenant_id,
                    GeneralSkill.slug == slug,
                )
            ).first()
            if conflict:
                if is_private_agent_scope and is_open_gallery_resource(
                    db,
                    request.tenant_id,
                    "general_skill",
                    conflict,
                ):
                    slug = _unique_slug(db, request.tenant_id, slug)
                else:
                    raise HTTPException(status_code=409, detail="General skill slug already exists")
        row.slug = slug
        row.name = name
        row.description = description
        row.homepage = homepage
        row.skill_markdown = markdown
        row.skill_files_json = [file.model_dump(mode="json") for file in files]
        row.metadata_json = metadata
        row.status = request.status
        row.updated_at = now
    else:
        row = GeneralSkill(
            tenant_id=request.tenant_id,
            slug=slug,
            name=name,
            description=description,
            homepage=homepage,
            skill_markdown=markdown,
            skill_files_json=[file.model_dump(mode="json") for file in files],
            metadata_json=metadata,
            status=request.status,
            permissions_json={"network": True, "python": True},
            runtime_config_json={"runtime": "python", "timeout_seconds": 12},
            created_at=now,
            updated_at=now,
        )
    if is_private_agent_scope:
        mark_resource_private_for_agent(row, agent.id, metadata)
    else:
        mark_resource_open_gallery(row, metadata)
    db.add(row)
    db.flush()
    if is_private_agent_scope:
        ensure_private_resource_binding(
            db,
            request.tenant_id,
            agent.id,
            "general_skill",
            row.id,
            "active" if request.status == "published" else "inactive",
            metadata_json=metadata,
        )
    else:
        ensure_open_gallery_binding(
            db,
            request.tenant_id,
            "general_skill",
            row.id,
            "active" if request.status == "published" else "inactive",
            metadata_json=metadata,
        )
    db.commit()
    db.refresh(row)
    return general_skill_read(row)


@router.post("/import-skillhub", response_model=GeneralSkillRead)
def import_skillhub_skill(
    request: GeneralSkillClawHubImportRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillRead:
    """从 SkillHub/ClawHub 远程平台导入通用技能。

    根据来源 URL 或 slug 下载远程技能包，解析后创建通用技能记录。

    Args:
        request: 远程导入请求体，包含来源地址和可选覆盖字段。
        db: 数据库会话。
        current_user: 当前登录用户。

    Returns:
        导入后的通用技能响应对象。

    Raises:
        HTTPException 400: 来源地址无法解析或下载失败。
    """
    ensure_tenant(db, request.tenant_id)
    raw_files = _load_clawhub_source(request.source)
    files = _normalize_skill_files(raw_files, None)
    return _create_imported_general_skill(
        db,
        tenant_id=request.tenant_id,
        files=files,
        import_source=request.source,
        agent_id=request.agent_id,
        status=request.status,
        name=request.name,
        slug=request.slug,
        description=request.description,
        homepage=request.homepage,
        current_user=current_user,
    )


@router.post("/import-clawhub", response_model=GeneralSkillRead)
def import_clawhub_skill(
    request: GeneralSkillClawHubImportRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillRead:
    """从 ClawHub 平台导入通用技能（``import-skillhub`` 的别名端点）。

    Args:
        request: 远程导入请求体。
        db: 数据库会话。
        current_user: 当前登录用户。

    Returns:
        导入后的通用技能响应对象。
    """
    return import_skillhub_skill(request, db, current_user)


@router.post("/import-package", response_model=GeneralSkillRead)
def import_general_skill_package(
    request: GeneralSkillPackageUploadRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillRead:
    """从上传的 zip 或 Markdown 文件包导入通用技能。

    根据文件扩展名选择解压（zip）或直接读取（Markdown）策略，
    解析后调用统一的导入流程创建技能记录。

    Args:
        request: 包含 base64 编码文件内容和文件名的上传请求体。
        db: 数据库会话。
        current_user: 当前登录用户。

    Returns:
        导入后的通用技能响应对象。

    Raises:
        HTTPException 400: 文件格式不支持、内容为空或超过大小限制。
    """
    ensure_tenant(db, request.tenant_id)
    filename = _clean_source_filename(request.filename)
    data = _decode_base64_payload(request.content_base64)
    if filename.lower().endswith(".zip"):
        raw_files = _files_from_zip(data)
    elif filename.lower().endswith((".md", ".markdown", ".txt")):
        text = _decode_text(data)
        raw_files = [
            GeneralSkillFile(
                path="SKILL.md",
                content=text,
                size=len(data),
                mime_type=_guess_mime_type(filename),
            )
        ]
    else:
        raise HTTPException(
            status_code=400, detail="Uploaded skill package must be a .zip or Markdown file"
        )
    files = _normalize_skill_files(raw_files, None)
    return _create_imported_general_skill(
        db,
        tenant_id=request.tenant_id,
        files=files,
        import_source=f"upload:{filename}",
        agent_id=request.agent_id,
        status=request.status,
        name=request.name,
        slug=request.slug,
        description=request.description,
        homepage=request.homepage,
        current_user=current_user,
    )


def _create_imported_general_skill(
    db: Session,
    *,
    tenant_id: str,
    files: list[GeneralSkillFile],
    import_source: str,
    agent_id: str | None,
    status: str,
    name: str | None = None,
    slug: str | None = None,
    description: str | None = None,
    homepage: str | None = None,
    current_user: object | None = None,
) -> GeneralSkillRead:
    """将已解析的文件列表创建为通用技能记录（远程导入的统一入口）。

    从 Markdown 提取元数据，生成唯一 slug，根据 Agent 作用域设置
    资源可见性与绑定关系。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。
        files: 已规范化处理的技能文件列表。
        import_source: 导入来源标识（如 URL 或 upload 文件名）。
        agent_id: 可选的 Agent ID，用于私有作用域绑定。
        status: 技能初始状态（published / archived 等）。
        name: 可选的名称覆盖。
        slug: 可选的 slug 覆盖。
        description: 可选的描述覆盖。
        homepage: 可选的主页 URL 覆盖。
        current_user: 当前登录用户。

    Returns:
        创建后的通用技能响应对象。

    Raises:
        HTTPException 400: slug 格式非法。
    """
    markdown = _skill_markdown_from_files(files)
    metadata = _parse_skill_metadata(markdown)
    resolved_name = (
        _optional_text(name)
        or _metadata_text(metadata, "name", "title")
        or _source_name(import_source)
    )
    source_slug = _clawhub_slug_from_source(import_source)
    slug_base = (
        _optional_text(slug)
        or _metadata_text(metadata, "slug", "id")
        or source_slug
        or _slugify(resolved_name)
    )
    resolved_slug = _unique_slug(db, tenant_id, slug_base)
    resolved_description = _optional_text(description) or _metadata_text(
        metadata, "description", "summary"
    )
    resolved_homepage = (
        _optional_text(homepage)
        or _metadata_text(metadata, "homepage", "url", "source")
        or _clawhub_homepage_from_source(import_source)
    )
    _validate_slug(resolved_slug)
    now = utc_now()
    resolved_agent_id = _agent_id_or_none(agent_id)
    agent = ensure_agent_scope_manager(db, tenant_id, resolved_agent_id, current_user)
    row = GeneralSkill(
        tenant_id=tenant_id,
        slug=resolved_slug,
        name=resolved_name,
        description=resolved_description,
        homepage=resolved_homepage,
        skill_markdown=markdown,
        skill_files_json=[file.model_dump(mode="json") for file in files],
        metadata_json=user_creator_metadata(
            current_user, {**metadata, "import_source": import_source}
        ),
        status=status,
        permissions_json={"network": True, "python": True},
        runtime_config_json={"runtime": "python", "timeout_seconds": 12},
        created_at=now,
        updated_at=now,
    )
    if not (agent and not agent.is_overall):
        ensure_open_gallery_admin(tenant_id, current_user)
    if agent and not agent.is_overall:
        mark_resource_private_for_agent(row, agent.id, row.metadata_json or {})
    else:
        mark_resource_open_gallery(row, row.metadata_json or {})
    db.add(row)
    db.flush()
    if agent and not agent.is_overall:
        ensure_private_resource_binding(
            db,
            tenant_id,
            agent.id,
            "general_skill",
            row.id,
            "active" if status == "published" else "inactive",
            metadata_json=row.metadata_json or {},
        )
    else:
        ensure_open_gallery_binding(
            db,
            tenant_id,
            "general_skill",
            row.id,
            "active" if status == "published" else "inactive",
            metadata_json=row.metadata_json or {},
        )
    db.commit()
    db.refresh(row)
    return general_skill_read(row)


@router.get(
    "", response_model=list[GeneralSkillRead], dependencies=[Depends(require_agent_scope_viewer)]
)
def list_general_skills(
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = Query(None),
) -> list[GeneralSkillRead]:
    """列出当前租户下的通用技能列表。

    当指定 Agent 且非全局 Agent 时，仅返回该 Agent 绑定的技能，
    并根据绑定状态动态计算发布/归档状态；否则返回开放画廊中的所有技能。

    Args:
        tenant_id: 租户 ID。
        db: 数据库会话。
        agent_id: 可选的 Agent ID，用于过滤私有作用域资源。

    Returns:
        通用技能响应对象列表，按更新时间倒序排列。
    """
    ensure_tenant(db, tenant_id)
    agent_id = _agent_id_or_none(agent_id)
    agent = get_agent(db, tenant_id, agent_id)
    if agent and not agent.is_overall:
        bindings = db.exec(
            select(AgentResourceBinding)
            .where(
                AgentResourceBinding.tenant_id == tenant_id,
                AgentResourceBinding.agent_id == agent.id,
                AgentResourceBinding.resource_type == "general_skill",
            )
            .order_by(AgentResourceBinding.updated_at.desc())
        ).all()
        if not bindings:
            return []
        rows_by_id = {
            row.id: row
            for row in db.exec(
                select(GeneralSkill).where(
                    GeneralSkill.tenant_id == tenant_id,
                    GeneralSkill.id.in_([binding.resource_id for binding in bindings]),
                )
            ).all()
        }
        visible_rows: list[GeneralSkillRead] = []
        for binding in bindings:
            row = rows_by_id.get(binding.resource_id)
            if not row:
                continue
            if not is_bound_resource_visible_for_agent(
                db, tenant_id, "general_skill", row, binding
            ):
                continue
            visible_rows.append(
                general_skill_read(
                    row,
                    status_override=(
                        "published"
                        if binding.status == "active" and row.status == "published"
                        else "archived"
                    ),
                )
            )
        return visible_rows
    rows = db.exec(
        select(GeneralSkill)
        .where(GeneralSkill.tenant_id == tenant_id)
        .order_by(GeneralSkill.updated_at.desc())
    ).all()
    rows = [row for row in rows if is_open_gallery_resource(db, tenant_id, "general_skill", row)]
    return [general_skill_read(row) for row in rows]


@router.get(
    "/{slug}", response_model=GeneralSkillRead, dependencies=[Depends(require_agent_scope_viewer)]
)
def get_general_skill(
    slug: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = Query(None),
) -> GeneralSkillRead:
    """按 slug 获取单个通用技能详情。

    Args:
        slug: 技能的唯一标识 slug。
        tenant_id: 租户 ID。
        db: 数据库会话。
        agent_id: 可选的 Agent ID，用于可见性校验。

    Returns:
        通用技能响应对象。

    Raises:
        HTTPException 404: 技能不存在或当前 Agent 不可见。
    """
    row = _get_general_skill(db, tenant_id, slug)
    _ensure_general_skill_visible(db, tenant_id, row, agent_id)
    return general_skill_read(row)


@router.post("/{slug}/publish", response_model=GeneralSkillRead)
def publish_general_skill(
    slug: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillRead:
    """发布（上线）指定通用技能。

    Agent 私有作用域下将绑定状态置为 active；开放画廊下将技能状态置为 published。

    Args:
        slug: 技能的唯一标识 slug。
        tenant_id: 租户 ID。
        db: 数据库会话。
        agent_id: 可选的 Agent ID。
        current_user: 当前登录用户。

    Returns:
        发布后的通用技能响应对象。

    Raises:
        HTTPException 404: 技能不存在。
        HTTPException 403: 无开放画廊管理员权限。
    """
    row = _get_general_skill(db, tenant_id, slug)
    agent_id = _agent_id_or_none(agent_id)
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        binding = _ensure_general_skill_binding(
            db,
            tenant_id,
            agent.id,
            row.id,
            metadata_json=row.metadata_json or {},
        )
        binding.status = "active"
        binding.updated_at = utc_now()
        db.add(binding)
        db.commit()
        return general_skill_read(row, status_override="published")
    ensure_open_gallery_admin(tenant_id, current_user)
    row.status = "published"
    mark_resource_open_gallery(row, row.metadata_json or {})
    row.updated_at = utc_now()
    db.add(row)
    db.flush()
    ensure_open_gallery_binding(
        db,
        tenant_id,
        "general_skill",
        row.id,
        "active",
        metadata_json=row.metadata_json or {},
    )
    db.commit()
    db.refresh(row)
    return general_skill_read(row)


@router.post("/{slug}/archive", response_model=GeneralSkillRead)
def archive_general_skill(
    slug: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillRead:
    """归档（下线）指定通用技能。

    Agent 私有作用域下将绑定状态置为 inactive；开放画廊下将技能状态置为 archived。

    Args:
        slug: 技能的唯一标识 slug。
        tenant_id: 租户 ID。
        db: 数据库会话。
        agent_id: 可选的 Agent ID。
        current_user: 当前登录用户。

    Returns:
        归档后的通用技能响应对象。

    Raises:
        HTTPException 404: 技能不存在。
        HTTPException 403: 无开放画廊管理员权限。
    """
    row = _get_general_skill(db, tenant_id, slug)
    agent_id = _agent_id_or_none(agent_id)
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        binding = _ensure_general_skill_binding(db, tenant_id, agent.id, row.id)
        binding.status = "inactive"
        binding.updated_at = utc_now()
        db.add(binding)
        db.commit()
        return general_skill_read(row, status_override="archived")
    ensure_open_gallery_admin(tenant_id, current_user)
    row.status = "archived"
    row.updated_at = utc_now()
    db.add(row)
    db.flush()
    ensure_open_gallery_binding(db, tenant_id, "general_skill", row.id, "inactive")
    db.commit()
    db.refresh(row)
    return general_skill_read(row)


@router.delete("/{slug}")
def delete_general_skill(
    slug: str,
    tenant_id: str = Query(...),
    db: Session = Depends(get_session),
    agent_id: str | None = Query(None),
    current_user: User = Depends(get_current_user),
) -> dict[str, str]:
    """删除或隐藏指定通用技能。

    Agent 私有作用域下将绑定状态置为 deleted（仅隐藏）；
    全局 Agent 下隐藏开放画廊绑定；
    超级管理员权限下真正从数据库删除记录。

    Args:
        slug: 技能的唯一标识 slug。
        tenant_id: 租户 ID。
        db: 数据库会话。
        agent_id: 可选的 Agent ID。
        current_user: 当前登录用户。

    Returns:
        包含操作结果状态和 slug 的字典：
        - ``hidden``：已隐藏（Agent 作用域或全局 Agent）。
        - ``deleted``：已从数据库彻底删除（超级管理员）。

    Raises:
        HTTPException 404: 技能不存在或不可见。
        HTTPException 403: 无操作权限。
    """
    agent_id = _agent_id_or_none(agent_id)
    row = _get_general_skill(db, tenant_id, slug)
    agent = ensure_agent_scope_manager(db, tenant_id, agent_id, current_user)
    if agent and not agent.is_overall:
        binding = _ensure_general_skill_binding(db, tenant_id, agent.id, row.id)
        binding.status = "deleted"
        binding.updated_at = utc_now()
        db.add(binding)
        db.commit()
        return {"status": "hidden", "slug": slug}
    if agent and agent.is_overall:
        if not is_open_gallery_resource(db, tenant_id, "general_skill", row):
            raise HTTPException(status_code=404, detail="General skill not visible in open gallery")
        ensure_open_gallery_admin(tenant_id, current_user)
        hide_open_gallery_binding(db, tenant_id, "general_skill", row.id)
        db.commit()
        return {"status": "hidden", "slug": slug}

    require_overall_agent(db, tenant_id, agent_id)
    ensure_open_gallery_admin(tenant_id, current_user)
    db.delete(row)
    db.commit()
    return {"status": "deleted", "slug": slug}


@router.post("/{slug}/run", response_model=GeneralSkillRunResponse)
def run_general_skill(
    slug: str,
    request: GeneralSkillRunRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> GeneralSkillRunResponse:
    """同步运行指定通用技能并返回执行结果。

    Args:
        slug: 技能的唯一标识 slug。
        request: 运行请求体，包含查询内容、模型配置等。
        db: 数据库会话。
        current_user: 当前登录用户。

    Returns:
        技能运行结果响应对象。

    Raises:
        HTTPException 400: 技能未发布（status != published）。
        HTTPException 403: 无 Agent 作用域查看权限。
        HTTPException 404: 技能不存在或模型配置未找到。
    """
    skill = _get_general_skill(db, request.tenant_id, slug)
    if skill.status != "published":
        raise HTTPException(status_code=400, detail="General skill is not published")
    require_agent_scope_viewer(request.tenant_id, request.agent_id, current_user, db)
    _ensure_general_skill_visible(db, request.tenant_id, skill, request.agent_id)
    model_config = _get_request_model(db, request.tenant_id, request.model_config_id)
    return GeneralSkillRunner().run(
        skill, request.query, model_config, current_user.id, request.max_attempts
    )


@router.post("/{slug}/run/stream")
def run_general_skill_stream(
    slug: str,
    request: GeneralSkillRunRequest,
    db: Session = Depends(get_session),
    current_user: User = Depends(get_current_user),
) -> StreamingResponse:
    """以 SSE（Server-Sent Events）流式运行指定通用技能。

    在后台线程中执行技能运行器，通过队列将 trace 事件实时推送至客户端，
    支持心跳保活和空闲超时检测。

    Args:
        slug: 技能的唯一标识 slug。
        request: 运行请求体，包含查询内容、模型配置等。
        db: 数据库会话。
        current_user: 当前登录用户。

    Returns:
        ``text/event-stream`` 类型的流式响应。

    Raises:
        HTTPException 400: 技能未发布。
        HTTPException 403: 无 Agent 作用域查看权限。
        HTTPException 404: 技能不存在或模型配置未找到。
    """
    skill = _get_general_skill(db, request.tenant_id, slug)
    if skill.status != "published":
        raise HTTPException(status_code=400, detail="General skill is not published")
    require_agent_scope_viewer(request.tenant_id, request.agent_id, current_user, db)
    _ensure_general_skill_visible(db, request.tenant_id, skill, request.agent_id)
    model_config = _get_request_model(db, request.tenant_id, request.model_config_id)
    skill_snapshot = _general_skill_snapshot(skill)
    model_snapshot = model_config

    def stream_events() -> Iterator[str]:
        events: queue.Queue[tuple[str, dict[str, object]] | None] = queue.Queue()

        def sink(item: dict[str, object]) -> None:
            events.put(("trace", item))

        def worker() -> None:
            try:
                response = GeneralSkillRunner().run(
                    skill_snapshot,
                    request.query,
                    model_snapshot,
                    current_user.id,
                    request.max_attempts,
                    sink,
                )
                events.put(("complete", response.model_dump(mode="json")))
            except Exception as exc:  # pragma: no cover - defensive stream boundary
                events.put(("error", {"message": str(exc)}))
            finally:
                events.put(None)

        threading.Thread(target=worker, daemon=True).start()
        yield _sse(
            "stream_started",
            {"skill_slug": skill_snapshot.slug, "max_attempts": request.max_attempts},
        )
        last_worker_event_at = time.monotonic()
        while True:
            try:
                item = events.get(timeout=5)
            except queue.Empty:
                if (
                    time.monotonic() - last_worker_event_at
                    > GENERAL_SKILL_STREAM_IDLE_TIMEOUT_SECONDS
                ):
                    yield _sse(
                        "error",
                        {
                            "message": "通用技能运行超时，请检查模型配置或稍后重试。",
                            "code": "general_skill_stream_timeout",
                        },
                    )
                    return
                yield _sse("heartbeat", {"phase": "running"})
                continue
            if item is None:
                return
            last_worker_event_at = time.monotonic()
            event, payload = item
            yield _sse(event, payload)

    return StreamingResponse(stream_events(), media_type="text/event-stream")


def _get_general_skill(db: Session, tenant_id: str, slug: str) -> GeneralSkill:
    """按 slug 从数据库查询通用技能记录。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。
        slug: 技能的唯一标识 slug。

    Returns:
        匹配的 GeneralSkill 数据库行。

    Raises:
        HTTPException 404: 技能不存在。
    """
    ensure_tenant(db, tenant_id)
    row = db.exec(
        select(GeneralSkill).where(GeneralSkill.tenant_id == tenant_id, GeneralSkill.slug == slug)
    ).first()
    if not row:
        raise HTTPException(status_code=404, detail="General skill not found")
    return row


def _ensure_general_skill_visible(
    db: Session,
    tenant_id: str,
    row: GeneralSkill,
    agent_id: str | None,
) -> None:
    """校验当前 Agent 是否有权查看指定通用技能。

    全局 Agent 或无 Agent 时检查开放画廊可见性；
    私有 Agent 时检查绑定记录及其可见性。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。
        row: 待校验的通用技能记录。
        agent_id: 可选的 Agent ID。

    Raises:
        HTTPException 404: 技能不在开放画廊或当前 Agent 不可见。
    """
    agent = get_agent(db, tenant_id, _agent_id_or_none(agent_id))
    if not agent or agent.is_overall:
        if is_open_gallery_resource(db, tenant_id, "general_skill", row):
            return
        raise HTTPException(status_code=404, detail="General skill not visible in open gallery")
    binding = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent.id,
            AgentResourceBinding.resource_type == "general_skill",
            AgentResourceBinding.resource_id == row.id,
        )
    ).first()
    if not binding or not is_bound_resource_visible_for_agent(
        db, tenant_id, "general_skill", row, binding
    ):
        raise HTTPException(status_code=404, detail="General skill not visible to this agent")


def _ensure_general_skill_binding(
    db: Session,
    tenant_id: str,
    agent_id: str,
    general_skill_id: str,
    metadata_json: dict[str, object] | None = None,
) -> AgentResourceBinding:
    """获取或创建 Agent 与通用技能之间的资源绑定记录。

    若绑定已存在则合并元数据后返回现有记录；否则创建新的绑定。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。
        agent_id: Agent ID。
        general_skill_id: 通用技能记录 ID。
        metadata_json: 可选的附加元数据。

    Returns:
        Agent 资源绑定记录（``AgentResourceBinding``）。
    """
    row = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent_id,
            AgentResourceBinding.resource_type == "general_skill",
            AgentResourceBinding.resource_id == general_skill_id,
        )
    ).first()
    metadata = {
        **(metadata_json or {}),
        "scope": "agent_private",
        "visibility": "agent_private",
        "owner_agent_id": agent_id,
        "created_from_agent": True,
    }
    if row:
        merged_metadata = {
            **(row.metadata_json or {}),
            **metadata,
        }
        row.metadata_json = metadata_preserving_creator(
            row.metadata_json,
            merged_metadata,
        )
        return row
    row = AgentResourceBinding(
        tenant_id=tenant_id,
        agent_id=agent_id,
        resource_type="general_skill",
        resource_id=general_skill_id,
        status="active",
        metadata_json=metadata,
    )
    db.add(row)
    db.flush()
    return row


def _general_skill_editable_by_agent(
    db: Session, tenant_id: str, agent_id: str, row: GeneralSkill
) -> bool:
    """判断指定 Agent 是否有权编辑该通用技能。

    通过元数据中的 owner_agent_id 或有效的非删除绑定记录来判断。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。
        agent_id: Agent ID。
        row: 通用技能记录。

    Returns:
        可编辑返回 ``True``，否则 ``False``。
    """
    metadata = row.metadata_json or {}
    if metadata.get("owner_agent_id") == agent_id and metadata.get("scope") == "agent_private":
        return True
    binding = db.exec(
        select(AgentResourceBinding).where(
            AgentResourceBinding.tenant_id == tenant_id,
            AgentResourceBinding.agent_id == agent_id,
            AgentResourceBinding.resource_type == "general_skill",
            AgentResourceBinding.resource_id == row.id,
            AgentResourceBinding.status != "deleted",
        )
    ).first()
    return bool(
        binding
        and not is_open_gallery_resource(db, tenant_id, "general_skill", row)
        and is_bound_resource_visible_for_agent(db, tenant_id, "general_skill", row, binding)
    )


def _get_default_model(db: Session, tenant_id: str) -> ModelConfig:
    """获取租户默认的模型配置并解析运行时参数。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。

    Returns:
        解析后的 ``ModelConfig`` 运行时配置。

    Raises:
        HTTPException 400: 未找到默认模型配置。
    """
    model_config = db.exec(
        select(ModelConfig).where(
            ModelConfig.tenant_id == tenant_id,
            ModelConfig.is_default == True,  # noqa: E712
            ModelConfig.enabled == True,  # noqa: E712
        )
    ).first()
    if not model_config:
        raise HTTPException(status_code=400, detail="No default model config")
    return _model_runtime_config(db, tenant_id, model_config)


def _get_request_model(
    db: Session, tenant_id: str, model_config_id: str | None = None
) -> ModelConfig:
    """根据请求中的模型配置 ID 获取模型，无指定时使用默认模型。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。
        model_config_id: 可选的模型配置 ID。

    Returns:
        解析后的 ``ModelConfig`` 运行时配置。

    Raises:
        HTTPException 404: 指定的模型配置不存在或未启用。
    """
    if not model_config_id:
        return _get_default_model(db, tenant_id)
    model_config = db.get(ModelConfig, model_config_id)
    if not model_config or model_config.tenant_id != tenant_id or not model_config.enabled:
        raise HTTPException(status_code=404, detail="Model config not found")
    return _model_runtime_config(db, tenant_id, model_config)


def _general_skill_snapshot(row: GeneralSkill) -> SimpleNamespace:
    """将 GeneralSkill 记录快照为轻量的命名空间对象，供后台线程使用。

    避免线程中持有 SQLAlchemy 会话依赖的 ORM 对象。

    Args:
        row: 通用技能数据库行记录。

    Returns:
        包含技能全部字段的 ``SimpleNamespace`` 快照。
    """
    return SimpleNamespace(
        tenant_id=row.tenant_id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        homepage=row.homepage,
        skill_markdown=row.skill_markdown,
        skill_files_json=_skill_files_or_markdown(row),
        metadata_json=row.metadata_json or {},
        permissions_json=row.permissions_json or {},
        runtime_config_json=row.runtime_config_json or {},
        status=row.status,
    )


def _model_runtime_config(db: Session, tenant_id: str, row: ModelConfig):
    """将模型配置解析为运行时可用的配置对象。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。
        row: 原始模型配置记录。

    Returns:
        解析后的运行时模型配置。
    """
    return resolve_model_config_for_runtime(db, tenant_id, row.id)


def _required_text(value: str | None, field: str) -> str:
    """校验文本字段非空并返回去除首尾空白后的值。

    Args:
        value: 原始文本值。
        field: 字段名称，用于错误提示。

    Returns:
        去除首尾空白后的非空字符串。

    Raises:
        HTTPException 400: 文本为空。
    """
    cleaned = (value or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail=f"General skill {field} cannot be empty")
    return cleaned


def _optional_text(value: str | None) -> str | None:
    """去除文本首尾空白，空字符串转为 None。

    Args:
        value: 原始文本值。

    Returns:
        非空白字符串或 ``None``。
    """
    cleaned = (value or "").strip()
    return cleaned or None


def _normalize_skill_files(
    requested_files: list[GeneralSkillFile],
    markdown: str | None,
) -> list[GeneralSkillFile]:
    """规范化技能文件列表，确保包含 SKILL.md 并去除公共目录前缀。

    若未提供文件列表，则从 markdown 参数构建单文件列表。
    若 SKILL.md 位于子目录中，会自动剥离该公共前缀。

    Args:
        requested_files: 请求中携带的文件列表。
        markdown: 备用的 Markdown 文本（当文件列表为空时使用）。

    Returns:
        规范化后的 ``GeneralSkillFile`` 列表。

    Raises:
        HTTPException 400: 缺少 SKILL.md 或 Markdown 为空。
    """
    if not requested_files:
        content = _required_text(markdown, "markdown")
        return [
            GeneralSkillFile(path="SKILL.md", content=content, size=len(content.encode("utf-8")))
        ]
    cleaned_files: list[GeneralSkillFile] = []
    for file in requested_files:
        path = _clean_package_path(file.path)
        content = file.content or ""
        cleaned_files.append(
            GeneralSkillFile(
                path=path,
                content=content,
                size=file.size if file.size is not None else len(content.encode("utf-8")),
                mime_type=file.mime_type,
            )
        )
    skill_file = _find_skill_file(cleaned_files)
    if not skill_file:
        raise HTTPException(status_code=400, detail="General skill folder must contain SKILL.md")
    base_dir = skill_file.path.rsplit("/", 1)[0] if "/" in skill_file.path else ""
    if not base_dir:
        return cleaned_files
    normalized: list[GeneralSkillFile] = []
    prefix = f"{base_dir}/"
    for file in cleaned_files:
        if file.path == base_dir or not file.path.startswith(prefix):
            continue
        normalized.append(file.model_copy(update={"path": file.path[len(prefix) :]}))
    return normalized


def _clean_package_path(path: str) -> str:
    """清理包内文件路径，统一为正斜杠并拒绝路径穿越。

    Args:
        path: 原始文件路径。

    Returns:
        清理后的规范化路径（正斜杠分隔）。

    Raises:
        HTTPException 400: 路径包含 ``..`` 或为空等非法情况。
    """
    cleaned = str(path or "").replace("\\", "/").strip().strip("/")
    parts = [part for part in cleaned.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise HTTPException(status_code=400, detail=f"Invalid general skill file path: {path}")
    return "/".join(parts)


def _find_skill_file(files: list[GeneralSkillFile]) -> GeneralSkillFile | None:
    """在文件列表中查找 SKILL.md 文件（不区分大小写）。

    Args:
        files: 技能文件列表。

    Returns:
        匹配的 ``GeneralSkillFile``，未找到返回 ``None``。
    """
    return next(
        (file for file in files if file.path.rsplit("/", 1)[-1].lower() == "skill.md"), None
    )


def _skill_markdown_from_files(files: list[GeneralSkillFile]) -> str:
    """从文件列表中提取 SKILL.md 的 Markdown 内容。

    Args:
        files: 技能文件列表。

    Returns:
        SKILL.md 的文本内容。

    Raises:
        HTTPException 400: 缺少 SKILL.md 或内容为空。
    """
    skill_file = _find_skill_file(files)
    if not skill_file or not skill_file.content.strip():
        raise HTTPException(status_code=400, detail="General skill SKILL.md cannot be empty")
    return skill_file.content


def _skill_files_or_markdown(row: GeneralSkill) -> list[dict[str, object]]:
    """获取技能的文件列表，无文件列表时从 skill_markdown 字段降级构建。

    Args:
        row: 通用技能数据库行记录。

    Returns:
        文件字典列表（包含 path / content / size 字段）。
    """
    files = row.skill_files_json or []
    if files:
        return files
    return [
        {
            "path": "SKILL.md",
            "content": row.skill_markdown,
            "size": len(row.skill_markdown.encode("utf-8")),
        }
    ]


def _parse_skill_metadata(markdown: str) -> dict[str, object]:
    """从 Markdown 的 YAML front matter 中解析技能元数据。

    解析 ``---`` 包裹的头部区域，提取 key: value 形式的元数据键值对。

    Args:
        markdown: 技能的 Markdown 全文。

    Returns:
        解析得到的元数据字典，无 front matter 时返回空字典。
    """
    lines = markdown.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    metadata: dict[str, object] = {}
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == "---":
            return metadata
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        if not key:
            continue
        metadata[key] = _parse_metadata_value(value.strip())
    return metadata


def _parse_metadata_value(value: str) -> object:
    """解析单个元数据值，支持列表和字符串。

    以方括号包裹的值解析为列表，否则返回去除引号的字符串。

    Args:
        value: 元数据原始值文本。

    Returns:
        解析后的字符串或字符串列表。
    """
    cleaned = value.strip().strip("'\"")
    if cleaned.startswith("[") and cleaned.endswith("]"):
        return [item.strip().strip("'\"") for item in cleaned[1:-1].split(",") if item.strip()]
    return cleaned


def _metadata_text(metadata: dict[str, object], *keys: str) -> str | None:
    """按优先顺序从元数据中提取首个非空字符串值。

    Args:
        metadata: 元数据字典。
        *keys: 候选键名，按优先级排序。

    Returns:
        第一个匹配的非空字符串值，均不匹配返回 ``None``。
    """
    for key in keys:
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _slugify(value: str) -> str:
    """将任意文本转换为 URL 友好的 slug。

    非字母数字及 ``-``/``_`` 字符替换为 ``-``，并去除首尾连字符。

    Args:
        value: 原始文本。

    Returns:
        小写化的 slug 字符串，空值时回退为 ``general-skill``。
    """
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-_")
    return slug or "general-skill"


def _unique_slug(db: Session, tenant_id: str, base_slug: str) -> str:
    """生成租户内唯一的 slug，冲突时追加数字后缀。

    Args:
        db: 数据库会话。
        tenant_id: 租户 ID。
        base_slug: 基础 slug 文本。

    Returns:
        不与现有记录冲突的唯一 slug。
    """
    base = _slugify(base_slug)
    candidate = base
    suffix = 2
    while db.exec(
        select(GeneralSkill).where(
            GeneralSkill.tenant_id == tenant_id, GeneralSkill.slug == candidate
        )
    ).first():
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


def _source_name(source: str) -> str:
    """从导入来源字符串中提取可读的技能名称。

    解析 URL 路径取最后一段，去除 .zip/.md 后缀和 upload: 前缀。

    Args:
        source: 导入来源字符串（URL 或 upload 文件名）。

    Returns:
        提取的名称，无法提取时回退为默认值。
    """
    parsed = urlparse(source)
    path = parsed.path if parsed.scheme else source
    cleaned = path.rstrip("/").rsplit("/", 1)[-1].removesuffix(".zip").removesuffix(".md")
    if cleaned.startswith("upload:"):
        cleaned = cleaned.removeprefix("upload:")
    return cleaned or "开源平台通用技能"


def _clean_source_filename(filename: str) -> str:
    """清理上传文件名，提取纯文件名部分。

    Args:
        filename: 原始文件名（可能含路径）。

    Returns:
        清理后的纯文件名。

    Raises:
        HTTPException 400: 文件名为空。
    """
    cleaned = str(filename or "").replace("\\", "/").strip().rsplit("/", 1)[-1]
    if not cleaned:
        raise HTTPException(status_code=400, detail="Uploaded skill package filename is required")
    return cleaned


def _decode_base64_payload(value: str) -> bytes:
    """解码 base64 编码的上传内容（支持 data URI 前缀）。

    Args:
        value: base64 编码字符串，可能含 ``data:...;base64,`` 前缀。

    Returns:
        解码后的二进制数据。

    Raises:
        HTTPException 400: 内容为空、解码失败或超过大小限制。
    """
    cleaned = str(value or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Uploaded skill package content is required")
    if "," in cleaned and cleaned[:80].lower().startswith("data:"):
        cleaned = cleaned.split(",", 1)[1]
    try:
        data = base64.b64decode(cleaned, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=400, detail="Uploaded skill package content is not valid base64"
        ) from exc
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded skill package is empty")
    if len(data) > MAX_CLAWHUB_PACKAGE_BYTES:
        raise HTTPException(status_code=400, detail="Uploaded skill package is too large")
    return data


def _load_clawhub_source(source: str) -> list[GeneralSkillFile]:
    """根据来源标识加载技能文件列表（远程导入的入口调度函数）。

    支持 ClawHub/SkillHub slug、HTTP(S) URL、GitHub owner/repo 简写等形式。

    Args:
        source: 来源标识字符串。

    Returns:
        加载到的技能文件列表。

    Raises:
        HTTPException 400: 来源格式不支持或下载失败。
    """
    cleaned = _required_text(source, "source")
    clawhub_slug = _clawhub_slug_from_source(cleaned)
    if clawhub_slug:
        source_url = cleaned if cleaned.startswith(("http://", "https://")) else None
        return _load_clawhub_skill_package(clawhub_slug, source_url=source_url)
    if cleaned.startswith(("http://", "https://")):
        return _load_remote_skill_source(cleaned)
    if _looks_like_github_shorthand(cleaned):
        return _load_remote_skill_source(f"https://github.com/{cleaned}")
    raise HTTPException(
        status_code=400,
        detail="开源平台来源必须是开源平台 slug、GitHub URL、raw SKILL.md URL、zip URL 或 owner/repo 路径",
    )


def _clawhub_slug_from_source(source: str) -> str | None:
    """从来源字符串中提取 ClawHub/SkillHub 的 slug。

    支持 URL 形式（从路径段提取）和纯 slug 形式。

    Args:
        source: 来源标识字符串。

    Returns:
        合法的 slug 字符串，不匹配时返回 ``None``。
    """
    cleaned = source.strip()
    if not cleaned:
        return None
    if cleaned.startswith(("http://", "https://")):
        parsed = urlparse(cleaned)
        if parsed.netloc not in REMOTE_SKILLHUB_HOSTS:
            return None
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        if len(parts) >= 2:
            slug = parts[1]
        elif len(parts) == 1:
            slug = parts[0]
        else:
            return None
        return _valid_clawhub_slug(slug)
    if "/" not in cleaned:
        return _valid_clawhub_slug(cleaned)
    return None


def _valid_clawhub_slug(value: str) -> str | None:
    """校验 slug 是否符合 ClawHub 命名规则。

    规则：以字母或数字开头，长度 2-128，允许字母、数字、下划线、点和连字符。

    Args:
        value: 待校验的 slug 值。

    Returns:
        合法则返回去除 .zip/.md 后缀的 slug，否则 ``None``。
    """
    slug = value.strip().removesuffix(".zip").removesuffix(".md")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{1,127}", slug):
        return slug
    return None


def _clawhub_homepage_from_source(source: str) -> str | None:
    """从来源字符串推断技能的主页 URL。

    Args:
        source: 来源标识字符串。

    Returns:
        主页 URL 字符串，无法推断时返回 ``None``。
    """
    cleaned = source.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme and parsed.netloc in REMOTE_SKILLHUB_HOSTS:
        return cleaned
    slug = _clawhub_slug_from_source(cleaned)
    if slug:
        return f"https://skillhub.ai/{slug}"
    return None


def _clawhub_download_url(slug: str) -> str:
    """构建 ClawHub 平台的技能包下载 URL。

    Args:
        slug: ClawHub 技能 slug。

    Returns:
        完整的下载 URL 字符串。
    """
    return f"{CLAWHUB_DOWNLOAD_ENDPOINT}?slug={quote(slug, safe='')}"


def _load_clawhub_skill_package(slug: str, source_url: str | None = None) -> list[GeneralSkillFile]:
    """从 ClawHub 下载端点加载技能包，失败时回退到原始 source_url。

    Args:
        slug: ClawHub 技能 slug。
        source_url: 可选的备用下载 URL。

    Returns:
        加载到的技能文件列表。

    Raises:
        HTTPException: 下载端点和备用 URL 均失败时抛出下载端点的错误。
    """
    download_url = _clawhub_download_url(slug)
    try:
        return _load_remote_skill_source(download_url)
    except HTTPException as download_error:
        if source_url:
            try:
                return _load_remote_skill_source(source_url)
            except HTTPException:
                pass
        raise download_error


def _looks_like_github_shorthand(value: str) -> bool:
    """判断字符串是否符合 ``owner/repo`` 形式的 GitHub 简写。

    Args:
        value: 待检测的字符串。

    Returns:
        符合简写格式返回 ``True``，否则 ``False``。
    """
    return bool(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:/.+)?", value.strip()))


def _load_remote_skill_source(url: str, visited: set[str] | None = None) -> list[GeneralSkillFile]:
    """从远程 URL 加载技能文件，支持重定向追踪和多种内容类型。

    根据内容类型自动选择处理策略：zip 包解压、GitHub 目录遍历、
    HTML 页面链接提取或直接作为 Markdown 导入。
    使用 ``visited`` 集合防止循环重定向，最多跟踪 5 次跳转。

    Args:
        url: 远程技能来源 URL。
        visited: 已访问的 URL 集合（用于递归防环）。

    Returns:
        加载到的技能文件列表。

    Raises:
        HTTPException 400: URL 无效、循环重定向、跳转过深或内容类型不支持。
    """
    normalized_url = url.strip()
    parsed = urlparse(normalized_url)
    if not parsed.scheme or not parsed.netloc:
        raise HTTPException(status_code=400, detail="Remote skill source must be a valid URL")
    visited = visited or set()
    if normalized_url in visited:
        raise HTTPException(status_code=400, detail="Remote skill source redirects to itself")
    if len(visited) >= 5:
        raise HTTPException(
            status_code=400, detail="Remote skill source contains too many indirections"
        )
    visited.add(normalized_url)
    if parsed.netloc in GITHUB_HOSTS or parsed.netloc == RAW_GITHUB_HOST:
        return _load_github_skill_source(parsed)
    data, content_type = _download_url(normalized_url)
    lower_content_type = content_type.lower()
    if parsed.path.lower().endswith(".zip") or "zip" in lower_content_type:
        return _files_from_zip(data)
    text = _decode_text(data)
    if _looks_like_html_response(text, lower_content_type):
        linked_source = _extract_skill_source_from_html(text, normalized_url)
        if linked_source:
            return _load_remote_skill_source(linked_source, visited)
        raise HTTPException(
            status_code=400,
            detail=(
                "开源平台页面没有暴露可下载的技能包或 GitHub 目录。"
                "HTML 页面不会被当作 SKILL.md 导入。"
            ),
        )
    if _looks_like_markdown_source(parsed.path, lower_content_type):
        file_name = unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1]) or "SKILL.md"
        if not file_name.lower().endswith(".md"):
            file_name = "SKILL.md"
        return [
            GeneralSkillFile(
                path=_clean_package_path(file_name),
                content=text,
                size=len(data),
                mime_type=content_type or "text/markdown",
            )
        ]
    raise HTTPException(
        status_code=400,
        detail="Remote source must be a zip package, GitHub skill directory, or raw Markdown skill file",
    )


def _load_github_skill_source(parsed) -> list[GeneralSkillFile]:
    """根据 GitHub URL 格式加载技能文件。

    支持 raw.githubusercontent.com 单文件、blob/raw 单文件引用、
    tree 目录遍历、archive 下载以及默认分支自动探测等多种 URL 形式。

    Args:
        parsed: 已解析的 URL ``ParseResult`` 对象。

    Returns:
        加载到的技能文件列表。

    Raises:
        HTTPException 400: URL 格式不完整或无法获取内容。
    """
    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if parsed.netloc == RAW_GITHUB_HOST:
        if len(parts) < 4:
            raise HTTPException(
                status_code=400,
                detail="Raw GitHub source must include owner, repo, branch and path",
            )
        owner, repo, branch = parts[0], parts[1], parts[2]
        file_path = "/".join(parts[3:])
        data, content_type = _download_url(parsed.geturl())
        return [
            GeneralSkillFile(
                path=file_path.rsplit("/", 1)[-1] or "SKILL.md",
                content=_decode_text(data),
                size=len(data),
                mime_type=content_type or "text/markdown",
            )
        ]
    if len(parts) < 2:
        raise HTTPException(
            status_code=400, detail="GitHub source must include owner and repository"
        )
    owner, repo = parts[0], parts[1].removesuffix(".git")
    if len(parts) >= 3 and parts[2] == "archive":
        data, _ = _download_url(parsed.geturl())
        return _files_from_zip(data)
    if len(parts) >= 5 and parts[2] in {"blob", "raw"}:
        branch = parts[3]
        file_path = "/".join(parts[4:])
        raw_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{file_path}"
        data, content_type = _download_url(raw_url)
        return [
            GeneralSkillFile(
                path=file_path.rsplit("/", 1)[-1] or "SKILL.md",
                content=_decode_text(data),
                size=len(data),
                mime_type=content_type or "text/markdown",
            )
        ]
    if len(parts) >= 5 and parts[2] == "tree":
        branch = parts[3]
        subtree = "/".join(parts[4:])
        return _download_github_directory(owner, repo, branch, subtree)
    subtree = "/".join(parts[2:]) if len(parts) > 2 else ""
    errors: list[str] = []
    for branch in ["main", "master"]:
        try:
            return _download_github_directory(owner, repo, branch, subtree)
        except HTTPException as exc:
            errors.append(str(exc.detail))
    return _download_github_archive(owner, repo, ["main", "master"], subtree)


def _download_github_directory(
    owner: str, repo: str, branch: str, subtree: str = ""
) -> list[GeneralSkillFile]:
    """下载 GitHub 目录中的技能文件，优先用 API 遍历，失败回退到 archive 下载。

    Args:
        owner: GitHub 仓库所有者。
        repo: GitHub 仓库名。
        branch: 分支名。
        subtree: 子目录路径（可选）。

    Returns:
        加载到的技能文件列表。

    Raises:
        HTTPException: API 遍历和 archive 下载均失败时抛出 API 错误。
    """
    try:
        return _download_github_directory_contents(owner, repo, branch, subtree)
    except HTTPException as api_error:
        try:
            return _download_github_archive(owner, repo, [branch], subtree)
        except HTTPException:
            raise api_error


def _download_github_directory_contents(
    owner: str, repo: str, branch: str, subtree: str = ""
) -> list[GeneralSkillFile]:
    """通过 GitHub Contents API 递归遍历目录并下载技能文件。

    内部使用 ``walk`` 闭包递归遍历子目录，跳过无用路径，
    并限制文件数量和单个文件大小。

    Args:
        owner: GitHub 仓库所有者。
        repo: GitHub 仓库名。
        branch: 分支名。
        subtree: 子目录路径（可选）。

    Returns:
        加载到的技能文件列表。

    Raises:
        HTTPException 400: 目录中不包含 SKILL.md。
    """
    normalized_subtree = subtree.strip("/")
    files: list[GeneralSkillFile] = []
    visited_dirs: set[str] = set()

    def walk(path: str) -> None:
        if len(files) >= MAX_CLAWHUB_FILES:
            return
        if path in visited_dirs:
            return
        visited_dirs.add(path)
        api_path = quote(path, safe="/")
        api_url = f"https://api.github.com/repos/{quote(owner)}/{quote(repo)}/contents"
        if api_path:
            api_url = f"{api_url}/{api_path}"
        api_url = f"{api_url}?ref={quote(branch, safe='')}"
        payload = _download_json(api_url)
        entries = payload if isinstance(payload, list) else [payload]
        for entry in entries:
            if len(files) >= MAX_CLAWHUB_FILES:
                break
            if not isinstance(entry, dict):
                continue
            item_type = str(entry.get("type") or "")
            item_path = str(entry.get("path") or "").strip("/")
            if not item_path or _skip_package_path(item_path):
                continue
            if item_type == "dir":
                walk(item_path)
                continue
            if item_type != "file":
                continue
            size = int(entry.get("size") or 0)
            if size > MAX_CLAWHUB_FILE_BYTES:
                continue
            download_url = str(entry.get("download_url") or "")
            if not download_url:
                continue
            relative = item_path
            if normalized_subtree and item_path.startswith(f"{normalized_subtree}/"):
                relative = item_path[len(normalized_subtree) + 1 :]
            data, content_type = _download_url(download_url)
            if len(data) > MAX_CLAWHUB_FILE_BYTES:
                continue
            files.append(
                GeneralSkillFile(
                    path=_clean_package_path(relative),
                    content=_decode_text(data),
                    size=len(data),
                    mime_type=content_type or _guess_mime_type(relative),
                )
            )

    walk(normalized_subtree)
    if not _find_skill_file(files):
        raise HTTPException(status_code=400, detail="GitHub directory does not contain SKILL.md")
    return files


def _download_github_archive(
    owner: str, repo: str, branches: list[str], subtree: str = ""
) -> list[GeneralSkillFile]:
    """从 GitHub 下载仓库的 zip 归档并提取技能文件。

    依次尝试指定的分支列表，第一个成功的分支即返回。

    Args:
        owner: GitHub 仓库所有者。
        repo: GitHub 仓库名。
        branches: 候选分支名列表。
        subtree: 子目录路径（可选）。

    Returns:
        从归档中提取的技能文件列表。

    Raises:
        HTTPException 400: 所有分支的归档下载均失败。
    """
    errors: list[str] = []
    for branch in branches:
        archive_url = f"https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.zip"
        try:
            data, _ = _download_url(archive_url)
            return _files_from_zip(data, subtree=subtree)
        except HTTPException as exc:
            errors.append(str(exc.detail))
    raise HTTPException(
        status_code=400, detail=f"Unable to download GitHub skill package: {'; '.join(errors)}"
    )


def _download_json(url: str) -> object:
    """下载 URL 内容并解析为 JSON 对象。

    Args:
        url: 远程 JSON API 的 URL。

    Returns:
        解析后的 JSON 对象（字典或列表）。

    Raises:
        HTTPException 400: 下载失败或 JSON 格式无效。
    """
    data, _ = _download_url(url)
    try:
        return json.loads(_decode_text(data))
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=400, detail="Remote GitHub API returned invalid JSON"
        ) from exc


def _looks_like_markdown_source(path: str, content_type: str) -> bool:
    """判断响应内容是否为 Markdown 源文件。

    通过文件扩展名和 Content-Type 综合判断。

    Args:
        path: URL 路径部分。
        content_type: HTTP 响应的 Content-Type 头。

    Returns:
        是 Markdown 源返回 ``True``，否则 ``False``。
    """
    lower_path = path.lower()
    lower_content_type = content_type.lower()
    return (
        lower_path.endswith(".md")
        or lower_path.endswith("/skill")
        or "text/markdown" in lower_content_type
        or "text/plain" in lower_content_type
    )


def _looks_like_html_response(text: str, content_type: str) -> bool:
    """判断响应内容是否为 HTML 页面。

    通过 Content-Type 头和内容开头的 HTML 标签综合判断。

    Args:
        text: 响应正文文本。
        content_type: HTTP 响应的 Content-Type 头。

    Returns:
        是 HTML 页面返回 ``True``，否则 ``False``。
    """
    stripped = text.lstrip().lower()
    return (
        "text/html" in content_type
        or stripped.startswith("<!doctype html")
        or stripped.startswith("<html")
    )


def _extract_skill_source_from_html(text: str, base_url: str) -> str | None:
    """从 HTML 页面中提取技能下载链接。

    扫描页面中的所有 URL 和 href/src 属性，按优先级匹配
    raw.githubusercontent、ClawHub 下载链接、GitHub 目录/归档等。

    Args:
        text: HTML 页面文本。
        base_url: 页面的基础 URL，用于解析相对路径。

    Returns:
        匹配到的技能来源 URL，未找到返回 ``None``。
    """
    normalized = unescape(text).replace("\\/", "/").replace("\\u002F", "/").replace("\\u002f", "/")
    candidates: list[str] = []
    candidates.extend(re.findall(r"https?://[^\s\"'<>]+", normalized))
    for match in re.finditer(
        r"""(?:href|src)\s*=\s*["']([^"']+)["']""", normalized, flags=re.IGNORECASE
    ):
        candidates.append(urljoin(base_url, match.group(1)))
    seen: set[str] = set()
    for candidate in candidates:
        cleaned = candidate.strip().rstrip("),.;]")
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        parsed = urlparse(cleaned)
        if not parsed.scheme or not parsed.netloc:
            continue
        lower_path = parsed.path.lower()
        if parsed.netloc == RAW_GITHUB_HOST:
            return cleaned
        if _is_clawhub_download_url(parsed):
            return cleaned
        if parsed.netloc in GITHUB_HOSTS and (
            "/tree/" in lower_path
            or "/blob/" in lower_path
            or lower_path.endswith(".zip")
            or "/archive/" in lower_path
        ):
            return cleaned
        if lower_path.endswith(".zip"):
            return cleaned
    return None


def _is_clawhub_download_url(parsed) -> bool:
    """判断 URL 是否为 ClawHub 下载端点。

    Args:
        parsed: 已解析的 URL ``ParseResult`` 对象。

    Returns:
        是 ClawHub 下载端点返回 ``True``，否则 ``False``。
    """
    path = parsed.path.lower().rstrip("/")
    return path.endswith("/api/v1/download") and "slug=" in parsed.query.lower()


def _download_url(url: str) -> tuple[bytes, str]:
    """通过 HTTP GET 下载远程资源，返回内容和 Content-Type。

    设置自定义 User-Agent 和超时时间，并限制下载大小。

    Args:
        url: 待下载的 URL。

    Returns:
        元组 ``(二进制数据, content_type)``。

    Raises:
        HTTPException 400: HTTP 错误、网络错误、超时或文件过大。
    """
    try:
        request = Request(url, headers={"User-Agent": "StaffDeck-GeneralSkillImporter/1.0"})
        with urlopen(request, timeout=REMOTE_SKILL_DOWNLOAD_TIMEOUT_SECONDS) as response:  # noqa: S310 - user-confirmed import source
            content_type = response.headers.get("content-type", "")
            data = response.read(MAX_CLAWHUB_PACKAGE_BYTES + 1)
    except HTTPError as exc:
        raise HTTPException(
            status_code=400, detail=f"Download failed with HTTP {exc.code}"
        ) from exc
    except URLError as exc:
        raise HTTPException(status_code=400, detail=f"Download failed: {exc.reason}") from exc
    except TimeoutError as exc:
        raise HTTPException(status_code=400, detail="Download timed out") from exc
    if len(data) > MAX_CLAWHUB_PACKAGE_BYTES:
        raise HTTPException(status_code=400, detail="General skill package is too large")
    return data, content_type


def _files_from_zip(data: bytes, subtree: str = "") -> list[GeneralSkillFile]:
    """从 zip 二进制数据中提取技能文件列表。

    定位 SKILL.md 所在目录作为基准目录，剥离该前缀后提取全部相关文件。
    受文件数量和大小限制约束。

    Args:
        data: zip 文件的二进制数据。
        subtree: 可选的子目录过滤路径。

    Returns:
        提取出的 ``GeneralSkillFile`` 列表。

    Raises:
        HTTPException 400: 包中不含 SKILL.md。
    """
    normalized_subtree = subtree.strip("/")
    with zipfile.ZipFile(BytesIO(data)) as archive:
        names = [
            name
            for name in archive.namelist()
            if not name.endswith("/") and not _skip_package_path(name)
        ]
        skill_candidates = [name for name in names if name.rsplit("/", 1)[-1].lower() == "skill.md"]
        if normalized_subtree:
            skill_candidates = [
                name
                for name in skill_candidates
                if _zip_relative_path(name, normalized_subtree) is not None
            ]
        if not skill_candidates:
            raise HTTPException(status_code=400, detail="Package does not contain SKILL.md")
        base = skill_candidates[0].rsplit("/", 1)[0] if "/" in skill_candidates[0] else ""
        files: list[GeneralSkillFile] = []
        for name in names:
            if base:
                if not name.startswith(f"{base}/"):
                    continue
                relative = name[len(base) + 1 :]
            else:
                relative = name
            if not relative or relative.endswith("/"):
                continue
            info = archive.getinfo(name)
            if info.file_size > MAX_CLAWHUB_FILE_BYTES:
                continue
            if len(files) >= MAX_CLAWHUB_FILES:
                break
            content = _decode_text(archive.read(name))
            files.append(
                GeneralSkillFile(
                    path=relative,
                    content=content,
                    size=info.file_size,
                    mime_type=_guess_mime_type(relative),
                )
            )
    return files


def _zip_relative_path(name: str, subtree: str) -> str | None:
    """检查 zip 内文件路径是否属于指定子目录，返回相对路径。

    Args:
        name: zip 内的文件路径。
        subtree: 目标子目录路径。

    Returns:
        相对于子目录的路径，不属于该子目录返回 ``None``。
    """
    parts = name.split("/")
    for index in range(1, len(parts)):
        candidate = "/".join(parts[index:])
        if candidate == subtree or candidate.startswith(f"{subtree}/"):
            return candidate
    return None


def _skip_package_path(path: str) -> bool:
    """判断文件路径是否属于应跳过的无关目录。

    跳过 ``__MACOSX``、``.git``、``node_modules``、``.venv``、``dist``、``build`` 等。

    Args:
        path: 文件路径。

    Returns:
        应跳过返回 ``True``，否则 ``False``。
    """
    parts = path.split("/")
    return any(
        part in {"__MACOSX", ".git", "node_modules", ".venv", "dist", "build"} for part in parts
    )


def _decode_text(data: bytes) -> str:
    """将二进制数据解码为 UTF-8 文本，解码失败时用 replace 策略容错。

    Args:
        data: 二进制数据。

    Returns:
        解码后的字符串。
    """
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def _guess_mime_type(path: str) -> str:
    """根据文件扩展名猜测 MIME 类型。

    Args:
        path: 文件路径。

    Returns:
        猜测的 MIME 类型字符串，默认为 ``text/plain``。
    """
    lower = path.lower()
    if lower.endswith(".md"):
        return "text/markdown"
    if lower.endswith((".py", ".sh", ".js", ".ts", ".json", ".txt", ".yaml", ".yml")):
        return "text/plain"
    return "text/plain"


def _validate_slug(value: str) -> None:
    """校验 slug 不含空格和斜杠。

    Args:
        value: 待校验的 slug 字符串。

    Raises:
        HTTPException 400: slug 包含空格或斜杠。
    """
    if any(char.isspace() for char in value) or "/" in value:
        raise HTTPException(
            status_code=400, detail="General skill slug cannot contain spaces or slashes"
        )


def _sse(event: object, data: object) -> str:
    """构建 SSE（Server-Sent Events）格式的消息字符串。

    Args:
        event: 事件名称。
        data: 事件数据对象（将被 JSON 序列化）。

    Returns:
        符合 SSE 协议的消息字符串（``event: ...\\ndata: ...\\n\\n``）。
    """
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"
