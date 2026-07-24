"""敏感数据加密模块。

本模块提供对敏感信息（如 API Key、第三方密钥等）的对称加密/解密/脱敏能力，
使用 Fernet 对称加密算法（AES-128-CBC + HMAC-SHA256），密钥从全局配置
``Settings.app_secret`` 派生。

核心概念
---------
- **密钥派生**：将 ``app_secret`` 做 SHA-256 哈希后做 base64 编码，得到
  Fernet 所需的 32 字节 URL-safe base64 密钥。
- **可逆加密**：``encrypt_secret`` / ``decrypt_secret`` 成对使用，加密结果
  可安全存储到数据库，需要时解密还原明文。
- **脱敏展示**：``mask_secret`` 用于在日志或 API 响应中隐藏敏感信息，
  仅保留首尾少量字符。

与其他模块的关系
-----------------
- 依赖 :mod:`app.config` 获取 ``app_secret`` 作为加密主密钥；
- 被 :mod:`app.api.model_configs` 等需要存储/展示敏感信息的路由模块调用。
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


def _fernet() -> Fernet:
    """根据全局 ``app_secret`` 构造 Fernet 加密器实例。

    密钥派生过程：``app_secret`` → SHA-256 哈希 → URL-safe base64 编码 → Fernet 密钥。

    Returns:
        可用于加密/解密的 :class:`Fernet` 实例。
    """
    secret = get_settings().app_secret.encode("utf-8")
    # SHA-256 产生 32 字节，正好满足 Fernet 对密钥长度的要求
    key = base64.urlsafe_b64encode(hashlib.sha256(secret).digest())
    return Fernet(key)


def encrypt_secret(value: str) -> str:
    """加密敏感字符串。

    Args:
        value: 待加密的明文字符串（如 API Key）。

    Returns:
        Fernet 加密后的密文字符串（UTF-8 编码），可安全持久化到数据库。
    """
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: str) -> str:
    """解密敏感字符串。

    Args:
        value: Fernet 密文字符串。

    Returns:
        解密后的明文字符串；若 ``value`` 为空则返回空字符串。

    Raises:
        ValueError: 密钥不匹配或密文损坏，无法解密。
    """
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        # 密钥变更或密文被篡改时触发
        raise ValueError("Secret cannot be decrypted with current APP_SECRET") from exc


def mask_secret(value: str) -> str:
    """对敏感字符串进行脱敏处理，用于日志或 API 响应中安全展示。

    规则：
      - 空字符串原样返回；
      - 长度不超过 8 的字符串全部替换为 ``****``；
      - 较长的字符串保留前 3 位和后 4 位，中间用 ``-****`` 替代。

    Args:
        value: 待脱敏的原始字符串。

    Returns:
        脱敏后的字符串。
    """
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:3]}-****{value[-4:]}"
