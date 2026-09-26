"""Scout Agent LLM 层.

★ 2026-09-25 启动减负（Windows 实测）：本包 `__init__` 此前 eager 导入
`providers.openai` 与 `providers.registry`，导致**任何**对 `scout.llm` 下子模块的
导入（哪怕只要一个抽象基类 `LLMClient`，或运行时才用到的 `scout.llm.tracker`）
都要先执行本文件 → 拉起整个 openai SDK（连带 aiohttp / httpx2 / 上千个 pydantic
模型模块）。实测代价：`import openai` 本身 1200 ms；改造前 `import
scout.engine.agent` 1432 ms、`import scout.web.server` 1772 ms，其中 87% 是这一条
链；打包版 PYZ 里 openai 独占 1524/4669 个模块。

现在改用 PEP 562 模块级 `__getattr__` 做**惰性再导出**：只有真正访问
`OpenAIProvider` / `create_provider` 时才导入对应模块（即真正要建 LLM 客户端时）。
轻量的 `LLMClient` / `APIMode` / `ModeAdapter` / `FallbackProvider` 保持 eager，
不牺牲日常导入便利。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scout.llm.base import LLMClient
from scout.llm.modes import APIMode, ModeAdapter
from scout.llm.providers.fallback import FallbackProvider

if TYPE_CHECKING:  # 仅供类型检查器 / IDE，运行时不导入（否则会拉起 openai SDK）
    from scout.llm.providers.openai import OpenAIProvider
    from scout.llm.providers.registry import create_provider

__all__ = [
    "LLMClient", "APIMode", "ModeAdapter",
    "FallbackProvider", "OpenAIProvider", "create_provider",
]

# 惰性导出符号 → 其真实所属模块
_LAZY_EXPORTS: dict[str, str] = {
    "OpenAIProvider": "scout.llm.providers.openai",
    "create_provider": "scout.llm.providers.registry",
}


def __getattr__(name: str):
    """PEP 562：按需导入重量级 provider 符号。

    首次访问时 import 真模块并回填到本模块 globals，后续访问零开销。
    """
    module_name = _LAZY_EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    value = getattr(importlib.import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
