"""内部服务间认证模块。

本模块为内部微服务之间的 API 调用提供轻量级认证机制，通过共享密钥
生成的 HMAC 令牌验证请求来源，防止外部未授权调用。

核心概念
---------
- **内部令牌**：使用全局 ``app_secret`` 对固定 scope 标识做 HMAC-SHA256 签名，
  生成确定性令牌。内部服务共享同一 ``app_secret`` 即可通过校验。
- **请求头传递**：令牌通过 ``X-UltraRAG-Internal-Token`` 请求头传递，
  服务端用恒定时间比较验证。

与其他模块的关系
-----------------
- 依赖 :mod:`app.config` 获取 ``app_secret``；
- 被 :mod:`app.api.mock` 等仅供内部调用的路由模块作为 FastAPI 依赖使用。
"""

from __future__ import annotations

import hashlib
import hmac

from fastapi import Header, HTTPException

from app.config import get_settings

# 内部服务认证专用请求头名称
INTERNAL_SERVICE_HEADER = "X-UltraRAG-Internal-Token"
# HMAC 签名的 scope 标识，用于区分不同用途的内部令牌
_INTERNAL_SERVICE_SCOPE = b"ultrarag-internal-mock-api-v1"


def internal_service_token() -> str:
    """生成当前配置下的内部服务认证令牌。

    令牌是确定性的：只要 ``app_secret`` 不变，生成的令牌始终相同。
    内部服务可调用本函数生成令牌放入请求头。

    Returns:
        HMAC-SHA256 十六进制签名字符串。
    """
    secret = get_settings().app_secret.encode("utf-8")
    return hmac.new(secret, _INTERNAL_SERVICE_SCOPE, hashlib.sha256).hexdigest()


def require_internal_service(
    token: str | None = Header(default=None, alias=INTERNAL_SERVICE_HEADER),
) -> None:
    """FastAPI 依赖：校验内部服务认证令牌。

    从请求头 ``X-UltraRAG-Internal-Token`` 中提取令牌，与本地计算的期望令牌
    做恒定时间比较，不匹配则拒绝访问。

    Args:
        token: 请求头中携带的内部令牌（自动注入，可能为 ``None``）。

    Raises:
        HTTPException: 令牌缺失或不匹配时抛出 401。
    """
    if token is None or not hmac.compare_digest(token, internal_service_token()):
        raise HTTPException(status_code=401, detail="Internal service authentication required")
