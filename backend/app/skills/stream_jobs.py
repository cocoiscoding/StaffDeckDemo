"""技能蒸馏 / 编辑流式任务管理。

本模块提供线程安全的流式任务存储，用于在技能蒸馏或编辑等
长时间运行的后台任务中管理事件队列。任务通过唯一 ID 标识，
前端可通过轮询或 SSE 获取增量事件。

核心组件：
- ``SkillStreamEvent``：单个流式事件（序号 + 事件名 + 数据）。
- ``SkillStreamJob``：一个流式任务（包含事件列表、状态等）。
- ``SkillStreamJobStore``：线程安全的任务存储，支持创建、追加事件、
  快照查询、取消、自动淘汰等操作。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from app.db.models import new_id, utc_now


@dataclass
class SkillStreamEvent:
    """单个流式事件数据结构。

    Attributes:
        seq: 事件序号（从 1 开始递增），用于前端增量拉取。
        event: 事件类型名称，如 ``"status"``、``"chunk"``、``"error"``。
        data: 事件携带的数据字典。
    """

    seq: int
    event: str
    data: dict[str, Any]


@dataclass
class SkillStreamJob:
    """流式任务数据结构。

    Attributes:
        id: 任务唯一标识（带 ``skilljob`` 前缀）。
        name: 任务名称。
        tenant_id: 租户 ID。
        user_id: 用户 ID。
        status: 任务状态，取值 ``queued`` / ``running`` / ``succeeded`` / ``failed``。
        events: 该任务累积的流式事件列表。
        error: 任务失败时的错误消息。
        created_at: 创建时间（ISO 格式）。
        updated_at: 最后更新时间（ISO 格式）。
        cancel_requested: 是否已收到取消请求。
    """

    id: str
    name: str
    tenant_id: str
    user_id: str
    status: str = "queued"
    events: list[SkillStreamEvent] = field(default_factory=list)
    error: str | None = None
    created_at: str = field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = field(default_factory=lambda: utc_now().isoformat())
    cancel_requested: bool = False


class SkillStreamJobStore:
    """线程安全的流式任务存储。

    内部使用 ``threading.Lock`` 保护字典操作，支持并发读写。
    当任务总数超过上限时，自动淘汰最早完成的（succeeded / failed）任务。

    Attributes:
        _max_jobs: 最大保留任务数，默认 200。
    """

    def __init__(self, max_jobs: int = 200):
        """初始化任务存储。

        Args:
            max_jobs: 最大保留的任务数量，超出后自动淘汰已完成的旧任务。
        """
        self._lock = Lock()
        self._jobs: dict[str, SkillStreamJob] = {}
        self._max_jobs = max_jobs

    def create(self, name: str, tenant_id: str, user_id: str) -> SkillStreamJob:
        """创建一个新的流式任务并加入存储。

        Args:
            name: 任务名称。
            tenant_id: 租户 ID。
            user_id: 用户 ID。

        Returns:
            新创建的 ``SkillStreamJob`` 实例。
        """
        job = SkillStreamJob(id=new_id("skilljob"), name=name, tenant_id=tenant_id, user_id=user_id)
        with self._lock:
            self._jobs[job.id] = job
            self._trim_locked()
        return job

    def start(self, job_id: str) -> None:
        """将任务状态更新为 ``running``。

        Args:
            job_id: 任务 ID。
        """
        self._update(job_id, status="running")

    def append(self, job_id: str, event: str, data: dict[str, Any]) -> None:
        """向任务追加一个流式事件。

        Args:
            job_id: 任务 ID。
            event: 事件类型名称。
            data: 事件数据字典。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            job.events.append(SkillStreamEvent(seq=len(job.events) + 1, event=event, data=data))
            job.updated_at = utc_now().isoformat()

    def complete(self, job_id: str) -> None:
        """将任务状态标记为 ``succeeded``。

        Args:
            job_id: 任务 ID。
        """
        self._update(job_id, status="succeeded")

    def fail(self, job_id: str, error: str) -> None:
        """将任务标记为 ``failed`` 并追加一个 error 事件。

        Args:
            job_id: 任务 ID。
            error: 错误消息。
        """
        self.append(job_id, "error", {"message": error})
        self._update(job_id, status="failed", error=error)

    def cancel(self, job_id: str) -> None:
        """对任务设置取消请求标记。

        实际的取消由执行端通过 ``is_cancelled`` 轮询检测后执行。

        Args:
            job_id: 任务 ID。
        """
        self._update(job_id, cancel_requested=True)

    def is_cancelled(self, job_id: str) -> bool:
        """检查任务是否已收到取消请求。

        Args:
            job_id: 任务 ID。

        Returns:
            已收到取消请求返回 True，否则返回 False。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            return bool(job and job.cancel_requested)

    def get(self, job_id: str) -> SkillStreamJob | None:
        """获取任务的深拷贝快照（含全部事件）。

        返回的是副本，调用方修改不会影响内部状态。

        Args:
            job_id: 任务 ID。

        Returns:
            任务副本，不存在时返回 None。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            return SkillStreamJob(
                id=job.id,
                name=job.name,
                tenant_id=job.tenant_id,
                user_id=job.user_id,
                status=job.status,
                events=list(job.events),
                error=job.error,
                created_at=job.created_at,
                updated_at=job.updated_at,
                cancel_requested=job.cancel_requested,
            )

    def snapshot(self, job_id: str, after: int = 0) -> tuple[SkillStreamJob | None, list[SkillStreamEvent]]:
        """获取任务元信息快照和增量事件列表。

        仅返回序号大于 ``after`` 的事件，用于前端增量拉取。

        Args:
            job_id: 任务 ID。
            after: 只返回序号大于此值的事件，默认 0 表示返回全部。

        Returns:
            二元组 ``(SkillStreamJob | None, list[SkillStreamEvent])``：
            - 任务副本（events 为空列表，仅含元信息），不存在时为 None。
            - 增量事件列表。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None, []
            events = [event for event in job.events if event.seq > after]
            copy = SkillStreamJob(
                id=job.id,
                name=job.name,
                tenant_id=job.tenant_id,
                user_id=job.user_id,
                status=job.status,
                events=[],
                error=job.error,
                created_at=job.created_at,
                updated_at=job.updated_at,
                cancel_requested=job.cancel_requested,
            )
            return copy, events

    def _update(self, job_id: str, **changes: Any) -> None:
        """在线程锁内更新任务的任意字段。

        Args:
            job_id: 任务 ID。
            **changes: 要更新的字段及值。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return
            for key, value in changes.items():
                setattr(job, key, value)
            job.updated_at = utc_now().isoformat()

    def _trim_locked(self) -> None:
        """在已持有锁的前提下，淘汰超额的已完成任务。

        当任务总数超过 ``_max_jobs`` 时，按 ``updated_at`` 升序
        淘汰最早完成的（succeeded / failed）任务，直到不超过上限。
        """
        overflow = len(self._jobs) - self._max_jobs
        if overflow <= 0:
            return
        removable = sorted(
            (job for job in self._jobs.values() if job.status in {"succeeded", "failed"}),
            key=lambda item: item.updated_at,
        )
        for job in removable[:overflow]:
            self._jobs.pop(job.id, None)


# 全局单例：技能蒸馏 / 编辑流式任务存储
stream_jobs = SkillStreamJobStore()
