"""可观测性 Span（调用链）模块。

提供基于 ContextVar 的轻量级链路追踪能力，支持：

1. **Span 事件发射**：通过 ``emit_span_event`` 将 LLM 调用、工具执行等
   关键操作的开始/结束/失败事件发送到绑定的 sink（如 EventLog）。
2. **嵌套上下文**：通过 ``observed_span`` 和 ``llm_operation`` 实现父子 Span
   的层级关联和属性继承。
3. **手动 Span**：``ManualSpan`` 数据类封装一次操作的计时、状态和属性，
   可在 ``start_llm_call`` 等场景中独立使用。

所有观测逻辑均为非侵入式：sink 未绑定时静默跳过，不会影响业务请求。
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import perf_counter
from typing import Any
from uuid import uuid4


# Span 事件回调类型：(event_type, payload) -> None
SpanSink = Callable[[str, dict[str, Any]], None]


# 以下 ContextVar 构成线程安全的 span 上下文栈：
# - _span_sink: 当前生效的事件输出回调（通常绑定到 EventLog.record）
# - _span_operation: 当前 LLM 操作名称（如 "memory.capture"、"feedback.analyze"）
# - _span_attributes: 当前操作的附加属性字典
# - _parent_span_id: 父 span 的唯一标识，用于构建调用链
_span_sink: ContextVar[SpanSink | None] = ContextVar("span_sink", default=None)
_span_operation: ContextVar[str] = ContextVar("span_operation", default="llm.request")
_span_attributes: ContextVar[dict[str, Any]] = ContextVar("span_attributes", default={})
_parent_span_id: ContextVar[str | None] = ContextVar("parent_span_id", default=None)


def _utc_iso() -> str:
    """返回当前 UTC 时间的 ISO 格式字符串（不含时区后缀）。"""
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def _span_id() -> str:
    """生成唯一的 span 标识符。"""
    return f"span_{uuid4().hex[:16]}"


def emit_span_event(event_type: str, payload: dict[str, Any]) -> None:
    """向当前绑定的 sink 发射一个 span 事件。

    如果当前上下文没有绑定 sink，则静默跳过。
    sink 执行异常也会被吞掉，确保观测逻辑不会中断业务流程。

    Args:
        event_type: 事件类型，如 ``"llm_call_started"``、``"llm_call_finished"``。
        payload: 事件负载数据。
    """
    sink = _span_sink.get()
    if sink is None:
        return
    try:
        sink(event_type, payload)
    except Exception:
        # Observability must never turn a successful business request into a failure.
        return


@contextmanager
def bind_span_sink(sink: SpanSink) -> Iterator[None]:
    """在 ``with`` 代码块内绑定一个 span 事件输出回调。

    退出时自动恢复之前的 sink 绑定状态。

    Args:
        sink: 事件输出回调函数。

    Yields:
        None（上下文管理器不产生值）。
    """
    token = _span_sink.set(sink)
    try:
        yield
    finally:
        _span_sink.reset(token)


def set_span_sink(sink: SpanSink):  # noqa: ANN201 - ContextVar token type is implementation-specific.
    """全局设置 span 事件输出回调（非上下文管理器方式）。

    与 ``bind_span_sink`` 不同，此函数不使用 ``with`` 语句，
    调用者需自行保存返回的 token 并在适当时机调用 ``reset_span_sink``。

    Args:
        sink: 事件输出回调函数。

    Returns:
        ContextVar 的 token，用于后续恢复。
    """
    return _span_sink.set(sink)


def reset_span_sink(token: Any) -> None:
    """恢复 ``set_span_sink`` 之前的状态。

    Args:
        token: ``set_span_sink`` 返回的 ContextVar token。
    """
    _span_sink.reset(token)


@contextmanager
def llm_operation(operation: str, **attributes: Any) -> Iterator[None]:
    """在 ``with`` 代码块内设置当前 LLM 操作名称和附加属性。

    进入时设置操作名并合并属性到当前上下文属性栈，
    退出时恢复之前的操作名和属性。嵌套调用会形成属性继承链。

    Args:
        operation: 操作名称，如 ``"memory.capture"``、``"feedback.analyze"``。
        **attributes: 附加属性键值对，会与上层属性合并。

    Yields:
        None。
    """
    operation_token = _span_operation.set(operation)
    attributes_token = _span_attributes.set({**_span_attributes.get(), **attributes})
    try:
        yield
    finally:
        _span_attributes.reset(attributes_token)
        _span_operation.reset(operation_token)


@contextmanager
def llm_span_attributes(**attributes: Any) -> Iterator[None]:
    """在 ``with`` 代码块内临时追加 span 属性（不改变操作名）。

    Args:
        **attributes: 附加属性键值对。

    Yields:
        None。
    """
    token = _span_attributes.set({**_span_attributes.get(), **attributes})
    try:
        yield
    finally:
        _span_attributes.reset(token)


def current_llm_operation() -> str:
    """获取当前上下文中的 LLM 操作名称。"""
    return _span_operation.get()


@dataclass
class ManualSpan:
    """手动管理的 Span 数据类，封装一次操作的计时和追踪信息。

    创建时自动发射 ``{prefix}_started`` 事件；调用 ``finish``
    或 ``fail`` 时发射对应结束事件。

    Attributes:
        event_prefix: 事件类型前缀，如 ``"llm_call"``。
        operation: 操作名称。
        attributes: 附加属性字典。
        span_id: 唯一标识符（自动生成）。
        parent_span_id: 父 span ID（从上下文自动继承）。
        started_at: 开始时间 ISO 字符串。
    """

    event_prefix: str
    operation: str
    attributes: dict[str, Any] = field(default_factory=dict)
    span_id: str = field(default_factory=_span_id)
    parent_span_id: str | None = field(default_factory=lambda: _parent_span_id.get())
    started_at: str = field(default_factory=_utc_iso)
    _started_perf: float = field(default_factory=perf_counter)
    _finished: bool = False

    def __post_init__(self) -> None:
        """对象初始化后自动发射 started 事件。"""
        emit_span_event(f"{self.event_prefix}_started", self._payload())

    def elapsed_ms(self) -> float:
        """返回从创建到当前的耗时（毫秒，保留 3 位小数）。"""
        return round((perf_counter() - self._started_perf) * 1000, 3)

    def finish(self, *, status: str = "success", **attributes: Any) -> None:
        """标记 span 成功结束并发射 finished 事件。

        重复调用会被忽略（幂等）。

        Args:
            status: 结束状态，默认为 ``"success"``。
            **attributes: 结束时附加的属性。
        """
        if self._finished:
            return
        self._finished = True
        emit_span_event(
            f"{self.event_prefix}_finished",
            self._payload(
                finished_at=_utc_iso(),
                duration_ms=self.elapsed_ms(),
                status=status,
                **attributes,
            ),
        )

    def fail(self, error: BaseException, **attributes: Any) -> None:
        """标记 span 因异常失败并发射 failed 事件。

        重复调用会被忽略（幂等）。

        Args:
            error: 导致失败的异常对象。
            **attributes: 失败时附加的属性。
        """
        if self._finished:
            return
        self._finished = True
        emit_span_event(
            f"{self.event_prefix}_failed",
            self._payload(
                finished_at=_utc_iso(),
                duration_ms=self.elapsed_ms(),
                status="failed",
                error_type=error.__class__.__name__,
                error=str(error)[:500],
                **attributes,
            ),
        )

    def _payload(self, **attributes: Any) -> dict[str, Any]:
        """组装 span 事件的 payload 字典。"""
        return {
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "operation": self.operation,
            "started_at": self.started_at,
            **self.attributes,
            **attributes,
        }


@contextmanager
def observed_span(
    event_prefix: str,
    operation: str,
    **attributes: Any,
) -> Iterator[ManualSpan]:
    """创建一个受上下文管理的 Span，自动处理成功/失败结束。

    在 ``with`` 块内将此 span 设为父 span（通过 ``_parent_span_id``），
    使得块内创建的子 span 能正确关联到当前 span。
    异常退出时自动标记为失败并重新抛出异常。

    Args:
        event_prefix: 事件类型前缀。
        operation: 操作名称。
        **attributes: 附加属性。

    Yields:
        已发射 started 事件的 ``ManualSpan`` 实例。
    """
    span = ManualSpan(event_prefix, operation, attributes)
    parent_token = _parent_span_id.set(span.span_id)
    try:
        yield span
    except BaseException as exc:
        span.fail(exc)
        raise
    else:
        span.finish()
    finally:
        _parent_span_id.reset(parent_token)


def start_llm_call(**attributes: Any) -> ManualSpan:
    """启动一个 LLM 调用 span，继承当前上下文的操作名和属性。

    返回的 span 不会自动结束，调用者需在合适时机调用
    ``span.finish()`` 或 ``span.fail(exc)``。

    Args:
        **attributes: 附加属性，与当前上下文属性合并。

    Returns:
        已发射 started 事件的 ``ManualSpan`` 实例。
    """
    return ManualSpan(
        "llm_call",
        _span_operation.get(),
        {**_span_attributes.get(), **attributes},
    )
