"""配置层加密链路测试 — scout/config/manager.py 的敏感字段加密存储.

覆盖：
- save() 时 api_key 加密落盘（文件里不出现明文）
- load() 时密文解密为明文
- 历史明文自动迁移加密回写
- 非敏感字段不加密
"""

import json

import pytest

import scout.config.manager as manager_mod
from scout.config.manager import ConfigManager, LLMConfig
from scout.security import secret as secret_mod


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """隔离 config.json 与密钥文件，避免污染真实 $SCOUT_DATA_DIR/."""
    monkeypatch.setattr(manager_mod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(secret_mod, "SECRET_PATH", tmp_path / "secret_key")
    # 屏蔽 .env 读取，保证测试环境纯净
    monkeypatch.setattr(ConfigManager, "_load_env_file", lambda self, path, config: None)
    return tmp_path


@pytest.fixture
def manager(isolated):
    return ConfigManager()


def _read_disk() -> dict:
    with open(manager_mod.CONFIG_PATH) as f:
        return json.load(f)


def _write_disk(data: dict) -> None:
    manager_mod.CONFIG_PATH.write_text(json.dumps(data), encoding="utf-8")


class TestSaveEncrypts:
    """save() 时敏感字段加密落盘."""

    @pytest.mark.unit
    def test_api_key_not_stored_in_plaintext(self, manager):
        manager.save(LLMConfig(provider="dashscope", model="qwen-plus", api_key="sk-super-secret"))
        disk = _read_disk()
        assert "sk-super-secret" not in json.dumps(disk)
        assert secret_mod.is_encrypted(disk["api_key"])

    @pytest.mark.unit
    def test_non_sensitive_fields_plain(self, manager):
        manager.save(LLMConfig(model="qwen-plus", temperature=0.7))
        disk = _read_disk()
        assert disk["model"] == "qwen-plus"
        assert disk["temperature"] == 0.7

    @pytest.mark.unit
    def test_empty_api_key_skipped(self, manager):
        manager.save(LLMConfig(api_key=""))
        disk = _read_disk()
        assert disk["api_key"] == ""


class TestLoadDecrypts:
    """load() 时密文还原为明文."""

    @pytest.mark.unit
    def test_save_then_load_roundtrip(self, manager):
        manager.save(LLMConfig(provider="openai", model="gpt-4o", api_key="sk-roundtrip"))
        loaded = manager.load()
        assert loaded.api_key == "sk-roundtrip"
        assert loaded.model == "gpt-4o"

    @pytest.mark.unit
    def test_load_handles_pre_encrypted_disk(self, manager):
        """直接写入密文的磁盘文件也能正确解密."""
        encrypted = secret_mod.encrypt_secret("sk-from-disk")
        _write_disk({"api_key": encrypted, "model": "qwen-max"})
        loaded = manager.load()
        assert loaded.api_key == "sk-from-disk"
        assert loaded.model == "qwen-max"


class TestPlaintextMigration:
    """历史明文自动加密回写（平滑迁移）."""

    @pytest.mark.unit
    def test_load_migrates_plaintext_and_rewrites(self, manager):
        _write_disk({"api_key": "sk-legacy-plain", "model": "qwen-plus"})
        loaded = manager.load()
        assert loaded.api_key == "sk-legacy-plain"
        # 回写后磁盘上必须是密文
        disk = _read_disk()
        assert secret_mod.is_encrypted(disk["api_key"])
        assert "sk-legacy-plain" not in json.dumps(disk)


class TestTamperResistance:
    """磁盘密文被篡改时不崩溃、不泄露."""

    @pytest.mark.unit
    def test_load_corrupted_cipher_returns_empty(self, manager):
        _write_disk({"api_key": secret_mod.PREFIX + "tampered-token", "model": "qwen-plus"})
        loaded = manager.load()
        assert loaded.api_key == ""


class TestSessionRestoreFields:
    """会话恢复设置字段（restore_last_session / restore_last_model）的默认值与持久化."""

    @pytest.mark.unit
    def test_defaults_enabled(self, manager):
        """默认配置下两个字段均为开启（与前端 connect 默认行为一致）."""
        loaded = manager.load()
        assert loaded.restore_last_session is True
        assert loaded.restore_last_model is True

    @pytest.mark.unit
    def test_save_and_load_roundtrip(self, manager):
        manager.save(LLMConfig(restore_last_session=False, restore_last_model=True))
        loaded = manager.load()
        assert loaded.restore_last_session is False
        assert loaded.restore_last_model is True
        disk = _read_disk()
        assert disk["restore_last_session"] is False
        assert disk["restore_last_model"] is True

    @pytest.mark.unit
    def test_backward_compatible_without_fields(self, manager):
        """旧版配置无这两个字段时加载不报错，回退默认开启."""
        _write_disk({"api_key": "sk-old-style", "model": "qwen-plus"})
        loaded = manager.load()
        assert loaded.restore_last_session is True
        assert loaded.restore_last_model is True


class TestProviderKeys:
    """多 provider API Key 保存、加密与切换."""

    @pytest.mark.unit
    def test_save_and_load_roundtrip(self, manager):
        manager.save_provider_key("dashscope", "sk-ds-1")
        manager.save_provider_key("openai", "sk-oa-2", activate=False)
        config = manager.load()
        assert config.provider_keys == {"dashscope": "sk-ds-1", "openai": "sk-oa-2"}
        # activate=True 的 dashscope 成为当前激活
        assert config.provider == "dashscope"
        assert config.api_key == "sk-ds-1"

    @pytest.mark.unit
    def test_keys_encrypted_on_disk(self, manager):
        manager.save_provider_key("dashscope", "sk-secret-ds")
        manager.save_provider_key("openai", "sk-secret-oa", activate=False)
        disk = _read_disk()
        raw = json.dumps(disk)
        assert "sk-secret-ds" not in raw
        assert "sk-secret-oa" not in raw
        for v in disk["provider_keys"].values():
            assert secret_mod.is_encrypted(v)

    @pytest.mark.unit
    def test_activate_switches_key(self, manager):
        manager.save_provider_key("dashscope", "sk-ds", activate=False)
        manager.save_provider_key("openai", "sk-oa", activate=False)
        assert manager.activate_provider("openai")
        config = manager.load()
        assert config.provider == "openai"
        assert config.api_key == "sk-oa"
        # 切回 dashscope
        assert manager.activate_provider("dashscope")
        config = manager.load()
        assert config.provider == "dashscope"
        assert config.api_key == "sk-ds"

    @pytest.mark.unit
    def test_activate_missing_key_returns_false(self, manager):
        assert manager.activate_provider("nope") is False

    @pytest.mark.unit
    def test_list_does_not_leak_plaintext(self, manager):
        manager.save_provider_key("dashscope", "sk-top-secret")
        listed = manager.list_provider_keys()
        assert listed == {"dashscope": True}
        raw = json.dumps(listed)
        assert "sk-top-secret" not in raw

    @pytest.mark.unit
    def test_migrates_plaintext_keys(self, manager):
        """历史明文 provider_keys 自动加密回写."""
        _write_disk({
            "api_key": "sk-legacy",
            "provider": "dashscope",
            "provider_keys": {"dashscope": "sk-legacy", "openai": "sk-plain-oa"},
        })
        config = manager.load()
        assert config.provider_keys["dashscope"] == "sk-legacy"
        assert config.provider_keys["openai"] == "sk-plain-oa"
        disk = _read_disk()
        assert "sk-legacy" not in json.dumps(disk)
        assert "sk-plain-oa" not in json.dumps(disk)
        for v in disk["provider_keys"].values():
            assert secret_mod.is_encrypted(v)

    @pytest.mark.unit
    def test_remove_key_with_empty_value(self, manager):
        manager.save_provider_key("dashscope", "sk-ds")
        manager.save_provider_key("dashscope", "")
        config = manager.load()
        assert "dashscope" not in config.provider_keys

    @pytest.mark.unit
    def test_overwrite_key(self, manager):
        manager.save_provider_key("dashscope", "sk-old", activate=False)
        manager.save_provider_key("dashscope", "sk-new", activate=False)
        config = manager.load()
        assert config.provider_keys["dashscope"] == "sk-new"

    @pytest.mark.unit
    def test_load_backward_compatible_without_provider_keys(self, manager):
        """旧版配置无 provider_keys 字段时正常加载."""
        _write_disk({"api_key": "sk-old-style", "model": "qwen-plus"})
        config = manager.load()
        assert config.api_key == "sk-old-style"
        assert config.provider_keys == {}


class TestProviderBaseUrls:
    """多 provider base_url 保存与切换恢复."""

    @pytest.mark.unit
    def test_save_key_with_base_url_roundtrip(self, manager):
        """save_provider_key 同时保存 key + base_url."""
        manager.save_provider_key("dashscope", "sk-ds-1", base_url="https://dashscope.example/v1")
        config = manager.load()
        assert config.provider_base_urls == {"dashscope": "https://dashscope.example/v1"}
        # activate=True 时同步当前激活 base_url
        assert config.base_url == "https://dashscope.example/v1"

    @pytest.mark.unit
    def test_base_url_stored_plaintext(self, manager):
        """base_url 非敏感，明文落盘."""
        manager.save_provider_key("dashscope", "sk-ds-1", base_url="https://dashscope.example/v1")
        disk = _read_disk()
        assert disk["provider_base_urls"] == {"dashscope": "https://dashscope.example/v1"}
        assert "https://dashscope.example/v1" in json.dumps(disk)

    @pytest.mark.unit
    def test_save_base_url_only(self, manager):
        """仅保存 base_url（不涉及 key）."""
        manager.save_provider_base_url("openai", "https://openai.example/v1")
        config = manager.load()
        assert config.provider_base_urls == {"openai": "https://openai.example/v1"}
        # 默认不激活，全局 base_url 不变
        assert config.base_url != "https://openai.example/v1"

    @pytest.mark.unit
    def test_save_base_url_only_activate(self, manager):
        manager.save_provider_base_url("openai", "https://openai.example/v1", activate=True)
        config = manager.load()
        assert config.base_url == "https://openai.example/v1"

    @pytest.mark.unit
    def test_list_provider_base_urls(self, manager):
        manager.save_provider_key("dashscope", "sk-ds", base_url="https://ds.example/v1", activate=False)
        manager.save_provider_base_url("openai", "https://oa.example/v1")
        assert manager.list_provider_base_urls() == {
            "dashscope": "https://ds.example/v1",
            "openai": "https://oa.example/v1",
        }

    @pytest.mark.unit
    def test_activate_restores_saved_base_url(self, manager):
        """activate_provider 切换厂商时优先恢复该厂商已保存的 base_url."""
        manager.save_provider_key("dashscope", "sk-ds", base_url="https://ds.example/v1", activate=False)
        manager.save_provider_key("openai", "sk-oa", base_url="https://oa.example/v1", activate=False)
        assert manager.activate_provider("openai")
        config = manager.load()
        assert config.base_url == "https://oa.example/v1"
        assert manager.activate_provider("dashscope")
        config = manager.load()
        assert config.base_url == "https://ds.example/v1"

    @pytest.mark.unit
    def test_activate_with_explicit_base_url_falls_back_to_saved(self, manager):
        """显式传 base_url 时，若该厂商已保存 base_url 则优先用已保存值."""
        manager.save_provider_key("dashscope", "sk-ds", base_url="https://ds.example/v1", activate=False)
        assert manager.activate_provider("dashscope", base_url="https://override.example/v1")
        config = manager.load()
        assert config.base_url == "https://ds.example/v1"

    @pytest.mark.unit
    def test_remove_base_url_with_empty_value(self, manager):
        manager.save_provider_key("dashscope", "sk-ds", base_url="https://ds.example/v1", activate=False)
        manager.save_provider_base_url("dashscope", "")
        config = manager.load()
        assert "dashscope" not in config.provider_base_urls

    @pytest.mark.unit
    def test_load_backward_compatible_without_provider_base_urls(self, manager):
        """旧版配置无 provider_base_urls 字段时正常加载."""
        _write_disk({"api_key": "sk-old-style", "model": "qwen-plus"})
        config = manager.load()
        assert config.provider_base_urls == {}


class TestProviderScopes:
    """视觉/图像模型独立厂商配置."""

    @pytest.mark.unit
    def test_defaults_empty(self, manager):
        config = manager.load()
        assert config.vision_provider == ""
        assert config.image_provider == ""

    @pytest.mark.unit
    def test_save_and_load_roundtrip(self, manager):
        manager.save_scope_providers(vision_provider="openai", image_provider="dashscope")
        config = manager.load()
        assert config.vision_provider == "openai"
        assert config.image_provider == "dashscope"

    @pytest.mark.unit
    def test_clear_scope_providers(self, manager):
        manager.save_scope_providers(vision_provider="openai", image_provider="dashscope")
        manager.save_scope_providers(vision_provider="", image_provider="")
        config = manager.load()
        assert config.vision_provider == ""
        assert config.image_provider == ""

    @pytest.mark.unit
    def test_backward_compatible_without_fields(self, manager):
        _write_disk({"api_key": "sk-old", "model": "qwen-plus"})
        config = manager.load()
        assert config.vision_provider == ""
        assert config.image_provider == ""

    @pytest.mark.unit
    def test_get_provider_credentials_returns_saved(self, manager):
        manager.save_provider_key("openai", "sk-oa-123", base_url="https://oa.example/v1", activate=False)
        key, url = manager.get_provider_credentials("openai")
        assert key == "sk-oa-123"
        assert url == "https://oa.example/v1"

    @pytest.mark.unit
    def test_get_provider_credentials_missing_returns_empty(self, manager):
        key, url = manager.get_provider_credentials("nonexistent")
        assert key == ""
        assert url == ""

    @pytest.mark.unit
    def test_resolve_scoped_empty_falls_back_to_main(self, manager):
        """scope_provider 为空（跟随主）时回退主配置."""
        manager.save_provider_key("openai", "sk-oa", base_url="https://oa.example/v1", activate=False)
        provider, key, url = manager.resolve_scoped_credentials("", "dashscope")
        assert provider == "dashscope"
        assert key == ""
        assert url == ""

    @pytest.mark.unit
    def test_resolve_scoped_same_as_main_falls_back(self, manager):
        provider, key, url = manager.resolve_scoped_credentials("dashscope", "dashscope")
        assert provider == "dashscope"
        assert key == ""
        assert url == ""

    @pytest.mark.unit
    def test_resolve_scoped_different_uses_provider_credentials(self, manager):
        manager.save_provider_key("openai", "sk-oa-9", base_url="https://oa.example/v1", activate=False)
        provider, key, url = manager.resolve_scoped_credentials("openai", "dashscope")
        assert provider == "openai"
        assert key == "sk-oa-9"
        assert url == "https://oa.example/v1"

    @pytest.mark.unit
    def test_resolve_scoped_different_but_no_credentials(self, manager):
        provider, key, url = manager.resolve_scoped_credentials("openai", "dashscope")
        assert provider == "openai"
        assert key == ""
        assert url == ""
