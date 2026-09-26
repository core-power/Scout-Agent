"""惰性模块代理 — 把"可有可无"的重型依赖推迟到第一次真正用到时再导入.

为什么需要（2026-09-25 Windows 性能实测）：顶层 `import numpy` 会无条件进入每条
启动路径（本机实测 ≈ 120 ms），而记忆系统只在**启用向量检索**时才真的用到 numpy；
默认纯文本检索（FTS5）路径一次都不会碰它。类似的还有可选的 GUI/浏览器依赖。

用法（保持原有 `np.xxx` 写法完全不变）::

    from typing import TYPE_CHECKING
    from scout.core.lazy import lazy_module

    if TYPE_CHECKING:          # 类型检查器看到真模块，注解依旧精确
        import numpy as np
    else:                      # 运行时拿到代理，首次属性访问才 import
        np = lazy_module("numpy")

注意：注解必须是字符串（文件头 `from __future__ import annotations`），否则
`def f(x: np.ndarray)` 在 def 时就会触发导入，等于没省。
"""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

__all__ = ["LazyModule", "lazy_module"]


class LazyModule:
    """模块代理：首次属性访问时导入目标模块并转发所有属性操作."""

    __slots__ = ("_name", "_mod")

    def __init__(self, name: str) -> None:
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_mod", None)

    @property
    def _resolved(self) -> ModuleType:
        mod = object.__getattribute__(self, "_mod")
        if mod is None:
            name = object.__getattribute__(self, "_name")
            # 已被别处导入过则直接复用，避免重复走 finder
            mod = sys.modules.get(name) or importlib.import_module(name)
            object.__setattr__(self, "_mod", mod)
        return mod

    # ── 属性代理 ────────────────────────────────────────────────
    def __getattr__(self, item):
        return getattr(self._resolved, item)

    def __setattr__(self, key, value) -> None:
        setattr(self._resolved, key, value)

    def __delattr__(self, item) -> None:
        delattr(self._resolved, item)

    def __dir__(self):
        return dir(self._resolved)

    def __repr__(self) -> str:
        name = object.__getattribute__(self, "_name")
        mod = object.__getattribute__(self, "_mod")
        state = "已加载" if mod is not None else "未加载"
        return f"<lazy module {name!r} ({state})>"


def lazy_module(name: str) -> LazyModule:
    """返回按需导入的模块代理（`name` 为可 import 的模块名）."""
    return LazyModule(name)
