"""工具管理 API 请求/响应模式定义模块。

本模块定义了工具管理系统所有 REST API 端点使用的 Pydantic 模型，
包括 HTTP 工具和 MCP Server 的创建、读取、更新请求和响应模式，
以及工具调用（ToolCall）、工具结果（ToolResult）和工具探测（Probe）等模型。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ToolCreateRequest(BaseModel):
    """创建工具的请求模型。"""

    tenant_id: str
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    bucket: str = "未分桶"
    tool_type: Literal["http", "mcp"] = "http"
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    auth: dict[str, Any] = Field(default_factory=dict)
    mcp_config: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    allowed_skills: list[str] = Field(default_factory=list)
    enabled: bool = True


class ToolUpdateRequest(ToolCreateRequest):
    """更新工具的请求模型（与创建模型字段相同）。"""


class ToolRead(BaseModel):
    """工具读取响应模型。"""

    id: str
    tenant_id: str
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    bucket: str
    tool_type: str
    method: str
    url: str
    headers: dict[str, Any]
    auth: dict[str, Any]
    mcp_config: dict[str, Any]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    allowed_skills: list[str]
    mcp_server_id: Optional[str] = None
    enabled: bool
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class ToolBucketRead(BaseModel):
    """工具分桶统计响应模型。"""

    bucket: str
    total: int
    enabled_count: int
    disabled_count: int
    tool_ids: list[str] = Field(default_factory=list)


class ToolCall(BaseModel):
    """工具调用请求模型。

    Attributes:
        name: 工具名称。
        arguments: 工具调用参数字典。
    """

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolError(BaseModel):
    """工具执行错误模型。

    Attributes:
        code: 错误代码（如 NOT_FOUND、TIMEOUT、HTTP_ERROR）。
        message: 人类可读的错误描述。
    """

    code: str
    message: str


class ToolResult(BaseModel):
    """工具执行结果模型。

    Attributes:
        tool_name: 工具名称。
        success: 是否执行成功。
        data: 成功时返回的数据（JSON 或文本）。
        error: 失败时的错误信息。
    """

    tool_name: str
    success: bool
    data: Optional[Any] = None
    error: Optional[ToolError] = None


class ToolTestRequest(BaseModel):
    """工具测试请求模型。"""

    tenant_id: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolProbeRequest(BaseModel):
    """工具探测请求模型（未保存前直接测试）。"""

    tenant_id: str
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    bucket: str = "技能自发现工具"
    tool_type: Literal["http", "mcp"] = "http"
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    url: str
    headers: dict[str, str] = Field(default_factory=dict)
    auth: dict[str, Any] = Field(default_factory=dict)
    mcp_config: dict[str, Any] = Field(default_factory=dict)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    sample_arguments: dict[str, Any] = Field(default_factory=dict)


class ToolProbeResponse(BaseModel):
    """工具探测响应模型。

    Attributes:
        success: 探测是否成功。
        status_code: HTTP 响应状态码。
        data_preview: 响应数据预览。
        inferred_output_schema: 从响应推断的输出 Schema。
        error: 失败时的错误信息。
    """

    success: bool
    status_code: Optional[int] = None
    data_preview: Optional[Any] = None
    inferred_output_schema: dict[str, Any] = Field(default_factory=dict)
    error: Optional[ToolError] = None


# MCP 支持的传输类型
MCPTransport = Literal["stdio", "streamable_http", "sse", "builtin"]


class MCPServerConnection(BaseModel):
    """MCP Server 连接配置（对齐标准 MCP Client 的连接语义）。"""

    transport: MCPTransport = "streamable_http"
    url: Optional[str] = None
    headers: dict[str, str] = Field(default_factory=dict)
    command: Optional[str] = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: Optional[str] = None


class MCPServerCreateRequest(BaseModel):
    """创建 MCP Server 的请求模型。"""

    tenant_id: str
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    bucket: str = "MCP 工具"
    connection: MCPServerConnection = Field(default_factory=MCPServerConnection)
    enabled: bool = True


class MCPServerUpdateRequest(MCPServerCreateRequest):
    """更新 MCP Server 的请求模型（与创建模型字段相同）。"""


class MCPDiscoveredTool(BaseModel):
    """MCP 发现的单个工具信息。"""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    # 该工具是否已同步为 Tool 行
    imported: bool = False
    tool_id: Optional[str] = None
    enabled: Optional[bool] = None


class MCPServerRead(BaseModel):
    """MCP Server 读取响应模型。"""

    id: str
    tenant_id: str
    name: str
    display_name: Optional[str] = None
    description: Optional[str] = None
    bucket: str
    connection: MCPServerConnection
    enabled: bool
    last_synced_at: Optional[str] = None
    tool_count: int = 0
    created_at: str
    updated_at: str


class MCPDiscoverRequest(BaseModel):
    """MCP 工具发现请求模型。"""

    tenant_id: str
    # 未保存前用连接配置直接探测；已保存则可只传 server_id
    connection: Optional[MCPServerConnection] = None


class MCPDiscoverResponse(BaseModel):
    """MCP 工具发现响应模型。"""

    success: bool
    tools: list[MCPDiscoveredTool] = Field(default_factory=list)
    error: Optional[ToolError] = None


class MCPSyncRequest(BaseModel):
    """MCP 工具同步请求模型。"""

    tenant_id: str
    # 需要导入/更新的工具名；为空表示导入全部发现到的工具
    tool_names: Optional[list[str]] = None


class MCPSyncResponse(BaseModel):
    """MCP 工具同步响应模型。"""

    success: bool
    imported: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    error: Optional[ToolError] = None
