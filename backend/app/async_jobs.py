"""异步任务队列模块。

本模块提供基于线程池的轻量级后台任务管理能力，用于在不阻塞 HTTP 请求
响应的情况下执行耗时操作（如数据导入、批量处理等）。

核心概念
---------
- **AsyncJob**：单个后台任务的状态记录，包含名称、状态、时间戳和错误信息。
- **AsyncJobQueue**：任务队列管理器，内部使用 ``ThreadPoolExecutor`` 执行任务，
  使用 ``threading.Lock`` 保护共享状态（任务字典），确保线程安全。
- **任务生命周期**：``queued``（排队）→ ``running``（执行中）→
  ``succeeded``（成功）或 ``failed``（失败）。
- **历史记录淘汰**：队列容量上限为 ``max_history``，满时优先淘汰已完成的
  旧任务，避免内存无限增长。

与其他模块的关系
-----------------
- 依赖 :mod:`app.db.models` 的 ``new_id`` 和 ``utc_now`` 工具函数；
- 被 :mod:`app.api` 下的各路由模块用于提交和查询后台任务；
- 被 :mod:`app.main` 在应用关闭时调用 ``shutdown_async_jobs`` 清理线程池。
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from threading import Lock
from typing import Any

from app.db.models import new_id, utc_now

# 任务状态的类型别名，取值为 "queued" / "running" / "succeeded" / "failed"
AsyncJobStatus = str


@dataclass
class AsyncJob:
    """后台任务的状态记录。

    使用 dataclass 定义，作为任务队列中每个任务的元数据和状态载体。
    所有时间字段均使用 UTC 时间。

    Attributes:
        id: 任务唯一标识（前缀 ``job-``）。
        name: 人类可读的任务名称。
        status: 当前状态（``queued`` / ``running`` / ``succeeded`` / ``failed``）。
        metadata: 任务附带的元数据（如进度、结果摘要等），由调用方自定义。
        created_at: 任务创建时间。
        started_at: 任务开始执行时间（尚未执行时为 ``None``）。
        finished_at: 任务完成时间（成功或失败，尚未完成时为 ``None``）。
        error: 失败时的错误信息（成功时为 ``None``）。
    """

    id: str
    name: str
    status: AsyncJobStatus = "queued"
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class AsyncJobQueue:
    """基于线程池的异步任务队列管理器。

    设计意图：在进程内维护任务状态字典，通过 ``ThreadPoolExecutor`` 并发执行
    任务，通过 ``Lock`` 保证多线程下状态读写的线程安全。不依赖外部消息队列
    或数据库，适合单实例部署场景。

    Attributes:
        _executor: 线程池执行器，负责实际运行任务函数。
        _lock: 线程锁，保护 ``_jobs`` 字典的并发读写。
        _jobs: 任务 ID 到 :class:`AsyncJob` 的映射。
        _max_history: 最多保留的历史任务数量，超出时自动淘汰已完成的旧任务。
    """

    def __init__(self, max_workers: int = 4, max_history: int = 500):
        """初始化任务队列。

        Args:
            max_workers: 线程池最大并发工作线程数。
            max_history: 队列中最多保留的任务记录数量。
        """
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ultrarag-job")
        self._lock = Lock()
        self._jobs: dict[str, AsyncJob] = {}
        self._max_history = max_history

    def enqueue(
        self,
        name: str,
        func: Callable[..., Any],
        *args: Any,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> AsyncJob:
        """提交一个后台任务到队列并立即返回。

        任务创建后状态为 ``queued``，线程池调度后自动转为 ``running``，
        执行完毕后转为 ``succeeded`` 或 ``failed``。

        Args:
            name: 任务名称（用于展示和识别）。
            func: 待执行的函数（将在后台线程中调用）。
            *args: 传递给 ``func`` 的位置参数。
            metadata: 任务附带的元数据字典（可选）。
            **kwargs: 传递给 ``func`` 的关键字参数。

        Returns:
            新创建的 :class:`AsyncJob` 实例（此时状态通常为 ``queued``）。
        """
        job = AsyncJob(id=new_id("job"), name=name, metadata=metadata or {})
        with self._lock:
            self._jobs[job.id] = job  # 注册到任务字典
            self._trim_history_locked()  # 清理超出的已完成任务
        self._executor.submit(self._run_job, job.id, func, args, kwargs)  # 提交到线程池
        return job

    def get(self, job_id: str) -> AsyncJob | None:
        """根据任务 ID 查询单个任务。

        Args:
            job_id: 任务 ID。

        Returns:
            :class:`AsyncJob` 实例；若不存在返回 ``None``。
        """
        with self._lock:
            return self._jobs.get(job_id)

    def list_recent(self, limit: int = 100) -> list[AsyncJob]:
        """按创建时间倒序返回最近的若干个任务。

        Args:
            limit: 最多返回的任务数量。

        Returns:
            按创建时间从新到旧排列的任务列表。
        """
        with self._lock:
            rows = sorted(self._jobs.values(), key=lambda item: item.created_at, reverse=True)
        return rows[:limit]

    def shutdown(self) -> None:
        """关闭线程池，释放资源。

        使用 ``wait=False`` 表示不等待正在执行的任务完成即关闭，
        ``cancel_futures=False`` 表示不取消已提交但尚未开始的任务。
        """
        self._executor.shutdown(wait=False, cancel_futures=False)

    def _run_job(
        self,
        job_id: str,
        func: Callable[..., Any],
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> None:
        """在后台线程中执行任务的内部方法。

        这是实际提交给线程池的包装函数，负责更新任务状态、捕获异常，
        确保任务无论成功还是失败都不会导致线程池崩溃。

        Args:
            job_id: 任务 ID。
            func: 待执行的函数。
            args: 位置参数元组。
            kwargs: 关键字参数字典。
        """
        self._update(job_id, status="running", started_at=utc_now())  # 标记为执行中
        try:
            func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 后台任务绝不能因异常导致请求路径崩溃
            # 捕获所有异常，记录错误信息，标记任务失败
            self._update(job_id, status="failed", finished_at=utc_now(), error=str(exc))
            return
        self._update(job_id, status="succeeded", finished_at=utc_now(), error=None)  # 标记为成功

    def _update(self, job_id: str, **changes: Any) -> None:
        """线程安全地更新指定任务的属性。

        Args:
            job_id: 任务 ID。
            **changes: 要更新的属性键值对（如 ``status="failed"``）。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return  # 任务可能已被淘汰清理
            for key, value in changes.items():
                setattr(job, key, value)

    def _trim_history_locked(self) -> None:
        """清理超出容量上限的已完成任务（必须在持有 ``_lock`` 时调用）。

        仅淘汰状态为 ``succeeded`` 或 ``failed`` 的任务，按创建时间
        从旧到新依次移除，直到数量降至 ``_max_history`` 以内。
        """
        overflow = len(self._jobs) - self._max_history
        if overflow <= 0:
            return  # 未超限，无需清理
        # 筛选已完成的任务，按创建时间升序排列（最旧的优先淘汰）
        removable = sorted(
            (
                job
                for job in self._jobs.values()
                if job.status in {"succeeded", "failed"}
            ),
            key=lambda item: item.created_at,
        )
        for job in removable[:overflow]:
            self._jobs.pop(job.id, None)


# 全局默认任务队列实例（应用级单例）
_default_queue = AsyncJobQueue()


def enqueue_async_job(
    name: str,
    func: Callable[..., Any],
    *args: Any,
    metadata: dict[str, Any] | None = None,
    **kwargs: Any,
) -> AsyncJob:
    """向全局默认任务队列提交后台任务的便捷函数。

    Args:
        name: 任务名称。
        func: 待执行的函数。
        *args: 传递给 ``func`` 的位置参数。
        metadata: 任务元数据（可选）。
        **kwargs: 传递给 ``func`` 的关键字参数。

    Returns:
        新创建的 :class:`AsyncJob` 实例。
    """
    return _default_queue.enqueue(name, func, *args, metadata=metadata, **kwargs)


def get_async_job_queue() -> AsyncJobQueue:
    """获取全局默认任务队列实例。

    Returns:
        :class:`AsyncJobQueue` 单例实例。
    """
    return _default_queue


def shutdown_async_jobs() -> None:
    """关闭全局默认任务队列的线程池。

    通常在应用关闭时调用，确保线程资源正确释放。
    """
    _default_queue.shutdown()
