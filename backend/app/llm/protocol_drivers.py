"""模型协议驱动层：封装三种 LLM API 协议的请求与响应处理。

本模块把上层统一格式的请求（dict，类似 OpenAI 风格）翻译为各厂商协议所需的
具体格式，并屏蔽响应差异，使上层调用方无需关心底层协议细节。

包含三个具体驱动（均实现 ``ProtocolDriver`` 协议）：

- ``ChatCompletionsDriver``：OpenAI 兼容的 chat.completions 接口，请求格式与
  上层几乎一致，主要用于透传；
- ``AnthropicMessagesDriver``：Anthropic Messages API，需把 messages 中的
  system 抽离、把 OpenAI 风格内容块转为 Anthropic 的 content blocks；
- ``GeminiGenerateContentDriver``：Google Gemini generateContent 接口，基于
  httpx 直接调用 REST，需把 messages 转为 contents/parts 结构。

此外提供：
- 图片安全限制（数量、单图大小、总大小、请求体大小）；
- 统一的错误分类（``ProtocolCallError`` + 错误码，并标记是否可重试）；
- 取消机制（``CancellationToken``，通过请求中的 ``_cancellation`` 字段传递）；
- 流式与非流式两种调用模式，流式产出统一的 chunk 结构。

本模块被 ``client.py`` 根据解析出的协议选择对应驱动实例来发起调用。
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
import base64
import binascii
import json
import re
from types import SimpleNamespace
from threading import Event
from typing import Any, Protocol
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import httpx


# 匹配 data: URL 形式的内联图片（base64）。
_DATA_URL = re.compile(r"^data:(image/(?:jpeg|png|gif|webp));base64,(.+)$", re.DOTALL)
# —— 图片与请求体安全限制 ——
_MAX_IMAGE_BYTES = 5 * 1024 * 1024  # 单张图片上限 5MB
_MAX_IMAGE_COUNT = 6  # 单次请求图片数量上限
_MAX_TOTAL_IMAGE_BYTES = 18 * 1024 * 1024  # 图片总大小上限 18MB
_MAX_REQUEST_BYTES = 25 * 1024 * 1024  # 请求体总大小上限 25MB


class ProtocolCallError(Exception):
    """协议调用错误，携带稳定错误码与是否可重试标记。

    Attributes:
        code: 稳定错误码（如 MODEL_RATE_LIMITED），供上层映射为业务错误。
        retryable: 该错误是否值得重试（如限流、超时、5xx）。
    """

    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class CancellationToken:
    """简单的线程安全取消令牌，基于 ``threading.Event`` 实现。

    用于在流式生成过程中主动取消请求；驱动在每个迭代点检查令牌状态，
    已取消时抛出 ``MODEL_CANCELLED``。
    """

    def __init__(self) -> None:
        self._event = Event()

    def cancel(self) -> None:
        """标记取消（设置内部事件）。"""
        self._event.set()

    @property
    def cancelled(self) -> bool:
        """是否已被取消。

        Returns:
            bool: 已取消返回 True。
        """
        return self._event.is_set()


class ProtocolDriver(Protocol):
    """协议驱动接口：定义 complete（非流式）与 stream（流式）两种调用方式。

    所有具体驱动需实现该接口，并提供 ``request_kind`` 标识请求类别。
    """

    request_kind: str

    def complete(self, request: dict[str, Any]) -> Any: ...

    def stream(self, request: dict[str, Any]) -> Iterator[Any]: ...


@dataclass(frozen=True)
class ChatCompletionsDriver:
    """OpenAI 兼容 chat.completions 协议驱动。

    直接调用 OpenAI SDK 风格的 ``client.chat.completions``，请求格式与上层
    基本一致，仅剥离以下划线开头的内部控制字段（如 ``_cancellation``）。
    """

    client: Any
    request_kind: str = "chat.completions"

    def complete(self, request: dict[str, Any]) -> Any:
        """非流式调用 chat.completions。

        Args:
            request: 上层统一请求字典（含 messages、temperature 等及内部控制字段）。

        Returns:
            Any: SDK 返回的原始响应对象。
        """
        _raise_if_cancelled(request)
        return self.client.chat.completions.create(**_wire_request(request))

    def stream(self, request: dict[str, Any]) -> Iterator[Any]:
        """流式调用 chat.completions，返回 chunk 迭代器。

        在每个 chunk 之间检查取消状态；迭代结束（含异常）时关闭流。

        Args:
            request: 上层统一请求字典。

        Returns:
            Iterator[Any]: 产出流式 chunk 的迭代器。
        """
        _raise_if_cancelled(request)
        stream = self.client.chat.completions.create(**_wire_request(request), stream=True)

        def iterate() -> Iterator[Any]:
            try:
                for chunk in stream:
                    _raise_if_cancelled(request)
                    yield chunk
            finally:
                close = getattr(stream, "close", None)
                if callable(close):
                    close()

        return iterate()


@dataclass(frozen=True)
class AnthropicMessagesDriver:
    """Anthropic Messages 协议驱动。

    将上层请求转换为 Anthropic Messages API 所需格式（system 抽离、内容块转换、
    角色合并、图片格式转换），并把 Anthropic 的响应/事件统一映射为类 OpenAI 的
    结构（choices/message/content），便于上层复用同一套解析逻辑。
    """

    client: Any
    request_kind: str = "anthropic.messages"

    def complete(self, request: dict[str, Any]) -> Any:
        """非流式调用 Anthropic Messages。

        把响应中的 text content block 拼接为纯文本，封装为类 OpenAI 的响应对象。

        Args:
            request: 上层统一请求字典。

        Returns:
            Any: 类 OpenAI 风格的响应对象（含 choices/message/usage）。
        """
        _raise_if_cancelled(request)
        payload = _anthropic_request(request)
        try:
            response = self.client.messages.create(**payload, stream=False)
        except ProtocolCallError:
            raise
        except Exception as exc:
            raise _protocol_call_error(exc) from exc
        text = "".join(
            str(getattr(block, "text", ""))
            for block in (getattr(response, "content", None) or [])
            if getattr(block, "type", None) == "text"
        )
        usage = getattr(response, "usage", None)
        return SimpleNamespace(
            id=getattr(response, "id", None),
            usage=_anthropic_usage(usage),
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=text),
                    finish_reason=getattr(response, "stop_reason", None),
                )
            ],
        )

    def stream(self, request: dict[str, Any]) -> Iterator[Any]:
        """流式调用 Anthropic Messages，把事件流映射为统一 chunk。

        处理 message_start（首块含 usage）、content_block_delta（文本增量）、
        message_delta（结束原因与 usage）三类事件，统一产出 ``_stream_chunk``。

        Args:
            request: 上层统一请求字典。

        Returns:
            Iterator[Any]: 产出统一 chunk 的迭代器。
        """
        payload = _anthropic_request(request)
        try:
            events = self.client.messages.create(**payload, stream=True)
        except Exception as exc:
            raise _protocol_call_error(exc) from exc
        try:
            response_id = None
            for event in events:
                _raise_if_cancelled(request)
                event_type = getattr(event, "type", None)
                if event_type == "message_start":
                    message = getattr(event, "message", None)
                    response_id = getattr(message, "id", None)
                    yield _stream_chunk(
                        response_id,
                        usage=_anthropic_usage(getattr(message, "usage", None)),
                    )
                    continue
                if event_type == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if getattr(delta, "type", None) == "text_delta":
                        yield _stream_chunk(
                            response_id,
                            text=str(getattr(delta, "text", "")),
                        )
                    continue
                if event_type == "message_delta":
                    delta = getattr(event, "delta", None)
                    yield _stream_chunk(
                        response_id,
                        finish_reason=getattr(delta, "stop_reason", None),
                        usage=_anthropic_usage(getattr(event, "usage", None)),
                    )
        except ProtocolCallError:
            raise
        except Exception as exc:
            raise _protocol_call_error(exc) from exc
        finally:
            close = getattr(events, "close", None)
            if callable(close):
                close()


@dataclass(frozen=True)
class GeminiGenerateContentDriver:
    """Google Gemini generateContent 协议驱动。

    不使用官方 SDK，而是通过 httpx 直接调用 REST 接口。负责把 messages 转为
    contents/parts 结构、拼装端点 URL（含 v1beta 版本与可选 SSE 参数）、
    设置鉴权头，并把响应映射为类 OpenAI 结构。
    """

    client: httpx.Client
    base_url: str
    api_key: str
    model: str
    request_kind: str = "gemini.generate_content"

    def complete(self, request: dict[str, Any]) -> Any:
        """非流式调用 Gemini generateContent。

        Args:
            request: 上层统一请求字典。

        Returns:
            Any: 类 OpenAI 风格的响应对象。
        """
        _raise_if_cancelled(request)
        payload = _gemini_request(request)
        try:
            response = self.client.post(
                _gemini_endpoint(self.base_url, self.model, "generateContent"),
                headers=_gemini_headers(self.api_key),
                json=payload,
            )
        except httpx.HTTPError as exc:
            raise _protocol_call_error(exc) from exc
        _raise_for_gemini_response(response)
        try:
            data = response.json()
        except ValueError as exc:
            raise ProtocolCallError("MODEL_INVALID_PROVIDER_RESPONSE") from exc
        return _gemini_completion(data)

    def stream(self, request: dict[str, Any]) -> Iterator[Any]:
        """流式调用 Gemini streamGenerateContent（SSE）。

        逐行解析 SSE 数据行，跳过 ``[DONE]``，把每个 JSON 对象映射为统一 chunk。

        Args:
            request: 上层统一请求字典。

        Returns:
            Iterator[Any]: 产出统一 chunk 的迭代器。
        """
        _raise_if_cancelled(request)
        payload = _gemini_request(request)
        try:
            with self.client.stream(
                "POST",
                _gemini_endpoint(self.base_url, self.model, "streamGenerateContent", stream=True),
                headers=_gemini_headers(self.api_key),
                json=payload,
            ) as response:
                _raise_for_gemini_response(response)
                for line in response.iter_lines():
                    _raise_if_cancelled(request)
                    if not line:
                        continue
                    # 兼容 ``data: {...}`` 与裸 JSON 两种行格式。
                    raw = line[5:].strip() if line.startswith("data:") else line.strip()
                    if raw == "[DONE]":
                        continue
                    try:
                        data = json.loads(raw)
                    except ValueError as exc:
                        raise ProtocolCallError("MODEL_INVALID_PROVIDER_RESPONSE") from exc
                    yield _gemini_completion(data)
        except ProtocolCallError:
            raise
        except httpx.HTTPError as exc:
            raise _protocol_call_error(exc) from exc


def _gemini_headers(api_key: str) -> dict[str, str]:
    """构造 Gemini 请求头（含 Bearer 与 x-goog-api-key 双重鉴权）。

    Args:
        api_key: API 密钥。

    Returns:
        dict[str, str]: 请求头字典。
    """
    return {
        "content-type": "application/json",
        "authorization": f"Bearer {api_key}",
        "x-goog-api-key": api_key,
        "accept": "text/event-stream, application/json",
    }


def _gemini_endpoint(
    base_url: str, model: str, method: str, *, stream: bool = False
) -> str:
    """拼装 Gemini REST 端点 URL。

    保证路径以 ``/v1beta`` 结尾，追加 ``models/{model}:{method}``；流式时补充
    ``alt=sse`` 查询参数以启用 SSE。

    Args:
        base_url: 模型基础地址。
        model: 模型名。
        method: 方法名（generateContent / streamGenerateContent）。
        stream: 是否为流式调用。

    Returns:
        str: 完整端点 URL。
    """
    parsed = urlsplit(base_url.rstrip("/"))
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1beta"):
        path = f"{path}/v1beta"
    path = f"{path}/models/{quote(model, safe='')}:{method}"
    query = parse_qsl(parsed.query, keep_blank_values=True)
    if stream and ("alt", "sse") not in query:
        query.append(("alt", "sse"))
    return urlunsplit((parsed.scheme, parsed.netloc, path, urlencode(query), ""))


def _gemini_request(request: dict[str, Any]) -> dict[str, Any]:
    """把上层统一请求转换为 Gemini generateContent 请求体。

    system 消息抽离到 ``systemInstruction``；assistant 映射为 model 角色；
    相邻同角色消息合并；首条若为 model 则丢弃。随后执行图片与请求体大小校验。

    Args:
        request: 上层统一请求字典。

    Returns:
        dict[str, Any]: Gemini 请求体。

    Raises:
        ValueError: 图片过多/过大或请求体超限时（携带错误码字符串）。
    """
    contents: list[dict[str, Any]] = []
    system_parts: list[dict[str, Any]] = []
    for message in request.get("messages") or []:
        role = str(message.get("role") or "")
        parts = _gemini_content_parts(message.get("content"), role)
        if not parts:
            continue
        if role == "system":
            system_parts.extend(parts)
            continue
        gemini_role = "model" if role == "assistant" else "user"
        # 合并相邻同角色消息，避免 Gemini 对连续同角色内容的报错。
        if contents and contents[-1]["role"] == gemini_role:
            contents[-1]["parts"].extend(parts)
        else:
            contents.append({"role": gemini_role, "parts": parts})
    if contents and contents[0]["role"] == "model":
        contents.pop(0)
    payload: dict[str, Any] = {
        "contents": contents,
        "generationConfig": {
            "temperature": request["temperature"],
            "maxOutputTokens": request["max_tokens"],
        },
    }
    if system_parts:
        payload["systemInstruction"] = {"parts": system_parts}
    response_format = request.get("response_format")
    if response_format and response_format.get("type") == "json_object":
        payload["generationConfig"]["responseMimeType"] = "application/json"
    # 图片与请求体大小安全校验。
    image_parts = [
        part
        for item in contents
        for part in item["parts"]
        if isinstance(part, dict) and "inlineData" in part
    ]
    if len(image_parts) > _MAX_IMAGE_COUNT:
        raise ValueError("MODEL_TOO_MANY_IMAGES")
    total_image_bytes = sum(
        len(base64.b64decode(part["inlineData"]["data"], validate=True))
        for part in image_parts
    )
    if total_image_bytes > _MAX_TOTAL_IMAGE_BYTES:
        raise ValueError("MODEL_REQUEST_TOO_LARGE")
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > _MAX_REQUEST_BYTES:
        raise ValueError("MODEL_REQUEST_TOO_LARGE")
    return payload


def _gemini_content_parts(value: Any, role: str) -> list[dict[str, Any]]:
    """把单条消息的 content 转换为 Gemini 的 parts 列表。

    文本 → ``{"text": ...}``；图片（仅 user 角色）→ ``{"inlineData": ...}``，
    要求图片为合法 data URL 且不超过单图大小上限。

    Args:
        value: content（字符串或内容块列表）。
        role: 消息角色。

    Returns:
        list[dict[str, Any]]: Gemini parts 列表。

    Raises:
        ValueError: 图片 data URL 非法、过大或数量超限。
    """
    if isinstance(value, str):
        return [{"text": value}] if value else []
    if not isinstance(value, list):
        return []
    parts: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text = str(item.get("text") or "")
            if text:
                parts.append({"text": text})
            continue
        if item.get("type") != "image_url" or role != "user":
            continue
        image = item.get("image_url")
        url = str(image.get("url") or "") if isinstance(image, dict) else ""
        match = _DATA_URL.fullmatch(url)
        if not match:
            raise ValueError("MODEL_IMAGE_DATA_URL_INVALID")
        try:
            decoded = base64.b64decode(match.group(2), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("MODEL_IMAGE_DATA_URL_INVALID") from exc
        if len(decoded) > _MAX_IMAGE_BYTES:
            raise ValueError("MODEL_IMAGE_TOO_LARGE")
        parts.append(
            {
                "inlineData": {
                    "mimeType": match.group(1),
                    "data": match.group(2),
                }
            }
        )
    if sum(1 for part in parts if "inlineData" in part) > _MAX_IMAGE_COUNT:
        raise ValueError("MODEL_TOO_MANY_IMAGES")
    return parts


def _raise_for_gemini_response(response: httpx.Response) -> None:
    """根据 Gemini HTTP 响应状态码抛出对应的协议错误。

    Args:
        response: httpx 响应对象。

    Raises:
        ProtocolCallError: 状态码 >= 400 时，携带对应错误码与可重试标记。
    """
    if response.status_code < 400:
        return
    status = response.status_code
    if status == 401:
        code = "MODEL_AUTHENTICATION_FAILED"
    elif status == 403:
        code = "MODEL_PERMISSION_DENIED"
    elif status == 404:
        code = "MODEL_ENDPOINT_NOT_FOUND"
    elif status == 429:
        code = "MODEL_RATE_LIMITED"
    else:
        code = "MODEL_UPSTREAM_ERROR"
    raise ProtocolCallError(code, retryable=status == 429 or status >= 500)


def _gemini_completion(data: dict[str, Any]) -> Any:
    """把 Gemini 响应 JSON 映射为类 OpenAI 的响应对象。

    取首个 candidate 的文本（过滤 thought 思考内容）与 finishReason，
    usageMetadata 映射为 prompt/completion/total tokens。

    Args:
        data: Gemini 响应 JSON。

    Returns:
        Any: 类 OpenAI 风格的响应对象。
    """
    candidates = data.get("candidates") or []
    candidate = candidates[0] if candidates else {}
    content = candidate.get("content") or {}
    text = "".join(
        str(part.get("text") or "")
        for part in content.get("parts") or []
        if isinstance(part, dict) and not part.get("thought")
    )
    usage = data.get("usageMetadata") or {}
    prompt_tokens = usage.get("promptTokenCount")
    output_tokens = usage.get("candidatesTokenCount")
    return SimpleNamespace(
        id=data.get("responseId"),
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=output_tokens,
            total_tokens=usage.get("totalTokenCount"),
        ),
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text),
                finish_reason=candidate.get("finishReason"),
                delta=SimpleNamespace(content=text),
            )
        ]
        if text or candidate.get("finishReason")
        else [],
    )


def _anthropic_request(request: dict[str, Any]) -> dict[str, Any]:
    """把上层统一请求转换为 Anthropic Messages 请求体。

    system 消息合并为顶层 ``system`` 字符串；user/assistant 内容块转换；
    相邻同角色合并；首条若为 assistant 则丢弃。随后执行图片与请求体大小校验。

    Args:
        request: 上层统一请求字典。

    Returns:
        dict[str, Any]: Anthropic 请求体。

    Raises:
        ValueError: 图片过多/过大或请求体超限时。
    """
    messages = list(request.get("messages") or [])
    system_parts: list[str] = []
    converted: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "system":
            system_parts.append(_content_text(message.get("content")))
            continue
        if role not in {"user", "assistant"}:
            continue
        blocks = _anthropic_content(message.get("content"), role)
        if not blocks:
            continue
        # 合并相邻同角色消息。
        if converted and converted[-1]["role"] == role:
            converted[-1]["content"].extend(blocks)
        else:
            converted.append({"role": role, "content": blocks})
    if converted and converted[0]["role"] == "assistant":
        converted.pop(0)
    payload = {
        "model": request["model"],
        "messages": converted,
        "max_tokens": request["max_tokens"],
        "temperature": request["temperature"],
    }
    system = "\n\n".join(part for part in system_parts if part)
    if system:
        payload["system"] = system
    # 图片与请求体大小安全校验。
    image_count = 0
    total_image_bytes = 0
    for message in converted:
        for block in message["content"]:
            if block.get("type") != "image":
                continue
            image_count += 1
            decoded_size = len(base64.b64decode(block["source"]["data"], validate=True))
            if decoded_size > _MAX_IMAGE_BYTES:
                raise ValueError("MODEL_IMAGE_TOO_LARGE")
            total_image_bytes += decoded_size
    if image_count > _MAX_IMAGE_COUNT:
        raise ValueError("MODEL_TOO_MANY_IMAGES")
    if total_image_bytes > _MAX_TOTAL_IMAGE_BYTES:
        raise ValueError("MODEL_REQUEST_TOO_LARGE")
    if len(str(payload).encode("utf-8")) > _MAX_REQUEST_BYTES:
        raise ValueError("MODEL_REQUEST_TOO_LARGE")
    return payload


def _protocol_call_error(exc: Exception) -> ProtocolCallError:
    """把任意异常分类映射为带错误码的 ``ProtocolCallError``。

    依据异常类型名与 status_code 推断：鉴权失败/权限拒绝/端点不存在/限流/
    超时/连接错误/其他上游错误，并标记可重试性。

    Args:
        exc: 原始异常。

    Returns:
        ProtocolCallError: 分类后的协议调用错误。
    """
    name = type(exc).__name__.lower()
    status = getattr(exc, "status_code", None)
    if status == 401 or "authentication" in name:
        return ProtocolCallError("MODEL_AUTHENTICATION_FAILED")
    if status == 403 or "permission" in name:
        return ProtocolCallError("MODEL_PERMISSION_DENIED")
    if status == 404 or "notfound" in name:
        return ProtocolCallError("MODEL_ENDPOINT_NOT_FOUND")
    if status == 429 or "ratelimit" in name:
        return ProtocolCallError("MODEL_RATE_LIMITED", retryable=True)
    if "timeout" in name or "connecterror" in name:
        return ProtocolCallError("MODEL_TIMEOUT", retryable=True)
    return ProtocolCallError("MODEL_UPSTREAM_ERROR", retryable=True)


def _raise_if_cancelled(request: dict[str, Any]) -> None:
    """若请求携带的取消令牌已被取消，抛出 ``MODEL_CANCELLED``。

    Args:
        request: 上层统一请求字典（可能含 ``_cancellation`` 令牌）。

    Raises:
        ProtocolCallError: 令牌已取消时。
    """
    token = request.get("_cancellation")
    if isinstance(token, CancellationToken) and token.cancelled:
        raise ProtocolCallError("MODEL_CANCELLED")


def _wire_request(request: dict[str, Any]) -> dict[str, Any]:
    """剥离请求中以下划线开头的内部控制字段，返回可透传给 SDK 的请求字典。

    Args:
        request: 上层统一请求字典。

    Returns:
        dict[str, Any]: 不含 ``_`` 前缀字段的请求字典。
    """
    return {key: value for key, value in request.items() if not key.startswith("_")}


def _anthropic_content(value: Any, role: str) -> list[dict[str, Any]]:
    """把单条消息的 content 转换为 Anthropic 的 content blocks。

    文本 → ``{"type": "text", ...}``；图片（仅 user）→ ``{"type": "image",
    "source": {...}}``，要求图片为合法 data URL。

    Args:
        value: content（字符串或内容块列表）。
        role: 消息角色。

    Returns:
        list[dict[str, Any]]: Anthropic content blocks。

    Raises:
        ValueError: 图片 data URL 非法时。
    """
    if isinstance(value, str):
        return [{"type": "text", "text": value}] if value else []
    if not isinstance(value, list):
        return []
    blocks: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text = str(item.get("text") or "")
            if text:
                blocks.append({"type": "text", "text": text})
            continue
        if item.get("type") != "image_url" or role != "user":
            continue
        image = item.get("image_url")
        url = str(image.get("url") or "") if isinstance(image, dict) else ""
        match = _DATA_URL.fullmatch(url)
        if not match:
            raise ValueError("MODEL_IMAGE_DATA_URL_INVALID")
        try:
            base64.b64decode(match.group(2), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("MODEL_IMAGE_DATA_URL_INVALID") from exc
        blocks.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": match.group(1),
                    "data": match.group(2),
                },
            }
        )
    return blocks


def _content_text(value: Any) -> str:
    """从 content（字符串或内容块列表）中提取纯文本。

    Args:
        value: content 值。

    Returns:
        str: 拼接后的纯文本。
    """
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    return "".join(
        str(item.get("text") or "")
        for item in value
        if isinstance(item, dict) and item.get("type") == "text"
    )


def _anthropic_usage(value: Any) -> Any:
    """把 Anthropic usage 对象映射为类 OpenAI 的 usage（prompt/completion/total）。

    Args:
        value: Anthropic usage 对象或 None。

    Returns:
        Any: 类 OpenAI usage 对象；输入为 None 时返回 None。
    """
    if value is None:
        return None
    input_tokens = getattr(value, "input_tokens", None)
    output_tokens = getattr(value, "output_tokens", None)
    return SimpleNamespace(
        prompt_tokens=input_tokens,
        completion_tokens=output_tokens,
        total_tokens=(input_tokens + output_tokens)
        if isinstance(input_tokens, int) and isinstance(output_tokens, int)
        else None,
    )


def _stream_chunk(
    response_id: Any,
    *,
    text: str = "",
    finish_reason: Any = None,
    usage: Any = None,
) -> Any:
    """构造一个统一的流式 chunk 对象（类 OpenAI 风格）。

    只有存在文本增量或结束原因时才生成 choice，避免产出空 choice 干扰上层。

    Args:
        response_id: 响应 id。
        text: 文本增量。
        finish_reason: 结束原因。
        usage: usage 对象。

    Returns:
        Any: 统一的流式 chunk 对象。
    """
    choices = []
    if text or finish_reason:
        choices.append(
            SimpleNamespace(
                delta=SimpleNamespace(content=text),
                finish_reason=finish_reason,
            )
        )
    return SimpleNamespace(id=response_id, usage=usage, choices=choices)
