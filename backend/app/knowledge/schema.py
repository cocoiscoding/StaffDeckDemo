"""知识管理 API 请求/响应模式定义模块。

本模块定义了知识管理系统所有 REST API 端点使用的 Pydantic 模型，
包括知识库、文档、入库任务、Bucket、Chunk、概念、检索和发现建议等
的创建、读取、更新请求和响应模式。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class KnowledgeBaseCreateRequest(BaseModel):
    """创建知识库的请求模型。"""

    tenant_id: str
    name: str
    description: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeBaseUpdateRequest(BaseModel):
    """更新知识库的请求模型。"""

    tenant_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    status: Optional[Literal["active", "archived"]] = None
    metadata: Optional[dict[str, Any]] = None


class KnowledgeBaseRollbackRequest(BaseModel):
    """知识库版本回滚的请求模型。"""

    tenant_id: str
    agent_id: str
    version: str


class KnowledgeBaseRead(BaseModel):
    """知识库读取响应模型。"""

    id: str
    tenant_id: str
    name: str
    description: Optional[str] = None
    status: str
    version: Optional[str] = None
    branch_sync_state: Optional[str] = None
    branch_base_version: Optional[str] = None
    branch_head_version: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    document_count: int = 0
    bucket_count: int = 0
    chunk_count: int = 0
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class KnowledgeDocumentUploadRequest(BaseModel):
    """文档上传请求模型。"""

    tenant_id: str
    knowledge_base_id: Optional[str] = None
    filename: str
    content_base64: str
    title: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeIngestJobRead(BaseModel):
    """入库任务读取响应模型。"""

    id: str
    tenant_id: str
    knowledge_base_id: str
    document_id: Optional[str] = None
    filename: str
    status: str
    stage: str
    progress: float
    error: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class KnowledgeDocumentRead(BaseModel):
    """文档读取响应模型。"""

    id: str
    tenant_id: str
    knowledge_base_id: str
    knowledge_base_version_id: Optional[str] = None
    filename: str
    file_type: str
    title: Optional[str] = None
    status: str
    bucket_count: int
    chunk_count: int
    metadata: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class KnowledgeDocumentUpdateRequest(BaseModel):
    """文档更新请求模型。"""

    tenant_id: str
    title: Optional[str] = None
    status: Optional[Literal["ready", "processing", "failed", "archived"]] = None
    metadata: Optional[dict[str, Any]] = None


class KnowledgeBucketRead(BaseModel):
    """Bucket（知识主题）读取响应模型。"""

    id: str
    tenant_id: str
    knowledge_base_id: str
    document_id: str
    bucket_key: str
    title: str
    summary: str
    token_estimate: int
    chunk_count: int = 0
    status: str = "ready"
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class KnowledgeBucketUpdateRequest(BaseModel):
    """Bucket 更新请求模型。"""

    tenant_id: str
    title: Optional[str] = None
    summary: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class KnowledgeChunkRead(BaseModel):
    """Chunk（引用来源）读取响应模型。"""

    id: str
    tenant_id: str
    knowledge_base_id: str
    document_id: str
    bucket_id: str
    chunk_index: int
    content: str
    summary: Optional[str] = None
    source_ref: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class KnowledgeChunkUpdateRequest(BaseModel):
    """Chunk 更新请求模型。"""

    tenant_id: str
    content: Optional[str] = None
    summary: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class KnowledgeConceptRead(BaseModel):
    """OKF 概念读取响应模型。"""

    id: str
    tenant_id: str
    knowledge_base_id: str
    knowledge_base_version_id: Optional[str] = None
    document_id: Optional[str] = None
    concept_id: str
    concept_type: str
    title: str
    description: Optional[str] = None
    content_md: str
    frontmatter: dict[str, Any] = Field(default_factory=dict)
    links: list[dict[str, Any]] = Field(default_factory=list)
    citations: list[dict[str, Any]] = Field(default_factory=list)
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    status: str
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class KnowledgeConceptUpdateRequest(BaseModel):
    """OKF 概念更新请求模型。"""

    tenant_id: str
    content_md: str
    document_id: Optional[str] = None
    status: Literal["active", "archived"] = "active"


class KnowledgeOkfImportRequest(BaseModel):
    """OKF 概念导入请求模型。"""

    tenant_id: str
    knowledge_base_id: Optional[str] = None
    filename: str
    content_base64: str
    agent_id: Optional[str] = None


class KnowledgeSearchRequest(BaseModel):
    """知识检索请求模型。

    Attributes:
        tenant_id: 租户 ID。
        agent_id: 智能体 ID（用于确定可见知识范围）。
        query: 用户查询字符串。
        model_config_id: LLM 模型配置 ID（用于语义路由，为 None 时使用词法匹配）。
        mode: 检索模式（chat 对话、skill_discovery 技能发现、debug 调试）。
        knowledge_base_ids: 限定的知识库 ID 列表。
        knowledge_base_version_ids: 限定的知识库版本 ID 列表。
        document_ids: 限定的文档 ID 列表。
        max_bucket_rounds: Bucket 路由轮数上限。
        max_buckets: 返回的 Bucket 数量上限。
        max_chunks: 返回的 Chunk 数量上限。
        budget_tokens: Token 预算。
        max_depth: 章节展开深度。
        need_evidence_pack: 是否构建证据包。
    """

    tenant_id: str
    agent_id: Optional[str] = None
    query: str
    model_config_id: Optional[str] = None
    mode: Literal["chat", "skill_discovery", "debug"] = "chat"
    knowledge_base_ids: list[str] = Field(default_factory=list)
    knowledge_base_version_ids: list[str] = Field(default_factory=list)
    document_ids: list[str] = Field(default_factory=list)
    max_bucket_rounds: int = 2
    max_buckets: int = 4
    max_chunks: int = 8
    budget_tokens: int = 4000
    max_depth: int = 2
    need_evidence_pack: bool = True


class KnowledgeSearchResponse(BaseModel):
    """知识检索响应模型。

    Attributes:
        selected_buckets: 选中的 Bucket 列表。
        chunks: 排序后的 Chunk 列表。
        trace: 检索追踪日志。
        route_trace: 路由追踪日志。
        selected_documents: 选中的文档卡片列表。
        selected_concepts: 选中的 OKF 概念卡片列表。
        expanded_sections: 展开的章节列表。
        okf_citations: OKF 引用列表。
        evidence_pack: 证据包列表。
    """

    selected_buckets: list[KnowledgeBucketRead] = Field(default_factory=list)
    chunks: list[KnowledgeChunkRead] = Field(default_factory=list)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    route_trace: list[dict[str, Any]] = Field(default_factory=list)
    selected_documents: list[dict[str, Any]] = Field(default_factory=list)
    selected_concepts: list[dict[str, Any]] = Field(default_factory=list)
    expanded_sections: list[dict[str, Any]] = Field(default_factory=list)
    okf_citations: list[dict[str, Any]] = Field(default_factory=list)
    evidence_pack: list[dict[str, Any]] = Field(default_factory=list)


class KnowledgeDiscoveryRead(BaseModel):
    """知识发现建议读取响应模型。"""

    id: str
    tenant_id: str
    knowledge_base_id: str
    document_id: str
    bucket_id: Optional[str] = None
    suggestion_type: Literal["skill", "tool", "warning"]
    title: str
    status: str
    payload: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[dict[str, Any]] = Field(default_factory=list)
    reason: Optional[str] = None
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)
