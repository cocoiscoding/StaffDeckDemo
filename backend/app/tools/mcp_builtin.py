"""内置 MCP 工具模块。

本模块提供了一个无需网络连接的内置 MCP Server（``builtin.demo``），
用于开发、测试和演示。支持以下工具：
- echo：回显输入文本并返回长度。
- sum：对数字数组求和。
- product_lookup：查询 demo 商品价格数据。
"""

from __future__ import annotations

from typing import Any


class BuiltinMCPError(ValueError):
    """内置 MCP 工具执行异常。"""


def execute_builtin_mcp(config: dict[str, Any], arguments: dict[str, Any]) -> Any:
    """执行内置 MCP 工具调用。

    根据 config 中的 server 和 tool 名称分派到对应的内置工具实现。

    Args:
        config: MCP 客户端配置（需包含 server="builtin.demo" 和 tool 名称）。
        arguments: 工具调用参数。

    Returns:
        工具执行结果字典。

    Raises:
        BuiltinMCPError: 当 server 或 tool 不支持、参数不合法时抛出。
    """
    server = str(config.get("server") or config.get("server_id") or "").strip()
    tool = str(config.get("tool") or config.get("tool_name") or "").strip()
    if server != "builtin.demo":
        raise BuiltinMCPError(f"不支持的内置 MCP server：{server or '<empty>'}")
    if tool == "echo":
        text = str(arguments.get("text") or "")
        return {"text": text, "length": len(text)}
    if tool == "sum":
        numbers = arguments.get("numbers")
        if not isinstance(numbers, list) or not all(
            isinstance(item, (int, float)) and not isinstance(item, bool) for item in numbers
        ):
            raise BuiltinMCPError("sum 工具需要 numbers 数字数组。")
        total = sum(numbers)
        return {"numbers": numbers, "total": total, "count": len(numbers)}
    if tool == "product_lookup":
        product_id = str(arguments.get("product_id") or arguments.get("product_name") or "").strip().lower()
        catalog = {
            "a1": {"product_id": "A1", "display_name": "A1 标准商品", "price": 129.0, "currency": "CNY"},
            "a3": {"product_id": "A3", "display_name": "A3 高阶商品", "price": 239.0, "currency": "CNY"},
        }
        item = catalog.get(product_id)
        return {"found": bool(item), **(item or {"query": product_id})}
    raise BuiltinMCPError(f"不支持的内置 MCP tool：{tool or '<empty>'}")


def builtin_mcp_tool_names() -> list[str]:
    """返回所有内置 MCP 工具名称列表。"""
    return ["echo", "sum", "product_lookup"]


_BUILTIN_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "echo",
        "description": "回显输入文本并返回其长度。",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "要回显的文本"}},
            "required": ["text"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}, "length": {"type": "integer"}},
        },
    },
    {
        "name": "sum",
        "description": "对一组数字求和。",
        "inputSchema": {
            "type": "object",
            "properties": {"numbers": {"type": "array", "items": {"type": "number"}}},
            "required": ["numbers"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "numbers": {"type": "array"},
                "total": {"type": "number"},
                "count": {"type": "integer"},
            },
        },
    },
    {
        "name": "product_lookup",
        "description": "查询 demo 商品价格数据。",
        "inputSchema": {
            "type": "object",
            "properties": {"product_id": {"type": "string", "description": "商品 ID，例如 A1 或 A3"}},
            "required": ["product_id"],
        },
        "outputSchema": {
            "type": "object",
            "properties": {
                "found": {"type": "boolean"},
                "product_id": {"type": "string"},
                "display_name": {"type": "string"},
                "price": {"type": "number"},
                "currency": {"type": "string"},
            },
        },
    },
]


def builtin_mcp_tool_definitions(config: dict[str, Any]) -> list[dict[str, Any]]:
    """返回内置 MCP Server 的工具定义列表（含 inputSchema 和 outputSchema）。

    Args:
        config: MCP 客户端配置（需包含 server="builtin.demo"）。

    Returns:
        工具定义字典列表的深拷贝。

    Raises:
        BuiltinMCPError: 当 server 名称不是 builtin.demo 时抛出。
    """
    server = str(config.get("server") or config.get("server_id") or "builtin.demo").strip()
    if server != "builtin.demo":
        raise BuiltinMCPError(f"不支持的内置 MCP server：{server or '<empty>'}")
    return [dict(item) for item in _BUILTIN_TOOL_DEFINITIONS]
