"""模型 API 协议定义与校验工具。

本模块是 LLM 接入层的协议中枢，定义了平台支持的三种模型 API 协议枚举
（``ModelApiProtocol``），并提供一组与协议、配置指纹、base_url 规范化相关的
纯函数工具：

- ``ModelApiProtocol``：OpenAI / Anthropic / Gemini 三种协议的枚举；
- ``resolve_api_protocol``：从 provider/api_protocol 解析出最终协议；
- ``normalize_chat_protocol_options`` / ``current_protocol_options``：协议级
  选项的校验与按协议取值；
- ``model_config_fingerprint``：基于配置关键字段生成稳定指纹，用于检测配置
  变更以触发模型可信校验；
- ``_normalize_base_url`` / ``validate_model_base_url``：base_url 的归一化
  与校验（统一大小写、剥离默认端口、禁止携带凭据）。

这些工具被 ``model_config_resolver.py``（配置解析）、``client.py``
（运行时调用）以及校验流水线共同复用。
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from fastapi import HTTPException


class ModelApiProtocol(StrEnum):
    """平台支持的模型 API 协议枚举。

    取值作为 ``model_configs.api_protocol`` 的存储值，同时驱动
    ``protocol_drivers.py`` 中具体驱动实例的选择。
    """

    OPENAI_CHAT_COMPLETIONS = "openai_chat_completions"
    ANTHROPIC_MESSAGES = "anthropic_messages"
    GEMINI_GENERATE_CONTENT = "gemini_generate_content"


# 旧版 provider 标识：协议化之前所有模型统一标记为 openai_compatible，
# 在 resolve_api_protocol 中映射为 OPENAI_CHAT_COMPLETIONS 协议。
LEGACY_OPENAI_PROVIDER = "openai_compatible"


def available_model_protocols() -> list[str]:
    """返回当前平台支持的全部协议值列表。

    Returns:
        list[str]: 协议字符串列表，如 ``["openai_chat_completions", ...]``。
    """
    return [protocol.value for protocol in ModelApiProtocol]


def resolve_api_protocol(api_protocol: str | None, provider: str | None) -> ModelApiProtocol:
    """根据显式协议与旧版 provider 解析出最终协议。

    解析规则：
    - 若提供 ``provider``，仅接受 ``LEGACY_OPENAI_PROVIDER``，否则报错；合法时
      映射为 OPENAI_CHAT_COMPLETIONS；
    - 若提供 ``api_protocol``，必须能转为枚举值，否则报错；
    - 当 provider 映射结果与显式协议冲突时报错；
    - 优先返回显式协议，其次 provider 映射，最后回退到 OPENAI_CHAT_COMPLETIONS。

    Args:
        api_protocol: 显式指定的协议值（可为 None）。
        provider: 旧版 provider 标识（可为 None）。

    Returns:
        ModelApiProtocol: 解析后的协议枚举。

    Raises:
        HTTPException: provider 不支持、协议值非法或二者冲突时（422）。
    """
    mapped_provider = None
    if provider is not None:
        if provider != LEGACY_OPENAI_PROVIDER:
            raise HTTPException(status_code=422, detail="MODEL_PROVIDER_UNSUPPORTED")
        mapped_provider = ModelApiProtocol.OPENAI_CHAT_COMPLETIONS
    try:
        explicit = ModelApiProtocol(api_protocol) if api_protocol is not None else None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="MODEL_PROTOCOL_UNSUPPORTED") from exc
    if explicit is not None and mapped_provider is not None and explicit != mapped_provider:
        raise HTTPException(status_code=422, detail="MODEL_PROTOCOL_CONFLICT")
    return explicit or mapped_provider or ModelApiProtocol.OPENAI_CHAT_COMPLETIONS


def normalize_chat_protocol_options(value: Any) -> dict[str, Any]:
    """校验并规范化 chat 协议的选项（目前仅支持 thinking 配置）。

    合法结构为 ``{"thinking": {"type": "enabled"|"disabled", "clear_thinking"?: bool}}``。
    其余键或非法取值会触发 422。

    Args:
        value: 待校验的原始选项值。

    Returns:
        dict[str, Any]: 规范化后的选项字典；输入为 None 或无 thinking 时返回空字典。

    Raises:
        HTTPException: 选项结构非法时（422）。
    """
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {"thinking"}:
        raise HTTPException(status_code=422, detail="MODEL_PROTOCOL_OPTIONS_INVALID")
    thinking = value.get("thinking")
    if thinking is None:
        return {}
    if not isinstance(thinking, dict) or set(thinking) - {"type", "clear_thinking"}:
        raise HTTPException(status_code=422, detail="MODEL_PROTOCOL_OPTIONS_INVALID")
    if thinking.get("type") not in {"enabled", "disabled"}:
        raise HTTPException(status_code=422, detail="MODEL_PROTOCOL_OPTIONS_INVALID")
    if "clear_thinking" in thinking and not isinstance(thinking["clear_thinking"], bool):
        raise HTTPException(status_code=422, detail="MODEL_PROTOCOL_OPTIONS_INVALID")
    return {"thinking": dict(thinking)}


def current_protocol_options(protocol_options: Any, protocol: ModelApiProtocol) -> dict[str, Any]:
    """从按协议分桶的选项字典中取出指定协议对应的选项。

    ``protocol_options_json`` 结构为 ``{协议值: 选项字典}``，本函数返回其中
    ``protocol`` 对应的那一份。

    Args:
        protocol_options: 按协议分桶的选项（dict 或其他）。
        protocol: 目标协议枚举。

    Returns:
        dict[str, Any]: 目标协议的选项字典；不存在或类型不符时返回空字典。
    """
    if not isinstance(protocol_options, dict):
        return {}
    value = protocol_options.get(protocol.value, {})
    return dict(value) if isinstance(value, dict) else {}


def model_config_fingerprint(
    *,
    api_protocol: str,
    base_url: str | None,
    model: str,
    key_revision: int,
    protocol_options: dict[str, Any],
    security_revision: int,
) -> str:
    """计算模型配置的稳定指纹（SHA256）。

    将影响调用安全与行为的字段序列化为规范化 JSON（排序键、紧凑分隔符）后取哈希，
    用于检测配置是否发生变化——一旦指纹与已校验指纹不一致，即视为配置变更，
    需要重新进行可信校验。

    Args:
        api_protocol: API 协议值。
        base_url: 模型端点（会先归一化）。
        model: 模型名。
        key_revision: 密钥版本号。
        protocol_options: 协议级选项。
        security_revision: 安全校验版本号。

    Returns:
        str: 配置指纹的十六进制摘要。
    """
    payload = {
        "fingerprint_version": 1,
        "api_protocol": api_protocol,
        "base_url": _normalize_base_url(base_url),
        "model": model,
        "key_revision": key_revision,
        "protocol_options": protocol_options,
        "security_revision": security_revision,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_base_url(value: str | None) -> str | None:
    """归一化模型 base_url。

    处理包括：NFC 规范化、scheme/hostname 转小写、剥离默认端口（https→443、
    http→80）、移除尾部斜杠与 fragment、禁止携带用户名密码。

    Args:
        value: 原始 base_url。

    Returns:
        str | None: 归一化后的 url；None 输入返回 None，空串返回空串。

    Raises:
        HTTPException: scheme 非 http/https、缺 hostname、含凭据或端口非法时（422）。
    """
    if value is None:
        return None
    raw = unicodedata.normalize("NFC", value.strip())
    if not raw:
        return ""
    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if scheme not in {"http", "https"} or not hostname or parsed.username or parsed.password:
        raise HTTPException(status_code=422, detail="MODEL_BASE_URL_INVALID")
    try:
        port = parsed.port
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="MODEL_BASE_URL_INVALID") from exc
    # 默认端口（https→443、http→80）省略不写，保证等价 url 指纹一致。
    if port == (443 if scheme == "https" else 80):
        port = None
    netloc = f"{hostname}:{port}" if port is not None else hostname
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def validate_model_base_url(value: str | None) -> None:
    """校验 base_url 合法性（仅做归一化，不保留结果）。

    Args:
        value: 待校验的 base_url。

    Raises:
        HTTPException: url 非法时（422）。
    """
    _normalize_base_url(value)
