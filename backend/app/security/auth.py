"""认证与令牌管理模块。

本模块负责用户身份认证的全链路，包括：
  - 密码的哈希与校验（PBKDF2-SHA256）；
  - 自定义无状态访问令牌的签发与解码（类 JWT 结构：payload.signature）；
  - FastAPI 依赖注入组件，用于在请求中提取并验证当前用户及其租户归属。

核心概念
---------
- **租户隔离 (tenant_id)**：每个用户隶属于一个租户，令牌中携带 tenant_id，
  所有后续操作均需校验请求的 tenant_id 与令牌中的 tenant_id 一致。
- **令牌格式**：``base64(payload).base64(HMAC-SHA256-signature)``，签名密钥
  取自全局配置 ``Settings.app_secret``，无需第三方库即可完成签发/校验。

与其他模块的关系
-----------------
- 依赖 :mod:`app.config` 获取签名密钥 ``app_secret``；
- 依赖 :mod:`app.db` 获取数据库会话和用户模型；
- 被 :mod:`app.security.permissions` 等权限模块复用以完成细粒度鉴权。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

from fastapi import Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session

from app.config import get_settings
from app.db import get_session
from app.db.models import User

# 访问令牌的有效期：14 天（秒）
TOKEN_TTL_SECONDS = 60 * 60 * 24 * 14
# HTTP Bearer 安全方案，auto_error=False 表示缺少 Authorization 头时不自动报错，
# 而是由 get_current_user 自行处理，便于返回更明确的错误信息。
security = HTTPBearer(auto_error=False)


def hash_password(password: str) -> str:
    """使用 PBKDF2-SHA256 对明文密码进行加盐哈希。

    每次调用都会生成随机 salt，因此同一密码的哈希结果各不相同。

    Args:
        password: 用户输入的明文密码。

    Returns:
        形如 ``pbkdf2_sha256$<salt_hex>$<base64_digest>`` 的哈希字符串，
        可直接持久化到数据库。
    """
    salt = os.urandom(16).hex()  # 生成 16 字节随机盐，转为十六进制字符串
    # PBKDF2 迭代 120 000 次，抵御暴力破解
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    return f"pbkdf2_sha256${salt}${base64.urlsafe_b64encode(digest).decode('utf-8')}"


def verify_password(password: str, stored_hash: str) -> bool:
    """校验明文密码是否与存储的哈希匹配。

    使用恒定时间比较（``hmac.compare_digest``）防止时序攻击。

    Args:
        password: 用户输入的明文密码。
        stored_hash: 数据库中存储的 ``hash_password`` 返回值。

    Returns:
        匹配返回 ``True``，否则返回 ``False``。
    """
    try:
        _algo, salt, _digest = stored_hash.split("$", 2)  # 拆出 salt 用于重新计算
    except ValueError:
        # 格式不合法（字段不足三段），直接判定不匹配
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 120_000)
    candidate = f"pbkdf2_sha256${salt}${base64.urlsafe_b64encode(digest).decode('utf-8')}"
    # 恒定时间比较，避免通过响应时间推断正确性
    return hmac.compare_digest(candidate, stored_hash)


def create_access_token(user: User) -> str:
    """为指定用户签发访问令牌。

    令牌结构为 ``<base64(payload)>.<base64(HMAC-SHA256签名)>``，
    服务端无需持久化令牌，校验时重新计算签名即可。

    Args:
        user: 已通过认证的 :class:`~app.db.models.User` 实例。

    Returns:
        可放入 ``Authorization: Bearer <token>`` 头的令牌字符串。
    """
    payload = {
        "tenant_id": user.tenant_id,
        "user_id": user.id,
        "username": user.username,
        "exp": int(time.time()) + TOKEN_TTL_SECONDS,  # 过期时间戳
    }
    # 紧凑 JSON 编码后做 base64，去除 padding 以缩短令牌长度
    body = _b64(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    signature = _sign(body)  # 对 body 做 HMAC 签名
    return f"{body}.{signature}"


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_session),
) -> User:
    """FastAPI 依赖：从请求中提取并验证当前登录用户。

    数据流转：``Authorization`` 头 → 解码令牌 → 校验用户存在且租户匹配 → 返回 User。

    Args:
        credentials: FastAPI 自动注入的 Bearer 凭证（可能为 ``None``）。
        db: 自动注入的数据库会话。

    Returns:
        当前已认证的 :class:`~app.db.models.User` 实例。

    Raises:
        HTTPException: 缺少凭证（401）、令牌无效（401）、用户不存在或租户不匹配（401）。
    """
    if not credentials:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = _decode_token(credentials.credentials)  # 解码并校验签名 + 过期
    user = db.get(User, payload.get("user_id", ""))
    if not user or user.tenant_id != payload.get("tenant_id"):
        # 防御性校验：即使令牌伪造了 user_id，也要确认租户归属一致
        raise HTTPException(status_code=401, detail="Invalid user token")
    return user


def ensure_current_user_tenant(tenant_id: str, current_user: User) -> None:
    """确保请求路径中的 tenant_id 与当前用户所属租户一致。

    用于在业务逻辑中做租户隔离校验，防止越权访问其他租户的数据。

    Args:
        tenant_id: 请求路径或查询参数中携带的租户 ID。
        current_user: 当前已认证的用户。

    Raises:
        HTTPException: 用户未认证（401）或租户不匹配（403）。
    """
    if not isinstance(current_user, User):
        raise HTTPException(status_code=401, detail="Not authenticated")
    if tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="Tenant mismatch")


def require_current_tenant(
    tenant_id: str = Query(...),
    current_user: User = Depends(get_current_user),
) -> User:
    """FastAPI 依赖：要求当前用户属于指定租户。

    组合 :func:`get_current_user` 与 :func:`ensure_current_user_tenant`，
    适用于仅需租户级身份验证（不需管理员权限）的路由。

    Args:
        tenant_id: 查询参数 ``tenant_id``（自动注入）。
        current_user: 通过 :func:`get_current_user` 注入的当前用户。

    Returns:
        通过校验的当前用户。

    Raises:
        HTTPException: 身份或租户校验失败时抛出（由内部调用传递）。
    """
    ensure_current_user_tenant(tenant_id, current_user)
    return current_user


def _decode_token(token: str) -> dict[str, Any]:
    """解码并验证访问令牌，返回 payload 字典。

    校验流程：拆分 body/signature → HMAC 签名比对 → base64 解码 JSON → 过期检查。

    Args:
        token: ``create_access_token`` 签发的令牌字符串。

    Returns:
        包含 ``tenant_id``、``user_id``、``username``、``exp`` 的 payload 字典。

    Raises:
        HTTPException: 令牌格式错误（401）、签名无效（401）、payload 损坏（401）、令牌过期（401）。
    """
    try:
        body, signature = token.split(".", 1)  # 拆分为 payload 部分和签名部分
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc
    # 恒定时间比较签名，防止时序攻击
    if not hmac.compare_digest(_sign(body), signature):
        raise HTTPException(status_code=401, detail="Invalid token signature")
    try:
        payload = json.loads(base64.urlsafe_b64decode(_pad_b64(body)).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid token payload") from exc
    if int(payload.get("exp", 0)) < int(time.time()):
        # 过期时间戳已过
        raise HTTPException(status_code=401, detail="Token expired")
    return payload


def _sign(body: str) -> str:
    """使用全局 ``app_secret`` 对令牌 body 做 HMAC-SHA256 签名。

    Args:
        body: 已 base64 编码的令牌 payload 字符串。

    Returns:
        base64 编码后的签名字符串（已去除 padding）。
    """
    secret = get_settings().app_secret.encode("utf-8")
    return _b64(hmac.new(secret, body.encode("utf-8"), hashlib.sha256).digest())


def _b64(value: bytes) -> str:
    """对字节数据做 URL-safe base64 编码并去除 ``=`` padding。

    Args:
        value: 原始字节数据。

    Returns:
        无 padding 的 base64 字符串。
    """
    return base64.urlsafe_b64encode(value).decode("utf-8").rstrip("=")


def _pad_b64(value: str) -> bytes:
    """为去除了 padding 的 base64 字符串补齐 ``=``，以便正确解码。

    Args:
        value: 无 padding 的 base64 字符串。

    Returns:
        补齐 padding 后的字节数据，可直接传给 ``base64.urlsafe_b64decode``。
    """
    return (value + "=" * (-len(value) % 4)).encode("utf-8")
