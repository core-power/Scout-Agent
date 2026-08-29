"""敏感配置加密 — API Key / Web 密码等敏感字段的加解密.

设计 (2026-08-18):
- 使用 Fernet（对称加密，cryptography 库）对敏感配置字段加密存储。
- 主密钥保存在 ~/.scout/secret_key，权限 600，仅当前用户可读。这样即使
  配置文件（config.json）泄露或被大模型通过文件读取工具看到，拿到的也只是
  密文，没有本机主密钥无法解密。
- 密文带 "enc:v1:" 前缀标识，便于识别与兼容性迁移。
- 若密钥文件不存在则自动生成（模式与 security/auth.py 的 jwt_secret 一致）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)

# 密文前缀，用于标识该值是加密过的
PREFIX = "enc:v1:"

SECRET_PATH = Path.home() / ".scout" / "secret_key"


def _get_key() -> bytes:
    """获取（或生成）主密钥。密钥文件权限设为 600。"""
    SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SECRET_PATH.exists():
        key = SECRET_PATH.read_bytes()
        if len(key) >= 32:
            return key
    key = Fernet.generate_key()
    # 写入并收紧权限，避免其他用户/进程读取
    with open(SECRET_PATH, "wb") as f:
        f.write(key)
    try:
        os.chmod(SECRET_PATH, 0o600)
    except OSError:
        pass
    logger.info("[secret] 已生成新的加密密钥: %s", SECRET_PATH)
    return key


def encrypt_secret(value: str) -> str:
    """加密一个字符串，返回带前缀的密文."""
    if not value:
        return ""
    try:
        fernet = Fernet(_get_key())
        token = fernet.encrypt(value.encode("utf-8"))
        return PREFIX + token.decode("ascii")
    except Exception as e:
        logger.error("[secret] 加密失败: %s", e)
        # 加密失败时回退为明文，避免配置不可用（但记录告警）
        return value


def decrypt_secret(value: str) -> str:
    """解密一个带前缀的密文；若传入的是明文则原样返回（兼容迁移）."""
    if not value:
        return ""
    if isinstance(value, str) and value.startswith(PREFIX):
        token = value[len(PREFIX):]
        try:
            fernet = Fernet(_get_key())
            return fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, Exception) as e:
            logger.error("[secret] 解密失败（可能是密钥变化或数据损坏）: %s", e)
            return ""
    # 非加密值（历史明文）原样返回
    return value


def is_encrypted(value: str) -> bool:
    """判断一个值是否已加密."""
    return bool(value) and isinstance(value, str) and value.startswith(PREFIX)


# 需要加密存储的配置字段
SENSITIVE_FIELDS = ("api_key", "web_password")
