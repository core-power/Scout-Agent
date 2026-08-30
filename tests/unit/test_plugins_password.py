"""插件写码密码二次确认测试.

覆盖 scout/plugins/api.py 的 _require_password_confirmation 与 create_plugin 集成。
"""
import pytest
from fastapi import HTTPException

import scout.plugins.api as plugin_api
import scout.security.auth as auth_mod


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(auth_mod, "SECRET_PATH", tmp_path / "jwt_secret")
    monkeypatch.setattr(auth_mod.AuthManager, "CREDENTIALS_PATH", tmp_path / "credentials.json")

    # 与 scout/plugins/api.py 的实现保持一致：
    # 仅当登录认证开启（auth_enabled=True）且已初始化凭证时才校验密码。
    class _Cfg:
        auth_enabled = True

    monkeypatch.setattr(
        "scout.config.manager.ConfigManager.load",
        lambda self: _Cfg(),
    )


class TestRequirePasswordConfirmation:
    def test_no_credentials_skips(self):
        # 未初始化凭证：即使开启认证，也跳过密码确认（由全局中间件 401 兜底）
        plugin_api._require_password_confirmation("whatever")

    def test_auth_disabled_skips(self, monkeypatch):
        # 登录认证关闭（本地单用户模式）：无需密码确认，否则无 token 用户会被空密码 401 卡住
        class _CfgOff:
            auth_enabled = False

        monkeypatch.setattr("scout.config.manager.ConfigManager.load", lambda self: _CfgOff())
        auth_mod.AuthManager().set_credentials("admin", "correct")
        plugin_api._require_password_confirmation("wrong")  # 不应抛异常

    def test_wrong_password_rejected(self):
        auth_mod.AuthManager().set_credentials("admin", "correct")
        with pytest.raises(HTTPException) as ei:
            plugin_api._require_password_confirmation("wrong")
        assert ei.value.status_code == 401

    def test_correct_password_accepted(self):
        auth_mod.AuthManager().set_credentials("admin", "correct")
        plugin_api._require_password_confirmation("correct")  # 不应抛异常


class FakeManager:
    """不落盘的假插件管理器."""

    def __init__(self, tmp_path):
        self.plugins_dir = tmp_path / "plugins"
        self.plugins_dir.mkdir()

    def get_plugin(self, name):
        return None

    def discover_plugins(self):
        pass

    def load_plugin(self, name):
        # 契约：create_plugin 要求加载成功才返回成功
        return True

    def list_plugins(self):
        return []


class TestCreatePluginIntegration:
    def test_create_rejects_wrong_password(self, tmp_path, monkeypatch):
        auth_mod.AuthManager().set_credentials("admin", "pw")
        fake = FakeManager(tmp_path)
        monkeypatch.setattr(plugin_api, "get_plugin_manager", lambda: fake)
        req = plugin_api.PluginCreateRequest(
            name="evilplug", code="print(1)", password="bad"
        )
        with pytest.raises(HTTPException) as ei:
            import asyncio
            asyncio.run(plugin_api.create_plugin(req))
        assert ei.value.status_code == 401
        # 未写入任何文件
        assert not (fake.plugins_dir / "evilplug").exists()

    def test_create_correct_password_creates(self, tmp_path, monkeypatch):
        auth_mod.AuthManager().set_credentials("admin", "pw")
        fake = FakeManager(tmp_path)
        monkeypatch.setattr(plugin_api, "get_plugin_manager", lambda: fake)
        req = plugin_api.PluginCreateRequest(
            name="goodplug", code="print(1)", password="pw"
        )
        import asyncio
        resp = asyncio.run(plugin_api.create_plugin(req))
        assert resp.success is True
        assert (fake.plugins_dir / "goodplug" / "__init__.py").exists()
