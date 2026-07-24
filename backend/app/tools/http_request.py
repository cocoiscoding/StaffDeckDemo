"""HTTP 请求构造工具模块。

本模块提供了 HTTP GET 请求的参数合并工具函数，
用于在工具执行器中将调用参数合并到 URL 查询字符串中。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def prepare_get_request(url: str, params: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """为 HTTP GET 请求准备 URL 和额外的请求参数。

    处理逻辑：
    1. 过滤掉值为 None 的参数。
    2. 若无有效参数，直接返回原 URL。
    3. 若原 URL 无查询字符串，参数通过 kwargs 的 params 传递。
    4. 若原 URL 已有查询字符串，将参数合并到 URL 中。

    Args:
        url: 原始 URL（可能已包含查询字符串）。
        params: 调用参数字典。

    Returns:
        ``(url, kwargs)`` 元组，url 为最终请求 URL，kwargs 为额外的 httpx 请求参数。
    """
    clean_params = {str(key): value for key, value in (params or {}).items() if value is not None}
    if not clean_params:
        return url, {}

    parsed = urlsplit(url)
    if not parsed.query:
        return url, {"params": clean_params}

    merged_params: dict[str, Any] = dict(parse_qsl(parsed.query, keep_blank_values=True))
    merged_params.update(clean_params)
    merged_url = urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(merged_params, doseq=True),
            parsed.fragment,
        )
    )
    return merged_url, {}
