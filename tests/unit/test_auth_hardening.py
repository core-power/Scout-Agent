"""认证加固测试 — PBKDF2 哈希 / 旧 sha256 兼容 / 登录失败锁定 / 自动迁移 / 文件权限.

覆盖 scout/security/auth.py 的安全加固行为。
"""
import json
import os
import stat
import time

import pytest

import scout.security.auth as auth_mod


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    """把 JWT 密钥与凭证文件都隔离到临时目录，避免触碰真实 $SCOUT_DATA_DIR."""
    monkeypatch.setattr(auth_mod, "SECRET_PATH", tmp_path / "jwt_secret")
    monkeypatch.setattr(auth_mod.AuthManager, "CREDENTIALS_PATH", tmp_path / "credentials.json")


class TestHashFormat:
    def test_pbkdf2_prefix(self):
        h, salt = auth_mod.hash_password("s3cret")
        assert h.startswith("pbkdf2$")
        assert len(salt) == 32  # 16 bytes hex

    def test_verify_roundtrip(self):
        h, salt = auth_mod.hash_password("p@ss")
        assert auth_mod.verify_password("p@ss", h, salt) is True
        assert auth_mod.verify_password("wrong", h, salt) is False

    def test_verify_legacy_sha256(self):
        # 旧格式：无前缀 sha256(salt + password)
        salt = "aabbccddeeff00112233445566778899"
        old = __import__("hashlib").sha256((salt + "oldpass").encode()).hexdigest()
        assert auth_mod.verify_password("oldpass", old, salt) is True
        assert auth_mod.verify_password("nope", old, salt) is False

    def test_needs_rehash(self):
        assert auth_mod.needs_rehash("pbkdf2$200000$abc") is False
        assert auth_mod.needs_rehash("a" * 64) is True  # 旧 sha256 需要迁移

    def test_verify_malformed_pbkdf2(self):
        # 畸形 pbkdf2 串不应崩溃，也不应误放行
        assert auth_mod.verify_password("x", "pbkdf2$notanint$xx", "salt") is False
        assert auth_mod.verify_password("x", "pbkdf2$200000$", "salt") is False


class TestFilePermissions:
    def test_credentials_0600(self, tmp_path):
        mgr = auth_mod.AuthManager()
        mgr.set_credentials("alice", "pw")
        mode = stat.S_IMODE(os.stat(mgr.CREDENTIALS_PATH).st_mode)
        assert mode == 0o600

    def test_jwt_secret_0600(self):
        auth_mod._get_secret()
        mode = stat.S_IMODE(os.stat(auth_mod.SECRET_PATH).st_mode)
        assert mode == 0o600


class TestLoginLockout:
    def test_first_login_sets_credentials(self):
        mgr = auth_mod.AuthManager()
        token = mgr.login("bob", "pw")
        assert token
        assert mgr.has_credentials()

    def test_5_failures_locks(self):
        mgr = auth_mod.AuthManager()
        mgr.login("bob", "pw")  # 首次设置凭证
        for _ in range(auth_mod.AuthManager.MAX_LOGIN_FAILURES):
            assert mgr.login("bob", "wrong") is None
        assert mgr.is_locked("bob")
        # 锁定期间即使密码正确也拒绝
        assert mgr.login("bob", "pw") is None

    def test_lock_expires(self):
        mgr = auth_mod.AuthManager()
        mgr.login("bob", "pw")
        for _ in range(auth_mod.AuthManager.MAX_LOGIN_FAILURES):
            mgr.login("bob", "wrong")
        assert mgr.is_locked("bob")
        # 模拟锁过期
        mgr._lock_until["bob"] = time.time() - 1
        assert mgr.is_locked("bob") is False
        assert mgr.login("bob", "pw")  # 恢复后可登录

    def test_success_resets_failures(self):
        mgr = auth_mod.AuthManager()
        mgr.login("bob", "pw")
        mgr.login("bob", "wrong")
        assert mgr.login("bob", "pw")
        assert mgr._fail_counts.get("bob", 0) == 0
        assert mgr.is_locked("bob") is False

    def test_wrong_username_counts(self):
        mgr = auth_mod.AuthManager()
        mgr.login("bob", "pw")
        assert mgr.login("alice", "pw") is None  # 错误用户
        assert mgr._fail_counts.get("alice", 0) >= 1


class TestLegacyMigration:
    def test_login_migrates_sha256_to_pbkdf2(self, tmp_path):
        mgr = auth_mod.AuthManager()
        # 手工写入旧格式凭证
        salt = "aabbccddeeff00112233445566778899"
        old_hash = __import__("hashlib").sha256((salt + "legacy").encode()).hexdigest()
        mgr.CREDENTIALS_PATH.write_text(json.dumps({
            "username": "olduser",
            "password_hash": old_hash,
            "password_salt": salt,
        }))
        token = mgr.login("olduser", "legacy")
        assert token
        data = json.loads(mgr.CREDENTIALS_PATH.read_text())
        assert data["password_hash"].startswith("pbkdf2$")
        # 迁移后仍可验证
        assert mgr.verify("olduser", "legacy")
