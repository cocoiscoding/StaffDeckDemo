"""Agent 资源管理的 Pydantic 请求/响应模型定义模块。

本模块定义了 Agent 配置管理（创建/更新/查询）、资源绑定（技能、知识库、工具、通用技能）、
模型绑定、以及工作记录（Work Record）等所有 API 端点所需的输入和输出数据结构。
这些模型通过 Pydantic 的类型验证确保 API 层数据的完整性和一致性。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# Agent 可绑定的资源类型枚举：技能(skill)、通用技能(general_skill)、知识库(knowledge_base)、工具(tool)
AgentResourceType = Literal["skill", "general_skill", "knowledge_base", "tool"]

# 工作记录事件的类别：对话(chat)、任务(task)、SOP(sop)、工具(tool)、知识(knowledge)、技能(skill)
AgentWorkRecordEventKind = Literal["chat", "task", "sop", "tool", "knowledge", "skill"]

# 工作记录事件所处的阶段：回复(reply)、上次运行(last_run)、下次运行(next_run)、已分配(assigned)
AgentWorkRecordEventPhase = Literal["reply", "last_run", "next_run", "assigned"]


class AgentProfileCreateRequest(BaseModel):
    """创建 Agent 配置的请求体模型。

    支持从空白创建或从现有 Agent 复制资源范围。
    """

    tenant_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    persona_prompt: Optional[str] = None
    is_overall: bool = False
    source_mode: Literal["copy", "blank"] = "copy"
    copy_from_agent_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentProfileUpdateRequest(BaseModel):
    """更新 Agent 配置的请求体模型。

    所有字段均为可选，仅更新传入的字段。
    """

    tenant_id: str
    name: Optional[str] = None
    description: Optional[str] = None
    persona_prompt: Optional[str] = None
    status: Optional[Literal["active", "archived"]] = None
    metadata: Optional[dict[str, Any]] = None


class AgentResourceBindingRead(BaseModel):
    """Agent 资源绑定关系的读取模型，用于 API 响应。

    表示一个 Agent 与某个具体资源（技能/知识库/工具等）之间的绑定关系。
    """

    id: str
    tenant_id: str
    agent_id: str
    resource_type: AgentResourceType
    resource_id: str
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class AgentProfileRead(BaseModel):
    """Agent 配置的完整读取模型，用于 API 响应。

    包含 Agent 的基本信息及其关联的资源绑定列表。
    """

    id: str
    tenant_id: str
    name: str
    description: Optional[str] = None
    persona_prompt: Optional[str] = None
    is_overall: bool
    status: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    resources: list[AgentResourceBindingRead] = Field(default_factory=list)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class AgentScopeRead(BaseModel):
    """Agent 作用域的读取模型。

    返回某个租户下所有可见的 Agent 配置列表。
    """

    tenant_id: str
    agents: list[AgentProfileRead] = Field(default_factory=list)


class AgentWorkRecordReplyStatsRead(BaseModel):
    """Agent 工作记录中的回复统计模型。

    包含总回复数、当日回复数及按日期分组的回复统计。
    """

    total: int = 0
    today: int = 0
    by_day: dict[str, int] = Field(default_factory=dict)


class AgentWorkRecordEventRead(BaseModel):
    """Agent 工作记录中的单条时间线事件模型。

    每条事件记录其类型、阶段、时间戳和展示标签。
    """

    id: str
    kind: AgentWorkRecordEventKind
    phase: AgentWorkRecordEventPhase
    timestamp: str
    label: str = ""


class AgentWorkRecordRead(BaseModel):
    """Agent 工作记录的完整读取模型。

    聚合了回复统计和时间线事件，用于展示 Agent 的工作概览。
    """

    agent_id: str
    timezone: str
    generated_at: str
    reply_stats: AgentWorkRecordReplyStatsRead
    events: list[AgentWorkRecordEventRead] = Field(default_factory=list)


class AgentResourceBindingInput(BaseModel):
    """Agent 资源绑定的输入模型，用于创建/更新绑定时指定资源信息。"""

    resource_type: AgentResourceType
    resource_id: str
    status: Literal["active", "inactive"] = "active"
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentResourcesUpdateRequest(BaseModel):
    """批量更新 Agent 资源绑定的请求体模型。

    通过传入完整的资源绑定列表，实现全量替换式的绑定更新。
    """

    tenant_id: str
    resources: list[AgentResourceBindingInput] = Field(default_factory=list)


class AgentResourceImportRequest(BaseModel):
    """从其他 Agent 导入资源的请求体模型。

    允许将源 Agent 的指定资源导入到目标 Agent。
    """

    tenant_id: str
    source_agent_id: str
    resource_type: AgentResourceType
    resource_ids: list[str] = Field(default_factory=list)


class AgentModelBindingInput(BaseModel):
    """Agent 模型绑定的输入模型，用于为不同角色指定 LLM 模型配置。

    角色包括：默认(default)、路由(router)、步骤(step)、响应(response)、通用技能(general_skill)。
    """

    role: Literal["default", "router", "step", "response", "general_skill"]
    model_config_id: str


class AgentModelsUpdateRequest(BaseModel):
    """批量更新 Agent 模型绑定的请求体模型。"""

    tenant_id: str
    bindings: list[AgentModelBindingInput] = Field(default_factory=list)


class AgentSkillRollbackRequest(BaseModel):
    """Agent 技能分支回滚的请求体模型。

    指定要回滚到的目标版本号。
    """

    tenant_id: str
    version: str
