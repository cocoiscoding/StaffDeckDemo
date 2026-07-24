"""模型配置解析器：将数据库中的 ``ModelConfig`` 行解析为不可变的运行时快照。

本模块是 LLM 调用前的“安全闸门”。它从数据库读取模型配置，校验其可信状态
（trust_status、指纹一致性），并在通过校验后生成一个冻结的
``ResolvedModelConfig`` 快照供调用方使用。

核心设计：
- 运行时解析（``resolve_model_config_for_runtime``）要求模型已通过可信校验，
  否则抛出 409，避免使用未校验的配置发起调用；
- 校验解析（``resolve_model_config_for_verification``）放宽可信校验，但要求
  当前校验会话（attempt）有效且处于 verifying 状态，防止并发校验互相覆盖；
- 快照中的协议选项与 legacy 参数均通过 ``_freeze`` 转为只读映射，防止调用方
  意外修改缓存配置。

与 ``model_protocols.py``（协议/指纹工具）和 ``client.py``（实际调用）协作。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Literal, Mapping

from fastapi import HTTPException
from sqlmodel import Session

from app.db.models import ModelConfig
from app.llm.model_protocols import (
    ModelApiProtocol,
    current_protocol_options,
    model_config_fingerprint,
)


@dataclass(frozen=True)
class ResolvedModelConfig:
    """模型配置的不可变运行时快照。

    冻结（frozen）的 dataclass，封装一次模型调用所需的全部信息。协议选项与
    legacy 额外参数以只读 ``Mapping`` 形式提供，避免被调用方意外修改。

    Attributes:
        id: 模型配置 id。
        tenant_id: 租户 id。
        api_protocol: 解析后的 API 协议枚举。
        base_url: 模型端点 URL。
        api_key_encrypted: 加密的 API 密钥（调用前需解密）。
        model: 模型名。
        temperature: 采样温度。
        max_output_tokens: 最大输出 token 数。
        protocol_options: 当前协议对应的选项（只读）。
        legacy_extra_body: 兼容旧配置的额外请求体参数（仅运行时、legacy 场景填充）。
        config_revision: 配置版本号。
        security_revision: 安全校验版本号。
        purpose: 用途——运行时调用（runtime）或配置校验（verification）。
        timeout_seconds: 调用超时（秒），可选。
    """

    id: str
    tenant_id: str
    api_protocol: ModelApiProtocol
    base_url: str | None
    api_key_encrypted: str
    model: str
    temperature: float
    max_output_tokens: int
    protocol_options: Mapping[str, Any]
    legacy_extra_body: Mapping[str, Any]
    config_revision: int
    security_revision: int
    purpose: Literal["runtime", "verification"]
    timeout_seconds: float | None = None


def resolve_model_config_for_runtime(
    db: Session, tenant_id: str, config_id: str
) -> ResolvedModelConfig:
    """为运行时调用解析模型配置（强制可信校验）。

    要求模型已启用且通过可信校验：legacy_trusted（旧版兼容）或 verified 且指纹
    匹配；处于迁移窗口的隐式 legacy OpenAI 配置也可放行。未通过则抛出 409，
    要求先完成校验。

    Args:
        db: SQLModel 会话。
        tenant_id: 租户 id。
        config_id: 模型配置 id。

    Returns:
        ResolvedModelConfig: 可用于调用的配置快照。

    Raises:
        HTTPException: 配置不存在/跨租户（404）、已禁用（409）或需校验（409）。
    """
    row = _current_model_config(db, tenant_id, config_id)
    if not row.enabled:
        raise HTTPException(status_code=409, detail="MODEL_CONFIG_DISABLED")
    protocol = _protocol(row)
    if row.trust_status == "legacy_trusted" or _is_implicit_legacy_openai(row, protocol):
        if protocol is not ModelApiProtocol.OPENAI_CHAT_COMPLETIONS:
            raise HTTPException(status_code=409, detail="MODEL_CONFIG_VERIFICATION_REQUIRED")
    elif row.trust_status != "verified" or row.verified_fingerprint != _fingerprint(row):
        raise HTTPException(status_code=409, detail="MODEL_CONFIG_VERIFICATION_REQUIRED")
    return _snapshot(row, protocol, purpose="runtime")


def resolve_model_config_for_verification(
    db: Session, tenant_id: str, config_id: str, attempt_id: str
) -> ResolvedModelConfig:
    """为配置校验流程解析模型配置（放宽可信校验，但校验会话须有效）。

    不强制 trust_status，但要求当前配置正处于指定的校验会话中且状态为
    verifying，防止过期或并发校验会话误用配置。

    Args:
        db: SQLModel 会话。
        tenant_id: 租户 id。
        config_id: 模型配置 id。
        attempt_id: 本次校验会话 id。

    Returns:
        ResolvedModelConfig: 用于发起校验调用的配置快照。

    Raises:
        HTTPException: 配置不存在/跨租户（404）或校验会话已失效（409）。
    """
    row = _current_model_config(db, tenant_id, config_id)
    if (
        row.verification_attempt_id != attempt_id
        or row.verification_attempt_status != "verifying"
    ):
        raise HTTPException(status_code=409, detail="MODEL_VERIFICATION_STALE")
    protocol = _protocol(row)
    return _snapshot(row, protocol, purpose="verification")


def _current_model_config(db: Session, tenant_id: str, config_id: str) -> ModelConfig:
    """读取并刷新当前模型配置行，校验归属。

    Args:
        db: SQLModel 会话。
        tenant_id: 租户 id。
        config_id: 模型配置 id。

    Returns:
        ModelConfig: 已刷新的模型配置 ORM 行。

    Raises:
        HTTPException: 不存在或跨租户时（404）。
    """
    row = db.get(ModelConfig, config_id)
    if row is None or row.tenant_id != tenant_id:
        raise HTTPException(status_code=404, detail="MODEL_CONFIG_NOT_FOUND")
    db.refresh(row)
    return row


def _protocol(row: ModelConfig) -> ModelApiProtocol:
    """将配置行的 api_protocol 字符串转为协议枚举。

    Args:
        row: 模型配置行。

    Returns:
        ModelApiProtocol: 协议枚举。

    Raises:
        HTTPException: 协议值非法时（422）。
    """
    try:
        return ModelApiProtocol(row.api_protocol)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="MODEL_PROTOCOL_UNSUPPORTED") from exc


def _snapshot(
    row: ModelConfig,
    protocol: ModelApiProtocol,
    *,
    purpose: Literal["runtime", "verification"],
) -> ResolvedModelConfig:
    """由 ORM 行构建冻结的配置快照。

    运行时且属 legacy 场景时，会把 ``extra_body_json`` 深拷贝进
    ``legacy_extra_body`` 以兼容旧调用；其余情况该字段为空。协议选项取当前协议
    对应的那一份并冻结。

    Args:
        row: 模型配置行。
        protocol: 解析后的协议枚举。
        purpose: 用途（runtime/verification）。

    Returns:
        ResolvedModelConfig: 冻结的配置快照。
    """
    options = current_protocol_options(row.protocol_options_json, protocol)
    legacy_extra_body: dict[str, Any] = {}
    if purpose == "runtime" and (
        row.trust_status == "legacy_trusted" or _is_implicit_legacy_openai(row, protocol)
    ):
        legacy_extra_body = copy.deepcopy(row.extra_body_json or {})
    return ResolvedModelConfig(
        id=row.id,
        tenant_id=row.tenant_id,
        api_protocol=protocol,
        base_url=row.base_url,
        api_key_encrypted=row.api_key_encrypted,
        model=row.model,
        temperature=row.temperature,
        max_output_tokens=row.max_output_tokens,
        protocol_options=_freeze(options),
        legacy_extra_body=_freeze(legacy_extra_body),
        config_revision=row.config_revision,
        security_revision=row.security_revision,
        purpose=purpose,
        timeout_seconds=None,
    )


def _fingerprint(row: ModelConfig) -> str:
    """计算配置行当前应具有的指纹，用于与已校验指纹比对。

    Args:
        row: 模型配置行。

    Returns:
        str: 配置指纹的十六进制摘要。
    """
    protocol = _protocol(row)
    return model_config_fingerprint(
        api_protocol=row.api_protocol,
        base_url=row.base_url,
        model=row.model,
        key_revision=row.key_revision,
        protocol_options=current_protocol_options(row.protocol_options_json, protocol),
        security_revision=row.security_revision,
    )


def _is_implicit_legacy_openai(row: ModelConfig, protocol: ModelApiProtocol) -> bool:
    """判断配置是否属于协议化迁移窗口内的隐式 legacy OpenAI 行。

    这类行（unverified、无指纹、版本号均为 1、无校验尝试）是协议化之前遗留的
    ORM/种子数据，在迁移窗口内仍允许以 legacy 方式运行，避免历史数据被全部锁死。

    Args:
        row: 模型配置行。
        protocol: 解析后的协议枚举。

    Returns:
        bool: 属于隐式 legacy OpenAI 返回 True。
    """
    """Keep pre-protocol ORM/fixture rows runnable during the migration window."""
    return (
        protocol is ModelApiProtocol.OPENAI_CHAT_COMPLETIONS
        and row.trust_status == "unverified"
        and row.verified_fingerprint is None
        and row.security_revision == 1
        and row.config_revision == 1
        and row.verification_attempt_status in {None, "idle"}
    )


def _freeze(value: dict[str, Any]) -> Mapping[str, Any]:
    """将字典递归冻结为只读 ``MappingProxyType`` 映射。

    Args:
        value: 待冻结的字典。

    Returns:
        Mapping[str, Any]: 只读映射。
    """
    return MappingProxyType({key: _freeze_value(item) for key, item in copy.deepcopy(value).items()})


def _freeze_value(value: Any) -> Any:
    """递归地把容器值转为不可变结构（dict→映射、list→元组）。

    Args:
        value: 任意值。

    Returns:
        Any: 冻结后的不可变值。
    """
    if isinstance(value, dict):
        return _freeze(value)
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    return value


def snapshot_model_config(
    model_config: Any, *, min_output_tokens: int = 0
) -> ResolvedModelConfig:
    """把任意模型配置对象（ORM 行或已有快照）统一转换为快照。

    若传入已是 ``ResolvedModelConfig`` 则直接复用（仅在 max_output_tokens 低于
    下限时提升）；否则按属性鸭子类型组装快照，兼容 ORM 行与旧式配置对象。

    Args:
        model_config: 模型配置对象（ResolvedModelConfig 或 ORM 行）。
        min_output_tokens: 输出 token 下限，低于此值会被提升。

    Returns:
        ResolvedModelConfig: 统一的配置快照。
    """
    if isinstance(model_config, ResolvedModelConfig):
        if model_config.max_output_tokens >= min_output_tokens:
            return model_config
        return ResolvedModelConfig(
            **{
                **model_config.__dict__,
                "max_output_tokens": min_output_tokens,
            }
        )
    protocol = ModelApiProtocol(
        getattr(model_config, "api_protocol", "openai_chat_completions")
    )
    return ResolvedModelConfig(
        id=str(getattr(model_config, "id", "")),
        tenant_id=str(getattr(model_config, "tenant_id", "")),
        api_protocol=protocol,
        purpose=getattr(model_config, "purpose", "runtime"),
        api_key_encrypted=model_config.api_key_encrypted,
        base_url=model_config.base_url,
        model=model_config.model,
        temperature=model_config.temperature,
        max_output_tokens=max(
            int(getattr(model_config, "max_output_tokens", 0) or 0), min_output_tokens
        ),
        protocol_options=_freeze(
            _snapshot_protocol_options(model_config, protocol)
        ),
        legacy_extra_body=_freeze(
            copy.deepcopy(
                getattr(model_config, "legacy_extra_body", {})
                or getattr(model_config, "extra_body_json", {})
            )
        ),
        config_revision=getattr(model_config, "config_revision", 1),
        security_revision=getattr(model_config, "security_revision", 1),
        timeout_seconds=getattr(model_config, "timeout_seconds", None),
    )


def _snapshot_protocol_options(model_config: Any, protocol: ModelApiProtocol) -> dict[str, Any]:
    """从配置对象中提取协议选项，优先使用已解析的 protocol_options 字段。

    Args:
        model_config: 模型配置对象。
        protocol: 目标协议枚举。

    Returns:
        dict[str, Any]: 协议选项字典。
    """
    direct = getattr(model_config, "protocol_options", {})
    if isinstance(direct, dict) and direct:
        return copy.deepcopy(direct)
    return current_protocol_options(
        getattr(model_config, "protocol_options_json", {}), protocol
    )
