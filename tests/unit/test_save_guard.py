"""save() 凭据护栏测试 — 防止"内存丢 key"被固化到磁盘（2026-09-24 事故回归）.

事故背景：某次带病保存把用户 openai key 清空，_write_encrypted 对空值静默
过滤、_legacy_restored 一次性标记又关闭自愈门，导致 key 永久丢失。
护栏：save() 默认对比磁盘现存凭据，磁盘有而新数据无 → 沿用磁盘值；
save_provider_key 是显式增删凭据的唯一入口（protect_credentials=False），
护栏不得复活用户主动删除的 key。
"""

import json

import pytest

import scout.config.manager as manager_mod
from scout.config.manager import ConfigManager, LLMConfig
from scout.security import secret as secret_mod


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """隔离 config.json 与密钥文件，避免污染真实数据目录."""
    monkeypatch.setattr(manager_mod, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(secret_mod, "SECRET_PATH", tmp_path / "secret_key")
    monkeypatch.setattr(ConfigManager, "_load_env_file", lambda self, path, config: None)
    return tmp_path


@pytest.fixture
def manager(isolated):
    return ConfigManager()


def _read_disk() -> dict:
    with open(manager_mod.CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def _decrypt(value: str) -> str:
    return secret_mod.decrypt_secret_ex(value)[0]


@pytest.mark.unit
class TestSaveCredentialGuard:
    def test_guard_keeps_disk_api_key_when_memory_lost(self, manager):
        """内存 config 丢了 api_key 时，save 沿用磁盘值而非落盘空值."""
        manager.save(LLMConfig(provider="openai", model="m", api_key="sk-real-key"))
        # 模拟上游 bug：内存对象丢 key
        sick = LLMConfig(provider="openai", model="m", api_key="")
        manager.save(sick)
        disk = _read_disk()
        assert _decrypt(disk["api_key"]) == "sk-real-key"

    def test_guard_restores_missing_provider_key(self, manager):
        """内存 config 丢了 provider_keys 条目时，save 从磁盘补回."""
        manager.save_provider_key("openai", "sk-a", activate=True)
        manager.save_provider_key("dashscope", "sk-dash", activate=False)
        sick = LLMConfig(provider="openai", model="m", api_key="sk-a", provider_keys={})
        manager.save(sick)
        disk = _read_disk()
        keys = {k: _decrypt(v) for k, v in disk["provider_keys"].items()}
        assert keys == {"openai": "sk-a", "dashscope": "sk-dash"}

    def test_normal_save_not_double_encrypted(self, manager):
        """护栏不干扰正常保存：内存 key 非空时按原值落盘，可解密还原."""
        manager.save(LLMConfig(provider="openai", model="m", api_key="sk-v1"))
        manager.save(LLMConfig(provider="openai", model="m", api_key="sk-v2"))
        disk = _read_disk()
        assert _decrypt(disk["api_key"]) == "sk-v2"

    def test_explicit_delete_via_save_provider_key_works(self, manager):
        """显式删除入口不被护栏复活：save_provider_key(p, '') 真删."""
        manager.save(LLMConfig(provider="openai", model="m", api_key="sk-a"))
        manager.save_provider_key("dashscope", "sk-dash", activate=False)
        manager.save_provider_key("dashscope", "", activate=False)
        disk = _read_disk()
        assert "dashscope" not in (disk.get("provider_keys") or {})

    def test_load_roundtrip_after_guard_save(self, manager):
        """护栏保存后 load 回来的对象凭据完整且为明文."""
        manager.save_provider_key("openai", "sk-a", activate=True)
        manager.save_provider_key("dashscope", "sk-dash", activate=False)
        manager.save(LLMConfig(provider="openai", model="m", api_key="", provider_keys={}))
        c = manager.load()
        assert c.api_key == "sk-a"
        assert c.provider_keys == {"openai": "sk-a", "dashscope": "sk-dash"}

    def test_guard_noop_when_disk_empty(self, manager):
        """首次保存（磁盘无凭据）护栏为无操作."""
        manager.save(LLMConfig(provider="openai", model="m", api_key=""))
        disk = _read_disk()
        assert disk["api_key"] == ""
