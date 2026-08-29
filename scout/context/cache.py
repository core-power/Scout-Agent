"""Prompt 缓存 — 借鉴 Hermes 的 Anthropic cache breakpoints.

对稳定部分 system prompt 加缓存标记，减少重复 token 消耗。
"""

from __future__ import annotations

from typing import Any


class PromptCache:
    """Prompt 缓存管理器 — 标记缓存断点."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._cache_stats = {
            "cache_marked": 0,      # 标记了缓存断点的消息数
            "cache_writes": 0,      # 首次写入缓存的次数
            "tokens_saved": 0,      # 估算节省的 token 数
        }

    def mark_cacheable(self, messages: list[dict]) -> list[dict]:
        """在消息列表中标记可缓存的 system 消息.

        DashScope/OpenAI 兼容 API 的前缀缓存是服务端自动管理的：
        - 相同 prompt 前缀 > 2048 tokens 时自动命中缓存
        - 此方法对长 system prompt 添加 cache_control 标记（Anthropic 风格）
        - OpenAI 兼容 API 会忽略此字段，不报错，但也不报错

        Args:
            messages: API 消息列表

        Returns:
            标记了缓存断点的消息列表
        """
        if not self.enabled:
            return messages

        result = []
        for i, msg in enumerate(messages):
            if msg.get("role") == "system":
                cached_msg = dict(msg)
                content = cached_msg.get("content", "")
                if isinstance(content, str) and len(content) > 500:
                    # 只对长 system prompt 加缓存标记
                    cached_msg["cache_control"] = {"type": "ephemeral"}
                    self._cache_stats["cache_marked"] += 1
                result.append(cached_msg)
            else:
                result.append(msg)

        return result

    def get_stats(self) -> dict:
        """获取缓存统计."""
        return dict(self._cache_stats)

    def reset_stats(self) -> None:
        """重置统计."""
        self._cache_stats = {
            "cache_marked": 0,
            "cache_writes": 0,
            "tokens_saved": 0,
        }
