"""Web 全局鉴权中间件加固测试.

覆盖 scout/web/server.py：
1. _is_initialization_whitelist 白名单判断
2. 无凭证时 /api 敏感面返回 401（防止默认 0.0.0.0 暴露下的裸奔）
3. 有凭证时 Bearer token 校验生效
"""
import json

import pytest

import scout.security.auth as auth_mod
from scout.web.server import _is_initialization_whitelist, create_web_app


class TestWhitelist:
    @pytest.mark.parametrize("path", [
        "/api/auth/login",
        "/api/auth/check",
        "/api/auth/status",
        "/api/auth/change-password",
        "/api/webhook/abc123",
        "/static/app.js",
        "/.well-known/anything",
    ])
    def test_whitelisted(self, path):
        assert _is_initialization_whitelist(path) is True

    @pytest.mark.parametrize("path", [
        "/api/sessions",
        "/api/config",
        "/api/plugins/create",
        "/api/files/download?path=/etc/passwd",
        "/api/newsfeed",
        "/v1/chat/completions",
        "/a2a/tasks/send",
        "/ws",
    ])
    def test_not_whitelisted(self, path):
        assert _is_initialization_whitelist(path) is False


@pytest.fixture()
def isolated(tmp_path, monkeypatch):
    """隔离 JWT 密钥、凭证文件与配置路径，避免触碰真实 $SCOUT_DATA_DIR."""
    monkeypatch.setattr(auth_mod, "SECRET_PATH", tmp_path / "jwt_secret")
    monkeypatch.setattr(auth_mod.AuthManager, "CREDENTIALS_PATH", tmp_path / "credentials.json")
    from scout.config import manager as config_manager_mod
    monkeypatch.setattr(config_manager_mod, "CONFIG_PATH", tmp_path / "config.json")
    from fastapi.testclient import TestClient
    # 不用 with 进入：避免触发 lifespan（日志清理/文件监听后台任务）
    return TestClient(create_web_app())


@pytest.fixture()
def auth_enabled(isolated, monkeypatch):
    """模拟登录认证开关已开启（auth_enabled=true）."""
    from scout.config import ConfigManager
    _orig_load = ConfigManager.load

    def _load_with_auth(self):
        cfg = _orig_load(self)
        cfg.auth_enabled = True
        return cfg

    monkeypatch.setattr(ConfigManager, "load", _load_with_auth)
    return isolated


class TestMiddlewareAuthDisabledByDefault:
    """登录认证开关默认关闭（auth_enabled=false）→ 所有接口放行."""

    def test_sensitive_api_allowed_without_credentials(self, isolated):
        resp = isolated.get("/api/sessions")
        assert resp.status_code != 401

    def test_auth_guide_allowed(self, isolated):
        # 初始化引导接口必须放行
        resp = isolated.get("/api/auth/status")
        assert resp.status_code != 401

    def test_login_flow_works(self, isolated):
        resp = isolated.post("/api/auth/login", json={"username": "u", "password": "p"})
        assert resp.status_code == 200
        assert "token" in resp.json()


class TestMiddlewareNoCredentials:
    """开关开启但尚未设置凭证 → 仅放行本地回环，非回环 401."""

    def test_sensitive_api_401_without_credentials(self, auth_enabled):
        resp = auth_enabled.get("/api/sessions")
        assert resp.status_code == 401


class TestMiddlewareWithCredentials:
    def test_bearer_token_granted(self, auth_enabled):
        # 首次登录建立凭证
        token = auth_enabled.post(
            "/api/auth/login", json={"username": "u", "password": "p"}
        ).json()["token"]
        # 带 token 访问敏感面 → 不再 401
        resp = auth_enabled.get("/api/sessions", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code != 401

    def test_no_token_401_when_credentials_set(self, auth_enabled):
        auth_enabled.post("/api/auth/login", json={"username": "u", "password": "p"})
        resp = auth_enabled.get("/api/sessions")
        assert resp.status_code == 401

    def test_bad_token_401(self, auth_enabled):
        auth_enabled.post("/api/auth/login", json={"username": "u", "password": "p"})
        resp = auth_enabled.get(
            "/api/sessions", headers={"Authorization": "Bearer forged.token.value"}
        )
        assert resp.status_code == 401


class TestDocsDisabled:
    def test_docs_404_by_default(self, isolated):
        # 默认关闭：避免泄露 API 结构
        assert isolated.get("/docs").status_code == 404
        assert isolated.get("/redoc").status_code == 404
        assert isolated.get("/openapi.json").status_code == 404

    def test_docs_enabled_via_env(self, monkeypatch):
        monkeypatch.setenv("SCOUT_ENABLE_DOCS", "1")
        from fastapi.testclient import TestClient
        app = create_web_app()
        with TestClient(app) as client:
            assert client.get("/docs").status_code == 200
            assert client.get("/openapi.json").status_code == 200
