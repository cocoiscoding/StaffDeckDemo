"""MCP（Model Context Protocol）客户端模块。

本模块实现了 StaffDeck 对 MCP 工具的客户端能力，支持四种传输方式（transport）：

1. **builtin**：内置 MCP Server（``builtin.demo``），无需网络连接，用于开发/测试。
2. **stdio**：通过子进程的标准输入/输出进行 JSON-RPC 通信。
3. **http (streamable_http)**：通过 HTTP POST 发送 JSON-RPC 请求，
   兼容纯 JSON 和 SSE（text/event-stream）响应格式。
4. **sse**：MCP 2024-11-05 HTTP+SSE 传输，先 GET 建立 SSE 流获取消息端点，
   再 POST 到端点发送请求，响应通过 SSE 流按 id 匹配返回。

典型调用路径：
    result = execute_mcp_tool(config, arguments, timeout_seconds=10)
    tools = list_mcp_tools(config, timeout_seconds=10)
"""

from __future__ import annotations

import json
import os
import selectors
import subprocess
import time
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

import httpx

from app.tools.mcp_builtin import (
    BuiltinMCPError,
    builtin_mcp_tool_definitions,
    execute_builtin_mcp,
)


class MCPClientError(RuntimeError):
    """MCP 客户端异常，封装所有 MCP 通信和执行过程中的错误。"""


# --------------------------------------------------------------------------- #
# Transport 归一化
# --------------------------------------------------------------------------- #

def normalize_transport(config: dict[str, Any]) -> str:
    """从连接配置推断 transport 类型。

    优先使用显式 transport 字段；否则根据 server/command/url 推断，
    以兼容历史配置。streamable_http 归一化为 http。
    """
    raw = str(config.get("transport") or "").strip().lower()
    if raw == "streamable_http":
        return "http"
    if raw:
        return raw
    server = str(config.get("server") or config.get("server_id") or "").strip()
    if server == "builtin.demo":
        return "builtin"
    if config.get("command"):
        return "stdio"
    if config.get("url") or config.get("endpoint"):
        return "http"
    return "builtin"


# --------------------------------------------------------------------------- #
# 对外入口：调用工具 / 列举工具
# --------------------------------------------------------------------------- #

def execute_mcp_tool(
    config: dict[str, Any],
    arguments: dict[str, Any],
    timeout_seconds: float = 10,
    tool_name: str | None = None,
) -> Any:
    """连接 MCP server 并调用单个工具。

    config 是「server 连接配置」（transport/url/command/headers 等）。
    tool_name 若显式传入则优先使用，否则回退到 config 里的 tool 字段
    （兼容历史「一个 config 一个 tool」的形态）。
    """
    normalized = dict(config or {})
    transport = normalize_transport(normalized)
    name = _resolve_tool_name(normalized, tool_name)

    if transport == "builtin":
        try:
            return execute_builtin_mcp({**normalized, "tool": name}, arguments)
        except BuiltinMCPError as exc:
            raise MCPClientError(str(exc)) from exc
    if transport == "stdio":
        return _StdioSession(normalized, timeout_seconds).call_tool(name, arguments)
    if transport in {"http", "streamable_http"}:
        return _HttpSession(normalized, timeout_seconds).call_tool(name, arguments)
    if transport == "sse":
        return _SseSession(normalized, timeout_seconds).call_tool(name, arguments)
    raise MCPClientError(f"不支持的 MCP transport：{transport or '<empty>'}")


def list_mcp_tools(
    config: dict[str, Any],
    timeout_seconds: float = 10,
) -> list[dict[str, Any]]:
    """连接 MCP server 并通过 tools/list 发现工具列表。

    返回标准化后的工具定义列表，每项包含 name / description /
    input_schema / output_schema（若 server 提供）。
    """
    normalized = dict(config or {})
    transport = normalize_transport(normalized)

    if transport == "builtin":
        try:
            raw = builtin_mcp_tool_definitions(normalized)
        except BuiltinMCPError as exc:
            raise MCPClientError(str(exc)) from exc
    elif transport == "stdio":
        raw = _StdioSession(normalized, timeout_seconds).list_tools()
    elif transport in {"http", "streamable_http"}:
        raw = _HttpSession(normalized, timeout_seconds).list_tools()
    elif transport == "sse":
        raw = _SseSession(normalized, timeout_seconds).list_tools()
    else:
        raise MCPClientError(f"不支持的 MCP transport：{transport or '<empty>'}")

    return [_normalize_tool_definition(item) for item in raw if isinstance(item, dict)]


def _resolve_tool_name(config: dict[str, Any], override: str | None) -> str:
    """从配置和覆盖值中解析工具名称。

    Args:
        config: MCP 客户端配置字典。
        override: 显式传入的工具名（优先使用）。

    Returns:
        解析后的工具名称。

    Raises:
        MCPClientError: 当工具名称为空时抛出。
    """
    name = str(override or config.get("tool") or config.get("tool_name") or config.get("name") or "").strip()
    if not name:
        raise MCPClientError("MCP 调用缺少 tool 名称。")
    return name


def _normalize_tool_definition(item: dict[str, Any]) -> dict[str, Any]:
    """将 MCP 工具定义规范化为统一格式。

    兼容 camelCase（inputSchema）和 snake_case（input_schema）两种字段命名。

    Args:
        item: 原始工具定义字典。

    Returns:
        包含 name、description、input_schema、output_schema 的规范字典。
    """
    input_schema = item.get("inputSchema") or item.get("input_schema") or {}
    output_schema = item.get("outputSchema") or item.get("output_schema") or {}
    return {
        "name": str(item.get("name") or "").strip(),
        "description": str(item.get("description") or "").strip(),
        "input_schema": input_schema if isinstance(input_schema, dict) else {},
        "output_schema": output_schema if isinstance(output_schema, dict) else {},
    }


# --------------------------------------------------------------------------- #
# JSON-RPC 会话基类
# --------------------------------------------------------------------------- #

class _MCPSession:
    """封装一次 MCP 连接的 initialize + list/call 交互。

    子类实现 `_request`（单次 JSON-RPC 请求/响应）和资源管理。
    """

    def __init__(self, config: dict[str, Any], timeout_seconds: float) -> None:
        self.config = config
        self.timeout_seconds = timeout_seconds

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """执行一次完整的 MCP 工具调用：初始化 → tools/call → 提取结果。

        Args:
            name: 工具名称。
            arguments: 工具调用参数。

        Returns:
            工具执行结果（提取后的文本或 JSON）。
        """
        with self:
            self._initialize()
            result = self._request(
                "tools/call",
                {"name": name, "arguments": arguments},
            )
            return _extract_tool_result(result)

    def list_tools(self) -> list[dict[str, Any]]:
        """列出 MCP Server 提供的所有工具：初始化 → tools/list。

        Returns:
            工具定义列表。
        """
        with self:
            self._initialize()
            result = self._request("tools/list", {})
            tools = result.get("tools") if isinstance(result, dict) else None
            return tools if isinstance(tools, list) else []

    def _initialize(self) -> None:
        """执行 MCP 握手：发送 initialize 请求和 initialized 通知。"""
        self._request("initialize", _initialize_params())
        self._notify("notifications/initialized", {})

    # 子类实现 ---------------------------------------------------------------
    def __enter__(self) -> "_MCPSession":
        return self

    def __exit__(self, *exc: Any) -> None:  # pragma: no cover - default no-op
        return None

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        raise NotImplementedError

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# stdio transport
# --------------------------------------------------------------------------- #

class _StdioSession(_MCPSession):
    """stdio 传输会话：通过子进程的 stdin/stdout 进行 JSON-RPC 通信。"""

    def __init__(self, config: dict[str, Any], timeout_seconds: float) -> None:
        super().__init__(config, timeout_seconds)
        self._proc: subprocess.Popen[str] | None = None
        self._next_id = 0

    def __enter__(self) -> "_StdioSession":
        """启动子进程：设置 env、cwd，打开 stdin/stdout/stderr 管道。"""
        command = _stdio_command(self.config)
        env = os.environ.copy()
        raw_env = self.config.get("env")
        if isinstance(raw_env, Mapping):
            env.update({str(key): str(value) for key, value in raw_env.items()})
        cwd = str(self.config["cwd"]) if self.config.get("cwd") else None
        self._proc = subprocess.Popen(
            command,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._proc is not None:
            _close_process(self._proc)
            self._proc = None

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        """通过 stdin 发送 JSON-RPC 请求并从 stdout 读取响应。"""
        proc = self._require_proc()
        self._next_id += 1
        request_id = self._next_id
        _send_json(proc, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        response = _read_response(proc, expected_id=request_id, timeout_seconds=self.timeout_seconds)
        _raise_json_rpc_error(response)
        return response.get("result")

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        """通过 stdin 发送 JSON-RPC 通知（无 id，不等待响应）。"""
        proc = self._require_proc()
        _send_json(proc, {"jsonrpc": "2.0", "method": method, "params": params})

    def _require_proc(self) -> subprocess.Popen[str]:
        if self._proc is None:
            raise MCPClientError("MCP stdio 会话未启动。")
        return self._proc


def _stdio_command(config: dict[str, Any]) -> list[str]:
    """从配置中解析 stdio 启动命令（command + args）。

    Args:
        config: MCP 客户端配置。

    Returns:
        命令和参数列表。

    Raises:
        MCPClientError: 当缺少 command 或 args 格式不正确时抛出。
    """
    command = config.get("command")
    args = config.get("args") or []
    if isinstance(command, list):
        parts = [str(part) for part in command]
    elif isinstance(command, str) and command.strip():
        parts = [command.strip()]
    else:
        raise MCPClientError("stdio MCP 连接缺少 command。")
    if not isinstance(args, list):
        raise MCPClientError("stdio MCP 连接的 args 必须是数组。")
    return [*parts, *[str(arg) for arg in args]]


# --------------------------------------------------------------------------- #
# HTTP (streamable_http) transport
# --------------------------------------------------------------------------- #

class _HttpSession(_MCPSession):
    """HTTP (streamable_http) 传输会话：通过 HTTP POST 发送 JSON-RPC 请求。

    支持纯 JSON 响应和 SSE（text/event-stream）响应格式，
    自动处理 Mcp-Session-Id 会话标识。
    """
    def __init__(self, config: dict[str, Any], timeout_seconds: float) -> None:
        super().__init__(config, timeout_seconds)
        self._client: httpx.Client | None = None
        self._next_id = 0
        self._session_id: str | None = None

    def __enter__(self) -> "_HttpSession":
        self._client = httpx.Client(timeout=self.timeout_seconds)
        return self

    def __exit__(self, *exc: Any) -> None:
        if self._client is not None:
            with suppress(Exception):
                self._client.close()
            self._client = None

    def _endpoint(self) -> str:
        url = str(self.config.get("url") or self.config.get("endpoint") or "").strip()
        if not url:
            raise MCPClientError("HTTP MCP 连接缺少 url/endpoint。")
        return url

    def _headers(self) -> dict[str, str]:
        raw = self.config.get("headers") if isinstance(self.config.get("headers"), dict) else {}
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **{str(k): str(v) for k, v in raw.items()},
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        client = self._require_client()
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        try:
            response = client.post(self._endpoint(), headers=self._headers(), json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise MCPClientError(f"HTTP MCP 返回异常状态码：{exc.response.status_code}") from exc
        except Exception as exc:
            raise MCPClientError(str(exc)) from exc
        session_id = response.headers.get("mcp-session-id")
        if session_id:
            self._session_id = session_id
        body = _parse_http_mcp_response(response)
        if not isinstance(body, dict):
            raise MCPClientError("HTTP MCP 返回内容不是 JSON-RPC object。")
        _raise_json_rpc_error(body)
        return body.get("result")

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        client = self._require_client()
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        with suppress(Exception):
            client.post(self._endpoint(), headers=self._headers(), json=payload)

    def _require_client(self) -> httpx.Client:
        if self._client is None:
            raise MCPClientError("HTTP MCP 会话未启动。")
        return self._client


def _parse_http_mcp_response(response: httpx.Response) -> Any:
    """解析 HTTP MCP 响应，兼容纯 JSON 和 SSE 格式（text/event-stream）。"""
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        payload = _last_sse_json(response.text)
        if payload is None:
            raise MCPClientError("SSE 响应中未找到有效的 JSON-RPC data 行。")
        return payload
    try:
        return response.json()
    except Exception as exc:
        raise MCPClientError(f"HTTP MCP 响应解析失败：{exc}") from exc


# --------------------------------------------------------------------------- #
# SSE transport
# --------------------------------------------------------------------------- #

class _SseSession(_MCPSession):
    """SSE transport（MCP 2024-11-05 HTTP+SSE）。

    连接流程：GET server url 建立 SSE 流，从首个 `event: endpoint`
    拿到用于发送 JSON-RPC 的消息端点；后续请求 POST 到该端点，
    响应通过 SSE 流按 id 匹配返回。
    """

    def __init__(self, config: dict[str, Any], timeout_seconds: float) -> None:
        """初始化 SSE 传输会话。

        Args:
            config: MCP 连接配置（url/endpoint/headers 等）。
            timeout_seconds: 请求超时秒数。
        """
        super().__init__(config, timeout_seconds)
        self._client: httpx.Client | None = None
        self._stream_ctx: Any = None
        self._events: Any = None
        self._message_url: str | None = None
        self._next_id = 0

    def __enter__(self) -> "_SseSession":
        """建立 SSE 连接：GET server URL 开启流，等待 endpoint 事件。"""
        self._client = httpx.Client(timeout=httpx.Timeout(self.timeout_seconds, read=None))
        url = str(self.config.get("url") or self.config.get("endpoint") or "").strip()
        if not url:
            raise MCPClientError("SSE MCP 连接缺少 url/endpoint。")
        raw = self.config.get("headers") if isinstance(self.config.get("headers"), dict) else {}
        headers = {"Accept": "text/event-stream", **{str(k): str(v) for k, v in raw.items()}}
        self._stream_ctx = self._client.stream("GET", url, headers=headers)
        response = self._stream_ctx.__enter__()
        response.raise_for_status()
        self._events = _iter_sse_events(response)
        self._message_url = self._await_endpoint(url)
        return self

    def __exit__(self, *exc: Any) -> None:
        """关闭 SSE 流和 HTTP 客户端，忽略清理异常。"""
        if self._stream_ctx is not None:
            with suppress(Exception):
                self._stream_ctx.__exit__(*exc)
            self._stream_ctx = None
        if self._client is not None:
            with suppress(Exception):
                self._client.close()
            self._client = None

    def _await_endpoint(self, base_url: str) -> str:
        """从 SSE 流中读取首个 ``endpoint`` 事件，解析为消息发送端点 URL。

        Args:
            base_url: SSE 连接的基础 URL，用于解析相对路径。

        Returns:
            解析后的完整消息端点 URL。

        Raises:
            MCPClientError: 如果在超时时间内未收到 endpoint 事件。
        """
        deadline = time.monotonic() + max(self.timeout_seconds, 0.1)
        for event, data in self._events:
            if event == "endpoint":
                return _resolve_endpoint(base_url, data.strip())
            if time.monotonic() > deadline:
                break
        raise MCPClientError("SSE MCP 未返回 endpoint 事件。")

    def _post_headers(self) -> dict[str, str]:
        """构建向消息端点 POST JSON-RPC 请求时使用的请求头。"""
        raw = self.config.get("headers") if isinstance(self.config.get("headers"), dict) else {}
        return {"Content-Type": "application/json", **{str(k): str(v) for k, v in raw.items()}}

    def _request(self, method: str, params: dict[str, Any]) -> Any:
        """通过 POST 消息端点发送 JSON-RPC 请求，并从 SSE 流等待匹配响应。

        Args:
            method: JSON-RPC 方法名。
            params: 请求参数。

        Returns:
            JSON-RPC 响应中的 result 字段。

        Raises:
            MCPClientError: POST 失败或等待响应超时。
        """
        client = self._require_client()
        self._next_id += 1
        request_id = self._next_id
        payload = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        try:
            posted = client.post(str(self._message_url), headers=self._post_headers(), json=payload)
            posted.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise MCPClientError(f"SSE MCP 返回异常状态码：{exc.response.status_code}") from exc
        except Exception as exc:
            raise MCPClientError(str(exc)) from exc
        body = self._await_response(request_id)
        _raise_json_rpc_error(body)
        return body.get("result")

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        """通过 POST 消息端点发送 JSON-RPC 通知（无 id，不等待响应）。"""
        client = self._require_client()
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        with suppress(Exception):
            client.post(str(self._message_url), headers=self._post_headers(), json=payload)

    def _await_response(self, expected_id: int) -> dict[str, Any]:
        """从 SSE 流中按 JSON-RPC id 匹配并读取响应。

        持续迭代 SSE 事件，解析 ``message`` 或空事件类型的 data，
        匹配到 ``id == expected_id`` 的 JSON-RPC 响应后返回。

        Args:
            expected_id: 期望匹配的 JSON-RPC 请求 id。

        Returns:
            匹配的 JSON-RPC 响应字典。

        Raises:
            MCPClientError: 如果在超时时间内未匹配到响应。
        """
        deadline = time.monotonic() + max(self.timeout_seconds, 0.1)
        for event, data in self._events:
            if event in {"message", ""}:
                with suppress(json.JSONDecodeError):
                    payload = json.loads(data)
                    if isinstance(payload, dict) and payload.get("id") == expected_id:
                        return payload
            if time.monotonic() > deadline:
                break
        raise MCPClientError(f"SSE MCP 等待响应超时：id={expected_id}")

    def _require_client(self) -> httpx.Client:
        """获取已初始化的 HTTP 客户端和消息端点 URL。

        Returns:
            ``httpx.Client`` 实例。

        Raises:
            MCPClientError: 如果会话未启动（客户端或消息端点未初始化）。
        """
        if self._client is None or self._message_url is None:
            raise MCPClientError("SSE MCP 会话未启动。")
        return self._client


def _iter_sse_events(response: httpx.Response):
    """迭代 SSE 流，逐个 yield (event_type, data)。"""
    event_type = ""
    data_lines: list[str] = []
    for raw_line in response.iter_lines():
        line = raw_line.rstrip("\r")
        if line == "":
            if data_lines:
                yield event_type or "message", "\n".join(data_lines)
            event_type = ""
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_type = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())


def _resolve_endpoint(base_url: str, endpoint: str) -> str:
    """将 SSE 返回的相对 endpoint 路径解析为完整 URL。"""
    if endpoint.startswith("http://") or endpoint.startswith("https://"):
        return endpoint
    from urllib.parse import urljoin

    return urljoin(base_url, endpoint)


def _last_sse_json(text: str) -> Any:
    """从 SSE 文本中提取最后一个 ``data:`` 行的 JSON 内容。"""
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("data:"):
            data = line[len("data:"):].strip()
            with suppress(json.JSONDecodeError):
                return json.loads(data)
    return None


# --------------------------------------------------------------------------- #
# 共享工具函数
# --------------------------------------------------------------------------- #

def _initialize_params() -> dict[str, Any]:
    """构建 MCP initialize 请求的参数字典。"""
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "StaffDeck", "version": "0.1.0"},
    }


def _send_json(proc: subprocess.Popen[str], payload: dict[str, Any]) -> None:
    """向子进程的 stdin 写入一行 JSON-RPC 消息并刷新。"""
    if proc.stdin is None:
        raise MCPClientError("MCP stdio stdin 不可用。")
    proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
    proc.stdin.flush()


def _read_response(
    proc: subprocess.Popen[str],
    expected_id: int,
    timeout_seconds: float,
) -> dict[str, Any]:
    """从子进程的 stdout 读取 JSON-RPC 响应，按 id 匹配并支持超时。

    使用 selectors 实现非阻塞读取，超时后抛出异常。
    非 JSON 行（如日志输出）会被跳过。

    Args:
        proc: 子进程对象。
        expected_id: 期望匹配的 JSON-RPC 请求 id。
        timeout_seconds: 超时秒数。

    Returns:
        匹配的 JSON-RPC 响应字典。

    Raises:
        MCPClientError: 超时、进程提前退出或 stdin/stdout 不可用时抛出。
    """
    if proc.stdout is None:
        raise MCPClientError("MCP stdio stdout 不可用。")
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + max(timeout_seconds, 0.1)
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPClientError(f"MCP stdio 等待响应超时：id={expected_id}")
            events = selector.select(remaining)
            if not events:
                raise MCPClientError(f"MCP stdio 等待响应超时：id={expected_id}")
            line = proc.stdout.readline()
            if not line:
                stderr = _read_stderr(proc)
                raise MCPClientError(f"MCP stdio server 提前退出。{stderr}".strip())
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if payload.get("id") == expected_id:
                return payload
    finally:
        selector.close()


def _raise_json_rpc_error(payload: dict[str, Any]) -> None:
    """检查 JSON-RPC 响应中的 error 字段，有错误则抛出 MCPClientError。"""
    if "error" not in payload:
        return
    error = payload.get("error") or {}
    if isinstance(error, dict):
        message = str(error.get("message") or error)
    else:
        message = str(error)
    raise MCPClientError(message)


def _extract_tool_result(result: Any) -> Any:
    """从 MCP 工具调用结果中提取有效内容。

    MCP 工具调用结果通常是一个包含 ``content`` 列表的字典，
    其中每个 content 项可能有 ``type: "text"`` 等类型。
    本函数负责：

    1. 检查 ``isError`` 标志，如有错误则抛出异常。
    2. 遍历 ``content`` 列表，提取文本内容（自动尝试 JSON 解析）。
    3. 如果仅有一项结果则直接返回，否则返回列表。

    Args:
        result: MCP ``tools/call`` 返回的原始结果。

    Returns:
        提取后的文本、JSON 值或列表。如果结果不是字典，则原样返回。

    Raises:
        MCPClientError: 当 ``isError`` 为 True 时抛出。
    """
    if not isinstance(result, dict):
        return result
    if result.get("isError"):
        raise MCPClientError(_content_text(result.get("content")) or "MCP tool returned isError=true。")
    content = result.get("content")
    if not isinstance(content, list):
        return result
    extracted: list[Any] = []
    for item in content:
        if not isinstance(item, dict):
            extracted.append(item)
            continue
        if item.get("type") == "text":
            text = str(item.get("text") or "")
            extracted.append(_parse_text_content(text))
        else:
            extracted.append(item)
    if len(extracted) == 1:
        return extracted[0]
    return extracted


def _parse_text_content(text: str) -> Any:
    """尝试将文本内容解析为 JSON，失败则返回原始文本。

    Args:
        text: 待解析的文本字符串。

    Returns:
        解析后的 JSON 值，或原始文本字符串（解析失败/空文本时）。
    """
    stripped = text.strip()
    if not stripped:
        return ""
    with suppress(json.JSONDecodeError):
        return json.loads(stripped)
    return text


def _content_text(content: Any) -> str:
    """从 MCP content 列表中拼接所有文本类型项的内容。

    Args:
        content: MCP content 列表，每项为包含 ``type`` 和 ``text`` 的字典。

    Returns:
        所有 ``type == "text"`` 项的文本拼接结果，以换行分隔。
    """
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
    return "\n".join(part for part in parts if part)


def _close_process(proc: subprocess.Popen[str]) -> None:
    """安全关闭子进程：先 terminate，超时后 kill。

    如果进程已退出则直接返回。先发送 SIGTERM 等待 1 秒，
    仍未退出则发送 SIGKILL 强制终止。

    Args:
        proc: 待关闭的子进程对象。
    """
    if proc.poll() is not None:
        return
    proc.terminate()
    with suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=1)
        return
    proc.kill()
    with suppress(Exception):
        proc.wait(timeout=1)


def _read_stderr(proc: subprocess.Popen[str]) -> str:
    """读取子进程 stderr 的全部内容（最多 1000 字符）。

    用于在 stdio MCP server 异常退出时收集错误日志，
    帮助诊断子进程崩溃原因。

    Args:
        proc: 子进程对象。

    Returns:
        stderr 内容字符串（最多 1000 字符），读取失败时返回空字符串。
    """
    if proc.stderr is None:
        return ""
    with suppress(Exception):
        return proc.stderr.read()[:1000]
    return ""
