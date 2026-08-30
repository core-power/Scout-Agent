"""敏感配置加密原语测试 — scout/security/secret.py."""

import os
import stat
import sys

import pytest

import scout.security.secret as secret


@pytest.fixture(autouse=True)
def isolated_key(tmp_path, monkeypatch):
    """隔离密钥文件，避免污染真实 $SCOUT_DATA_DIR/secret_key."""
    monkeypatch.setattr(secret, "SECRET_PATH", tmp_path / "secret_key")
    yield tmp_path


class TestEncryptRoundtrip:
    """加密 → 解密往返."""

    @pytest.mark.unit
    def test_roundtrip(self):
        plain = "sk-abcdef1234567890"
        encrypted = secret.encrypt_secret(plain)
        assert secret.decrypt_secret(encrypted) == plain

    @pytest.mark.unit
    def test_roundtrip_unicode(self):
        plain = "密钥中\u4e2d\u6587-`~!@#$%^&*()_+"
        encrypted = secret.encrypt_secret(plain)
        assert secret.decrypt_secret(encrypted) == plain

    @pytest.mark.unit
    def test_empty_value(self):
        assert secret.encrypt_secret("") == ""
        assert secret.decrypt_secret("") == ""
        assert secret.decrypt_secret(None) == ""


class TestCiphertextFormat:
    """密文格式与可读性."""

    @pytest.mark.unit
    def test_prefix_and_no_plaintext(self):
        plain = "sk-very-secret-key"
        encrypted = secret.encrypt_secret(plain)
        assert encrypted.startswith(secret.PREFIX)
        assert plain not in encrypted

    @pytest.mark.unit
    def test_encrypt_is_deterministic_shape(self):
        """Fernet 密文每次不同（带时间戳），但均可解密."""
        plain = "sk-test"
        e1 = secret.encrypt_secret(plain)
        e2 = secret.encrypt_secret(plain)
        assert e1 != e2
        assert secret.decrypt_secret(e1) == secret.decrypt_secret(e2) == plain

    @pytest.mark.unit
    def test_is_encrypted(self):
        assert secret.is_encrypted(secret.encrypt_secret("x"))
        assert not secret.is_encrypted("plaintext")
        assert not secret.is_encrypted("")
        assert not secret.is_encrypted(None)


class TestPlaintextMigration:
    """历史明文兼容."""

    @pytest.mark.unit
    def test_plaintext_passthrough(self):
        """非加密值原样返回（平滑迁移路径）."""
        assert secret.decrypt_secret("sk-plain-legacy") == "sk-plain-legacy"


class TestKeyHandling:
    """主密钥生成与权限."""

    @pytest.mark.unit
    def test_key_auto_generated(self):
        secret.encrypt_secret("x")
        assert secret.SECRET_PATH.exists()

    @pytest.mark.unit
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX 权限语义")
    def test_key_permission_600(self):
        secret.encrypt_secret("x")
        mode = stat.S_IMODE(os.stat(secret.SECRET_PATH).st_mode)
        assert mode == 0o600

    @pytest.mark.unit
    def test_key_reused_across_calls(self):
        """同一密钥文件复用，不重复生成."""
        secret.encrypt_secret("a")
        first = secret.SECRET_PATH.read_bytes()
        secret.encrypt_secret("b")
        assert secret.SECRET_PATH.read_bytes() == first


class TestDecryptFailure:
    """解密失败（密钥变化/数据损坏）应返回空串而非崩溃."""

    @pytest.mark.unit
    def test_decrypt_with_stale_key_returns_empty(self, monkeypatch):
        encrypted = secret.encrypt_secret("sk-original")
        # 换一个密钥文件后，旧密文解不开
        monkeypatch.setattr(secret, "SECRET_PATH", secret.SECRET_PATH.parent / "secret_key_new")
        assert secret.decrypt_secret(encrypted) == ""

    @pytest.mark.unit
    def test_decrypt_corrupted_token_returns_empty(self):
        assert secret.decrypt_secret(secret.PREFIX + "not-a-valid-token") == ""
