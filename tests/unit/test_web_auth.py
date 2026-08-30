"""Web 敏感接口鉴权测试 — scout/adapters/web.py 的 _require_auth 校验逻辑.

覆盖：
- 登录认证开关关闭（默认）→ 一律放行
- 开关开启 + 未设置凭证（首次使用）→ 放行
- 开关开启 + 已设置凭证 + 无 token / 无效 token → 拒绝
- 开关开启 + 已设置凭证 + 有效 token（Bearer header / query param）→ 放行
"""

import pytest

import scout.config.manager as config_manager_mod
import scout.security.auth as auth_mod
from scout.adapters.web import WebAdapter


class _FakeHeaders:
    def __init__(self, authorization: str = ""):
        self._authorization = authorization

    def get(self, key: str, default: str = ""):
        return self._authorization if key == "Authorization" else default


class _FakeRequest:
    def __init__(self, authorization: str = "", query_token: str = ""):
        self.headers = _FakeHeaders(authorization)
        self.query_params = {"token": query_token}


class _FakeAuthMgr:
    """模拟 AuthManager.has_credentials（不落盘真实凭证文件）."""

    def __init__(self, has_credentials: bool):
        self._has = has_credentials

    def has_credentials(self) -> bool:
        return self._has


@pytest.fixture(autouse=True)
def isolated_jwt(tmp_path, monkeypatch):
    """隔离 JWT 密钥路径与配置路径，避免读写真实 $SCOUT_DATA_DIR 文件."""
    monkeypatch.setattr(auth_mod, "SECRET_PATH", tmp_path / "jwt_secret")
    monkeypatch.setattr(config_manager_mod, "CONFIG_PATH", tmp_path / "config.json")
    return tmp_path


@pytest.fixture
def auth_enabled(monkeypatch):
    """模拟登录认证开关已开启（auth_enabled=true）."""
    from scout.config.manager import ConfigManager

    _orig_load = ConfigManager.load

    def _load_with_auth(self):
        cfg = _orig_load(self)
        cfg.auth_enabled = True
        return cfg

    monkeypatch.setattr(ConfigManager, "load", _load_with_auth)


@pytest.fixture
def adapter():
    """构造最小 WebAdapter 实例（跳过 __init__，仅挂载鉴权所需状态）."""
    obj = WebAdapter.__new__(WebAdapter)
    return obj


class TestNoCredentials:
    """未设置凭证（首次使用）→ 一律放行."""

    @pytest.mark.unit
    def test_allows_without_token(self, adapter):
        adapter.auth_mgr = _FakeAuthMgr(has_credentials=False)
        assert adapter._require_auth(_FakeRequest()) is True

    @pytest.mark.unit
    def test_allows_with_garbage_token(self, adapter):
        adapter.auth_mgr = _FakeAuthMgr(has_credentials=False)
        assert adapter._require_auth(_FakeRequest(authorization="Bearer garbage")) is True


class TestCredentialsRequireToken:
    """开关开启 + 已设置凭证 → 必须携带有效 token."""

    @pytest.mark.unit
    def test_rejects_missing_token(self, adapter, auth_enabled):
        adapter.auth_mgr = _FakeAuthMgr(has_credentials=True)
        assert adapter._require_auth(_FakeRequest()) is False

    @pytest.mark.unit
    def test_rejects_invalid_token(self, adapter, auth_enabled):
        adapter.auth_mgr = _FakeAuthMgr(has_credentials=True)
        req = _FakeRequest(authorization="Bearer not-a-valid-token")
        assert adapter._require_auth(req) is False

    @pytest.mark.unit
    def test_rejects_expired_token(self, adapter, auth_enabled):
        adapter.auth_mgr = _FakeAuthMgr(has_credentials=True)
        expired = auth_mod.create_token("admin", expiry=-100)  # 已过期
        req = _FakeRequest(authorization=f"Bearer {expired}")
        assert adapter._require_auth(req) is False


class TestValidTokenAccepted:
    """开关开启 + 有效 token（header / query 两种传递方式）→ 放行."""

    @pytest.mark.unit
    def test_accepts_bearer_header(self, adapter, auth_enabled):
        adapter.auth_mgr = _FakeAuthMgr(has_credentials=True)
        req = _FakeRequest(authorization=f"Bearer {auth_mod.create_token('admin')}")
        assert adapter._require_auth(req) is True

    @pytest.mark.unit
    def test_accepts_query_token(self, adapter, auth_enabled):
        adapter.auth_mgr = _FakeAuthMgr(has_credentials=True)
        req = _FakeRequest(query_token=auth_mod.create_token("admin"))
        assert adapter._require_auth(req) is True

    @pytest.mark.unit
    def test_accepts_ws_style_query_token(self, adapter, auth_enabled):
        """WebSocket 前端用的 ?token= 形式同样被识别."""
        adapter.auth_mgr = _FakeAuthMgr(has_credentials=True)
        req = _FakeRequest(query_token=auth_mod.create_token("admin"))
        assert adapter._require_auth(req) is True
