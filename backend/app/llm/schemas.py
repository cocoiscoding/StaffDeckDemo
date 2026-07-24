"""LLM 模型配置的数据模型（schema）定义。

定义与模型配置相关的请求体与响应体模型，涵盖创建、更新、读取与连通性测试
等场景。所有模型基于 Pydantic，用于 API 层的参数校验与序列化。
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field


class ModelConfigCreateRequest(BaseModel):
    """创建模型配置的请求模型。

    用于在系统中新增一条 LLM 模型配置记录。
    """

    tenant_id: str  # 租户 ID，用于多租户隔离
    name: str  # 配置名称，需在同租户内唯一
    provider: Optional[str] = None  # 模型供应商标识，如 "openai"、"anthropic"
    api_protocol: Optional[str] = None  # API 协议类型，如 "chat_completions"
    base_url: Optional[str] = None  # 模型服务的基础 URL
    api_key: str = Field(default="", repr=False)  # API 密钥，repr=False 以避免日志泄露
    model: str  # 模型标识符，如 "gpt-4"、"claude-3-sonnet"
    temperature: float = 0.2  # 采样温度，控制输出随机性
    max_output_tokens: int = 8192  # 最大输出令牌数
    extra_body: dict[str, Any] = Field(default_factory=dict)  # 传递给模型 API 的额外请求体字段
    protocol_options: Optional[dict[str, Any]] = None  # 协议级别的可选项（如自定义请求头）
    is_default: bool = False  # 是否设为该租户的默认模型配置
    enabled: bool = True  # 是否启用


class ModelConfigUpdateRequest(BaseModel):
    """更新模型配置的请求模型。

    所有字段均为可选，仅更新传入的字段。
    """

    tenant_id: str  # 租户 ID
    name: Optional[str] = None  # 配置名称
    provider: Optional[str] = None  # 模型供应商标识
    api_protocol: Optional[str] = None  # API 协议类型
    base_url: Optional[str] = None  # 模型服务基础 URL
    api_key: Optional[str] = Field(default=None, repr=False)  # API 密钥，不传则保持不变
    model: Optional[str] = None  # 模型标识符
    temperature: Optional[float] = None  # 采样温度
    max_output_tokens: Optional[int] = None  # 最大输出令牌数
    extra_body: Optional[dict[str, Any]] = None  # 额外请求体字段
    protocol_options: Optional[dict[str, Any]] = None  # 协议级可选项
    is_default: Optional[bool] = None  # 是否设为默认配置
    enabled: Optional[bool] = None  # 是否启用


class ModelConfigRead(BaseModel):
    """模型配置的只读响应模型。

    用于 API 响应序列化，包含配置的全部字段及系统维护的元信息。
    """

    id: str  # 配置记录的唯一标识
    tenant_id: str  # 租户 ID
    name: str  # 配置名称
    provider: str  # 模型供应商标识
    api_protocol: str  # API 协议类型
    base_url: Optional[str]  # 模型服务基础 URL
    api_key_masked: str  # 脱敏后的 API 密钥（如 "sk-****abcd"）
    model: str  # 模型标识符
    temperature: float  # 采样温度
    max_output_tokens: int  # 最大输出令牌数
    extra_body: dict[str, Any]  # 额外请求体字段
    protocol_options: dict[str, Any]  # 协议级可选项
    legacy_unmapped_options: dict[str, Any]  # 历史遗留的未映射选项
    trust_status: str  # 信任状态，标识该配置是否通过连通性验证
    verification_attempt_status: str  # 最近一次验证尝试的状态
    config_revision: int  # 配置版本号，每次修改递增
    security_revision: int  # 安全版本号，跟踪安全相关字段的变更
    is_default: bool  # 是否为租户默认配置
    enabled: bool  # 是否启用
    created_at: str  # 创建时间（ISO 格式字符串）
    updated_at: str  # 最后更新时间（ISO 格式字符串）

    #: 允许从 ORM 模型属性自动构造，用于数据库模型到响应模型的转换。
    model_config = ConfigDict(from_attributes=True)


class ModelCapabilityTestResult(BaseModel):
    """单项模型能力测试结果。

    表示对模型某一项能力（如流式输出、函数调用）的测试结论。
    """

    id: str  # 能力标识符，如 "streaming"、"function_calling"
    success: bool  # 该能力测试是否通过
    error_code: Optional[str] = None  # 失败时的错误代码，成功时为 None


class ModelConfigTestResponse(BaseModel):
    """模型配置连通性测试的完整响应。

    包含整体测试结论、用户提示信息、激活状态以及各能力项的明细。
    """

    success: bool  # 整体测试是否成功
    message: str  # 面向用户的提示信息
    activated: bool = False  # 测试通过后是否已自动激活该配置
    model: Optional[ModelConfigRead] = None  # 被测试的模型配置详情
    output: Optional[str] = None  # 测试请求的模型实际输出内容
    attempt_id: Optional[str] = None  # 本次测试尝试的唯一标识
    trust_status: Optional[str] = None  # 测试后的信任状态
    attempt_status: Optional[str] = None  # 本次测试尝试的执行状态
    capabilities: list[ModelCapabilityTestResult] = Field(default_factory=list)  # 各能力项测试明细列表
