"""核心组件包。

导出整个系统赖以运行的核心引擎与基础设施。

导出对象:
    AgentLoop: 代理循环引擎，负责编排多轮推理、工具调用与技能执行的完整流程。
"""

from app.core.agent_loop import AgentLoop

__all__ = ["AgentLoop"]
