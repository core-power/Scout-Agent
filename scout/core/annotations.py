"""工具行为注解 — 借鉴 OpenHands/MCP 规范，帮助安全分析器理解工具副作用."""

from __future__ import annotations

from pydantic import BaseModel


class ToolAnnotations(BaseModel):
    """工具行为提示."""

    read_only: bool = False  # 是否只读（不修改状态）
    destructive: bool = False  # 是否可能删除/覆盖数据
    idempotent: bool = False  # 重复调用是否幂等
    open_world: bool = False  # 是否与外部世界交互
    requires_approval: bool = False  # 是否需要用户确认
