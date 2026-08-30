"""认证模块 — JWT Token + 密码哈希.

单用户模式：在配置中设置用户名和密码，登录后颁发 JWT token。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from base64 import b64decode, b64encode
from pathlib import Path
from typing import Any

# JWT 密钥路径：延迟解析，避免模块导入期 import scout.config 触发
# config → manager → secret → ... 的循环导入。测试可直接 monkeypatch 覆盖。
SECRET_PATH: Path | None = None
TOKEN_EXPIRY = 86400 * 7  # 7 天


def _jwt_secret_path() -> Path:
    """数据目录下的 JWT 密钥路径（运行时解析，避免循环导入）."""
    from scout.config.paths import DATA_DIR

    return DATA_DIR / "jwt_secret"

# PBKDF2 迭代次数 — 对抗离线爆破（登录频率低，200k 兼顾性能与安全）
_PBKDF2_ITERATIONS = 200_000
_PBKDF2_PREFIX = "pbkdf2$"


def _get_secret() -> bytes:
    """获取或生成 JWT 密钥."""
    secret_path = SECRET_PATH if SECRET_PATH is not None else _jwt_secret_path()
    if secret_path.exists():
        return secret_path.read_bytes()
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_bytes(32)
    secret_path.write_bytes(secret)
    os.chmod(secret_path, 0o600)
    return secret


def rotate_secret() -> None:
    """轮换 JWT 密钥 — 使所有已签发 token 立即失效.

    在修改密码后调用，吊销旧会话（旧 token 签名校验将失败）。
    注意：当前进程所有已登录会话都会因此需要重新登录。
    """
    secret_path = SECRET_PATH if SECRET_PATH is not None else _jwt_secret_path()
    if secret_path.exists():
        secret_path.unlink()
    _get_secret()


def _b64url_encode(data: bytes) -> str:
    """Base64 URL-safe 编码（无填充）."""
    return b64encode(data).decode().rstrip("=").replace("+", "-").replace("/", "_")


def _b64url_decode(s: str) -> bytes:
    """Base64 URL-safe 解码."""
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (4 - len(s) % 4)
    return b64decode(s)


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    """哈希密码 — 返回 (hash, salt).

    使用 PBKDF2-HMAC-SHA256（200k 迭代）以对抗离线爆破。
    hash 格式: pbkdf2$<iterations>$<hex>
    """
    if salt is None:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ITERATIONS)
    return f"{_PBKDF2_PREFIX}{_PBKDF2_ITERATIONS}${dk.hex()}", salt


def verify_password(password: str, stored_hash: str, salt: str) -> bool:
    """验证密码 — 兼容旧版 sha256(salt+password) 格式."""
    if stored_hash.startswith(_PBKDF2_PREFIX):
        try:
            _, iters_s, dk_hex = stored_hash.split("$")
            iters = int(iters_s)
            dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iters)
            return hmac.compare_digest(dk.hex(), dk_hex)
        except (ValueError, AttributeError):
            return False
    # 旧格式（无前缀）: sha256(salt + password)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return hmac.compare_digest(h, stored_hash)


def needs_rehash(stored_hash: str) -> bool:
    """是否为旧版 sha256 格式（需要迁移为 PBKDF2）."""
    return not stored_hash.startswith(_PBKDF2_PREFIX)


def create_token(username: str, expiry: int = TOKEN_EXPIRY) -> str:
    """创建 JWT-like token."""
    secret = _get_secret()
    now = int(time.time())
    payload = {
        "sub": username,
        "iat": now,
        "exp": now + expiry,
    }
    header = {"alg": "HS256", "typ": "JWT"}
    
    h = _b64url_encode(json.dumps(header).encode())
    p = _b64url_encode(json.dumps(payload).encode())
    
    signature = hmac.new(secret, f"{h}.{p}".encode(), hashlib.sha256).digest()
    s = _b64url_encode(signature)
    
    return f"{h}.{p}.{s}"


def verify_token(token: str) -> dict[str, Any] | None:
    """验证 token — 返回 payload 或 None."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        
        secret = _get_secret()
        h, p, s = parts
        
        # 验证签名
        expected_sig = hmac.new(secret, f"{h}.{p}".encode(), hashlib.sha256).digest()
        actual_sig = _b64url_decode(s)
        
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        
        # 解析 payload
        payload = json.loads(_b64url_decode(p))
        
        # 检查过期
        if payload.get("exp", 0) < time.time():
            return None
        
        return payload
    except Exception:
        return None


class AuthManager:
    """认证管理器 — 管理用户凭证."""

    # 凭证路径：延迟解析避免模块导入期循环导入；测试可 monkeypatch 覆盖
    CREDENTIALS_PATH: Path | None = None

    # 登录失败锁定：连续失败 MAX_LOGIN_FAILURES 次后锁定 LOCK_SECONDS 秒
    MAX_LOGIN_FAILURES = 5
    LOCK_SECONDS = 300
    # 类级内存状态（多实例共享）
    _fail_counts: dict[str, int] = {}
    _lock_until: dict[str, float] = {}

    @staticmethod
    def _credentials_path() -> Path:
        """数据目录下的凭证文件路径（运行时解析，避免循环导入）."""
        from scout.config.paths import DATA_DIR

        return DATA_DIR / "credentials.json"

    def _resolve_credentials_path(self) -> Path:
        return (
            self.CREDENTIALS_PATH
            if self.CREDENTIALS_PATH is not None
            else self._credentials_path()
        )

    def __init__(self):
        self._resolve_credentials_path().parent.mkdir(parents=True, exist_ok=True)

    def has_credentials(self) -> bool:
        """是否已设置凭证."""
        return self._resolve_credentials_path().exists()

    def _write_credentials(self, data: dict) -> None:
        """写凭证文件（600 权限，仅当前用户可读）. """
        with open(self._resolve_credentials_path(), "w") as f:
            json.dump(data, f)
        os.chmod(self._resolve_credentials_path(), 0o600)

    def _read_credentials(self) -> dict:
        with open(self._resolve_credentials_path()) as f:
            return json.load(f)

    def set_credentials(self, username: str, password: str) -> None:
        """设置用户凭证."""
        h, salt = hash_password(password)
        self._write_credentials({
            "username": username,
            "password_hash": h,
            "password_salt": salt,
        })

    def _migrate_hash_if_needed(self, password: str) -> None:
        """旧版 sha256 哈希验证成功后自动迁移为 PBKDF2."""
        if not self.has_credentials():
            return
        data = self._read_credentials()
        if needs_rehash(data.get("password_hash", "")):
            h, salt = hash_password(password)
            data["password_hash"] = h
            data["password_salt"] = salt
            self._write_credentials(data)

    def is_locked(self, username: str) -> bool:
        """是否处于失败锁定中."""
        until = self._lock_until.get(username, 0)
        if until > time.time():
            return True
        # 锁不存在或已过期：仅当确实存在锁时才清理状态，
        # 避免误清空尚未触发的失败计数。
        if username in self._lock_until:
            self._lock_until.pop(username, None)
            self._fail_counts.pop(username, None)
        return False

    def verify(self, username: str, password: str) -> bool:
        """验证用户名和密码（凭证未设置时返回 False；首次设置由 login 负责引导）."""
        if not self.has_credentials():
            return False
        data = self._read_credentials()
        if username != data["username"]:
            return False
        return verify_password(password, data["password_hash"], data["password_salt"])

    def login(self, username: str, password: str) -> str | None:
        """登录 — 返回 token 或 None."""
        if self.is_locked(username):
            return None
        if not self.has_credentials():
            # 首次登录，保存凭证
            self.set_credentials(username, password)
        if self.verify(username, password):
            self._fail_counts.pop(username, None)
            self._lock_until.pop(username, None)
            # 旧格式哈希自动迁移（验证成功、密码明文在手）
            self._migrate_hash_if_needed(password)
            return create_token(username)
        # 失败计数 → 触发锁定
        self._fail_counts[username] = self._fail_counts.get(username, 0) + 1
        if self._fail_counts[username] >= self.MAX_LOGIN_FAILURES:
            self._lock_until[username] = time.time() + self.LOCK_SECONDS
            self._fail_counts[username] = 0
        return None

    def get_username(self) -> str:
        """获取已保存的用户名."""
        if not self.has_credentials():
            return ""
        return self._read_credentials().get("username", "")

    def change_password(self, old_password: str, new_password: str) -> bool:
        """修改密码."""
        if not self.has_credentials():
            return False
        data = self._read_credentials()
        if not verify_password(old_password, data["password_hash"], data["password_salt"]):
            return False
        h, salt = hash_password(new_password)
        data["password_hash"] = h
        data["password_salt"] = salt
        self._write_credentials(data)
        # 改密后轮换 JWT 密钥，吊销所有已签发 token（旧会话需重新登录）；
        # 轮换失败（如权限问题）不应阻塞改密本身。
        try:
            rotate_secret()
        except Exception:
            pass
        return True
