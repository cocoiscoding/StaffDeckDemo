"""数据库模型定义模块。

本模块使用 SQLModel（SQLAlchemy + Pydantic 的融合 ORM）定义了 StaffDeck 平台的
全部数据表（共 35 张表）。这些表覆盖了整个平台的核心数据领域，按职责可划分为
以下几组：

1. 租户与用户 (Tenant / User)
    多租户隔离的基础实体，几乎所有业务表都携带 ``tenant_id`` 字段用于数据隔离。

2. 技能体系 (Skill / SkillVersion / AgentSkillBranch / AgentSkillBranchVersion / GeneralSkill)
    描述智能体可执行的"技能"及其版本管理、分支（fork）机制。
    - ``Skill`` / ``SkillVersion`` 维护技能的权威定义与历史版本；
    - ``AgentSkillBranch`` 让单个智能体能针对某个技能维护独立分支并独立演进；
    - ``GeneralSkill`` 用于存储以 Markdown + 附件形式表达的通用技能。

3. 知识库体系 (KnowledgeBase / KnowledgeBaseVersion / AgentKnowledgeBranch /
    KnowledgeDocument / KnowledgeBucket / KnowledgeChunk / KnowledgeConcept /
    KnowledgeDiscoverySuggestion / KnowledgeIngestJob)
    覆盖知识库从原始文档摄取、分桶分块、概念抽取、版本管理、到智能体分支与
    发现建议的完整生命周期。

4. 模型与配置 (ModelConfig / PersonaConfig / UIConfig)
    - ``ModelConfig`` 描述 LLM 接入参数（含协议、密钥、可信状态、校验进度等）；
    - ``PersonaConfig`` 以租户为粒度保存系统人设提示词；
    - ``UIConfig`` 控制前端展示与智能体循环行为的租户级开关。

5. 智能体与绑定 (AgentProfile / AgentUsage / AgentModelBinding / AgentResourceBinding)
    描述智能体画像、用户-智能体使用关系，以及智能体到模型/资源（技能、知识库等）
    的多角色绑定关系。

6. 工具与 MCP (Tool / MCPServer / MockOrder)
    外部工具与 Model Context Protocol (MCP) 服务器的注册表；``MockOrder`` 为
    内置的业务示例数据，用于演示工具调用效果。

7. 会话与对话 (ChatSession / Message / MessageFeedback / SkillFeedback /
    AgentEvent / MemoryRecord)
    聊天会话、消息、用户反馈、智能体执行事件以及长期记忆。

8. 人工转接与调度 (HumanHandoffRequest / ScheduledTask / ScheduledTaskRun)
    人工接管请求、定时任务及其执行记录。

本模块只负责表结构定义（schema），不包含建表与迁移逻辑——后者见
``database.py``；初始演示数据见 ``seed.py``。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import Column, JSON, UniqueConstraint
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    """返回去掉时区信息的当前 UTC 时间。

    数据库中以原生 naive datetime 存储（不带 tzinfo），统一使用本函数生成时间戳，
    可避免不同驱动对带时区时间的兼容性差异，并保证全平台时间基准一致（UTC）。

    Returns:
        datetime: 当前 UTC 时间（naive，``tzinfo=None``）。
    """
    return datetime.now(UTC).replace(tzinfo=None)


def new_id(prefix: str) -> str:
    """生成带业务前缀的全局唯一 ID。

    格式为 ``{prefix}_{16位hex}``，例如 ``user_a1b2c3...``。前缀便于在日志、调试
    与人工排查中快速识别记录类型；后缀取自 UUID4 的前 16 位 hex，碰撞概率极低。

    Args:
        prefix: 业务前缀，如 ``user``、``skill``、``msg`` 等。

    Returns:
        str: 形如 ``{prefix}_{16hex}`` 的唯一标识符。
    """
    return f"{prefix}_{uuid4().hex[:16]}"


class Tenant(SQLModel, table=True):
    """租户表：平台多租户隔离的根实体。

    每个租户是一个独立的组织/工作空间，其下所有业务数据通过 ``tenant_id``
    关联回本表。租户本身只承载最基本的名称信息。
    """

    __tablename__ = "tenants"

    id: str = Field(primary_key=True)
    name: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class User(SQLModel, table=True):
    """用户表：租户内的登录账号。

    通过 ``(tenant_id, username)`` 联合唯一约束保证同一租户内用户名不重复；
    跨租户则允许同名。密码以哈希形式存储（``password_hash``），不存明文。
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("tenant_id", "username", name="uq_user_tenant_username"),)

    id: str = Field(default_factory=lambda: new_id("user"), primary_key=True)
    tenant_id: str = Field(index=True)
    username: str = Field(index=True)
    display_name: Optional[str] = None
    role: str = Field(default="member", index=True)
    password_hash: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Skill(SQLModel, table=True):
    """技能表：智能体可执行流程的权威定义。

    技能以 ``content_json``（结构化 JSON）描述其步骤与逻辑。通过
    ``(tenant_id, skill_id)`` 联合唯一约束定位一个技能；``skill_id`` 是业务标识，
    与主键 ``id`` 区分。``status`` 控制技能处于草稿/发布等生命周期阶段。
    """

    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("tenant_id", "skill_id", name="uq_skill_tenant_skill_id"),)

    id: str = Field(default_factory=lambda: new_id("skill"), primary_key=True)
    tenant_id: str = Field(index=True)
    skill_id: str = Field(index=True)
    version: str = "1.0.0"
    name: str
    business_domain: Optional[str] = None
    description: Optional[str] = None
    content_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    status: str = Field(default="draft", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SkillVersion(SQLModel, table=True):
    """技能版本表：技能的历史版本快照。

    每次技能发布或重要变更都会落一条版本记录，通过
    ``(tenant_id, skill_id, version)`` 唯一约束保证同一技能的版本号不重复，
    便于回溯与对比历史。
    """

    __tablename__ = "skill_versions"
    __table_args__ = (UniqueConstraint("tenant_id", "skill_id", "version", name="uq_skill_version"),)

    id: str = Field(default_factory=lambda: new_id("skillver"), primary_key=True)
    tenant_id: str = Field(index=True)
    skill_id: str = Field(index=True)
    version: str = Field(index=True)
    name: str
    business_domain: Optional[str] = None
    description: Optional[str] = None
    content_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    status: str = Field(default="draft", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentSkillBranch(SQLModel, table=True):
    """智能体技能分支表：单个智能体对某技能的私有派生（fork）。

    智能体可在全局技能基础上维护自己的分支：``base_version`` 标记派生起点，
    ``head_version`` 标记当前分支头部版本，``sync_state`` 描述与上游技能的同步
    状态（synced / diverged 等）。通过 ``(tenant_id, agent_id, skill_id)``
    唯一约束保证每个智能体对每个技能只存在一条分支记录。
    """

    __tablename__ = "agent_skill_branches"
    __table_args__ = (
        UniqueConstraint("tenant_id", "agent_id", "skill_id", name="uq_agent_skill_branch"),
    )

    id: str = Field(default_factory=lambda: new_id("agentbranch"), primary_key=True)
    tenant_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    skill_id: str = Field(index=True)
    source_skill_id: str = Field(index=True)
    base_version: str = "1.0.0"
    head_version: str = "1.0.0"
    content_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    status: str = Field(default="active", index=True)
    sync_state: str = Field(default="synced", index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentSkillBranchVersion(SQLModel, table=True):
    """智能体技能分支版本表：分支上每次变更的版本快照。

    与 ``SkillVersion`` 类似，但归属为某个智能体的分支。``sync_state`` 默认为
    ``diverged``，表示该版本相对于上游技能已经发生偏离。
    """

    __tablename__ = "agent_skill_branch_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "agent_id", "skill_id", "version", name="uq_agent_skill_branch_version"),
    )

    id: str = Field(default_factory=lambda: new_id("agentbranchver"), primary_key=True)
    tenant_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    skill_id: str = Field(index=True)
    source_skill_id: str = Field(index=True)
    version: str = Field(index=True)
    base_version: str = "1.0.0"
    content_json: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    status: str = Field(default="active", index=True)
    sync_state: str = Field(default="diverged", index=True)
    change_summary: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class GeneralSkill(SQLModel, table=True):
    """通用技能表：以 Markdown + 附件文件形式表达的技能。

    区别于以 ``content_json`` 描述的结构化技能，通用技能直接保存
    ``skill_markdown`` 正文与 ``skill_files_json`` 附件清单，更贴近"文档即技能"
    的表达方式。``slug`` 为业务唯一标识。
    """

    __tablename__ = "general_skills"
    __table_args__ = (UniqueConstraint("tenant_id", "slug", name="uq_general_skill_tenant_slug"),)

    id: str = Field(default_factory=lambda: new_id("genskill"), primary_key=True)
    tenant_id: str = Field(index=True)
    slug: str = Field(index=True)
    name: str
    description: Optional[str] = None
    homepage: Optional[str] = None
    skill_markdown: str
    skill_files_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    status: str = Field(default="draft", index=True)
    permissions_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    runtime_config_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeBase(SQLModel, table=True):
    """知识库表：知识资源的容器与组织单元。

    一个知识库聚合若干文档、桶、块、概念，供智能体检索引用。通过
    ``(tenant_id, name)`` 唯一约束保证租户内知识库名称唯一。
    """

    __tablename__ = "knowledge_bases"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_knowledge_base_tenant_name"),)

    id: str = Field(default_factory=lambda: new_id("kb"), primary_key=True)
    tenant_id: str = Field(index=True)
    name: str
    description: Optional[str] = None
    status: str = Field(default="active", index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeBaseVersion(SQLModel, table=True):
    """知识库版本表：知识库的版本快照。

    知识库内容变更通过版本管理，``KnowledgeDocument``/``KnowledgeBucket`` 等
    子实体通过 ``knowledge_base_version_id`` 关联到具体版本，实现可追溯的版本化
    检索。
    """

    __tablename__ = "knowledge_base_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "knowledge_base_id", "version", name="uq_knowledge_base_version"),
    )

    id: str = Field(default_factory=lambda: new_id("kbver"), primary_key=True)
    tenant_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    version: str = Field(default="1.0.0", index=True)
    name: str
    description: Optional[str] = None
    status: str = Field(default="active", index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentKnowledgeBranch(SQLModel, table=True):
    """智能体知识分支表：单个智能体对某知识库的私有派生。

    与 ``AgentSkillBranch`` 对应，允许智能体绑定知识库的特定版本基线
    （``base_version``/``head_version``）并独立追踪同步状态。
    """

    __tablename__ = "agent_knowledge_branches"
    __table_args__ = (
        UniqueConstraint("tenant_id", "agent_id", "knowledge_base_id", name="uq_agent_knowledge_branch"),
    )

    id: str = Field(default_factory=lambda: new_id("agentkb"), primary_key=True)
    tenant_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    base_version: str = "1.0.0"
    head_version: str = "1.0.0"
    status: str = Field(default="active", index=True)
    sync_state: str = Field(default="synced", index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeDocument(SQLModel, table=True):
    """知识文档表：被摄取进知识库的原始文档记录。

    记录文档的文件名、类型、处理状态以及切分后的桶/块数量统计。``status``
    跟踪摄取流水线的进度（processing / ready / failed 等），``error`` 保存失败原因。
    """

    __tablename__ = "knowledge_documents"

    id: str = Field(default_factory=lambda: new_id("kdoc"), primary_key=True)
    tenant_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    knowledge_base_version_id: Optional[str] = Field(default=None, index=True)
    filename: str
    file_type: str = Field(index=True)
    title: Optional[str] = None
    status: str = Field(default="processing", index=True)
    bucket_count: int = 0
    chunk_count: int = 0
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    error: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeBucket(SQLModel, table=True):
    """知识桶表：文档切分后的中粒度分组。

    摄取流水线将文档先切成"桶"（主题/章节级），桶下再细分为"块"（``KnowledgeChunk``）。
    桶携带标题、摘要与 token 估算，便于检索时做粗筛与上下文组装。
    """

    __tablename__ = "knowledge_buckets"

    id: str = Field(default_factory=lambda: new_id("kbucket"), primary_key=True)
    tenant_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    knowledge_base_version_id: Optional[str] = Field(default=None, index=True)
    document_id: str = Field(index=True)
    bucket_key: str = Field(index=True)
    title: str
    summary: str
    token_estimate: int = 0
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeChunk(SQLModel, table=True):
    """知识块表：检索的最小粒度文本片段。

    每个块属于某个桶，通过 ``chunk_index`` 标记桶内顺序；``content`` 为实际文本，
    ``source_ref`` 指向原始文档位置，便于在回答中溯源引用。
    """

    __tablename__ = "knowledge_chunks"

    id: str = Field(default_factory=lambda: new_id("kchunk"), primary_key=True)
    tenant_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    knowledge_base_version_id: Optional[str] = Field(default=None, index=True)
    document_id: str = Field(index=True)
    bucket_id: str = Field(index=True)
    chunk_index: int = Field(index=True)
    content: str
    summary: Optional[str] = None
    source_ref: Optional[str] = None
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeConcept(SQLModel, table=True):
    """知识概念表：从知识中抽取的结构化概念实体。

    以 Markdown (``content_md``) + frontmatter 形式存储概念正文与元数据，
    并维护概念之间的链接（``links_json``）、引用（``citations_json``）与来源
    （``source_refs_json``）。``concept_type`` 区分概念类别，通过
    ``(tenant_id, knowledge_base_version_id, concept_id)`` 唯一约束定位。
    """

    __tablename__ = "knowledge_concepts"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "knowledge_base_version_id",
            "concept_id",
            name="uq_knowledge_concept_version_path",
        ),
    )

    id: str = Field(default_factory=lambda: new_id("kconcept"), primary_key=True)
    tenant_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    knowledge_base_version_id: Optional[str] = Field(default=None, index=True)
    document_id: Optional[str] = Field(default=None, index=True)
    concept_id: str = Field(index=True)
    concept_type: str = Field(index=True)
    title: str
    description: Optional[str] = None
    content_md: str
    frontmatter_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    links_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    citations_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    source_refs_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    status: str = Field(default="active", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeDiscoverySuggestion(SQLModel, table=True):
    """知识发现建议表：摄取阶段自动产出的待审核建议。

    流水线在处理文档/桶时可能发现新概念、缺失链接等候选信息，作为"建议"落库，
    供人工评审（``status``：pending / accepted / rejected）。``payload_json``
    承载建议的具体内容，``reason`` 解释产出依据。
    """

    __tablename__ = "knowledge_discovery_suggestions"

    id: str = Field(default_factory=lambda: new_id("kdisc"), primary_key=True)
    tenant_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    knowledge_base_version_id: Optional[str] = Field(default=None, index=True)
    document_id: str = Field(index=True)
    bucket_id: Optional[str] = Field(default=None, index=True)
    suggestion_type: str = Field(index=True)
    title: str
    status: str = Field(default="pending", index=True)
    payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    source_refs_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    reason: Optional[str] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class KnowledgeIngestJob(SQLModel, table=True):
    """知识摄取任务表：跟踪单次文档摄取流水线的执行。

    ``status`` 跟踪整体状态（queued/running/completed/failed），``stage`` 标记
    当前所处阶段，``progress`` 为 0~1 的进度值。记录开始/结束时间用于耗时分析。
    """

    __tablename__ = "knowledge_ingest_jobs"

    id: str = Field(default_factory=lambda: new_id("kjob"), primary_key=True)
    tenant_id: str = Field(index=True)
    knowledge_base_id: str = Field(index=True)
    knowledge_base_version_id: Optional[str] = Field(default=None, index=True)
    document_id: Optional[str] = Field(default=None, index=True)
    filename: str
    status: str = Field(default="queued", index=True)
    stage: str = "queued"
    progress: float = 0.0
    error: Optional[str] = None
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    updated_at: datetime = Field(default_factory=utc_now)


class ModelConfig(SQLModel, table=True):
    """模型配置表：LLM 接入参数与可信校验状态。

    存储访问一个大语言模型所需的全部信息：协议类型（``api_protocol``，决定请求
    体格式与解析方式）、端点（``base_url``）、加密的密钥（``api_key_encrypted``）、
    模型名、采样参数以及协议级选项。

    安全相关字段：``trust_status``（可信状态）、``verified_*`` 一组字段记录最近一次
    配置校验（fingerprint 校验）的过程与结果；``*_revision`` 三个版本号用于检测
    配置/安全/密钥变更以触发重新校验。``is_default`` 标记租户默认模型。
    """

    __tablename__ = "model_configs"

    id: str = Field(default_factory=lambda: new_id("model"), primary_key=True)
    tenant_id: str = Field(index=True)
    name: str
    provider: str = "openai_compatible"
    api_protocol: str = Field(default="openai_chat_completions", index=True)
    base_url: Optional[str] = None
    api_key_encrypted: str
    model: str
    temperature: float = 0.2
    max_output_tokens: int = 8192
    extra_body_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    protocol_options_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    legacy_unmapped_options_json: dict[str, Any] = Field(
        default_factory=dict, sa_column=Column(JSON)
    )
    trust_status: str = Field(default="unverified", index=True)
    verified_at: Optional[datetime] = None
    verified_fingerprint: Optional[str] = None
    verification_attempt_id: Optional[str] = None
    verification_started_at: Optional[datetime] = None
    verification_attempt_status: str = Field(default="idle", index=True)
    verification_attempt_error_code: Optional[str] = None
    config_revision: int = 1
    security_revision: int = 1
    key_revision: int = 1
    is_default: bool = False
    enabled: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PersonaConfig(SQLModel, table=True):
    """人设配置表：租户级系统提示词。

    以 ``tenant_id`` 为主键（每租户一条），保存注入到对话的系统人设提示词
    （``system_prompt``），用于统一控制智能体的语气与角色定位。
    """

    __tablename__ = "persona_configs"

    tenant_id: str = Field(primary_key=True)
    system_prompt: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class UIConfig(SQLModel, table=True):
    """UI/行为配置表：租户级前端展示与智能体循环开关。

    控制是否在前端展示思考轨迹（``show_thinking_trace``）、技能轨迹、工具轨迹，
    以及反思循环最大轮数（``reflection_max_rounds``）与智能体单轮最大动作数
    （``agent_loop_max_actions``）。以 ``tenant_id`` 为主键，每租户一条。
    """

    __tablename__ = "ui_configs"

    tenant_id: str = Field(primary_key=True)
    show_thinking_trace: bool = True
    show_skill_trace: bool = True
    show_tool_trace: bool = True
    reflection_max_rounds: int = 1
    agent_loop_max_actions: int = 6
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentProfile(SQLModel, table=True):
    """智能体画像表：定义一个可被调用的智能体。

    携带名称、描述、人设提示词等。``is_overall`` 标记是否为租户级"全局智能体"
    （无具体技能绑定的通用入口）。通过 ``(tenant_id, name)`` 唯一约束保证
    租户内名称唯一。
    """

    __tablename__ = "agent_profiles"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_agent_profile_tenant_name"),)

    id: str = Field(default_factory=lambda: new_id("agent"), primary_key=True)
    tenant_id: str = Field(index=True)
    name: str
    description: Optional[str] = None
    persona_prompt: Optional[str] = None
    is_overall: bool = Field(default=False, index=True)
    status: str = Field(default="active", index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentUsage(SQLModel, table=True):
    """智能体使用关系表：记录用户与智能体的关联。

    一个用户可"使用/订阅"多个智能体，通过 ``(tenant_id, user_id, agent_id)``
    唯一约束保证同一用户对同一智能体只保留一条关系记录。``metadata_json`` 可
    存放偏好或最近使用时间等附加信息。
    """

    __tablename__ = "agent_usages"
    __table_args__ = (
        UniqueConstraint("tenant_id", "user_id", "agent_id", name="uq_agent_usage_user_agent"),
    )

    id: str = Field(default_factory=lambda: new_id("agentuse"), primary_key=True)
    tenant_id: str = Field(index=True)
    user_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentModelBinding(SQLModel, table=True):
    """智能体模型绑定表：为智能体的不同角色指定模型。

    同一智能体的不同职能（如 ``default``/``planning``/``reflection`` 等角色，
    存于 ``role``）可绑定不同的 ``ModelConfig``。通过
    ``(tenant_id, agent_id, role)`` 唯一约束保证每个角色只绑定一个模型。
    """

    __tablename__ = "agent_model_bindings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "agent_id", "role", name="uq_agent_model_binding"),
    )

    id: str = Field(default_factory=lambda: new_id("agentmodel"), primary_key=True)
    tenant_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    role: str = Field(default="default", index=True)
    model_config_id: str = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentResourceBinding(SQLModel, table=True):
    """智能体资源绑定表：将技能/知识库等资源挂载到智能体。

    ``resource_type`` 标识资源类别（如 skill / knowledge_base），``resource_id``
    指向具体资源。通过 ``(tenant_id, agent_id, resource_type, resource_id)``
    唯一约束防止重复绑定。``status`` 控制绑定是否生效。
    """

    __tablename__ = "agent_resource_bindings"
    __table_args__ = (
        UniqueConstraint("tenant_id", "agent_id", "resource_type", "resource_id", name="uq_agent_resource"),
    )

    id: str = Field(default_factory=lambda: new_id("agentres"), primary_key=True)
    tenant_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    resource_type: str = Field(index=True)
    resource_id: str = Field(index=True)
    status: str = Field(default="active", index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Tool(SQLModel, table=True):
    """工具表：可被智能体调用的外部 HTTP/MCP 工具定义。

    描述工具的调用方式（``method``/``url``/``headers_json``/``auth_json``）、
    输入输出 schema、所属分桶（``bucket``）以及允许调用它的技能白名单
    （``allowed_skills_json``）。``tool_type`` 区分 http / mcp 等类型，
    ``mcp_server_id`` 在工具来自 MCP 服务器时指向其来源。
    """

    __tablename__ = "tools"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_tool_tenant_name"),)

    id: str = Field(default_factory=lambda: new_id("tool"), primary_key=True)
    tenant_id: str = Field(index=True)
    name: str = Field(index=True)
    display_name: Optional[str] = None
    description: Optional[str] = None
    bucket: str = Field(default="未分桶", index=True)
    tool_type: str = Field(default="http", index=True)
    method: str
    url: str
    headers_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    auth_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    config_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    input_schema: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    output_schema: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    allowed_skills_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    mcp_server_id: Optional[str] = Field(default=None, index=True)
    enabled: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class MCPServer(SQLModel, table=True):
    """MCP 服务器表：Model Context Protocol 服务器的连接配置。

    支持多种传输方式（``transport``）：stdio（本地子进程）、streamable_http、
    sse 以及 builtin。不同传输方式使用不同字段——HTTP 系使用 ``url``/``headers_json``，
    stdio 系使用 ``command``/``args_json``/``env_json``/``cwd``。
    ``discovered_tools_json`` 缓存最近一次从该服务器发现的工具定义，用于预览与审计。
    """

    __tablename__ = "mcp_servers"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_mcp_server_tenant_name"),)

    id: str = Field(default_factory=lambda: new_id("mcpsrv"), primary_key=True)
    tenant_id: str = Field(index=True)
    name: str = Field(index=True)
    display_name: Optional[str] = None
    description: Optional[str] = None
    bucket: str = Field(default="MCP 工具", index=True)
    # 连接方式：stdio / streamable_http / sse / builtin
    transport: str = Field(default="streamable_http", index=True)
    # streamable_http / sse 使用
    url: Optional[str] = None
    headers_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # stdio 使用
    command: Optional[str] = None
    args_json: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    env_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    cwd: Optional[str] = None
    # 最近一次发现的原始工具定义（预览/审计用）
    discovered_tools_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    last_synced_at: Optional[datetime] = None
    enabled: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class MockOrder(SQLModel, table=True):
    """模拟订单表：内置业务示例数据，用于演示工具调用。

    提供一套贴近真实电商场景的订单/商品/支付状态字段，方便在无需接入真实业务
    系统的情况下演示智能体调用工具完成订单查询、退款等流程的效果。
    """

    __tablename__ = "mock_orders"

    order_id: str = Field(primary_key=True)
    user_id: Optional[str] = Field(default=None, index=True)
    product_id: Optional[str] = Field(default=None, index=True)
    sku_id: Optional[str] = None
    quantity: int = 1
    status: str = Field(default="created", index=True)
    payment_status: Optional[str] = None
    order_status: Optional[str] = None
    signed_days: int = 0
    refundable: bool = True
    total_amount: float = 0.0
    currency: str = "CNY"
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ChatSession(SQLModel, table=True):
    """聊天会话表：一次对话会话的完整运行时状态。

    除基本元信息外，承载了大量运行时上下文：当前激活的技能/步骤、槽位
    （``slots_json``）、技能调用栈（``skill_stack_json``）、待办任务、等待用户
    输入的状态（``awaiting_input_json``）、知识上下文、以及会话摘要等。这些
    JSON 字段使会话可在中断后完整恢复执行。
    """

    __tablename__ = "sessions"

    id: str = Field(primary_key=True)
    tenant_id: str = Field(index=True)
    user_id: Optional[str] = Field(default=None, index=True)
    agent_id: Optional[str] = Field(default=None, index=True)
    title: Optional[str] = None
    active_skill_id: Optional[str] = None
    active_step_id: Optional[str] = None
    slots_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    skill_stack_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    pending_tasks_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    resume_after_answer_json: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    awaiting_input_json: Optional[dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    knowledge_context_json: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    context_state_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    summary: Optional[str] = None
    last_agent_question: Optional[str] = None
    status: str = "active"
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class HumanHandoffRequest(SQLModel, table=True):
    """人工转接请求表：智能体请求人工介入的记录。

    当智能体在执行中遇到需要人工判断的问题时，创建一条转接请求，记录触发上下文
    （技能/步骤）、待回答问题与上下文摘要。人工回复后，``resume_payload_json``
    承载用于恢复智能体执行的负载，``status`` 标记处理进度。
    """

    __tablename__ = "human_handoff_requests"

    id: str = Field(default_factory=lambda: new_id("handoff"), primary_key=True)
    tenant_id: str = Field(index=True)
    session_id: str = Field(index=True)
    agent_id: Optional[str] = Field(default=None, index=True)
    requester_user_id: Optional[str] = Field(default=None, index=True)
    assignee_user_id: Optional[str] = Field(default=None, index=True)
    trigger_skill_id: Optional[str] = Field(default=None, index=True)
    trigger_step_id: Optional[str] = Field(default=None, index=True)
    context_summary: Optional[str] = None
    pending_question: Optional[str] = None
    status: str = Field(default="pending", index=True)
    human_reply: Optional[str] = None
    resume_payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    answered_at: Optional[datetime] = None


class ScheduledTask(SQLModel, table=True):
    """定时任务表：周期性触发智能体的调度配置。

    通过 ``schedule_type`` 与 ``schedule_json``（或 ``rrule``）描述调度规则，
    ``timezone`` 指定时区。包含并发策略（``concurrency_policy``）、误触策略
    （``misfire_policy``）、最大执行次数（``max_runs``）、租约（``lease_*``）等
    调度控制字段，以及下次/上次执行时间用于调度器轮询。
    """

    __tablename__ = "scheduled_tasks"

    id: str = Field(default_factory=lambda: new_id("sched"), primary_key=True)
    tenant_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    created_by_user_id: str = Field(index=True)
    title: str
    prompt: str
    description: Optional[str] = None
    schedule_type: str = Field(default="daily", index=True)
    schedule_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    timezone: str = Field(default="Asia/Shanghai", index=True)
    rrule: Optional[str] = None
    status: str = Field(default="active", index=True)
    concurrency_policy: str = Field(default="forbid", index=True)
    misfire_policy: str = Field(default="coalesce", index=True)
    max_runs: Optional[int] = None
    end_at: Optional[datetime] = Field(default=None, index=True)
    next_run_at: Optional[datetime] = Field(default=None, index=True)
    last_run_at: Optional[datetime] = Field(default=None, index=True)
    last_status: Optional[str] = Field(default=None, index=True)
    run_count: int = 0
    lease_owner: Optional[str] = Field(default=None, index=True)
    lease_until: Optional[datetime] = Field(default=None, index=True)
    source_session_id: Optional[str] = Field(default=None, index=True)
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ScheduledTaskRun(SQLModel, table=True):
    """定时任务执行记录表：单次调度触发的执行实例。

    每次定时任务被触发产生一条运行记录，``scheduled_for`` 为计划触发时间，通过
    ``(scheduled_task_id, scheduled_for)`` 唯一约束防止同一计划时间重复触发。
    记录执行起止时间、结果摘要、错误与执行轨迹（``trace_json``）。
    """

    __tablename__ = "scheduled_task_runs"
    __table_args__ = (
        UniqueConstraint("scheduled_task_id", "scheduled_for", name="uq_scheduled_task_run_due_time"),
    )

    id: str = Field(default_factory=lambda: new_id("schedrun"), primary_key=True)
    tenant_id: str = Field(index=True)
    scheduled_task_id: str = Field(index=True)
    agent_id: str = Field(index=True)
    user_id: str = Field(index=True)
    session_id: Optional[str] = Field(default=None, index=True)
    scheduled_for: datetime = Field(index=True)
    status: str = Field(default="queued", index=True)
    started_at: Optional[datetime] = Field(default=None, index=True)
    finished_at: Optional[datetime] = Field(default=None, index=True)
    result_summary: Optional[str] = None
    error: Optional[str] = None
    trace_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Message(SQLModel, table=True):
    """消息表：会话中的一条消息（用户输入或智能体输出）。

    ``role`` 标识消息角色（user/assistant/system 等），``content`` 为正文，
    ``metadata_json`` 承载附加结构化信息（如引用、工具调用等）。按会话
    （``session_id``）组织。
    """

    __tablename__ = "messages"

    id: str = Field(default_factory=lambda: new_id("msg"), primary_key=True)
    tenant_id: str = Field(index=True)
    session_id: str = Field(index=True)
    role: str
    content: str
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)


class MessageFeedback(SQLModel, table=True):
    """消息反馈表：用户对单条消息的评分与分析结果。

    用户对消息给出 ``rating``（如 positive/negative），系统可进一步对负面反馈
    做自动分析，结果存于 ``analysis_*`` 字段（分桶、原因、摘要、置信度等）。
    通过 ``(tenant_id, message_id, user_id)`` 唯一约束保证同一用户对同一消息
    只有一条反馈。
    """

    __tablename__ = "message_feedback"
    __table_args__ = (UniqueConstraint("tenant_id", "message_id", "user_id", name="uq_feedback_message_user"),)

    id: str = Field(default_factory=lambda: new_id("fb"), primary_key=True)
    tenant_id: str = Field(index=True)
    session_id: str = Field(index=True)
    message_id: str = Field(index=True)
    user_id: str = Field(index=True)
    rating: str = Field(index=True)
    analysis_status: str = Field(default="pending", index=True)
    analysis_bucket: Optional[str] = Field(default=None, index=True)
    analysis_reason: Optional[str] = None
    analysis_summary: Optional[str] = None
    analysis_confidence: Optional[float] = None
    analysis_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    analyzed_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SkillFeedback(SQLModel, table=True):
    """技能反馈表：用户对某技能（具体到步骤）的评分。

    比 ``MessageFeedback`` 更细粒度，可定位到 ``skill_id`` / ``skill_version``
    甚至 ``step_id``，用于评估单个技能步骤的体验质量。通过
    ``(tenant_id, message_id, user_id)`` 唯一约束去重。
    """

    __tablename__ = "skill_feedback"
    __table_args__ = (UniqueConstraint("tenant_id", "message_id", "user_id", name="uq_skill_feedback_message_user"),)

    id: str = Field(default_factory=lambda: new_id("skillfb"), primary_key=True)
    tenant_id: str = Field(index=True)
    skill_id: str = Field(index=True)
    skill_version: Optional[str] = Field(default=None, index=True)
    step_id: Optional[str] = Field(default=None, index=True)
    session_id: str = Field(index=True)
    message_id: str = Field(index=True)
    user_id: str = Field(index=True)
    rating: str = Field(index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AgentEvent(SQLModel, table=True):
    """智能体事件表：智能体执行过程中的事件流。

    以 ``event_type`` 区分事件类型（如思考、工具调用、技能切换等），``payload_json``
    承载事件负载。事件按会话组织，可用于审计、回放与前端轨迹展示。
    """

    __tablename__ = "agent_events"

    id: str = Field(default_factory=lambda: new_id("evt"), primary_key=True)
    tenant_id: str = Field(index=True)
    session_id: str = Field(index=True)
    event_type: str = Field(index=True)
    payload_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)


class MemoryRecord(SQLModel, table=True):
    """记忆记录表：智能体的长期记忆条目。

    以 ``kind`` 区分记忆类型（如 conversation / fact / preference），``content``
    为记忆正文，``importance`` 为重要度评分（用于检索排序与遗忘策略）。
    记忆可关联到用户、会话，支撑跨会话的个性化与上下文延续。
    """

    __tablename__ = "memories"

    id: str = Field(default_factory=lambda: new_id("mem"), primary_key=True)
    tenant_id: str = Field(index=True)
    user_id: str = Field(index=True)
    username: Optional[str] = Field(default=None, index=True)
    session_id: Optional[str] = Field(default=None, index=True)
    kind: str = Field(default="conversation", index=True)
    content: str
    importance: float = 0.5
    metadata_json: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
