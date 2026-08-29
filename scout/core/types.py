"""核心数据类型 — 所有模块共享的基础数据结构."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Role(str, Enum):
    """消息角色."""

    USER = "user"
    ASSISTANT = "assistant"
    SYSTEM = "system"
    TOOL = "tool"


class Message(BaseModel):
    """统一消息格式 — 所有适配器产出这个."""

    role: Role
    content: str
    sender: str = ""
    session_id: str = ""
    source: str = ""  # "console" / "web" / "wechat"
    metadata: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=datetime.now)
    reasoning: str | None = None  # 扩展思考内容

    def to_api_dict(self) -> dict:
        """转换为 LLM API 格式."""
        d: dict[str, Any] = {"role": self.role.value, "content": self.content}
        if self.reasoning:
            d["reasoning"] = self.reasoning
        return d


class ToolCall(BaseModel):
    """LLM 返回的工具调用请求."""

    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Action(BaseModel):
    """Agent 决策的动作 — 要么调工具，要么回复."""

    type: str  # "tool_call" | "reply" | "finish"
    tool_name: str | None = None
    tool_args: dict[str, Any] = Field(default_factory=dict)
    text: str | None = None  # reply 内容
    reasoning: str | None = None


class Observation(BaseModel):
    """工具执行后的观察结果.

    error_code: 统一错误码（2026-08-27，工具契约增强）。
    约定值：UNKNOWN_TOOL / INVALID_ARGS / INTERNAL（注册表兜底）；
    工具自身可细化：NOT_FOUND / PERMISSION / TIMEOUT / NETWORK / SANDBOX / UNAUTHORIZED。
    """

    tool_name: str
    success: bool
    output: str
    error: str | None = None
    error_code: str = ""
    duration_ms: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class Session(BaseModel):
    """一次会话的完整状态."""

    id: str
    agent_id: str = "default"
    messages: list[Message] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    status: str = "idle"  # idle | thinking | acting | done | error
    parent_id: str | None = None  # 压缩血缘
    lineage_id: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)

    def to_api_messages(self) -> list[dict]:
        """转换为 LLM API 消息列表."""
        return [m.to_api_dict() for m in self.messages]


class LLMResponse(BaseModel):
    """LLM 响应."""

    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    finish_reason: str = ""
    reasoning: str | None = None


class Delta(BaseModel):
    """流式响应的最小单元."""

    text: str = ""
    reasoning: str = ""  # 推理模型的思考内容（reasoning_content）
    tool_calls: list[ToolCall] = Field(default_factory=list)
    done: bool = False
    suggestions: list[str] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
