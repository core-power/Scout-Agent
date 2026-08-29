"""Fallback LLM Provider — 主模型失败时自动降级到备用模型.

捕获 403 (access_denied)、429 (rate_limit)、5xx、超时等错误，
自动切换到预配置的 fallback 模型重试.
"""

from __future__ import annotations

import logging
from typing import AsyncIterator

from scout.core.types import Delta, LLMResponse
from scout.llm.base import LLMClient

logger = logging.getLogger("scout.llm.fallback")

# 触发 fallback 的错误关键词
FALLBACK_KEYWORDS = (
    "access_denied", "403",
    "rate_limit", "429",
    "overloaded", "503",
    "internal_error", "500",
    "timeout", "timed out",
    "connection",
)


def _should_fallback(error: Exception) -> bool:
    """判断错误是否应触发 fallback."""
    msg = str(error).lower()
    return any(kw in msg for kw in FALLBACK_KEYWORDS)


class FallbackProvider(LLMClient):
    """带自动降级的 LLM Provider.

    主模型调用失败（403/429/5xx/超时）时，自动切换到备用模型重试.
    支持多级 fallback 链（从配置读取）.
    """

    def __init__(self, primary: LLMClient, fallback: LLMClient | list[LLMClient]):
        self.primary = primary
        # 支持单个 fallback 或 fallback 链
        if isinstance(fallback, list):
            self.fallback_chain = fallback
        else:
            self.fallback_chain = [fallback] if fallback else []
        # 暴露 primary 的属性，方便外部读取 model 名
        self.model = getattr(primary, "model", "")
        self._provider_name = getattr(primary, "_provider_name", "unknown")
        self._fallback_count = 0

    async def complete(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> LLMResponse:
        """同步调用 — 主模型失败时自动降级到 fallback 链."""
        try:
            return await self.primary.complete(messages, tools=tools, **kwargs)
        except Exception as e:
            if not _should_fallback(e):
                raise  # 非降级类错误，直接抛出
            
            # 遍历 fallback 链
            for i, fb_model in enumerate(self.fallback_chain):
                try:
                    logger.warning(
                        f"主模型 {self.primary.model} 调用失败 ({e}), "
                        f"降级到 {fb_model.model} (fallback #{i+1})"
                    )
                    self._fallback_count += 1
                    return await fb_model.complete(messages, tools=tools, **kwargs)
                except Exception as fb_error:
                    if not _should_fallback(fb_error):
                        raise
                    logger.warning(
                        f"Fallback #{i+1} ({fb_model.model}) 也失败: {fb_error}"
                    )
                    continue
            
            # 所有 fallback 都失败
            raise RuntimeError(
                f"主模型和所有 {len(self.fallback_chain)} 个 fallback 模型均失败"
            )

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        **kwargs,
    ) -> AsyncIterator[Delta]:
        """流式调用 — 主模型失败时自动降级.

        注意：流式场景下，如果主模型已经开始输出文本后失败，
        无法回退（已 yield 部分内容）。仅在首次连接失败时降级.
        """
        try:
            # 尝试主模型流式
            async for delta in self.primary.stream(messages, tools=tools, **kwargs):
                yield delta
            return  # 成功完成
        except Exception as e:
            if not _should_fallback(e):
                raise
            
            # 遍历 fallback 链
            for i, fb_model in enumerate(self.fallback_chain):
                try:
                    logger.warning(
                        f"主模型 {self.primary.model} 流式失败 ({e}), "
                        f"降级到 {fb_model.model} (fallback #{i+1})"
                    )
                    self._fallback_count += 1
                    # 降级到 fallback 模型重新流式
                    async for delta in fb_model.stream(messages, tools=tools, **kwargs):
                        yield delta
                    return  # 成功完成
                except Exception as fb_error:
                    if not _should_fallback(fb_error):
                        raise
                    logger.warning(
                        f"Fallback #{i+1} ({fb_model.model}) 流式也失败: {fb_error}"
                    )
                    continue
            
            # 所有 fallback 都失败
            raise RuntimeError(
                f"主模型和所有 {len(self.fallback_chain)} 个 fallback 模型均失败"
            )

    @property
    def stats(self) -> dict:
        return {
            "primary_model": getattr(self.primary, "model", ""),
            "fallback_models": [getattr(fb, "model", "") for fb in self.fallback_chain],
            "fallback_count": self._fallback_count,
        }
