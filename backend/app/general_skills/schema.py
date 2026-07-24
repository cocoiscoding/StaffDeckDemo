"""通用技能数据模型与请求/响应 Schema 定义。

本模块定义了通用技能（General Skill）相关的 Pydantic 模型，涵盖：
- 技能文件结构（GeneralSkillFile）
- 导入请求（Import / ClawHub / PackageUpload）
- 技能读取视图（GeneralSkillRead）
- 运行请求/响应（RunRequest / RunResponse）
- 技能路由选择（GeneralSkillSelection）
- 执行计划与审查（ExecutionPlan / ExecutionReview）
- 最终回复（GeneralSkillReply）
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class GeneralSkillFile(BaseModel):
    """通用技能包中的单个文件描述。"""

    path: str
    content: str
    size: Optional[int] = None
    mime_type: Optional[str] = None


class GeneralSkillImportRequest(BaseModel):
    """通用技能导入请求体。

    支持从 Markdown 文本、附带文件列表等方式导入一个通用技能。
    """

    tenant_id: str
    agent_id: Optional[str] = None
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    homepage: Optional[str] = None
    markdown: Optional[str] = None
    files: list[GeneralSkillFile] = Field(default_factory=list)
    status: str = "published"
    original_slug: Optional[str] = None


class GeneralSkillClawHubImportRequest(BaseModel):
    """从 ClawHub 远程源导入通用技能的请求体。"""

    tenant_id: str
    agent_id: Optional[str] = None
    source: str
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    homepage: Optional[str] = None
    status: str = "published"


class GeneralSkillPackageUploadRequest(BaseModel):
    """通过 Base64 编码包上传导入通用技能的请求体。"""

    tenant_id: str
    agent_id: Optional[str] = None
    filename: str
    content_base64: str
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    homepage: Optional[str] = None
    status: str = "published"


class GeneralSkillRead(BaseModel):
    """通用技能的完整读取视图，用于 API 返回。"""

    id: str
    tenant_id: str
    slug: str
    name: str
    description: Optional[str] = None
    homepage: Optional[str] = None
    skill_markdown: str
    skill_files: list[GeneralSkillFile] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    status: str
    permissions: dict[str, Any] = Field(default_factory=dict)
    runtime_config: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class GeneralSkillRunRequest(BaseModel):
    """通用技能运行请求体。"""

    tenant_id: str
    agent_id: Optional[str] = None
    user_id: str = ""
    query: str
    session_id: Optional[str] = None
    model_config_id: Optional[str] = None
    max_attempts: int = Field(default=10, ge=1, le=10)


class GeneralSkillRunResponse(BaseModel):
    """通用技能运行结果响应体。

    包含执行轨迹、生成的代码、标准输出/错误输出、结构化结果以及面向用户的最终回复。
    """

    skill_slug: str
    execution_trace: list[dict[str, Any]] = Field(default_factory=list)
    generated_code: str = ""
    stdout: str = ""
    stderr: str = ""
    structured_result: dict[str, Any] = Field(default_factory=dict)
    reply: str


class GeneralSkillSelection(BaseModel):
    """技能路由选择结果——决定是否使用通用技能以及选择哪个技能。"""

    use_general_skill: bool = False
    selected_slug: Optional[str] = None
    use_knowledge: bool = False
    knowledge_query: Optional[str] = None
    confidence: float = 0.0
    reason: Optional[str] = None


class GeneralSkillExecutionPlan(BaseModel):
    """通用技能执行计划——由 LLM 生成的可执行代码与运行时信息。"""

    code: str
    runtime: str = "python"
    rationale: Optional[str] = None
    expected_output: Optional[str] = None


class GeneralSkillExecutionReview(BaseModel):
    """执行结果审查结论——决定是否需要重试或终止。"""

    result_sufficient: bool = False
    needs_retry: bool = False
    terminal: bool = False
    reason: str = ""
    repair_hint: Optional[str] = None


class GeneralSkillReply(BaseModel):
    """通用技能最终面向用户的文本回复。"""

    reply: str
