"""/api/config/keys 多厂商 key + base_url API 集成测试."""
import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """隔离配置路径，避免触碰真实 $SCOUT_DATA_DIR."""
    from scout.config import manager as config_manager_mod
    monkeypatch.setattr(config_manager_mod, "CONFIG_PATH", tmp_path / "config.json")
    from fastapi.testclient import TestClient
    from scout.web.server import create_web_app
    return TestClient(create_web_app())


class TestConfigKeysBaseUrl:
    @pytest.mark.unit
    def test_put_saves_key_and_base_url(self, client):
        r = client.put(
            "/api/config/keys/dashscope",
            json={"api_key": "sk-ds-1", "base_url": "https://ds.example/v1", "activate": False},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"

        g = client.get("/api/config/keys")
        assert g.status_code == 200
        data = g.json()
        assert data["keys"] == {"dashscope": True}
        assert data["base_urls"] == {"dashscope": "https://ds.example/v1"}

    @pytest.mark.unit
    def test_put_base_url_only(self, client):
        """仅保存 base_url（api_key 为空）时不应报错."""
        r = client.put(
            "/api/config/keys/openai",
            json={"api_key": "", "base_url": "https://oa.example/v1", "activate": False},
        )
        assert r.status_code == 200
        g = client.get("/api/config/keys")
        assert g.json()["base_urls"] == {"openai": "https://oa.example/v1"}
        assert g.json()["keys"] == {}

    @pytest.mark.unit
    def test_put_rejects_empty_both(self, client):
        r = client.put("/api/config/keys/openai", json={"api_key": "", "base_url": ""})
        assert r.status_code == 400

    @pytest.mark.unit
    def test_get_empty_base_urls(self, client):
        g = client.get("/api/config/keys")
        assert g.status_code == 200
        data = g.json()
        assert data["base_urls"] == {}
        assert data["keys"] == {}


class TestConfigScopeProviders:
    """视觉/图像模型独立厂商配置 API."""

    @pytest.mark.unit
    def test_post_and_get_scope_providers(self, client):
        r = client.post(
            "/api/config",
            json={"provider": "dashscope", "vision_provider": "openai", "image_provider": "zhipu"},
        )
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

        g = client.get("/api/config")
        cfg = g.json()
        assert cfg["vision_provider"] == "openai"
        assert cfg["image_provider"] == "zhipu"

    @pytest.mark.unit
    def test_scope_providers_default_empty(self, client):
        g = client.get("/api/config")
        cfg = g.json()
        assert cfg.get("vision_provider", "") == ""
        assert cfg.get("image_provider", "") == ""

    @pytest.mark.unit
    def test_scope_providers_can_be_cleared(self, client):
        client.post("/api/config", json={"provider": "dashscope", "vision_provider": "openai"})
        r = client.post("/api/config", json={"provider": "dashscope", "vision_provider": ""})
        assert r.status_code == 200
        cfg = client.get("/api/config").json()
        assert cfg["vision_provider"] == ""
