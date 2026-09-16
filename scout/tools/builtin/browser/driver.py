"""Browser 工具 — 安全的浏览器自动化（兼容模块）.

此模块是 browser 包的兼容入口，实际实现见同目录 __init__.py。
保留本文件以确保旧代码 `from scout.tools.builtin.browser.driver import BrowserTool`
仍然可用，并统一到增强版实现（多 tab / 登录态 / 下载 / iframe 等）。

安全策略：
- 彻底移除 eval/exec action，仅保留安全交互
- URL 白名单校验（仅 http/https，阻止内网）
- 页面加载超时保护
"""

from __future__ import annotations

# 复用 __init__.py 中的增强实现，避免两份不同步的实现
from scout.tools.builtin.browser import BrowserTool, _validate_url

# 兼容旧引用名
ActionType = str

__all__ = ["ActionType", "BrowserTool", "_validate_url"]
