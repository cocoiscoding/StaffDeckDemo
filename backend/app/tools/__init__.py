"""工具（Tool）包。

提供外部工具的执行能力，将 HTTP 接口、MCP 服务等外部资源封装为可被
代理调用的工具，统一管理请求构造、鉴权注入与响应解析。

导出对象:
    ToolExecutor: 工具执行器，负责根据工具配置发起请求并返回结构化结果。
"""

from app.tools.tool_executor import ToolExecutor

__all__ = ["ToolExecutor"]
