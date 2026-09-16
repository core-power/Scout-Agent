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
