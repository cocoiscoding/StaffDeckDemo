"""定时任务（Scheduled Tasks）数据模型模块。

定义定时任务相关的 Pydantic 模型，包括：

- ``ScheduledTaskBase``：任务基础字段（调度类型、时区、并发策略等）。
- ``ScheduledTaskCreateRequest`` / ``ScheduledTaskUpdateRequest``：创建/更新请求模型。
- ``ScheduledTaskDraftRequest`` / ``ScheduledTaskDraftRead``：LLM 草案检测请求/响应模型。
- ``ScheduledTaskRead`` / ``ScheduledTaskRunRead``：任务和执行记录的读取响应模型。

调度类型（ScheduleType）支持：``once``（一次性）、``daily``（每天）、
``weekly``（每周）、``monthly``（每月）。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


ScheduleType = Literal["once", "daily", "weekly", "monthly"]
ScheduledTaskStatus = Literal["active", "paused", "completed", "archived"]
ConcurrencyPolicy = Literal["forbid", "allow"]
MisfirePolicy = Literal["coalesce", "skip"]


class ScheduledTaskBase(BaseModel):
    """定时任务基础模型，包含调度和策略的完整字段定义。"""

    tenant_id: str
    agent_id: str
    title: str
    prompt: str
    description: Optional[str] = None
    schedule_type: ScheduleType = "daily"
    schedule: dict[str, Any] = Field(default_factory=dict)
    timezone: str = "Asia/Shanghai"
    rrule: Optional[str] = None
    status: ScheduledTaskStatus = "active"
    concurrency_policy: ConcurrencyPolicy = "forbid"
    misfire_policy: MisfirePolicy = "coalesce"
    max_runs: Optional[int] = None
    end_at: Optional[str] = None
    source_session_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ScheduledTaskCreateRequest(ScheduledTaskBase):
    """定时任务创建请求模型。"""

    pass


class ScheduledTaskUpdateRequest(BaseModel):
    """定时任务更新请求模型，所有字段可选（部分更新）。"""

    tenant_id: str
    agent_id: Optional[str] = None
    title: Optional[str] = None
    prompt: Optional[str] = None
    description: Optional[str] = None
    schedule_type: Optional[ScheduleType] = None
    schedule: Optional[dict[str, Any]] = None
    timezone: Optional[str] = None
    rrule: Optional[str] = None
    status: Optional[ScheduledTaskStatus] = None
    concurrency_policy: Optional[ConcurrencyPolicy] = None
    misfire_policy: Optional[MisfirePolicy] = None
    max_runs: Optional[int] = None
    end_at: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None


class ScheduledTaskDraftRequest(BaseModel):
    """定时任务草案检测请求模型，用于从用户消息中解析任务草案。"""

    tenant_id: str
    agent_id: str
    session_id: Optional[str] = None
    message: str
    timezone: Optional[str] = None


class ScheduledTaskDraftRead(BaseModel):
    """定时任务草案检测结果模型，包含 LLM 解析出的可编辑任务预填充字段。"""

    should_create: bool
    tenant_id: str
    agent_id: str
    title: str = ""
    prompt: str = ""
    description: Optional[str] = None
    schedule_type: ScheduleType = "daily"
    schedule: dict[str, Any] = Field(default_factory=dict)
    timezone: str = "Asia/Shanghai"
    rrule: Optional[str] = None
    confidence: float = 0.0
    reason: Optional[str] = None
    source_session_id: Optional[str] = None


class ScheduledTaskRead(BaseModel):
    """定时任务读取响应模型，包含任务完整状态和调度信息。"""

    id: str
    tenant_id: str
    agent_id: str
    created_by_user_id: str
    title: str
    prompt: str
    description: Optional[str] = None
    schedule_type: str
    schedule: dict[str, Any] = Field(default_factory=dict)
    timezone: str
    rrule: Optional[str] = None
    status: str
    concurrency_policy: str
    misfire_policy: str
    max_runs: Optional[int] = None
    end_at: Optional[str] = None
    next_run_at: Optional[str] = None
    last_run_at: Optional[str] = None
    last_status: Optional[str] = None
    run_count: int
    source_session_id: Optional[str] = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)


class ScheduledTaskRunRead(BaseModel):
    """定时任务执行记录读取响应模型。"""

    id: str
    tenant_id: str
    scheduled_task_id: str
    task_title: Optional[str] = None
    task_status: Optional[str] = None
    agent_id: str
    user_id: str
    session_id: Optional[str] = None
    scheduled_for: str
    status: str
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    result_summary: Optional[str] = None
    error: Optional[str] = None
    trace: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str

    model_config = ConfigDict(from_attributes=True)
