"""多搜索引擎源测试（2026-08-21）.

覆盖：
- search_with_engine 按 type 分发到各适配器
- 需 Key 的引擎（bing/google/tavily）缺 Key 时抛 ValueError
- WebSearchTool._enabled_engines 兼容逻辑（旧版单值 / 多源 / 禁用过滤 / is_enabled）
- ConfigManager 对 search_engines 内嵌 api_key 的加密/解密往返
"""

import asyncio

import pytest

import scout.config.manager as config_manager_mod
import scout.tools.builtin.web.engines as engines_mod
from scout.tools.builtin.web import WebSearchTool
from scout.tools.builtin.web.engines import search_with_engine

# ── 引擎适配器分发 ──


def test_search_with_engine_dispatch(monkeypatch):
    """不同 type 分发到对应适配器，URL/Key/查询透传."""
    calls = {}

    def _fake(etype):
        async def _impl(url, api_key, query, **kw):
            calls.update({"type": etype, "url": url, "api_key": api_key, "query": query})
            return [{"title": "t", "url": "u", "content": "c"}]

        return _impl

    monkeypatch.setattr(engines_mod, "_search_searxng", _fake("searxng"))
    monkeypatch.setattr(engines_mod, "_search_bing", _fake("bing"))
    monkeypatch.setattr(engines_mod, "_search_google", _fake("google"))
    monkeypatch.setattr(engines_mod, "_search_tavily", _fake("tavily"))
    monkeypatch.setattr(engines_mod, "_search_duckduckgo", _fake("duckduckgo"))
    monkeypatch.setattr(engines_mod, "_search_custom", _fake("custom"))

    async def _run():
        return await search_with_engine(
            {"type": "bing", "url": "https://api.bing/v7", "api_key": "KEY-1"}, "hello"
        )

    res = asyncio.run(_run())
    assert calls["type"] == "bing"
    assert calls["url"] == "https://api.bing/v7"
    assert calls["api_key"] == "KEY-1"
    assert calls["query"] == "hello"
    assert res[0]["url"] == "u"


def test_keyless_engines_raise_without_key():
    """bing/google/tavily 缺 Key 必须报错（failover 由调用方处理）."""
    for etype in ("bing", "google", "tavily"):

        async def _run(t=etype):
            return await search_with_engine({"type": t, "url": "", "api_key": ""}, "q")

        with pytest.raises(ValueError):
            asyncio.run(_run())


# ── WebSearchTool 兼容逻辑 ──


class _FakeCfg:
    def __init__(self, engines, legacy=""):
        self.search_engines = engines
        self.search_engine = legacy


def _patch_config(monkeypatch, cfg):
    monkeypatch.setattr(config_manager_mod.ConfigManager, "load", lambda self: cfg)


def test_enabled_engines_legacy_single_value(monkeypatch):
    """旧版单值 search_engine → 自动补一个 searxng 源."""
    _patch_config(monkeypatch, _FakeCfg([], "http://legacy/search"))
    engines = WebSearchTool()._enabled_engines()
    assert len(engines) == 1
    assert engines[0]["type"] == "searxng"
    assert engines[0]["url"] == "http://legacy/search"
    assert engines[0]["enabled"] is True


def test_enabled_engines_multi_and_filter(monkeypatch):
    """多源：禁用项被过滤，url 保留原值."""
    cfg = _FakeCfg([
        {"name": "A", "type": "searxng", "url": "http://a/search", "api_key": "", "enabled": True},
        {"name": "B", "type": "bing", "url": "", "api_key": "k", "enabled": False},
        {"name": "C", "type": "tavily", "url": "", "api_key": "t", "enabled": True},
    ])
    _patch_config(monkeypatch, cfg)
    engines = WebSearchTool()._enabled_engines()
    assert [e["name"] for e in engines] == ["A", "C"]


def test_is_enabled(monkeypatch):
    """未配置任何源 → 工具不启用."""
    _patch_config(monkeypatch, _FakeCfg([]))
    assert WebSearchTool().is_enabled() is False
    _patch_config(monkeypatch, _FakeCfg([{"name": "A", "type": "searxng", "url": "", "api_key": "", "enabled": True}]))
    assert WebSearchTool().is_enabled() is True


# ── ConfigManager 加密往返 ──


def test_search_engines_api_key_encrypt_roundtrip(tmp_path, monkeypatch):
    """search_engines 内嵌 api_key 落盘加密、读回解密（与顶层 api_key 一致）."""
    monkeypatch.setattr(config_manager_mod, "CONFIG_PATH", tmp_path / "config.json")
    mgr = config_manager_mod.ConfigManager()
    cfg = mgr.load()
    cfg.search_engines = [
        {"name": "bing", "type": "bing", "url": "", "api_key": "sk-secret-123456", "enabled": True}
    ]
    mgr.save(cfg)

    # 落盘文件应为加密值（enc:v1: 前缀），不得明文
    import json

    raw = json.loads((tmp_path / "config.json").read_text())
    assert raw["search_engines"][0]["api_key"].startswith("enc:v1:")

    # 读回解密为明文
    loaded = mgr.load()
    assert loaded.search_engines[0]["api_key"] == "sk-secret-123456"
