"""LLM 客户端核心：面向上层智能体的统一模型调用入口。

本模块封装对大语言模型的实际调用，提供 ``LLMClient`` 作为统一入口。它在
``protocol_drivers.py``（底层协议驱动）之上增加了以下能力：

- **配置装配**：根据 ``ModelConfig`` 解密密钥、选择协议、构建对应 SDK 客户端
  与驱动实例，并解析思考模式（thinking）与 legacy 额外参数；
- **输入处理**：把阶段负载（``stage_protocol.py`` 产出）或普通负载组装成请求
  messages，处理多轮上下文、图片附件与 token 预算裁剪；
- **三种调用模式**：
    * ``generate_text``：非流式文本生成，含空响应重试；
    * ``generate_text_stream``：流式文本生成（生成器），含首 token 计时与空流重试；
    * ``generate_json``：结构化 JSON 生成，含 JSON 修复重试与候选变体解析；
- **可观测性**：通过 ``observability.spans`` 记录每次调用的耗时、token 用量、
  结束原因等指标；
- **健壮性**：空响应重试、JSON 修复、错误分类（``LLMError``）、敏感信息脱敏
  （``_safe_fragment`` 会擦除 key/token）。

本模块是智能体运行时调用 LLM 的唯一通道，被路由/步骤/反思等阶段间接复用。
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Mapping
import copy
import hashlib
import json
import math
import re
from typing import Any
from urllib.parse import urlsplit

import httpx
from openai import OpenAI
from anthropic import Anthropic

from app.config import get_settings
from app.db.models import ModelConfig
from app.llm.model_protocols import ModelApiProtocol
from app.llm.output_policy import operation_output_tokens
from app.llm.protocol_drivers import (
    AnthropicMessagesDriver,
    CancellationToken,
    ChatCompletionsDriver,
    GeminiGenerateContentDriver,
    ProtocolCallError,
)
from app.llm.stage_protocol import (
    STAGE_PROTOCOL_KEY,
    TURN_STAGE_MESSAGES_KEY,
    render_stage_user_message,
)
from app.observability.spans import current_llm_operation, llm_span_attributes, start_llm_call
from app.security.encryption import decrypt_secret


class LLMError(Exception):
    """Raised when an LLM provider request or response normalization fails."""


# —— 调用重试与超时相关常量 ——
JSON_REPAIR_ATTEMPTS = 3  # JSON 生成失败后的最大修复重试次数
EMPTY_RESPONSE_RETRIES = 2  # 空响应最大重试次数
EMPTY_RESPONSE_MESSAGE = "Model returned an empty response"
DEFAULT_MODEL_API_TIMEOUT_SECONDS = 600.0  # 模型调用默认超时（秒）
DEFAULT_INPUT_TOKEN_BUDGET = 32_000  # 输入 token 预算（用于裁剪历史消息）
TURN_STAGE_MESSAGE_MARKER = "_agent_turn_message"  # 标记多阶段轮次消息的内部字段


class _CurrentStageText(str):
    """标记字符串为“当前阶段渲染文本”的 str 子类。

    用于在 ``_request_messages`` 中区分：该字符串是阶段协议渲染出的当前输入
    （不应再额外包装），而非普通 JSON 序列化负载。
    """


class LLMClient:
    """LLM 统一客户端：封装模型配置装配与三种调用模式。

    根据传入的 ``ModelConfig`` 解密密钥并按协议（OpenAI/Anthropic/Gemini）构建
    对应的 SDK 客户端与 ``ProtocolDriver`` 实例，对外提供文本生成、流式生成与
    JSON 生成三种调用方式。实例在创建后即可重复调用。

    Attributes:
        api_protocol: 解析后的协议枚举。
        api_key: 解密后的 API 密钥。
        model: 模型名。
        temperature: 采样温度。
        max_output_tokens: 最大输出 token 数。
        base_url: 模型端点 URL。
        timeout_seconds: 调用超时（秒）。
        extra_body: 透传给协议的额外请求体参数。
        thinking_mode: 思考模式（enabled/disabled/空）。
        client: 底层 SDK 客户端对象。
        driver: 协议驱动实例。
    """

    def __init__(self, model_config: ModelConfig):
        """根据模型配置初始化客户端。

        解析协议、解密密钥、确定超时与端点，并按协议创建对应的 SDK 客户端与
        驱动；同时解析 legacy 额外参数与思考模式。

        Args:
            model_config: 模型配置对象（含 api_protocol、加密密钥、端点等）。

        Raises:
            LLMError: 协议不支持、密钥未配置或协议未知时。
        """
        try:
            protocol = ModelApiProtocol(
                getattr(model_config, "api_protocol", "openai_chat_completions")
            )
        except ValueError as exc:
            raise LLMError("MODEL_PROTOCOL_UNSUPPORTED") from exc
        api_key = decrypt_secret(model_config.api_key_encrypted)
        if not api_key:
            raise LLMError("Model API key is not configured")
        self.timeout_seconds = (
            getattr(model_config, "timeout_seconds", None)
            or get_settings().model_api_timeout_seconds
            or DEFAULT_MODEL_API_TIMEOUT_SECONDS
        )
        self.base_url = str(model_config.base_url or "")
        if protocol is ModelApiProtocol.OPENAI_CHAT_COMPLETIONS:
            self.client = OpenAI(
                api_key=api_key,
                base_url=self.base_url,
                timeout=self.timeout_seconds,
            )
            self.driver = ChatCompletionsDriver(self.client)
        elif protocol is ModelApiProtocol.ANTHROPIC_MESSAGES:
            kwargs: dict[str, Any] = {
                "api_key": api_key,
                "timeout": self.timeout_seconds,
                "max_retries": 0,
            }
            if self.base_url:
                kwargs["base_url"] = self.base_url
            self.client = Anthropic(**kwargs)
            self.driver = AnthropicMessagesDriver(self.client)
        elif protocol is ModelApiProtocol.GEMINI_GENERATE_CONTENT:
            self.client = httpx.Client(timeout=self.timeout_seconds)
            self.driver = GeminiGenerateContentDriver(
                self.client,
                self.base_url,
                api_key,
                model_config.model,
            )
        else:
            raise LLMError("MODEL_PROTOCOL_UNSUPPORTED")
        self.api_protocol = protocol
        self.api_key = api_key
        self.model = model_config.model
        self.temperature = model_config.temperature
        self.max_output_tokens = model_config.max_output_tokens
        legacy_extra_body = getattr(model_config, "legacy_extra_body", {})
        protocol_options = getattr(model_config, "protocol_options", {})
        self.extra_body = _normalize_extra_body(
            legacy_extra_body
            or getattr(model_config, "extra_body_json", {})
            or protocol_options
        )
        settings = get_settings()
        self.thinking_mode = (
            _thinking_mode_from_extra_body(self.extra_body)
            or _thinking_mode_for_model(
                getattr(settings, "model_thinking_mode", ""),
                getattr(settings, "model_thinking_models", ""),
                self.model,
            )
        )

    def generate_text(
        self,
        system_prompt: str,
        user_payload: dict[str, Any] | str,
        response_format: dict[str, str] | None = None,
        cancellation: CancellationToken | None = None,
    ) -> str:
        """非流式文本生成，含空响应重试。

        组装请求 messages（含上下文裁剪）、注入思考参数，调用驱动 complete；
        若返回空内容则按 ``EMPTY_RESPONSE_RETRIES`` 重试。每次调用均记录可观测
        span，成功后会记录阶段交换（用于多轮阶段上下文）。

        Args:
            system_prompt: 系统提示词。
            user_payload: 用户负载（阶段负载 dict 或纯字符串）。
            response_format: 可选响应格式约束（如 ``{"type": "json_object"}``）。
            cancellation: 可选取消令牌。

        Returns:
            str: 模型生成的文本内容。

        Raises:
            LLMError: 多次重试仍为空响应、协议错误或其他上游失败时。
        """
        max_output_tokens = operation_output_tokens(
            current_llm_operation(), self.max_output_tokens
        )
        context_messages, serialized = _prepare_user_input(user_payload)
        request_messages = _request_messages(system_prompt, context_messages, serialized)
        request_messages = _fit_request_messages(request_messages)
        if isinstance(user_payload, dict) and isinstance(
            user_payload.get(STAGE_PROTOCOL_KEY), dict
        ):
            self._last_stage_request_user_content = copy.deepcopy(
                request_messages[-1].get("content")
            )
        request_shape = _request_shape_metrics(
            system_prompt, context_messages, serialized, request_messages
        )
        try:
            request: dict[str, Any] = {
                "model": self.model,
                "messages": request_messages,
                "temperature": self.temperature,
                "max_tokens": max_output_tokens,
            }
            if cancellation is not None:
                request["_cancellation"] = cancellation
            if response_format:
                request["response_format"] = response_format
            request.update(
                _thinking_request_kwargs(
                    getattr(self, "thinking_mode", ""),
                    getattr(self, "extra_body", {}),
                )
            )
            empty_diagnostics: list[str] = []
            for attempt in range(EMPTY_RESPONSE_RETRIES + 1):
                span = start_llm_call(
                    model=self.model,
                    endpoint=_endpoint_label(getattr(self, "base_url", "")),
                    request_kind=self._protocol_driver().request_kind,
                    stream=False,
                    attempt=attempt + 1,
                    retry_count=attempt,
                    max_attempts=EMPTY_RESPONSE_RETRIES + 1,
                    max_output_tokens=max_output_tokens,
                    thinking_mode=getattr(self, "thinking_mode", "") or "provider_default",
                    **request_shape,
                )
                try:
                    completion = self._protocol_driver().complete(request)
                except BaseException as exc:
                    span.fail(exc, **_completion_span_metrics(None))
                    raise
                content = _completion_message_content(completion)
                metrics = _completion_span_metrics(completion)
                if content.strip():
                    span.finish(
                        ttft_ms=span.elapsed_ms(),
                        output_chars=len(content),
                        status="success",
                        **metrics,
                    )
                    if not getattr(self, "_defer_stage_recording", False):
                        _record_stage_exchange(
                            user_payload,
                            content,
                            request_user_content=getattr(
                                self, "_last_stage_request_user_content", None
                            ),
                        )
                    return content
                span.finish(
                    ttft_ms=span.elapsed_ms(),
                    output_chars=0,
                    status="empty",
                    **metrics,
                )
                empty_diagnostics.append(_completion_empty_diagnostic(completion, attempt + 1))
                if attempt >= EMPTY_RESPONSE_RETRIES:
                    raise LLMError(_empty_response_detail(self, empty_diagnostics))
        except Exception as exc:
            if isinstance(exc, LLMError):
                raise
            if isinstance(exc, ProtocolCallError):
                raise LLMError(exc.code) from exc
            raise LLMError(_provider_failure_detail(self, exc)) from exc

    def generate_text_stream(
        self,
        system_prompt: str,
        user_payload: dict[str, Any] | str,
        cancellation: CancellationToken | None = None,
    ) -> Iterator[str]:
        """流式文本生成（生成器），含空流重试。

        以生成器逐块产出文本增量：首块缓冲直到出现非空白内容再首次 yield（避免
        先 yield 空串）。过程中累计 chunk 数、首 token 时间、token 用量等指标，
        全部空流时按 ``EMPTY_RESPONSE_RETRIES`` 重试。

        Args:
            system_prompt: 系统提示词。
            user_payload: 用户负载（阶段负载 dict 或纯字符串）。
            cancellation: 可选取消令牌。

        Yields:
            str: 文本增量片段。

        Raises:
            LLMError: 多次重试仍为空流、协议错误或其他上游失败时。
        """
        max_output_tokens = operation_output_tokens(
            current_llm_operation(), self.max_output_tokens
        )
        context_messages, serialized = _prepare_user_input(user_payload)
        request_messages = _request_messages(system_prompt, context_messages, serialized)
        request_messages = _fit_request_messages(request_messages)
        if isinstance(user_payload, dict) and isinstance(
            user_payload.get(STAGE_PROTOCOL_KEY), dict
        ):
            self._last_stage_request_user_content = copy.deepcopy(
                request_messages[-1].get("content")
            )
        request_shape = _request_shape_metrics(
            system_prompt, context_messages, serialized, request_messages
        )
        try:
            empty_diagnostics: list[str] = []
            for attempt in range(EMPTY_RESPONSE_RETRIES + 1):
                span = start_llm_call(
                    model=self.model,
                    endpoint=_endpoint_label(getattr(self, "base_url", "")),
                    request_kind=self._protocol_driver().request_kind,
                    stream=True,
                    attempt=attempt + 1,
                    retry_count=attempt,
                    max_attempts=EMPTY_RESPONSE_RETRIES + 1,
                    max_output_tokens=max_output_tokens,
                    thinking_mode=getattr(self, "thinking_mode", "") or "provider_default",
                    **request_shape,
                )
                stream_usage_metrics: dict[str, Any] = {}
                pending_parts: list[str] = []
                recorded_parts: list[str] = []
                emitted_text = False
                chunk_count = 0
                choice_chunk_count = 0
                reasoning_chars = 0
                output_chars = 0
                first_content_ms: float | None = None
                provider_setup_ms: float | None = None
                finish_reasons: set[str] = set()
                response_ids: set[str] = set()
                try:
                    stream_request = {
                        "model": self.model,
                        "messages": request_messages,
                        "temperature": self.temperature,
                        "max_tokens": max_output_tokens,
                        **_thinking_request_kwargs(
                            getattr(self, "thinking_mode", ""),
                            getattr(self, "extra_body", {}),
                        ),
                    }
                    if cancellation is not None:
                        stream_request["_cancellation"] = cancellation
                    stream = self._protocol_driver().stream(stream_request)
                    provider_setup_ms = span.elapsed_ms()
                    for chunk in stream:
                        chunk_count += 1
                        chunk_usage_metrics = _usage_span_metrics(getattr(chunk, "usage", None))
                        if chunk_usage_metrics:
                            stream_usage_metrics.update(chunk_usage_metrics)
                        response_id = _safe_fragment(getattr(chunk, "id", None), 48)
                        if response_id:
                            response_ids.add(response_id)
                        choices = getattr(chunk, "choices", None) or []
                        if not choices:
                            continue
                        choice_chunk_count += len(choices)
                        choice = choices[0]
                        finish_reason = _safe_fragment(getattr(choice, "finish_reason", None), 32)
                        if finish_reason:
                            finish_reasons.add(finish_reason)
                        delta = getattr(choice, "delta", None)
                        reasoning_chars += len(_reasoning_text(delta))
                        content = _content_text(getattr(delta, "content", None))
                        if not content:
                            continue
                        recorded_parts.append(content)
                        output_chars += len(content)
                        if first_content_ms is None:
                            first_content_ms = span.elapsed_ms()
                        if emitted_text:
                            yield content
                            continue
                        pending_parts.append(content)
                        buffered = "".join(pending_parts)
                        if buffered.strip():
                            emitted_text = True
                            pending_parts.clear()
                            yield buffered
                except BaseException as exc:
                    span.fail(
                        exc,
                        provider_setup_ms=provider_setup_ms,
                        ttft_ms=first_content_ms,
                        output_chars=output_chars,
                        stream_chunks=chunk_count,
                        reasoning_chars=reasoning_chars,
                        **stream_usage_metrics,
                    )
                    raise
                if emitted_text:
                    span.finish(
                        provider_setup_ms=provider_setup_ms,
                        ttft_ms=first_content_ms,
                        stream_duration_ms=round(span.elapsed_ms() - (first_content_ms or 0), 3),
                        output_chars=output_chars,
                        stream_chunks=chunk_count,
                        choice_chunks=choice_chunk_count,
                        reasoning_chars=reasoning_chars,
                        finish_reasons=sorted(finish_reasons),
                        provider_response_ids=sorted(response_ids),
                        **stream_usage_metrics,
                    )
                    _record_stage_exchange(
                        user_payload,
                        "".join(recorded_parts),
                        request_user_content=getattr(
                            self, "_last_stage_request_user_content", None
                        ),
                    )
                    return
                span.finish(
                    provider_setup_ms=provider_setup_ms,
                    ttft_ms=None,
                    output_chars=0,
                    stream_chunks=chunk_count,
                    choice_chunks=choice_chunk_count,
                    reasoning_chars=reasoning_chars,
                    finish_reasons=sorted(finish_reasons),
                    provider_response_ids=sorted(response_ids),
                    status="empty",
                    **stream_usage_metrics,
                )
                empty_diagnostics.append(
                    _stream_empty_diagnostic(
                        attempt + 1,
                        chunk_count,
                        choice_chunk_count,
                        reasoning_chars,
                        finish_reasons,
                        response_ids,
                    )
                )
            raise LLMError(_empty_response_detail(self, empty_diagnostics))
        except Exception as exc:
            if isinstance(exc, LLMError):
                raise
            if isinstance(exc, ProtocolCallError):
                raise LLMError(exc.code) from exc
            raise LLMError(_provider_failure_detail(self, exc)) from exc

    def _protocol_driver(
        self,
    ) -> ChatCompletionsDriver | AnthropicMessagesDriver | GeminiGenerateContentDriver:
        """惰性获取当前协议驱动实例（缺失时按协议重建）。

        Returns:
            对应协议的驱动实例。
        """
        driver = getattr(self, "driver", None)
        if driver is None:
            if getattr(self, "api_protocol", ModelApiProtocol.OPENAI_CHAT_COMPLETIONS) is (
                ModelApiProtocol.GEMINI_GENERATE_CONTENT
            ):
                driver = GeminiGenerateContentDriver(
                    self.client,
                    self.base_url,
                    getattr(self, "api_key", ""),
                    self.model,
                )
            else:
                driver = ChatCompletionsDriver(self.client)
            self.driver = driver
        return driver

    def generate_json(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
        cancellation: CancellationToken | None = None,
    ) -> dict[str, Any]:
        """结构化 JSON 生成，含修复重试与候选变体解析。

        优先尝试 JSON 模式（response_format=json_object）；若不支持则退回普通文本。
        解析失败时会把上一轮的错误与输出作为修复提示注入下一轮，最多重试
        ``JSON_REPAIR_ATTEMPTS`` 次。解析阶段会尝试多种候选变体（去尾逗号、修复
        字符串内容等）。

        Args:
            system_prompt: 系统提示词。
            user_payload: 用户负载（dict）。
            cancellation: 可选取消令牌。

        Returns:
            dict[str, Any]: 解析得到的 JSON 对象。

        Raises:
            LLMError: 达到最大修复次数仍无法解析为合法 JSON 时。
        """
        outputs: list[str] = []
        next_payload = user_payload
        last_error: json.JSONDecodeError | None = None
        json_mode_supported = True
        for attempt in range(JSON_REPAIR_ATTEMPTS + 1):
            with llm_span_attributes(
                response_mode="json",
                json_attempt=attempt + 1,
                json_retry_count=attempt,
                json_max_attempts=JSON_REPAIR_ATTEMPTS + 1,
            ):
                previous_defer = getattr(self, "_defer_stage_recording", False)
                self._defer_stage_recording = True
                try:
                    text = self._generate_json_candidate(
                        system_prompt, next_payload, json_mode_supported, cancellation
                    )
                    if json_mode_supported and _response_format_unsupported(text):
                        json_mode_supported = False
                        text = self.generate_text(system_prompt, next_payload)
                finally:
                    self._defer_stage_recording = previous_defer
            outputs.append(text)
            try:
                parsed = _loads_llm_json(text)
                _record_stage_exchange(
                    next_payload,
                    text,
                    request_user_content=getattr(
                        self, "_last_stage_request_user_content", None
                    ),
                )
                return parsed
            except json.JSONDecodeError as exc:
                last_error = exc
                _record_stage_exchange(
                    next_payload,
                    text,
                    request_user_content=getattr(
                        self, "_last_stage_request_user_content", None
                    ),
                )
                if attempt >= JSON_REPAIR_ATTEMPTS:
                    break
                next_payload = copy.deepcopy(user_payload)
                if isinstance(user_payload.get(STAGE_PROTOCOL_KEY), dict):
                    next_payload["conversation_context"] = user_payload.get(
                        "conversation_context"
                    )
                next_payload["_json_repair"] = {
                    "attempt": attempt + 1,
                    "max_attempts": JSON_REPAIR_ATTEMPTS,
                    "previous_output": _preview(text),
                    "parser_error": str(exc),
                    "instruction": (
                        "上一轮输出不是合法 JSON。请基于原始任务上下文重新输出完整、可解析的 JSON object。"
                        "字符串内部的双引号必须转义；不要输出 Markdown、解释、代码块或额外文本。"
                    ),
                }
        previews = "; ".join(
            f"attempt_{index + 1}_preview={_preview(output)!r}"
            for index, output in enumerate(outputs)
        )
        raise LLMError(
            f"Model did not return valid JSON after {JSON_REPAIR_ATTEMPTS} repair attempts; {previews}"
        ) from last_error

    def _generate_json_candidate(
        self,
        system_prompt: str,
        user_payload: dict[str, Any],
        json_mode_supported: bool,
        cancellation: CancellationToken | None = None,
    ) -> str:
        """生成单个 JSON 候选文本（单次尝试，不含重试循环）。

        Anthropic 协议无 JSON 模式，改为在系统提示词追加 JSON 约束；其余协议
        优先使用 JSON 模式，不支持或报错时退回普通文本。

        Args:
            system_prompt: 系统提示词。
            user_payload: 用户负载。
            json_mode_supported: 是否仍可使用 JSON 模式。
            cancellation: 可选取消令牌。

        Returns:
            str: 候选文本（可能非合法 JSON，由上层解析）。
        """
        def call_generate_text(prompt: str, payload: dict[str, Any], **kwargs: Any) -> str:
            if cancellation is not None:
                kwargs["cancellation"] = cancellation
            return self.generate_text(prompt, payload, **kwargs)

        if getattr(self, "api_protocol", ModelApiProtocol.OPENAI_CHAT_COMPLETIONS) is (
            ModelApiProtocol.ANTHROPIC_MESSAGES
        ):
            return call_generate_text(
                system_prompt.rstrip()
                + "\n\n只返回一个合法 JSON object；不要输出 Markdown、代码围栏、解释或额外文本。",
                user_payload,
            )
        if not json_mode_supported:
            return call_generate_text(system_prompt, user_payload)
        try:
            return call_generate_text(
                system_prompt,
                user_payload,
                response_format={"type": "json_object"},
            )
        except TypeError:
            return call_generate_text(system_prompt, user_payload)
        except LLMError as exc:
            message = str(exc)
            if _response_format_unsupported(message):
                return message
            if _empty_response(message):
                return call_generate_text(system_prompt, user_payload)
            raise


def _completion_message_content(completion: Any) -> str:
    """从非流式响应对象中提取首条 choice 的文本内容。

    Args:
        completion: 协议驱动返回的响应对象。

    Returns:
        str: 文本内容；结构异常或缺失时返回空串。
    """
    try:
        choice = completion.choices[0]
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
    except (IndexError, TypeError, AttributeError):
        return ""
    return _content_text(content)


def _request_shape_metrics(
    system_prompt: str,
    context_messages: list[dict[str, Any]],
    serialized_payload: str | list[dict[str, Any]],
    request_messages: list[dict[str, Any]],
) -> dict[str, Any]:
    """计算请求形状指标（字符数、消息数、角色、前缀指纹等），用于可观测 span。

    Args:
        system_prompt: 系统提示词。
        context_messages: 上下文消息列表。
        serialized_payload: 序列化后的负载。
        request_messages: 最终请求消息列表。

    Returns:
        dict[str, Any]: 指标字典。
    """
    context_chars = sum(
        len(_content_text(message.get("content"))) for message in context_messages
    )
    request_chars = sum(
        len(_content_text(message.get("content"))) for message in request_messages
    )
    return {
        "system_prompt_chars": len(system_prompt),
        "context_message_count": len(context_messages),
        "context_text_chars": context_chars,
        "payload_chars": len(_content_text(serialized_payload)),
        "request_text_chars": request_chars,
        "request_message_count": len(request_messages),
        "request_message_roles": [str(message.get("role") or "") for message in request_messages],
        "request_message_chars": [
            len(_content_text(message.get("content"))) for message in request_messages
        ],
        "request_prefix_fingerprints": _request_prefix_fingerprints(request_messages),
    }


def _request_prefix_fingerprints(messages: list[dict[str, Any]]) -> list[str]:
    """计算消息序列的滚动前缀指纹（每条消息累计的 SHA256 前 16 位）。

    用于可观测性，便于排查请求内容差异。

    Args:
        messages: 请求消息列表。

    Returns:
        list[str]: 与消息一一对应的前缀指纹列表。
    """
    digest = hashlib.sha256()
    fingerprints: list[str] = []
    for message in messages:
        serialized = json.dumps(
            {
                "role": str(message.get("role") or ""),
                "content": message.get("content"),
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest.update(serialized.encode("utf-8"))
        digest.update(b"\n")
        fingerprints.append(digest.hexdigest()[:16])
    return fingerprints


def _normalize_thinking_mode(value: Any) -> str:
    """把思考模式值规范化为 ``enabled``/``disabled`` 或空串。

    Args:
        value: 原始思考模式值。

    Returns:
        str: 规范化后的模式字符串。
    """
    mode = str(value or "").strip().lower()
    return mode if mode in {"enabled", "disabled"} else ""


def _thinking_mode_for_model(mode: Any, configured_models: Any, model: Any) -> str:
    """根据全局配置与模型白名单确定某模型的思考模式。

    仅当配置的模式有效且模型在白名单内（白名单为空时不限制）时返回模式值。

    Args:
        mode: 配置的思考模式。
        configured_models: 允许的模型白名单（逗号分隔字符串）。
        model: 当前模型名。

    Returns:
        str: 适用的思考模式，不适用返回空串。
    """
    normalized_mode = _normalize_thinking_mode(mode)
    if not normalized_mode:
        return ""
    allowed_models = {
        item.strip().lower()
        for item in str(configured_models or "").split(",")
        if item.strip()
    }
    if allowed_models and str(model or "").strip().lower() not in allowed_models:
        return ""
    return normalized_mode


def _normalize_extra_body(value: Any) -> dict[str, Any]:
    """把 extra_body 归一化为可变字典（递归拷贝，元组转列表）。

    Args:
        value: 原始值（期望为 Mapping）。

    Returns:
        dict[str, Any]: 可变字典副本；非 Mapping 返回空字典。
    """
    if not isinstance(value, Mapping):
        return {}
    return {str(key): _mutable_copy(item) for key, item in value.items()}


def _mutable_copy(value: Any) -> Any:
    """递归生成可变拷贝：Mapping→dict、tuple→list，其余深拷贝。

    Args:
        value: 任意值。

    Returns:
        Any: 可变副本。
    """
    if isinstance(value, Mapping):
        return {str(key): _mutable_copy(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_mutable_copy(item) for item in value]
    return copy.deepcopy(value)


def _thinking_mode_from_extra_body(extra_body: Any) -> str:
    """从 extra_body 中提取思考模式（thinking.type）。

    Args:
        extra_body: 额外请求体。

    Returns:
        str: 规范化思考模式，无则空串。
    """
    normalized = _normalize_extra_body(extra_body)
    thinking = normalized.get("thinking")
    if not isinstance(thinking, dict):
        return ""
    return _normalize_thinking_mode(thinking.get("type"))


def _thinking_request_kwargs(mode: Any, extra_body: Any = None) -> dict[str, Any]:
    """构造注入请求的思考参数（``extra_body`` 中含 thinking 配置）。

    若指定了有效思考模式，则写入/覆盖 extra_body.thinking.type。

    Args:
        mode: 思考模式。
        extra_body: 原始额外请求体。

    Returns:
        dict[str, Any]: 形如 ``{"extra_body": {...}}`` 的请求参数；为空时返回空字典。
    """
    body = _normalize_extra_body(extra_body)
    normalized = _normalize_thinking_mode(mode)
    if normalized:
        thinking = body.get("thinking")
        body["thinking"] = {
            **(thinking if isinstance(thinking, dict) else {}),
            "type": normalized,
        }
    return {"extra_body": body} if body else {}


def _request_messages(
    system_prompt: str,
    context_messages: list[dict[str, Any]],
    serialized_payload: str | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """组装最终的请求 messages：system + 上下文 + 当前用户输入。

    若当前输入是普通序列化字符串且存在上下文，会加上“本轮输入（不写入历史）”
    前缀；若是 ``_CurrentStageText``（阶段渲染文本）则原样使用；空负载会补一条
    ``{}`` 用户消息以保证至少有一轮 user 输入。

    Args:
        system_prompt: 系统提示词。
        context_messages: 上下文消息。
        serialized_payload: 序列化后的当前负载。

    Returns:
        list[dict[str, Any]]: 请求 messages。
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt.rstrip()}
    ]
    messages.extend(context_messages)
    if serialized_payload != "{}":
        current_input = serialized_payload
        if (
            context_messages
            and isinstance(serialized_payload, str)
            and not isinstance(serialized_payload, _CurrentStageText)
        ):
            current_input = (
                "本轮输入（仅用于当前调用，不写入对话历史）：\n"
                f"{serialized_payload}"
            )
        messages.append(
            {
                "role": "user",
                "content": current_input,
            }
        )
    elif not context_messages:
        messages.append({"role": "user", "content": "{}"})
    return messages


def _fit_request_messages(
    messages: list[dict[str, Any]], token_budget: int = DEFAULT_INPUT_TOKEN_BUDGET
) -> list[dict[str, Any]]:
    """在 token 预算内裁剪请求消息，优先保留摘要与阶段消息。

    裁剪顺序：先删普通历史消息 → 再删非阶段历史消息 → 裁剪过长的阶段消息 →
    删除最旧的阶段轮次 → 最后裁剪当前消息。最终剥离内部标记字段。

    Args:
        messages: 原始请求消息。
        token_budget: token 预算上限。

    Returns:
        list[dict[str, Any]]: 裁剪后的消息（不含内部标记字段）。
    """
    projected = copy.deepcopy(messages)
    while len(projected) > 2 and _request_tokens(projected) > token_budget:
        removable_index = next(
            (
                index
                for index in range(1, len(projected) - 1)
                if not _is_history_summary_message(projected[index])
                and not _is_turn_stage_message(projected[index])
            ),
            None,
        )
        if removable_index is None:
            break
        projected.pop(removable_index)

    while len(projected) > 2 and _request_tokens(projected) > token_budget:
        removable_index = next(
            (
                index
                for index in range(1, len(projected) - 1)
                if not _is_turn_stage_message(projected[index])
            ),
            None,
        )
        if removable_index is None:
            break
        projected.pop(removable_index)

    _trim_turn_stage_messages(projected, token_budget)
    _drop_oldest_turn_stage_exchanges(projected, token_budget)

    if projected and _request_tokens(projected) > token_budget:
        fixed_tokens = _request_tokens(projected[:-1])
        projected[-1] = _trim_request_message(
            projected[-1], max(1, token_budget - fixed_tokens)
        )
    return [
        {key: value for key, value in message.items() if key != TURN_STAGE_MESSAGE_MARKER}
        for message in projected
    ]


def _request_tokens(messages: list[dict[str, Any]]) -> int:
    """粗略估算消息列表的 token 数（UTF-8 字节数 / 4 + 每条 6 token 开销）。

    Args:
        messages: 消息列表。

    Returns:
        int: 估算 token 数（至少每条 1）。
    """
    return sum(
        max(1, math.ceil(len(_content_text(message.get("content")).encode("utf-8")) / 4))
        + 6
        for message in messages
    )


def _is_history_summary_message(message: dict[str, Any]) -> bool:
    """判断消息是否为历史摘要消息（裁剪时应优先保留）。

    Args:
        message: 消息字典。

    Returns:
        bool: 是历史摘要返回 True。
    """
    content = _content_text(message.get("content")).lstrip()
    return content.startswith(
        ("历史的信息可以被总结为：", "近期的历史信息总结为：")
    )


def _is_turn_stage_message(message: dict[str, Any]) -> bool:
    """判断消息是否为多阶段轮次消息（带内部标记）。

    Args:
        message: 消息字典。

    Returns:
        bool: 是轮次阶段消息返回 True。
    """
    return message.get(TURN_STAGE_MESSAGE_MARKER) is True


def _trim_turn_stage_messages(
    messages: list[dict[str, Any]], token_budget: int
) -> None:
    """裁剪过长的轮次阶段消息（>512 字符的），缩小到预算内。

    在仍超预算且存在较长阶段消息时，按需缩减最长那条的 token；裁剪后保留标记。

    Args:
        messages: 消息列表（原地修改）。
        token_budget: token 预算。
    """
    while _request_tokens(messages) > token_budget:
        candidates = [
            (len(_content_text(message.get("content"))), index)
            for index, message in enumerate(messages[1:-1], start=1)
            if _is_turn_stage_message(message)
            and len(_content_text(message.get("content"))) > 512
        ]
        if not candidates:
            break
        current_length, index = max(candidates)
        excess_tokens = _request_tokens(messages) - token_budget
        target_tokens = max(128, math.ceil(current_length / 4) - excess_tokens)
        trimmed = _trim_request_message(messages[index], target_tokens)
        trimmed[TURN_STAGE_MESSAGE_MARKER] = True
        if len(_content_text(trimmed.get("content"))) >= current_length:
            break
        messages[index] = trimmed


def _drop_oldest_turn_stage_exchanges(
    messages: list[dict[str, Any]], token_budget: int
) -> None:
    """在仍超预算时，按 user+assistant 对删除最旧的轮次阶段交换。

    Args:
        messages: 消息列表（原地修改）。
        token_budget: token 预算。
    """
    while _request_tokens(messages) > token_budget:
        stage_indices = [
            index
            for index, message in enumerate(messages[1:-1], start=1)
            if _is_turn_stage_message(message)
        ]
        if len(stage_indices) <= 2:
            break
        first_index = stage_indices[0]
        remove_count = 1
        if (
            len(stage_indices) > 1
            and stage_indices[1] == first_index + 1
            and messages[first_index].get("role") == "user"
            and messages[first_index + 1].get("role") == "assistant"
        ):
            remove_count = 2
        del messages[first_index : first_index + remove_count]


def _trim_request_message(
    message: dict[str, Any], token_budget: int
) -> dict[str, Any]:
    """把单条消息内容裁剪到 token 预算内（支持文本块列表与纯文本）。

    Args:
        message: 消息字典。
        token_budget: 该消息允许的 token 预算。

    Returns:
        dict[str, Any]: 裁剪后的消息副本。
    """
    content = message.get("content")
    byte_budget = max(4, token_budget * 4)
    if isinstance(content, list):
        parts = copy.deepcopy(content)
        text_part = next(
            (
                part
                for part in parts
                if isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            ),
            None,
        )
        if text_part is not None:
            text_part["text"] = _trim_request_text(text_part["text"], byte_budget)
        return {**message, "content": parts}
    return {**message, "content": _trim_request_text(str(content or ""), byte_budget)}


def _trim_request_text(text: str, byte_budget: int) -> str:
    """把文本裁剪到字节预算内，保留头尾并插入省略标记。

    Args:
        text: 原始文本。
        byte_budget: 字节预算。

    Returns:
        str: 裁剪后的文本（含省略标记）；未超预算则原样返回。
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_budget:
        return text
    marker = "\n...<输入超过 32k，已省略中间部分>...\n"
    marker_bytes = len(marker.encode("utf-8"))
    available = max(8, byte_budget - marker_bytes)
    head_size = int(available * 0.7)
    tail_size = available - head_size
    head = encoded[:head_size].decode("utf-8", errors="ignore")
    tail = encoded[-tail_size:].decode("utf-8", errors="ignore")
    return f"{head}{marker}{tail}"


def _completion_span_metrics(completion: Any) -> dict[str, Any]:
    """从非流式响应对象提取 span 指标（response_id、finish_reason、usage 等）。

    Args:
        completion: 响应对象。

    Returns:
        dict[str, Any]: 指标字典；响应为 None 时返回空字典。
    """
    if completion is None:
        return {}
    choices = getattr(completion, "choices", None) or []
    finish_reason = None
    message = None
    if choices:
        finish_reason = _safe_fragment(getattr(choices[0], "finish_reason", None), 32) or None
        message = getattr(choices[0], "message", None)
    usage = getattr(completion, "usage", None)
    return {
        "provider_response_id": _safe_fragment(getattr(completion, "id", None), 48) or None,
        "finish_reason": finish_reason,
        "reasoning_chars": len(_reasoning_text(message)),
        **_usage_span_metrics(usage),
    }


def _usage_span_metrics(usage: Any) -> dict[str, Any]:
    """把 usage 对象映射为 span 指标（input/output/total/cached tokens）。

    兼容多家厂商的字段命名（prompt_tokens/input_tokens 等）。

    Args:
        usage: usage 对象或 None。

    Returns:
        dict[str, Any]: 非 None 指标字典；usage 为 None 时返回空字典。
    """
    if usage is None:
        return {}
    input_tokens = _usage_value(usage, "prompt_tokens", "input_tokens")
    output_tokens = _usage_value(usage, "completion_tokens", "output_tokens")
    total_tokens = _usage_value(usage, "total_tokens")
    prompt_details = _usage_object(usage, "prompt_tokens_details", "input_tokens_details")
    cached_input_tokens = _usage_value(
        prompt_details,
        "cached_tokens",
        "cache_read_tokens",
        "cache_read_input_tokens",
    )
    if cached_input_tokens is None:
        cached_input_tokens = _usage_value(
            usage,
            "cached_tokens",
            "prompt_cache_hit_tokens",
            "cache_read_input_tokens",
        )
    metrics: dict[str, Any] = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "cached_input_tokens": cached_input_tokens,
    }
    if input_tokens is not None and cached_input_tokens is not None:
        metrics["uncached_input_tokens"] = max(0, input_tokens - cached_input_tokens)
    return {key: value for key, value in metrics.items() if value is not None}


def _usage_object(source: Any, *names: str) -> Any:
    """按多个候选字段名从 dict/对象中取首个非空值。

    Args:
        source: 数据源（dict 或对象）。
        *names: 候选字段名。

    Returns:
        Any: 首个非空值；都为空返回 None。
    """
    for name in names:
        value = source.get(name) if isinstance(source, dict) else getattr(source, name, None)
        if value is not None:
            return value
    return None


def _usage_value(source: Any, *names: str) -> int | None:
    """按候选字段名取数值型 usage 值（布尔值视为无效）。

    Args:
        source: 数据源。
        *names: 候选字段名。

    Returns:
        int | None: 整数值；无效或缺失返回 None。
    """
    value = _usage_object(source, *names)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    return None


def _content_text(content: Any) -> str:
    """从 content（字符串/列表/对象）中提取拼接的纯文本。

    Args:
        content: content 值。

    Returns:
        str: 纯文本。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_content_part_text(item) for item in content)
    return _content_part_text(content)


def _content_part_text(item: Any) -> str:
    """从单个 content part（dict/对象/字符串）中提取文本。

    兼容 ``text`` 字段、嵌套 ``value`` 字段等多种结构。

    Args:
        item: content part。

    Returns:
        str: 文本；无法提取返回空串。
    """
    if isinstance(item, str):
        return item
    text: Any = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
    if isinstance(text, str):
        return text
    if isinstance(text, dict) and isinstance(text.get("value"), str):
        return text["value"]
    value = getattr(text, "value", None)
    return value if isinstance(value, str) else ""


def _completion_empty_diagnostic(completion: Any, attempt: int) -> str:
    """为非流式空响应生成诊断字符串（response_id/choices/finish_reason 等）。

    Args:
        completion: 响应对象。
        attempt: 第几次尝试（1 基）。

    Returns:
        str: 诊断字符串。
    """
    choices = getattr(completion, "choices", None) or []
    response_id = _safe_fragment(getattr(completion, "id", None), 48) or "missing"
    if not choices:
        return f"attempt_{attempt}: response_id={response_id}, choices=0"
    choice = choices[0]
    message = getattr(choice, "message", None)
    finish_reason = _safe_fragment(getattr(choice, "finish_reason", None), 32) or "missing"
    refusal = _safe_fragment(getattr(message, "refusal", None), 80)
    reasoning_chars = len(_reasoning_text(message))
    tool_calls = getattr(message, "tool_calls", None) or []
    content = getattr(message, "content", None)
    content_shape = _content_shape(content)
    usage = getattr(completion, "usage", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    parts = [
        f"attempt_{attempt}: response_id={response_id}",
        f"choices={len(choices)}",
        f"finish_reason={finish_reason}",
        f"content={content_shape}",
        f"reasoning_chars={reasoning_chars}",
        f"tool_calls={len(tool_calls)}",
    ]
    if refusal:
        parts.append(f"refusal={refusal}")
    if completion_tokens is not None:
        parts.append(f"completion_tokens={completion_tokens}")
    return ", ".join(parts)


def _stream_empty_diagnostic(
    attempt: int,
    chunk_count: int,
    choice_chunk_count: int,
    reasoning_chars: int,
    finish_reasons: set[str],
    response_ids: set[str],
) -> str:
    """为空流生成诊断字符串（chunk 数、finish_reason、response_id 等）。

    Args:
        attempt: 第几次尝试（1 基）。
        chunk_count: 总 chunk 数。
        choice_chunk_count: 含 choices 的 chunk 数。
        reasoning_chars: 思考内容字符数。
        finish_reasons: 收集到的结束原因集合。
        response_ids: 收集到的响应 id 集合。

    Returns:
        str: 诊断字符串。
    """
    return (
        f"attempt_{attempt}: stream_chunks={chunk_count}, choice_chunks={choice_chunk_count}, "
        f"finish_reason={','.join(sorted(finish_reasons)) or 'missing'}, text_chars=0, "
        f"reasoning_chars={reasoning_chars}, response_id={','.join(sorted(response_ids)) or 'missing'}"
    )


def _empty_response_detail(client: Any, diagnostics: list[str]) -> str:
    """拼接空响应错误的详细消息（含尝试次数、模型、端点与各次诊断）。

    Args:
        client: LLM 客户端实例。
        diagnostics: 各次尝试的诊断字符串列表。

    Returns:
        str: 详细错误消息。
    """
    attempts = EMPTY_RESPONSE_RETRIES + 1
    model = _safe_fragment(getattr(client, "model", None), 80) or "unknown"
    endpoint = _endpoint_label(getattr(client, "base_url", None))
    response_details = " | ".join(diagnostics)
    return (
        f"{EMPTY_RESPONSE_MESSAGE} after {attempts} attempts; provider returned no usable message.content; "
        f"model={model}; endpoint={endpoint}; {response_details}"
    )


def _provider_failure_detail(client: Any, exc: Exception) -> str:
    """拼接上游调用失败的详细消息（错误类型、状态码、provider_code 等）。

    Args:
        client: LLM 客户端实例。
        exc: 原始异常。

    Returns:
        str: 详细错误消息（敏感信息已脱敏）。
    """
    model = _safe_fragment(getattr(client, "model", None), 80) or "unknown"
    endpoint = _endpoint_label(getattr(client, "base_url", None))
    timeout = getattr(client, "timeout_seconds", None)
    status_code = getattr(exc, "status_code", None)
    request_id = _safe_fragment(getattr(exc, "request_id", None), 64)
    error_type = type(exc).__name__
    message = _safe_fragment(exc, 240) or "no provider error message"
    provider_code = ""
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error_body = body.get("error") if isinstance(body.get("error"), dict) else body
        provider_code = _safe_fragment(error_body.get("code") or error_body.get("type"), 64)
        provider_message = _safe_fragment(error_body.get("message"), 160)
        if provider_message and provider_message not in message:
            message = f"{message}; provider_message={provider_message}"
    details = [
        f"LLM provider request failed ({error_type})",
        f"message={message}",
        f"model={model}",
        f"endpoint={endpoint}",
    ]
    if status_code is not None:
        details.append(f"status_code={status_code}")
    if provider_code:
        details.append(f"provider_code={provider_code}")
    if request_id:
        details.append(f"request_id={request_id}")
    if timeout is not None:
        details.append(f"timeout_seconds={timeout}")
    return "; ".join(details)


def _content_shape(content: Any) -> str:
    """描述 content 的结构形状（类型、字符数、是否纯空白等），用于诊断。

    Args:
        content: content 值。

    Returns:
        str: 形状描述字符串。
    """
    if content is None:
        return "null"
    text = _content_text(content)
    if isinstance(content, str):
        return f"string({len(content)} chars{' whitespace' if content and not content.strip() else ''})"
    if isinstance(content, list):
        return f"list({len(content)} parts, {len(text)} text_chars)"
    return f"{type(content).__name__}({len(text)} text_chars)"


def _reasoning_text(value: Any) -> str:
    """从 message/delta 中提取思考内容（reasoning_content/reasoning/thinking）。

    Args:
        value: message 或 delta 对象。

    Returns:
        str: 思考文本；无则空串。
    """
    if value is None:
        return ""
    for key in ("reasoning_content", "reasoning", "thinking"):
        content = value.get(key) if isinstance(value, dict) else getattr(value, key, None)
        text = _content_text(content)
        if text:
            return text
    return ""


def _safe_fragment(value: Any, limit: int) -> str:
    """把任意值转为安全片段：折叠空白、脱敏密钥/token、截断长度。

    用于日志与诊断，避免泄露 ``sk-``/``pt-`` 前缀的密钥或 query 中的 token。

    Args:
        value: 原始值。
        limit: 最大字符数。

    Returns:
        str: 脱敏并截断后的字符串。
    """
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-***", text)
    text = re.sub(r"\bpt-[A-Za-z0-9_-]{8,}\b", "pt-***", text)
    text = re.sub(
        r"(?i)(api[_-]?key|authorization|access[_-]?token|token)=([^&\s;]+)",
        r"\1=***",
        text,
    )
    return text[:limit]


def _endpoint_label(value: Any) -> str:
    """把 base_url 转为可观测用的端点标签（脱敏、保留 host:port/path）。

    Args:
        value: base_url 值。

    Returns:
        str: 端点标签；无法解析返回 ``unknown`` 或 ``configured-endpoint``。
    """
    raw = str(value or "").strip()
    if not raw:
        return "unknown"
    parsed = urlsplit(raw)
    if not parsed.hostname:
        return "configured-endpoint"
    host = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        return "configured-endpoint"
    if port:
        host = f"{host}:{port}"
    path = parsed.path.rstrip("/")
    return _safe_fragment(f"{parsed.scheme or 'http'}://{host}{path}", 160)


def _extract_json(text: str) -> str:
    """从模型输出文本中提取 JSON 片段（剥离代码围栏、截取首末大括号）。

    Args:
        text: 模型输出文本。

    Returns:
        str: 提取出的 JSON 候选字符串。
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.startswith("json"):
            stripped = stripped[4:].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end >= start:
        return stripped[start : end + 1]
    return stripped


def _loads_llm_json(text: str) -> Any:
    """尝试多种方式把模型输出解析为 JSON（含候选变体与 ast 兜底）。

    依次尝试原始文本、去尾逗号、修复字符串内容等变体；均失败时再用
    ``ast.literal_eval`` 兜底解析 dict/list。

    Args:
        text: 模型输出文本。

    Returns:
        Any: 解析得到的对象（通常为 dict）。

    Raises:
        json.JSONDecodeError: 所有变体均无法解析时。
    """
    candidate = _extract_json(text)
    last_error: json.JSONDecodeError | None = None
    seen: set[str] = set()
    for variant in _json_candidate_variants(candidate):
        if variant in seen:
            continue
        seen.add(variant)
        try:
            return json.loads(variant)
        except json.JSONDecodeError as exc:
            last_error = exc
    try:
        literal = ast.literal_eval(candidate)
    except (SyntaxError, ValueError):
        literal = None
    if isinstance(literal, (dict, list)):
        return literal
    if last_error is not None:
        raise last_error
    raise json.JSONDecodeError("Could not decode JSON", candidate, 0)


def _json_candidate_variants(text: str) -> tuple[str, ...]:
    """生成 JSON 解析候选变体（原始、去尾逗号、修复字符串、二者组合）。

    Args:
        text: JSON 候选文本。

    Returns:
        tuple[str, ...]: 候选变体元组。
    """
    stripped = text.strip()
    no_trailing_commas = _remove_trailing_commas(stripped)
    repaired_strings = _repair_json_string_content(stripped)
    repaired_strings_no_trailing = _remove_trailing_commas(repaired_strings)
    return (
        stripped,
        no_trailing_commas,
        repaired_strings,
        repaired_strings_no_trailing,
    )


def _remove_trailing_commas(text: str) -> str:
    """移除 JSON 中 ``}``/``]`` 前的多余逗号（模型常见错误）。

    Args:
        text: JSON 文本。

    Returns:
        str: 去尾逗号后的文本。
    """
    return re.sub(r",\s*([}\]])", r"\1", text)


def _repair_json_string_content(text: str) -> str:
    """修复 JSON 字符串内容：转义未转义的控制字符与误判的引号。

    通过状态机逐字符扫描：在字符串内把换行/制表符转义，并依据后续字符判断
    引号是字符串结束还是内部未转义引号（后者转义为 ``\\"``）。

    Args:
        text: JSON 文本。

    Returns:
        str: 修复后的 JSON 文本。
    """
    output: list[str] = []
    in_string = False
    index = 0
    while index < len(text):
        char = text[index]
        if not in_string:
            output.append(char)
            if char == '"':
                in_string = True
            index += 1
            continue
        if char == "\\":
            output.append(char)
            index += 1
            if index < len(text):
                output.append(text[index])
                index += 1
            continue
        if char == '"':
            if _quote_likely_closes_string(text, index):
                output.append(char)
                in_string = False
            else:
                output.append('\\"')
            index += 1
            continue
        if char == "\n":
            output.append("\\n")
        elif char == "\r":
            output.append("\\r")
        elif char == "\t":
            output.append("\\t")
        else:
            output.append(char)
        index += 1
    return "".join(output)


def _quote_likely_closes_string(text: str, quote_index: int) -> bool:
    """判断引号是否为字符串结束引号（看其后是否紧跟 ``:``/``,``/``}``/``]``）。

    Args:
        text: JSON 文本。
        quote_index: 引号位置。

    Returns:
        bool: 疑似结束引号返回 True。
    """
    index = quote_index + 1
    while index < len(text) and text[index].isspace():
        index += 1
    return index >= len(text) or text[index] in {":", ",", "}", "]"}


def _preview(text: str, limit: int = 1200) -> str:
    """截取文本预览（超长时截断并追加省略标记）。

    Args:
        text: 原始文本。
        limit: 最大字符数。

    Returns:
        str: 预览文本。
    """
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...<truncated>"


def _response_format_unsupported(message: str) -> bool:
    """判断错误消息是否表示不支持 response_format 参数。

    Args:
        message: 错误消息。

    Returns:
        bool: 不支持返回 True。
    """
    lowered = message.lower()
    return "response_format" in lowered and any(
        phrase in lowered
        for phrase in (
            "unsupported",
            "not support",
            "not_supported",
            "unknown parameter",
            "unrecognized",
            "extra inputs are not permitted",
            "invalid parameter",
        )
    )


def _empty_response(message: str) -> bool:
    """判断错误消息是否为空响应错误。

    Args:
        message: 错误消息。

    Returns:
        bool: 是空响应错误返回 True。
    """
    return EMPTY_RESPONSE_MESSAGE.lower() in message.lower()


def _project_context_messages(
    user_payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """从普通负载中投影出上下文消息与精简后的负载。

    从 ``conversation_context.messages`` 提取 user/assistant 消息（含图片附件），
    并从负载中移除已作为上下文出现的重复 user_message。

    Args:
        user_payload: 用户负载。

    Returns:
        tuple[list, dict]: (上下文消息列表, 精简后的负载)。
    """
    payload = copy.deepcopy(user_payload)
    context = payload.pop("conversation_context", None)
    if not isinstance(context, dict):
        return [], _drop_empty_values(payload)
    messages = context.get("messages", [])
    if not isinstance(messages, list):
        return [], _drop_empty_values(payload)
    projected: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip()
        content = str(message.get("content") or "").strip()
        images = _normalize_image_parts(message.get("images"))
        if role not in {"user", "assistant"} or (not content and not images):
            continue
        if images and role == "user":
            projected.append(
                {
                    "role": role,
                    "content": [
                        {"type": "text", "text": content or "（用户上传了图片附件）"},
                        *images,
                    ],
                }
            )
        else:
            projected.append({"role": role, "content": content})
    current_user_message = str(payload.get("user_message") or "").strip()
    latest_user_message = next(
        (
            _content_text(message.get("content")).strip()
            for message in reversed(projected)
            if message.get("role") == "user"
        ),
        "",
    )
    if current_user_message and current_user_message == latest_user_message:
        payload.pop("user_message", None)
    return projected, _drop_empty_values(payload)


def _prepare_user_input(
    user_payload: dict[str, Any] | str,
) -> tuple[list[dict[str, Any]], str | list[dict[str, Any]]]:
    """把用户负载预处理为 (上下文消息, 序列化当前输入)。

    字符串负载无上下文；阶段负载走 ``_prepare_stage_user_input``；普通 dict
    走 ``_project_context_messages`` 并序列化为 JSON。

    Args:
        user_payload: 用户负载（dict 或字符串）。

    Returns:
        tuple[list, str | list]: (上下文消息列表, 当前输入)。
    """
    if isinstance(user_payload, str):
        return [], user_payload.strip()
    if isinstance(user_payload.get(STAGE_PROTOCOL_KEY), dict):
        return _prepare_stage_user_input(user_payload)
    context_messages, projected_payload = _project_context_messages(user_payload)
    return context_messages, json.dumps(projected_payload, ensure_ascii=False)


def _prepare_stage_user_input(
    user_payload: dict[str, Any],
) -> tuple[list[dict[str, Any]], str | list[dict[str, Any]]]:
    """把阶段负载预处理为 (上下文消息, 渲染后的当前输入)。

    从上下文投影历史消息与轮次阶段消息，移除与当前输入重复的末尾用户消息，
    并通过 ``render_stage_user_message`` 渲染当前阶段输入。若有图片附件则一并
    返回为内容块列表。

    Args:
        user_payload: 阶段负载。

    Returns:
        tuple[list, str | list]: (上下文消息列表, 渲染输入)。
    """
    context = user_payload.get("conversation_context")
    payload = copy.deepcopy(
        {
            key: value
            for key, value in user_payload.items()
            if key != "conversation_context"
        }
    )
    payload.pop(STAGE_PROTOCOL_KEY, None)
    context_messages = _project_messages_from_context(context)
    user_message = str(payload.pop("user_message", "") or "").strip()
    current_images: list[dict[str, Any]] = []
    for index in range(len(context_messages) - 1, -1, -1):
        message = context_messages[index]
        if message.get("role") != "user":
            continue
        if _content_text(message.get("content")).strip() != user_message:
            break
        content = message.get("content")
        if isinstance(content, list):
            current_images = [
                item
                for item in content
                if isinstance(item, dict) and item.get("type") == "image_url"
            ]
        context_messages.pop(index)
        break

    turn_stage_messages = _project_turn_stage_messages(context)
    context_messages.extend(turn_stage_messages)
    serialized = render_stage_user_message(
        user_payload, include_turn_header=not turn_stage_messages
    )
    if not current_images:
        return context_messages, _CurrentStageText(serialized)
    return context_messages, [
        {"type": "text", "text": serialized},
        *current_images,
    ]


def _project_messages_from_context(context: Any) -> list[dict[str, Any]]:
    """从 conversation_context 投影历史消息（含图片附件规范化）。

    Args:
        context: 对话上下文。

    Returns:
        list[dict[str, Any]]: 投影后的消息列表。
    """
    if not isinstance(context, dict) or not isinstance(context.get("messages"), list):
        return []
    projected: list[dict[str, Any]] = []
    for message in context["messages"]:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip()
        content = str(message.get("content") or "").strip()
        images = _normalize_image_parts(message.get("images"))
        if role not in {"user", "assistant"} or (not content and not images):
            continue
        if images and role == "user":
            projected.append(
                {
                    "role": role,
                    "content": [
                        {"type": "text", "text": content or "（用户上传了图片附件）"},
                        *images,
                    ],
                }
            )
        else:
            projected.append({"role": role, "content": content})
    return projected


def _project_turn_stage_messages(context: Any) -> list[dict[str, Any]]:
    """从上下文投影多阶段轮次消息（标记为内部轮次消息）。

    Args:
        context: 对话上下文。

    Returns:
        list[dict[str, Any]]: 带 ``TURN_STAGE_MESSAGE_MARKER`` 的轮次消息列表。
    """
    if not isinstance(context, dict):
        return []
    messages = context.get(TURN_STAGE_MESSAGES_KEY)
    if not isinstance(messages, list):
        return []
    projected: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip()
        content = message.get("content")
        if role not in {"user", "assistant"} or not _content_text(content).strip():
            continue
        projected.append(
            {
                "role": role,
                "content": content,
                TURN_STAGE_MESSAGE_MARKER: True,
            }
        )
    return projected


def _record_stage_exchange(
    context_payload: dict[str, Any] | str,
    assistant_content: str,
    *,
    request_user_content: Any = None,
) -> None:
    """把一次阶段交互（user 输入 + assistant 输出）追加到上下文的轮次消息。

    仅对阶段负载生效。记录的轮次消息供后续阶段作为上下文复用，实现单轮内
    多阶段（Router→Step→Reflection）的信息传递。

    Args:
        context_payload: 原始负载（非阶段负载则跳过）。
        assistant_content: 本阶段 assistant 输出文本。
        request_user_content: 本阶段请求的 user 内容（缺失时回退渲染）。
    """
    if not isinstance(context_payload, dict):
        return
    if not isinstance(context_payload.get(STAGE_PROTOCOL_KEY), dict):
        return
    context = context_payload.get("conversation_context")
    if not isinstance(context, dict):
        return
    turn_messages = context.setdefault(TURN_STAGE_MESSAGES_KEY, [])
    if not isinstance(turn_messages, list):
        return
    content = str(assistant_content or "").strip()
    if not content:
        return
    user_content = request_user_content
    if not _content_text(user_content).strip():
        user_content = render_stage_user_message(
            context_payload,
            include_turn_header=not _project_turn_stage_messages(context),
        )
    turn_messages.extend(
        [
            {"role": "user", "content": copy.deepcopy(user_content)},
            {"role": "assistant", "content": content},
        ]
    )


def _drop_empty_values(value: Any) -> Any:
    """递归剔除容器中的空值（None/空串/空列表/空字典）。

    Args:
        value: 任意值（dict/list/标量）。

    Returns:
        Any: 剔除空值后的结构；标量原样返回。
    """
    if isinstance(value, dict):
        projected = {
            key: _drop_empty_values(item)
            for key, item in value.items()
        }
        return {
            key: item
            for key, item in projected.items()
            if item is not None and item != "" and item != [] and item != {}
        }
    if isinstance(value, list):
        projected = [_drop_empty_values(item) for item in value]
        return [
            item
            for item in projected
            if item is not None and item != "" and item != [] and item != {}
        ]
    return value


def _normalize_image_parts(value: Any) -> list[dict[str, Any]]:
    """把原始图片附件列表规范化为 OpenAI 风格的 image_url 内容块。

    仅保留含有效 url 的项，可选保留 detail 字段。

    Args:
        value: 原始图片附件列表。

    Returns:
        list[dict[str, Any]]: 规范化后的 image_url 内容块列表。
    """
    if not isinstance(value, list):
        return []
    parts: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "image_url" and isinstance(item.get("image_url"), dict):
            url = str(item["image_url"].get("url") or "").strip()
            if not url:
                continue
            image_url: dict[str, Any] = {"url": url}
            detail = str(item["image_url"].get("detail") or "").strip()
            if detail:
                image_url["detail"] = detail
            parts.append({"type": "image_url", "image_url": image_url})
    return parts
