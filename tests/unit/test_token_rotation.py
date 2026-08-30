"""JWT 密钥轮换测试 — 改密后旧 token 吊销.

覆盖 scout/security/auth.py 的 rotate_secret 与 change_password 联动。
"""
import pytest

import scout.security.auth as auth_mod


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_mod, "SECRET_PATH", tmp_path / "jwt_secret")
    monkeypatch.setattr(auth_mod.AuthManager, "CREDENTIALS_PATH", tmp_path / "credentials.json")


class TestTokenRotation:
    def test_rotate_invalidates_old_token(self):
        token = auth_mod.create_token("alice")
        assert auth_mod.verify_token(token) is not None
        auth_mod.rotate_secret()
        assert auth_mod.verify_token(token) is None

    def test_change_password_revokes_tokens(self):
        mgr = auth_mod.AuthManager()
        mgr.set_credentials("alice", "old")
        token = mgr.login("alice", "old")
        assert auth_mod.verify_token(token) is not None
        assert mgr.change_password("old", "new") is True
        # 旧 token 已吊销
        assert auth_mod.verify_token(token) is None
        # 新密码可登录，旧密码不可
        assert mgr.login("alice", "new")
        assert mgr.login("alice", "old") is None

    def test_change_password_wrong_old_does_not_rotate(self):
        mgr = auth_mod.AuthManager()
        mgr.set_credentials("alice", "old")
        token = mgr.login("alice", "old")
        assert mgr.change_password("wrong", "new") is False
        # 旧密码验证失败不轮换，token 仍有效
        assert auth_mod.verify_token(token) is not None
