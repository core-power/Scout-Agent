"""敏感配置加密 — API Key / Web 密码等敏感字段的加解密.

设计 (2026-08-18):
- 使用 Fernet（对称加密，cryptography 库）对敏感配置字段加密存储。
- 主密钥保存在 $SCOUT_DATA_DIR/secret_key，权限 600，仅当前用户可读。这样即使
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

# 密钥路径：延迟解析，避免模块导入期 import scout.config 触发
# config → manager → secret 的循环导入。测试可直接 monkeypatch 覆盖。
SECRET_PATH: Path | None = None


def _default_secret_path() -> Path:
    """数据目录下的密钥路径（运行时解析，避免循环导入）."""
    from scout.config.paths import DATA_DIR

    return DATA_DIR / "secret_key"


def _get_key() -> bytes:
    """获取（或生成）主密钥。密钥文件权限设为 600。"""
    secret_path = SECRET_PATH if SECRET_PATH is not None else _default_secret_path()
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    if secret_path.exists():
        key = secret_path.read_bytes()
        if len(key) >= 32:
            return key
    key = Fernet.generate_key()
    # 写入并收紧权限，避免其他用户/进程读取
    with open(secret_path, "wb") as f:
        f.write(key)
    try:
        os.chmod(secret_path, 0o600)
    except OSError:
        pass
    logger.info("[secret] 已生成新的加密密钥: %s", secret_path)
    return key


def encrypt_secret(value: str) -> str:
    """加密一个字符串，返回带前缀的密文.

    加密失败时抛 RuntimeError，绝不回退为明文落盘（明文泄露即失去加密意义）。
    """
    if not value:
        return ""
    try:
        fernet = Fernet(_get_key())
        token = fernet.encrypt(value.encode("utf-8"))
        return PREFIX + token.decode("ascii")
    except Exception as e:
        logger.error("[secret] 加密失败（拒绝明文回退）: %s", e)
        raise RuntimeError(f"敏感字段加密失败: {e}") from e


def decrypt_secret(value: str) -> str:
    """解密一个带前缀的密文；若传入的是明文则原样返回（兼容迁移）."""
    if not value:
        return ""
    if isinstance(value, str) and value.startswith(PREFIX):
        token = value[len(PREFIX):]
        try:
            fernet = Fernet(_get_key())
            return fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except Exception as e:  # noqa: BLE001 - InvalidToken 及其子类均属解密失败
            logger.error("[secret] 解密失败（可能是密钥变化或数据损坏）: %s", e)
            return ""
    # 非加密值（历史明文）原样返回
    return value


def is_encrypted(value: str) -> bool:
    """判断一个值是否已加密."""
    return bool(value) and isinstance(value, str) and value.startswith(PREFIX)


# 需要加密存储的配置字段
SENSITIVE_FIELDS = ("api_key", "web_password")
