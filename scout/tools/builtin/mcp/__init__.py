"""MCP 工具 — 真实 MCP 客户端.

此模块导入 `scout.tools.mcp` 中的真实实现（JSON-RPC over stdio /
streamable-http，含超时与白名单校验），并触发工具注册。

2026-08-21 修复：此前这里是一个演示用占位实现（直接返回模拟成功，
不连接 MCP Server、不校验工具），导致 MCP 功能名不副实。现改为
复用真实客户端，避免功能不可用。
"""

from __future__ import annotations

from scout.tools.mcp import (  # noqa: F401
    MCPManager,
    MCPTool,
    get_mcp_manager,
    mcp_manager,
)
