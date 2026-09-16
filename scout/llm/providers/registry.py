"""LLM Provider 注册表 — 按配置动态创建 LLM 客户端."""

from __future__ import annotations

from scout.llm.base import LLMClient
from scout.llm.providers.openai import OpenAIProvider

# 重试/超时相关参数名（从 kwargs 中提取传给 OpenAIProvider）
_RETRY_PARAM_KEYS = (
    "max_retries",
    "retry_backoff_base",
    "retry_backoff_max",
    "stream_timeout",
    "request_timeout",
)


def create_provider(
    provider: str = "openai",
    api_key: str = "",
    model: str = "gpt-4o-mini",
    base_url: str | None = None,
    **kwargs,
) -> LLMClient:
    """创建 LLM provider 实例.

    支持的重试/超时参数（可选，有合理默认值）:
    - max_retries: 最大重试次数（默认 3）
    - retry_backoff_base: 退避基数秒（默认 2.0）
    - retry_backoff_max: 退避上限秒（默认 30.0）
    - stream_timeout: 流式总超时秒（默认 180）
    - request_timeout: 非流式超时秒（默认 90）
    """
    provider = provider.lower().strip()

    # 提取重试参数
    retry_kwargs = {k: kwargs.pop(k) for k in _RETRY_PARAM_KEYS if k in kwargs}

    # 所有 OpenAI 兼容端点
    compatible_providers = {
        "openai", "openrouter", "dashscope", "moonshot", "deepseek",
        "zhipu", "volcano", "claude", "gemini", "compatible",
    }
    if provider in compatible_providers:
        p = OpenAIProvider(
            api_key=api_key,
            model=model,
            base_url=base_url,
            **retry_kwargs,
            **kwargs,
        )
        p._provider_name = provider
        return p
    raise ValueError(
        f"未知 provider: {provider}。支持: {', '.join(sorted(compatible_providers))}"
    )
