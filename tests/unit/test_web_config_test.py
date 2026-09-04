"""「测试连接」401 修复回归（2026-09-04）.

覆盖四个根因：
- Key 全链路去空白（请求传入 / 历史落盘脏 key）
- base_url 回落顺序：请求传入 → 主配置 → 凭据区（跨区错位修复）
- bare host 自动补 /v1（_normalize_base_url）
- 401 类错误附排查提示
"""
import types

import pytest


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """隔离配置路径，避免触碰真实 $SCOUT_DATA_DIR."""
    from scout.config import manager as config_manager_mod
    monkeypatch.setattr(config_manager_mod, "CONFIG_PATH", tmp_path / "config.json")
    from fastapi.testclient import TestClient
    from scout.web.server import create_web_app
    return TestClient(create_web_app())


@pytest.fixture()
def capture(monkeypatch):
    """替换 registry.create_provider，捕获「测试连接」端点实际收到的连接参数."""
    box = {}

    class _FakeLLM:
        def __init__(self, provider="", api_key="", model="", base_url=None, **kw):
            box.update(provider=provider, api_key=api_key, model=model, base_url=base_url)
            self.client = types.SimpleNamespace(
                base_url=base_url or "https://api.openai.com/v1"
            )

        async def complete(self, messages):
            return types.SimpleNamespace(content="pong")

    import scout.llm.providers.registry as registry_mod
    monkeypatch.setattr(registry_mod, "create_provider", _FakeLLM)
    return box


def _write_main_config(**kw):
    """直接写主配置区（绕过 POST /api/config 的 Agent 重建副作用）."""
    from scout.config import ConfigManager
    mgr = ConfigManager()
    cfg = mgr.load()
    for k, v in kw.items():
        setattr(cfg, k, v)
    mgr.save(cfg)


class TestNormalizeBaseUrl:
    """bare host 自动补 /v1；带路径的端点原样保留."""

    @pytest.mark.unit
    def test_bare_host_gets_v1(self):
        from scout.llm.providers.openai import _normalize_base_url
        assert _normalize_base_url("https://relay.example.com") == "https://relay.example.com/v1"
        assert _normalize_base_url("https://relay.example.com/") == "https://relay.example.com/v1"
        assert _normalize_base_url("  https://relay.example.com  ") == "https://relay.example.com/v1"
        assert _normalize_base_url("http://localhost:8000") == "http://localhost:8000/v1"

    @pytest.mark.unit
    def test_path_preserved(self):
        from scout.llm.providers.openai import _normalize_base_url
        assert _normalize_base_url("https://api.openai.com/v1") == "https://api.openai.com/v1"
        assert (
            _normalize_base_url("https://dashscope.aliyuncs.com/compatible-mode/v1")
            == "https://dashscope.aliyuncs.com/compatible-mode/v1"
        )
        assert (
            _normalize_base_url("https://ark.cn-beijing.volces.com/api/v3")
            == "https://ark.cn-beijing.volces.com/api/v3"
        )

    @pytest.mark.unit
    def test_none_and_empty(self):
        from scout.llm.providers.openai import _normalize_base_url
        assert _normalize_base_url(None) is None
        assert _normalize_base_url("") == ""
        assert _normalize_base_url("   ") == ""


class TestOpenAIProviderNormalization:
    """根治层：脏 key/model 去空白 + bare host 补 /v1."""

    @pytest.mark.unit
    def test_dirty_key_model_and_bare_host(self):
        from scout.llm.providers.openai import OpenAIProvider
        p = OpenAIProvider(
            api_key=" sk-relay \n", model=" gpt-4o ", base_url=" https://relay.example.com "
        )
        assert p.client.api_key == "sk-relay"
        assert p.model == "gpt-4o"
        # SDK 会在 base_url 尾部规范化补 "/"，比较时去掉
        assert str(p.client.base_url).rstrip("/") == "https://relay.example.com/v1"

    @pytest.mark.unit
    def test_none_key_semantics_preserved(self):
        """api_key=None 保持 None（SDK 做 env 回落），不被误转为空串."""
        import os
        from scout.llm.providers.openai import OpenAIProvider
        old = os.environ.pop("OPENAI_API_KEY", None)
        try:
            with pytest.raises(Exception):
                OpenAIProvider(api_key=None, model="gpt-4o")
        finally:
            if old is not None:
                os.environ["OPENAI_API_KEY"] = old


class TestTestConfigKeyStrip:
    """方案1：Key 去空白（请求传入 + 历史落盘脏 key）."""

    @pytest.mark.unit
    def test_request_key_stripped(self, client, capture):
        r = client.post("/api/config/test", json={
            "provider": "openai", "model": "gpt-4o",
            "api_key": " sk-relay \n", "base_url": "https://x.example/v1",
        })
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert capture["api_key"] == "sk-relay"

    @pytest.mark.unit
    def test_dirty_stored_key_cured(self, client, capture):
        """历史落盘的首尾带空白 key（脱敏回显触发回落）必须被二次清洗."""
        from scout.config import ConfigManager
        mgr = ConfigManager()
        cfg = mgr.load()
        cfg.provider_keys = {"openai": " sk-dirty "}
        mgr.save(cfg)

        r = client.post("/api/config/test", json={
            "provider": "openai", "model": "gpt-4o",
            "api_key": "***", "base_url": "https://x.example/v1",
        })
        assert r.status_code == 200
        assert capture["api_key"] == "sk-dirty"


class TestTestConfigBaseUrlFallback:
    """方案2：base_url 回落顺序 请求传入 → 主配置 → 凭据区."""

    @pytest.mark.unit
    def test_request_url_takes_priority(self, client, capture):
        _write_main_config(provider="openai", api_key="sk-main", base_url="https://main.example/v1")
        client.put("/api/config/keys/openai", json={
            "api_key": "sk-main", "base_url": "https://creds.example/v1", "activate": False,
        })
        r = client.post("/api/config/test", json={
            "provider": "openai", "model": "gpt-4o",
            "api_key": "sk-main", "base_url": "https://request.example/v1",
        })
        assert r.status_code == 200
        assert capture["base_url"] == "https://request.example/v1"
        assert r.json()["endpoint"] == "https://request.example/v1"

    @pytest.mark.unit
    def test_empty_url_falls_back_to_main_config(self, client, capture):
        """请求 URL 为空且 provider == 激活主配置 → 用主配置 base_url（非凭据区旧值）."""
        _write_main_config(provider="openai", api_key="sk-main", base_url="https://main.example/v1")
        client.put("/api/config/keys/openai", json={
            "api_key": "sk-main", "base_url": "https://creds.example/v1", "activate": False,
        })
        r = client.post("/api/config/test", json={
            "provider": "openai", "model": "gpt-4o", "api_key": "***", "base_url": "",
        })
        assert r.status_code == 200
        assert capture["base_url"] == "https://main.example/v1"

    @pytest.mark.unit
    def test_provider_mismatch_falls_to_creds_zone(self, client, capture):
        """被测 provider ≠ 激活主配置 → 主配置 URL 不可用，回落该 provider 凭据区."""
        _write_main_config(provider="openai", api_key="sk-main", base_url="https://main.example/v1")
        client.put("/api/config/keys/deepseek", json={
            "api_key": "sk-ds", "base_url": "https://creds.example/v1", "activate": False,
        })
        r = client.post("/api/config/test", json={
            "provider": "deepseek", "model": "deepseek-chat",
            "api_key": "sk-ds", "base_url": "",
        })
        assert r.status_code == 200
        assert capture["base_url"] == "https://creds.example/v1"

    @pytest.mark.unit
    def test_empty_base_url_passes_none(self, client, capture):
        """全部来源为空 → 传 None 让 SDK 回落官方默认端点（而非把空串当端点）."""
        r = client.post("/api/config/test", json={
            "provider": "openai", "model": "gpt-4o", "api_key": "sk-x", "base_url": "",
        })
        assert r.status_code == 200
        assert capture["base_url"] is None
        assert r.json()["endpoint"] == "https://api.openai.com/v1"


class TestTestConfigErrorHint:
    """401 类错误附排查提示（根因①：令牌无模型权限等只能服务商侧解决）."""

    @pytest.mark.unit
    def test_401_error_contains_hint(self, client, monkeypatch):
        class _ErrLLM:
            def __init__(self, **kw):
                self.client = types.SimpleNamespace(base_url="https://x.example/v1")

            async def complete(self, messages):
                raise RuntimeError("Error code: 401 - Authorization failed.")

        import scout.llm.providers.registry as registry_mod
        monkeypatch.setattr(registry_mod, "create_provider", _ErrLLM)
        r = client.post("/api/config/test", json={
            "provider": "openai", "model": "gpt-4o",
            "api_key": "sk-x", "base_url": "https://x.example/v1",
        })
        assert r.status_code == 400
        err = r.json()["error"]
        assert "Authorization failed" in err
        assert "排查" in err
        assert "/v1" in err  # 提示 ③

    @pytest.mark.unit
    def test_non_auth_error_no_hint(self, client, monkeypatch):
        class _ErrLLM:
            def __init__(self, **kw):
                self.client = types.SimpleNamespace(base_url="https://x.example/v1")

            async def complete(self, messages):
                raise RuntimeError("Connection timed out")

        import scout.llm.providers.registry as registry_mod
        monkeypatch.setattr(registry_mod, "create_provider", _ErrLLM)
        r = client.post("/api/config/test", json={
            "provider": "openai", "model": "gpt-4o",
            "api_key": "sk-x", "base_url": "https://x.example/v1",
        })
        assert r.status_code == 400
        assert "排查" not in r.json()["error"]


class TestSaveConfigStrip:
    """落盘前清洗：脏 base_url 不进 config.json."""

    @pytest.mark.unit
    def test_save_config_strips_base_url(self, client):
        r = client.post("/api/config", json={
            "provider": "openai", "base_url": "  https://relay.example.com/v1  ",
        })
        assert r.status_code == 200
        cfg = client.get("/api/config").json()
        assert cfg["base_url"] == "https://relay.example.com/v1"
