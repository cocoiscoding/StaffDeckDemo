"""记忆（Memory）包。

负责代理长期记忆的存储与检索，将对话中产生的重要事实、偏好与事件
结构化持久化，供后续对话上下文召回使用。

导出对象:
    MemoryService: 记忆服务，封装记忆的捕获、检索与过期清理等业务逻辑。
"""

from app.memory.service import MemoryService

__all__ = ["MemoryService"]
