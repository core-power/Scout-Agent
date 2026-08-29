"""LLM 客户端抽象接口."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import AsyncIterator

from scout.core.types import Delta, LLMResponse


class LLMClient(ABC):
    """LLM 供应商统一接口."""

    @abstractmethod
    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse:
        """异步完成."""
        ...

    @abstractmethod
    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> AsyncIterator[Delta]:
        """流式完成."""
        ...
        yield  # type: ignore[unreachable]


class LLMClientFactory:
    """LLM 客户端工厂."""
    
    @staticmethod
    async def create(provider: str, **kwargs) -> LLMClient:
        """创建 LLM 客户端实例."""
        if provider == "spi":
            # 插件 SPI（2026-08-27）：provider="spi" 时使用插件注册的 LLM 实现。
            # 插件侧 provide("llm") 返回可调用对象（类或工厂），按 impl(**kwargs) 实例化。
            from scout.plugins.spi import SPI_KIND_LLM, get_provider

            impl = get_provider(SPI_KIND_LLM)
            if impl is None:
                raise ValueError(
                    "LLM SPI 未注册：provider='spi' 但无插件提供 'llm' 实现。"
                    "请加载声明 provides=['llm'] 的插件，或改用内置 provider。"
                )
            return impl(**kwargs) if callable(impl) else impl
        elif provider == "openai":
            from scout.llm.openai_client import OpenAIClient
            return OpenAIClient(**kwargs)
        elif provider == "dashscope":
            from scout.llm.dashscope_client import DashScopeClient
            return DashScopeClient(**kwargs)
        elif provider == "anthropic":
            from scout.llm.anthropic_client import AnthropicClient
            return AnthropicClient(**kwargs)
        else:
            raise ValueError(f"不支持的 LLM 提供商: {provider}")
