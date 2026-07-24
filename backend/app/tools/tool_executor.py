"""工具执行器模块。

本模块负责根据工具调用请求（ToolCall）查找并执行对应的工具（HTTP 或 MCP），
返回结构化的执行结果（ToolResult）。主要职责包括：

1. 工具查找与权限校验：按租户和名称查找工具，校验启用状态、可见性和技能绑定。
2. HTTP 工具执行：构造请求头、解析密钥引用、发送 HTTP 请求并处理响应。
3. MCP 工具执行：解析 MCP Server 配置，通过 MCP 客户端执行工具调用。
4. 密钥解析：将 ``${secret.NAME}`` 模板替换为环境变量值。
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlmodel import Session, select

from app.agents.branching import visible_tool_rows
from app.config import get_settings
from app.db.models import MCPServer, Tool
from app.tools.http_request import prepare_get_request
from app.tools.mcp_client import MCPClientError, execute_mcp_tool
from app.tools.tool_schema import ToolCall, ToolError, ToolResult
from app.security.internal_service import INTERNAL_SERVICE_HEADER, internal_service_token


# 匹配 ${secret.NAME} 模板，用于从环境变量解析密钥引用
SECRET_PATTERN = re.compile(r"\$\{secret\.([A-Z0-9_]+)\}")


class ToolExecutor:
    """工具执行器。

    负责查找、校验和执行工具调用，支持 HTTP 和 MCP 两种工具类型。

    Attributes:
        db: 数据库会话。
        settings: 全局配置。
    """

    def __init__(self, db: Session):
        self.db = db
        self.settings = get_settings()

    def execute(
        self,
        tenant_id: str,
        tool_call: ToolCall,
        active_skill_id: str | None = None,
        agent_id: str | None = None,
    ) -> ToolResult:
        """执行一次工具调用。

        执行流程：
        1. 按租户和工具名查找工具行，校验存在性、启用状态、可见性和技能绑定。
        2. 根据工具类型分派：MCP 工具走 _execute_mcp_tool，HTTP 工具走 httpx 调用。
        3. HTTP 工具：构造请求头（含密钥解析和内部服务认证），发送请求并解析响应。

        Args:
            tenant_id: 租户 ID。
            tool_call: 工具调用请求（含工具名和参数）。
            active_skill_id: 当前活跃的技能 ID（用于技能绑定校验）。
            agent_id: 当前智能体 ID（用于可见性校验）。

        Returns:
            工具执行结果（成功或失败）。
        """
        with self.db.no_autoflush:
            tool = self.db.exec(
                select(Tool).where(Tool.tenant_id == tenant_id, Tool.name == tool_call.name)
            ).first()
        if not tool:
            return self._error(tool_call.name, "NOT_FOUND", "工具不存在或未配置。")
        if not tool.enabled:
            return self._error(tool.name, "DISABLED", "工具当前未启用。")
        if agent_id and tool.id not in {
            row.id
            for row in visible_tool_rows(self.db, tenant_id, agent_id, include_inactive=False)
        }:
            return self._error(tool.name, "NOT_ALLOWED", "当前员工未启用该工具。")
        if (
            active_skill_id
            and tool.allowed_skills_json
            and active_skill_id not in tool.allowed_skills_json
        ):
            return self._error(tool.name, "NOT_ALLOWED", "当前技能不允许调用该工具。")

        if (tool.tool_type or "http") == "mcp":
            return self._execute_mcp_tool(tool, tool_call.arguments)
        if (tool.tool_type or "http") != "http":
            return self._error(
                tool.name, "UNSUPPORTED_TOOL_TYPE", f"不支持的工具类型：{tool.tool_type}"
            )

        headers = self._request_headers(
            tool.url,
            self._resolve_headers(tool.headers_json or {}, tool.auth_json or {}),
        )
        try:
            with httpx.Client(timeout=self.settings.tool_timeout_seconds) as client:
                if tool.method.upper() == "GET":
                    request_url, request_kwargs = prepare_get_request(tool.url, tool_call.arguments)
                    response = client.request(
                        tool.method.upper(), request_url, headers=headers, **request_kwargs
                    )
                else:
                    response = client.request(
                        tool.method.upper(), tool.url, headers=headers, json=tool_call.arguments
                    )
                response.raise_for_status()
                return ToolResult(
                    tool_name=tool.name,
                    success=True,
                    data=self._response_data(response),
                    error=None,
                )
        except httpx.TimeoutException:
            return self._error(tool.name, "TIMEOUT", "工具调用超时。")
        except httpx.HTTPStatusError as exc:
            return self._error(
                tool.name,
                "HTTP_ERROR",
                f"工具返回异常状态码：{exc.response.status_code}",
            )
        except Exception as exc:
            return self._error(tool.name, "EXECUTION_ERROR", str(exc))

    def _execute_mcp_tool(self, tool: Tool, arguments: dict[str, Any]) -> ToolResult:
        """执行 MCP 类型的工具调用。

        Args:
            tool: 工具行对象。
            arguments: 工具调用参数。

        Returns:
            工具执行结果。
        """
        try:
            config, tool_name = self._resolve_mcp_config(tool)
            data = execute_mcp_tool(
                config,
                arguments,
                timeout_seconds=self.settings.tool_timeout_seconds,
                tool_name=tool_name,
            )
            return ToolResult(tool_name=tool.name, success=True, data=data, error=None)
        except MCPClientError as exc:
            return self._error(tool.name, "MCP_ERROR", str(exc))
        except Exception as exc:
            return self._error(tool.name, "MCP_EXECUTION_ERROR", str(exc))

    def _resolve_mcp_config(self, tool: Tool) -> tuple[dict[str, Any], str | None]:
        """从工具行解析 MCP 客户端配置和实际工具名。

        通过工具关联的 MCPServer 行获取传输配置（URL/command/headers 等），
        并从工具的 config_json 中提取实际的 MCP 工具名。

        Args:
            tool: 工具行对象。

        Returns:
            ``(config, tool_name)`` 元组。

        Raises:
            MCPClientError: 当工具未关联 Server 或 Server 不存在时抛出。
        """
        tool_config = tool.config_json or {}
        tool_name = (
            str(tool_config.get("tool") or tool_config.get("tool_name") or "").strip() or None
        )
        if not tool.mcp_server_id:
            raise MCPClientError("MCP 工具未关联 Server。")
        server = self.db.get(MCPServer, tool.mcp_server_id)
        if server is None:
            raise MCPClientError("MCP 工具关联的 Server 不存在或已删除。")
        return self._server_client_config(server), tool_name

    def _server_client_config(self, server: MCPServer) -> dict[str, Any]:
        """将 MCPServer 行转换为 MCP 客户端连接配置字典。

        根据 transport 类型组装不同的配置字段：
        - streamable_http / sse：url + headers。
        - stdio：command + args + env + cwd。
        - builtin：server="builtin.demo"。

        Args:
            server: MCPServer 数据库行。

        Returns:
            MCP 客户端配置字典。
        """
        transport = server.transport or "streamable_http"
        config: dict[str, Any] = {"transport": transport}
        if transport in {"streamable_http", "sse"}:
            config["url"] = server.url or ""
            if server.headers_json:
                config["headers"] = dict(server.headers_json)
        elif transport == "stdio":
            config["command"] = server.command or ""
            config["args"] = list(server.args_json or [])
            if server.env_json:
                config["env"] = dict(server.env_json)
            if server.cwd:
                config["cwd"] = server.cwd
        elif transport == "builtin":
            config["server"] = "builtin.demo"
        return config

    def _response_data(self, response: httpx.Response) -> Any:
        """解析 HTTP 响应：优先 JSON，失败则返回纯文本。"""
        try:
            return response.json()
        except Exception:
            return response.text

    def _resolve_headers(self, headers: dict[str, Any], auth: dict[str, Any]) -> dict[str, str]:
        """解析请求头和认证信息，将密钥引用替换为实际值。

        Args:
            headers: 原始请求头字典。
            auth: 认证配置字典（支持 bearer token 类型）。

        Returns:
            解析后的请求头字典。
        """
        resolved = {key: self._resolve_secret(str(value)) for key, value in headers.items()}
        if auth.get("type") == "bearer" and auth.get("token"):
            resolved["Authorization"] = f"Bearer {self._resolve_secret(str(auth['token']))}"
        return resolved

    def _request_headers(self, url: str, headers: dict[str, str]) -> dict[str, str]:
        """为请求添加内部服务认证头（当 URL 指向内部 mock 端点时）。

        Args:
            url: 目标 URL。
            headers: 已解析的请求头。

        Returns:
            可能追加了内部服务认证头的请求头字典。
        """
        if not self._is_internal_mock_url(url):
            return headers
        resolved = dict(headers)
        resolved[INTERNAL_SERVICE_HEADER] = internal_service_token()
        return resolved

    def _is_internal_mock_url(self, url: str) -> bool:
        """判断 URL 是否指向内部 mock 端点（``/api/mock/``路径）。

        Args:
            url: 目标 URL。

        Returns:
            是内部 mock 端点返回 True，否则返回 False。
        """
        target = urlsplit(url)
        if not target.path.startswith("/api/mock/"):
            return False
        if not target.scheme and not target.netloc:
            return True
        configured = urlsplit(self.settings.normalized_tool_base_url)
        return (
            target.scheme.lower(),
            target.hostname,
            target.port or _default_port(target.scheme),
        ) == (
            configured.scheme.lower(),
            configured.hostname,
            configured.port or _default_port(configured.scheme),
        )

    def _resolve_secret(self, value: str) -> str:
        """将 ``${secret.NAME}`` 模板替换为对应的环境变量值。

        Args:
            value: 可能包含密钥模板的字符串。

        Returns:
            替换后的字符串（环境变量不存在时替换为空字符串）。
        """
        def repl(match: re.Match[str]) -> str:
            return os.getenv(match.group(1), "")

        return SECRET_PATTERN.sub(repl, value)

    def _error(self, tool_name: str, code: str, message: str) -> ToolResult:
        """构建错误结果。

        Args:
            tool_name: 工具名称。
            code: 错误代码。
            message: 错误描述。

        Returns:
            失败的 ToolResult 对象。
        """
        return ToolResult(
            tool_name=tool_name,
            success=False,
            data=None,
            error=ToolError(code=code, message=message),
        )


def _default_port(scheme: str) -> int | None:
    """根据 URL scheme 返回默认端口号。

    Args:
        scheme: URL 协议（http 或 https）。

    Returns:
        默认端口号（http=80, https=443），其他返回 None。
    """
    return 443 if scheme.lower() == "https" else 80 if scheme.lower() == "http" else None
