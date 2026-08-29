"""插件 SPI（服务提供接口）— 对标 DeepSeek Harness「一切皆插件」.

此前 scout 的插件机制只能「附加」（事件钩子），无法「替换」核心组件。
本模块提供轻量 SPI 注册表：插件可声明 provides 并注册 LLM / 存储 / 缓存 /
会话 / 记忆等核心组件的替代实现；应用侧通过 get_provider() 获取，插件实现优先，
默认实现兜底。

使用方式（插件侧）:
    class MyLLMPlugin(Plugin):
        provides = [SPI_KIND_LLM]

        async def provide(self, kind: str):
            if kind == SPI_KIND_LLM:
                return MyLLMClient(**self.config)

应用侧接入（2026-08-27 全类型落地）:
    # LLMClientFactory.create(provider="spi")        → 插件注册的 LLM 实现
    # get_storage_backend(backend="spi")             → 插件注册的存储实现
    # get_cache_backend(backend="spi")               → 插件注册的缓存实现（SCOUT_CACHE_BACKEND=spi）
    # get_session_store(backend="spi")               → 插件注册的会话实现（SCOUT_SESSION_STORE=spi）
    # get_memory_store(backend="spi")                → 插件注册的记忆实现（SCOUT_MEMORY_STORE=spi）
    以上 backend="spi" 在插件未注册时都会明确报错（不静默回退）。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("scout.plugins.spi")

# ── SPI 类型常量 ─────────────────────────────────────────
SPI_KIND_LLM = "llm"            # LLM 客户端（实现 scout.llm.base.LLMClient 接口）
SPI_KIND_STORAGE = "storage"    # 关系存储（实现 scout.storage.base.StorageBackend）
SPI_KIND_CACHE = "cache"        # KV 缓存（实现 scout.storage.base.CacheBackend）
SPI_KIND_SESSION = "session"    # 会话存取
SPI_KIND_MEMORY = "memory"      # 长期记忆

ALL_KINDS = (SPI_KIND_LLM, SPI_KIND_STORAGE, SPI_KIND_CACHE, SPI_KIND_SESSION, SPI_KIND_MEMORY)


class SPIRegistry:
    """进程内 SPI 注册表（全局单例）."""

    _providers: dict[str, Any] = {}
    _source: dict[str, str] = {}  # kind → 提供者插件名（可观测性）

    @classmethod
    def register(cls, kind: str, impl: Any, source: str = "plugin") -> None:
        if kind not in ALL_KINDS:
            logger.warning("未知 SPI 类型 %s，仍将注册（类型需与应用侧约定一致）", kind)
        cls._providers[kind] = impl
        cls._source[kind] = source
        logger.info("SPI 已注册: %s <- %s", kind, source)

    @classmethod
    def get(cls, kind: str) -> Any | None:
        return cls._providers.get(kind)

    @classmethod
    def unregister(cls, kind: str) -> None:
        cls._providers.pop(kind, None)
        cls._source.pop(kind, None)

    @classmethod
    def source(cls, kind: str) -> str:
        return cls._source.get(kind, "")

    @classmethod
    def clear(cls) -> None:
        cls._providers.clear()
        cls._source.clear()

    @classmethod
    def all(cls) -> dict[str, Any]:
        return dict(cls._providers)


def register_provider(kind: str, impl: Any, source: str = "plugin") -> None:
    """注册 SPI 实现（应用侧或插件侧通用入口）."""
    SPIRegistry.register(kind, impl, source)


def unregister_provider(kind: str) -> None:
    """注销 SPI 实现."""
    SPIRegistry.unregister(kind)


def get_provider(kind: str) -> Any | None:
    """获取 SPI 实现；未注册返回 None（调用方回退默认实现）."""
    return SPIRegistry.get(kind)


def has_provider(kind: str) -> bool:
    return kind in SPIRegistry._providers
