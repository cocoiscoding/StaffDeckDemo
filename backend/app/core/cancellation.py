"""聊天轮次取消状态管理模块。

本模块提供进程内的、线程安全的"取消标记"机制，用于在多轮对话流式生成过程中
支持用户中断当前轮次（chat turn）。当用户在前端点击"停止生成"时，业务层会调用
``cancel_chat_turn`` 写入取消标记；流式生成协程在每次产出 token 前通过
``is_chat_turn_cancelled`` 轮询该标记，若已被取消则提前终止生成。

核心概念
--------
- **session_id + turn_id 唯一标识一个聊天轮次**：不同会话、不同轮次之间互不影响。
- **进程内内存存储**：取消状态仅存在于当前进程内存中（``set``），不做持久化，
  适用于单实例或同一会话粘滞到同一实例的部署场景。
- **线程安全**：所有读写操作通过同一把 ``Lock`` 串行化，避免并发竞态。

与其他模块的关系
----------------
- 上游：由 API 路由层（如 WebSocket / SSE handler）在收到用户取消请求时调用。
- 下游：由 LLM 流式生成层在生成循环中轮询，决定是否提前 break。
"""

from __future__ import annotations

from threading import Lock

# 全局互斥锁，保护 ``_cancelled_turns`` 的并发读写
_lock = Lock()
# 已被取消的聊天轮次集合，元素为 (session_id, turn_id) 二元组
_cancelled_turns: set[tuple[str, str]] = set()


def cancel_chat_turn(session_id: str, turn_id: str) -> None:
    """将指定聊天轮次标记为已取消。

    当用户主动中断某轮对话生成时调用。写入后，对应的流式生成协程
    可通过 :func:`is_chat_turn_cancelled` 检测到取消信号并停止生成。

    Args:
        session_id: 会话唯一标识，用于隔离不同会话的取消状态。
        turn_id: 轮次唯一标识，用于定位会话内的具体一轮对话。
    """
    if not session_id or not turn_id:
        return
    with _lock:
        _cancelled_turns.add((session_id, turn_id))


def clear_chat_turn_cancelled(session_id: str, turn_id: str) -> None:
    """清除指定聊天轮次的取消标记。

    在轮次正常结束、或被取消后完成清理时调用，从取消集合中移除对应条目，
    避免内存无限增长。使用 ``discard`` 而非 ``remove``，即使条目不存在也不会报错。

    Args:
        session_id: 会话唯一标识。
        turn_id: 轮次唯一标识。
    """
    if not session_id or not turn_id:
        return
    with _lock:
        _cancelled_turns.discard((session_id, turn_id))


def is_chat_turn_cancelled(session_id: str, turn_id: str) -> bool:
    """查询指定聊天轮次是否已被取消。

    供流式生成协程在生成循环中高频调用（每产出若干 token 检查一次），
    若返回 ``True`` 则应立即终止当前轮次的生成。

    Args:
        session_id: 会话唯一标识。
        turn_id: 轮次唯一标识。

    Returns:
        ``True`` 表示该轮次已被用户取消；``False`` 表示未取消或参数无效。
    """
    if not session_id or not turn_id:
        return False
    with _lock:
        return (session_id, turn_id) in _cancelled_turns
